#!/usr/bin/env python3
"""
Offline test of the off-box watchdog and the agent's heartbeat.

The watchdog exists to catch the one failure the agent cannot cover itself:
its host dying mid-lease. So the checks that matter are the severity
judgements -- silent-and-holding is a plant that may be importing at peak,
silent-and-idle is merely an availability problem -- and the guarantee that
the heartbeat can never harm the controller. A monitoring call that can stall
or crash the thing it monitors is worse than no monitoring.

    python3 test_cloud.py
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "cloud"))

import control            # noqa: E402
import reconcile          # noqa: E402
import server             # noqa: E402
import sigen              # noqa: E402
from test_reconcile import FakeOctopus, client_for, make_plant  # noqa: E402
import tempfile

# Android has no /tmp -- Termux puts it at $PREFIX/tmp -- so the whole
# suite refused to run on a phone until this stopped being hardcoded.
_TMP = Path(tempfile.gettempdir())

failures: list[str] = []


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label:<58} got {got!r}")
    if not ok:
        failures.append(f"{label}: expected {want!r}, got {got!r}")


def post(url, token, payload):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {token}"} if token else {})},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def get(url, token=None):
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}"} if token else {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, None


def main() -> int:
    logging.disable(logging.CRITICAL)
    sigen.MIN_REQUEST_INTERVAL = 0.0
    control.STATE_FILE = _TMP / ".lease-cloud-test.json"
    control.clear_state()

    db_path = _TMP / "watchdog-test.db"
    if db_path.exists():
        db_path.unlink()

    db = server.connect(db_path)
    token = server.add_site(db, "home")
    db.close()

    admin = "admin-secret"
    server.Handler.db_path = db_path
    server.Handler.admin_token_hash = server.hash_token(admin)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_port}"
    print(f"\nWatchdog on {base}\n")

    print("Tokens are the only thing between a site and the record")
    check("minted token has the expected shape", token.startswith("sig_"), True)
    db = server.connect(db_path)
    check("only the hash is stored",
          db.execute("SELECT token_hash FROM site").fetchone()[0] != token,
          True)
    db.close()

    print("\nHTTP surface")
    check("healthz is open", get(f"{base}/healthz")[0], 200)
    check("heartbeat accepted with a good token",
          post(f"{base}/v1/heartbeat", token, {"lease_held": False}), 202)
    check("rejected with a bad token",
          post(f"{base}/v1/heartbeat", "sig_wrong", {}), 401)
    check("rejected with no token",
          post(f"{base}/v1/heartbeat", None, {}), 401)
    check("unknown path is 404",
          post(f"{base}/v1/nope", token, {}), 404)
    check("site list needs the admin token", get(f"{base}/v1/sites")[0], 401)
    check("site list opens with it", get(f"{base}/v1/sites", admin)[0], 200)

    print("\nSeverity: the distinction the watchdog exists to make")
    db = server.connect(db_path)
    now = datetime.now(timezone.utc)

    def set_seen(minutes_ago: float, held: bool, expires=None, cloud=False):
        db.execute("UPDATE heartbeat SET seen_at=?, lease_held=?, "
                   "cloud_held=?, lease_expires=? WHERE site_id=1",
                   ((now - timedelta(minutes=minutes_ago)).isoformat(),
                    1 if held else 0, 1 if cloud else 0, expires))
        db.commit()

    set_seen(1, False)
    check("fresh and idle -> OK", server.evaluate(db, now)[0]["severity"], "OK")
    set_seen(1, True)
    check("fresh and holding -> still OK",
          server.evaluate(db, now)[0]["severity"], "OK")
    set_seen(60, False)
    check("silent but holding nothing -> WARN (availability only)",
          server.evaluate(db, now)[0]["severity"], "WARN")
    set_seen(60, True, (now + timedelta(minutes=5)).isoformat())
    row = server.evaluate(db, now)[0]
    check("silent WHILE HOLDING -> CRITICAL", row["severity"], "CRITICAL")
    check("and says the deadman has not yet had cause to fire",
          "not yet had cause" in row["detail"], True)
    set_seen(60, True, (now - timedelta(minutes=30)).isoformat())
    check("silent, holding, lease already expired -> flags it unconfirmed",
          "unconfirmed" in server.evaluate(db, now)[0]["detail"], True)

    # The cloud path takes no lease -- it selects a charge profile -- so
    # lease_held is False for the whole of every --via-cloud slot. Grading
    # that WARN told the owner "plant is not at risk" about a plant that was
    # grid-charging with Sigen AI suspended. This is the deployed path, so
    # this was the failure the watchdog was least able to report.
    set_seen(1, False, cloud=True)
    row = server.evaluate(db, now)[0]
    check("fresh and holding a cloud profile -> OK", row["severity"], "OK")
    check("and names the actuator it is holding",
          "holding a cloud charge profile" in row["detail"], True)

    set_seen(60, False, cloud=True)
    row = server.evaluate(db, now)[0]
    check("SILENT while holding a cloud profile -> CRITICAL, not WARN",
          row["severity"], "CRITICAL")
    check("and never says the plant is not at risk",
          "not at risk" in row["detail"], False)
    check("and points at the restore that actually applies",
          "sigencloud --deadman" in row["detail"], True)
    check("and does not invent a lease expiry it cannot have",
          "lease has expired" in row["detail"], False)
    check("both actuators are reported separately",
          (row["lease_held"], row["cloud_held"]), (False, True))

    set_seen(60, False, cloud=False)
    check("neither held, still silent -> back to WARN",
          server.evaluate(db, now)[0]["severity"], "WARN")

    server.add_site(db, "never-seen")
    check("a site that never reported -> UNKNOWN",
          [r["severity"] for r in server.evaluate(db, now)
           if r["site"] == "never-seen"][0], "UNKNOWN")
    db.close()

    print("\nA database predating cloud_held is migrated, not broken")
    # CREATE TABLE IF NOT EXISTS does nothing to an existing table, so a
    # watchdog that has been running since before this column would keep the
    # old schema and fail on the first heartbeat after an upgrade. That is a
    # monitoring outage caused by improving the monitoring.
    old_path = _TMP / "watchdog-legacy.db"
    if old_path.exists():
        old_path.unlink()
    legacy = sqlite3.connect(old_path)
    legacy.executescript("""
        CREATE TABLE site (
            id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL,
            token_hash TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE heartbeat (
            site_id INTEGER PRIMARY KEY REFERENCES site(id),
            seen_at TEXT NOT NULL, lease_held INTEGER NOT NULL,
            lease_expires TEXT, soc REAL, reverted_at TEXT, mode INTEGER,
            enable INTEGER, action TEXT, agent_version TEXT);
    """)
    legacy.commit()
    legacy.close()

    db = server.connect(old_path)
    columns = {r["name"] for r in db.execute("PRAGMA table_info(heartbeat)")}
    check("cloud_held is added to the old table", "cloud_held" in columns, True)
    server.add_site(db, "legacy")
    server.record(db, 1, {"lease_held": False, "cloud_held": True, "soc": 50.0})
    check("and a cloud-held heartbeat records against it",
          db.execute("SELECT cloud_held FROM heartbeat").fetchone()[0], 1)
    check("rows that predate the column default to not-held, not NULL",
          server.evaluate(db, now)[0]["cloud_held"] in (True, False), True)
    server.migrate(db)
    check("migrating an already-migrated database is a no-op",
          len({r["name"] for r in db.execute("PRAGMA table_info(heartbeat)")}),
          len(columns))
    db.close()
    old_path.unlink()

    print("\nA mode revert must not hide under a green OK")
    db = server.connect(db_path)
    db.execute("UPDATE heartbeat SET seen_at=?, reverted_at=? WHERE site_id=1",
               (now.isoformat(), now.isoformat()))
    db.commit()
    row = server.evaluate(db, now)[0]
    check("fresh but reverted -> ACTION, not OK", row["severity"], "ACTION")
    check("and says what to do",
          "reset the mode in the app" in row["detail"], True)
    db.execute("UPDATE heartbeat SET reverted_at=NULL WHERE site_id=1")
    db.commit()
    check("no revert -> plain OK",
          server.evaluate(db, now)[0]["severity"], "OK")
    db.close()

    print("\nEnd to end: a real agent tick reaches the watchdog")
    plant = make_plant(soc_pct=42.0)
    rec = reconcile.Reconciler(
        client_for(plant), FakeOctopus([]), 5.0, 95.0,
        heartbeat_url=f"{base}/v1/heartbeat", site_token=token)
    action = rec.tick()
    check("tick ran", action, "idle")
    db = server.connect(db_path)
    row = db.execute("SELECT * FROM heartbeat WHERE site_id=1").fetchone()
    db.close()
    check("watchdog recorded the SOC the agent read", row["soc"], 42.0)
    check("and the action it took", row["action"], "idle")
    check("and the agent version", row["agent_version"],
          reconcile.AGENT_VERSION)

    # The bug was in the AGENT's payload, not only the server's reading of it,
    # so pin the wire format: a reconciler holding the plant via the cloud
    # must SAY so. Asserting on the server's stored row would pass even if
    # send_heartbeat dropped the field and the column merely defaulted to 0.
    rec.cloud_held = True
    sent: dict = {}
    real_urlopen = urllib.request.urlopen

    def capture(request, *args, **kwargs):
        sent.update(json.loads(request.data.decode()))
        return real_urlopen(request, *args, **kwargs)

    urllib.request.urlopen = capture
    try:
        rec.tick()
    finally:
        urllib.request.urlopen = real_urlopen
    check("a cloud hold is reported on the wire", sent.get("cloud_held"), True)
    check("and the lease is reported separately, not conflated",
          sent.get("lease_held"), False)

    print("\nThe watchdog address comes from .env, not only the command line")
    # It is deployment config, and putting it only on the command line means
    # editing a systemd unit as root -- which the owner running the agent
    # usually is not. That friction is why a plant ran for weeks with no
    # off-box watching at all.
    import config
    env_file = _TMP / "watchdog-env-test"
    env_file.write_text("IOG_HEARTBEAT_URL = https://hc-ping.com/abc \n"
                        "IOG_SITE_TOKEN=\n", encoding="utf-8")
    real_load = config.load_env
    config.load_env = lambda *a, **k: {"IOG_HEARTBEAT_URL":
                                       "  https://hc-ping.com/abc  ",
                                       "IOG_SITE_TOKEN": "  ",
                                       "IOG_EMPTY": ""}
    try:
        check("a URL is read and stripped",
              config.env_setting("IOG_HEARTBEAT_URL"), "https://hc-ping.com/abc")
        check("whitespace-only is None, not a truthy blank",
              config.env_setting("IOG_SITE_TOKEN"), None)
        check("empty is None", config.env_setting("IOG_EMPTY"), None)
        check("an absent key is None", config.env_setting("IOG_NOT_SET"), None)
        config.load_env = lambda *a, **k: (_ for _ in ()).throw(
            config.ConfigError("no .env here"))
        check("no .env at all is None, not a crash",
              config.env_setting("IOG_HEARTBEAT_URL"), None)
    finally:
        config.load_env = real_load
        env_file.unlink(missing_ok=True)
    # send_heartbeat needs BOTH truthy or it silently no-ops, so a URL with no
    # token must not look like a configured watchdog that never reports.
    check("a URL without a token would otherwise disable the heartbeat",
          reconcile.Reconciler(
              client_for(make_plant()), FakeOctopus([]), 5.0, 95.0,
              heartbeat_url="https://hc-ping.com/abc",
              site_token=None).send_heartbeat(
                  reconcile.PlantState(enable=0, mode=0, soc=50.0,
                                       charge_limit_kw=None), "idle"),
          False)

    print("\nThe watchdog must never be able to harm the controller")
    rec_bad = reconcile.Reconciler(
        client_for(plant), FakeOctopus([]), 5.0, 95.0,
        heartbeat_url="http://127.0.0.1:1/v1/heartbeat", site_token=token)
    check("a dead watchdog does not raise", rec_bad.tick(), "idle")
    rec_bad.heartbeat_url = f"{base}/v1/heartbeat"
    rec_bad.site_token = "sig_rejected"
    check("a rejecting watchdog does not raise", rec_bad.tick(), "idle")
    check("send_heartbeat reports failure rather than throwing",
          rec_bad.send_heartbeat(
              reconcile.PlantState(0, 0, 50.0, None), "idle"), False)
    rec_none = reconcile.Reconciler(client_for(plant), FakeOctopus([]),
                                    5.0, 95.0)
    check("no watchdog configured is simply a no-op",
          rec_none.send_heartbeat(
              reconcile.PlantState(0, 0, 50.0, None), "idle"), False)

    print("\nBrowser dashboard")
    import base64 as _b64
    def dash(user_pass):
        req = urllib.request.Request(base + "/")
        if user_pass:
            req.add_header("Authorization", "Basic " + _b64.b64encode(
                user_pass.encode()).decode())
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, (e.headers.get("WWW-Authenticate") or "")

    code, detail = dash(None)
    check("dashboard is closed without credentials", code, 401)
    check("and asks the browser for them", "Basic" in detail, True)
    check("wrong password stays closed", dash("x:nope")[0], 401)
    code, body = dash(f"admin:{admin}")
    check("opens with the admin token as password", code, 200)
    check("lists the site", "home" in body, True)
    check("shows a severity", "CRITICAL" in body or "OK" in body
          or "WARN" in body, True)
    check("is self-contained (no external scripts)",
          "<script" not in body and "http://" not in body.replace(base, ""),
          True)

    httpd.shutdown()
    print("\n" + "=" * 72)
    if failures:
        print(f"{len(failures)} FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("All checks passed. The watchdog observes and cannot interfere.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
