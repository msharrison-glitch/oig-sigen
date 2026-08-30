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
    control.STATE_FILE = Path("/tmp/.lease-cloud-test.json")
    control.clear_state()

    db_path = Path("/tmp/watchdog-test.db")
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

    def set_seen(minutes_ago: float, held: bool, expires=None):
        db.execute("UPDATE heartbeat SET seen_at=?, lease_held=?, "
                   "lease_expires=? WHERE site_id=1",
                   ((now - timedelta(minutes=minutes_ago)).isoformat(),
                    1 if held else 0, expires))
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

    server.add_site(db, "never-seen")
    check("a site that never reported -> UNKNOWN",
          [r["severity"] for r in server.evaluate(db, now)
           if r["site"] == "never-seen"][0], "UNKNOWN")
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
