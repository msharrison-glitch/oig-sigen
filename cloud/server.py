#!/usr/bin/env python3
"""
Off-box watchdog for OIG -> SigenStor agents.

The agent on each site can protect the plant against almost everything: its
own exceptions, SIGTERM, a hard kill, an expired lease. The one thing it
structurally cannot cover is its own host dying, because systemd, cron and
the signal handlers all die with it -- and the plant has no Modbus watchdog,
so whatever mode was latched stays latched. This service is the observer that
is not on that host.

It deliberately CANNOT command anything. It accepts heartbeats and raises
alarms. A compromised or broken watchdog must never be able to make a plant
less safe than no watchdog at all, so there is no control path here to abuse
-- releasing a stuck plant stays a human decision, or the site's own deadman.

Stdlib only: http.server, sqlite3, hashlib. No framework, no pip install,
deployable anywhere including the same UTM VM as the agent.

    python3 cloud/server.py --add-site home       # mint a site token
    python3 cloud/server.py --serve --port 8080
    python3 cloud/server.py --status              # what the watchdog sees

API:
    POST /v1/heartbeat   Authorization: Bearer <site token>
    GET  /v1/sites       Authorization: Bearer <admin token>
    GET  /healthz
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DB_PATH = Path(__file__).with_name("watchdog.db")

# An agent ticks at most every POLL_INTERVAL (300 s). Three missed beats is a
# real outage rather than one slow Octopus call.
STALE_AFTER = timedelta(seconds=900)

# Biggest heartbeat we will read, so a bad client cannot exhaust memory.
MAX_BODY = 16 * 1024

SCHEMA = """
CREATE TABLE IF NOT EXISTS site (
    id          INTEGER PRIMARY KEY,
    name        TEXT UNIQUE NOT NULL,
    token_hash  TEXT UNIQUE NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS heartbeat (
    site_id       INTEGER PRIMARY KEY REFERENCES site(id),
    seen_at       TEXT NOT NULL,
    lease_held    INTEGER NOT NULL,
    lease_expires TEXT,
    soc           REAL,
    mode          INTEGER,
    enable        INTEGER,
    action        TEXT,
    agent_version TEXT
);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


# --------------------------------------------------------------------------
# sites
# --------------------------------------------------------------------------

def add_site(db: sqlite3.Connection, name: str) -> str:
    """Mint a site token. Only the hash is stored, so this is the one and
    only time the token is recoverable."""
    token = "sig_" + secrets.token_urlsafe(32)
    db.execute(
        "INSERT INTO site (name, token_hash, created_at) VALUES (?, ?, ?)",
        (name, hash_token(token), utcnow().isoformat()),
    )
    db.commit()
    return token


def site_for_token(db: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM site WHERE token_hash = ?",
                      (hash_token(token),)).fetchone()


# --------------------------------------------------------------------------
# heartbeats and alarms
# --------------------------------------------------------------------------

def record(db: sqlite3.Connection, site_id: int, payload: dict) -> None:
    db.execute(
        """INSERT INTO heartbeat
             (site_id, seen_at, lease_held, lease_expires, soc, mode,
              enable, action, agent_version)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(site_id) DO UPDATE SET
             seen_at=excluded.seen_at, lease_held=excluded.lease_held,
             lease_expires=excluded.lease_expires, soc=excluded.soc,
             mode=excluded.mode, enable=excluded.enable,
             action=excluded.action, agent_version=excluded.agent_version""",
        (site_id, utcnow().isoformat(),
         1 if payload.get("lease_held") else 0,
         payload.get("lease_expires"),
         payload.get("soc"), payload.get("mode"), payload.get("enable"),
         payload.get("action"), payload.get("agent_version")),
    )
    db.commit()


def evaluate(db: sqlite3.Connection, now: datetime | None = None) -> list[dict]:
    """One row per site, with a severity.

    The distinction that matters is not 'is the agent up' but 'was it holding
    a lease when it went quiet'. A silent agent with nothing held is an
    availability problem; a silent agent mid-lease is a plant that may be
    importing at peak with nobody watching.
    """
    now = now or utcnow()
    out = []
    rows = db.execute(
        """SELECT s.name, h.* FROM site s
           LEFT JOIN heartbeat h ON h.site_id = s.id
           ORDER BY s.name"""
    ).fetchall()
    for row in rows:
        if row["seen_at"] is None:
            out.append({"site": row["name"], "severity": "UNKNOWN",
                        "detail": "no heartbeat ever received"})
            continue
        seen = datetime.fromisoformat(row["seen_at"])
        quiet = now - seen
        held = bool(row["lease_held"])
        if quiet <= STALE_AFTER:
            out.append({"site": row["name"], "severity": "OK",
                        "detail": f"last seen {quiet.total_seconds():.0f}s ago"
                                  + (", holding a lease" if held else ""),
                        "soc": row["soc"], "lease_held": held})
            continue
        if held:
            detail = (f"SILENT for {quiet.total_seconds() / 60:.0f} min WHILE "
                      f"HOLDING A LEASE")
            expires = row["lease_expires"]
            if expires and datetime.fromisoformat(expires) < now:
                detail += ("; its lease has expired, so the site deadman "
                           "should have released -- unconfirmed")
            else:
                detail += "; the site deadman has not yet had cause to fire"
            out.append({"site": row["name"], "severity": "CRITICAL",
                        "detail": detail, "soc": row["soc"],
                        "lease_held": True})
        else:
            out.append({"site": row["name"], "severity": "WARN",
                        "detail": f"silent for "
                                  f"{quiet.total_seconds() / 60:.0f} min, "
                                  f"nothing held -- plant is not at risk",
                        "soc": row["soc"], "lease_held": False})
    return out


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "oig-watchdog/1.0"
    db_path = DB_PATH
    admin_token_hash: str | None = None

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _reply(self, code: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _bearer(self) -> str | None:
        header = self.headers.get("Authorization", "")
        return header[7:].strip() if header.startswith("Bearer ") else None

    def do_GET(self) -> None:
        if self.path == "/healthz":
            return self._reply(200, {"ok": True})
        if self.path == "/v1/sites":
            token = self._bearer()
            if not token or self.admin_token_hash is None or \
                    not secrets.compare_digest(hash_token(token),
                                               self.admin_token_hash):
                return self._reply(401, {"error": "unauthorized"})
            db = connect(self.db_path)
            try:
                return self._reply(200, {"sites": evaluate(db)})
            finally:
                db.close()
        self._reply(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/v1/heartbeat":
            return self._reply(404, {"error": "not found"})
        token = self._bearer()
        if not token:
            return self._reply(401, {"error": "missing bearer token"})
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self._reply(413, {"error": "body too large"})
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._reply(400, {"error": "invalid json"})
        if not isinstance(payload, dict):
            return self._reply(400, {"error": "expected an object"})
        db = connect(self.db_path)
        try:
            site = site_for_token(db, token)
            if site is None:
                return self._reply(401, {"error": "unknown site token"})
            record(db, site["id"], payload)
        finally:
            db.close()
        self._reply(202, {"ok": True, "stale_after_s": STALE_AFTER.seconds})


def serve(port: int, db_path: Path, admin_token: str | None,
          bind: str = "127.0.0.1") -> int:
    Handler.db_path = db_path
    Handler.admin_token_hash = hash_token(admin_token) if admin_token else None
    if admin_token is None:
        print("WARNING: no --admin-token, so /v1/sites is closed. "
              "Use --status locally to read state.", file=sys.stderr)
    # Loopback by default. Exposing a service that holds every site's
    # health to the whole LAN should be a decision, not an accident; in
    # production put it behind a TLS terminator and bind 0.0.0.0 explicitly.
    httpd = ThreadingHTTPServer((bind, port), Handler)
    print(f"watchdog listening on {bind}:{port}, db {db_path}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=DB_PATH)
    ap.add_argument("--add-site", metavar="NAME")
    ap.add_argument("--serve", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--bind", default="127.0.0.1",
                    help="interface to listen on (default loopback only)")
    ap.add_argument("--admin-token",
                    default=os.environ.get("WATCHDOG_ADMIN_TOKEN"))
    args = ap.parse_args()

    if args.add_site:
        with connect(args.db) as db:
            try:
                token = add_site(db, args.add_site)
            except sqlite3.IntegrityError:
                print(f"site {args.add_site!r} already exists", file=sys.stderr)
                return 1
        print(f"site: {args.add_site}\ntoken: {token}\n"
              "Store it now -- only its hash is kept.")
        return 0

    if args.status:
        with connect(args.db) as db:
            for row in evaluate(db):
                print(f"  {row['severity']:<9} {row['site']:<16} "
                      f"{row['detail']}")
        return 0

    if args.serve:
        return serve(args.port, args.db, args.admin_token, args.bind)

    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
