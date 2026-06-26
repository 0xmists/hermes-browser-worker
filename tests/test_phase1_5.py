"""
test_phase1_5.py — Persistent Profile Validation via HTTP API.

Validates the Phase 1.5 contract:
  - Profile directory reuse
  - Session vs profile lifetime separation
  - Profile refresh
  - Concurrent profile access (API-level)

Run against a live worker: BROWSER_URL and BROWSER_API_KEY must be set.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request


BASE = os.environ.get(
    "BROWSER_URL",
    "https://hermes-browser-worker-production.up.railway.app",
)
API_KEY = os.environ.get("BROWSER_API_KEY", "")


def _headers():
    return {
        "X-API-Key": API_KEY,
        "Content-Type": "application/json",
    }


def _post(path, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, headers=_headers(), method="POST")
    try:
        with urllib.request.urlopen(req) as r:
            return {"ok": True, "status": r.status, "data": json.loads(r.read())}
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = body
        return {"ok": False, "status": e.code, "error": parsed}


def _get(path):
    req = urllib.request.Request(BASE + path, headers=_headers(), method="GET")
    try:
        with urllib.request.urlopen(req) as r:
            return {"ok": True, "status": r.status, "data": json.loads(r.read())}
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = body
        return {"ok": False, "status": e.code, "error": parsed}


def test_session_list_after_operations():
    print("\n=== session list baseline ===")
    res = _post("/session/list", {})
    print(res)
    assert res["ok"], f"session list failed: {res}"


def test_login_start_status_cancel():
    print("\n=== login start/status/cancel ===")
    site = "example-validation"
    start = _post("/login/start", {"site": site, "url": f"https://{site}"})
    print("start:", start)
    assert start["ok"] and start["data"].get("session_id") == site

    sid = start["data"]["session_id"]
    status = _get(f"/login/status/{sid}")
    print("status:", status)
    assert status["ok"]
    assert status["data"]["state"] == "waiting_user"

    cancel = _post("/login/cancel", {"session_id": sid})
    print("cancel:", cancel)
    assert cancel["ok"]
    status_after = _get(f"/login/status/{sid}")
    print("status after cancel:", status_after)
    assert status_after["status"] == 404


def test_session_vs_profile_lifetime():
    print("\n=== session vs profile lifetime ===")
    site = "lifetime-test"
    start = _post("/login/start", {"site": site, "url": f"https://{site}"})
    assert start["ok"]
    sid = start["data"]["session_id"]

    browse = _post("/browse", {"session_id": sid, "url": "https://example.com"})
    print("browse:", browse)
    assert browse["ok"]

    del_res = _post("/session/delete", {"session_id": sid})
    print("delete session:", del_res)
    assert del_res["ok"]

    # After delete, status should be 404 because login session was closed.
    status = _get(f"/login/status/{sid}")
    print("status after delete:", status)
    assert status["status"] == 404

    # Reopen same profile id should still work (profile dir exists, not deleted)
    reopen = _post("/browse", {"session_id": sid, "url": "https://example.com"})
    print("reopen:", reopen)
    assert reopen["ok"]

    # Cleanup profile explicitly
    cancel = _post("/login/cancel", {"session_id": sid})
    print("cancel:", cancel)
    assert cancel["ok"]


def test_profile_refresh():
    print("\n=== profile refresh ===")
    site = "refresh-test"
    start = _post("/login/start", {"site": site, "url": f"https://{site}"})
    assert start["ok"]
    sid = start["data"]["session_id"]

    _post("/browse", {"session_id": sid, "url": "https://example.com"})

    refresh = _post("/session/refresh", {"session_id": sid})
    print("refresh:", refresh)
    assert refresh["ok"]
    assert refresh["data"].get("refreshed") is True

    browse2 = _post("/browse", {"session_id": sid, "url": "https://example.com"})
    print("browse after refresh:", browse2)
    assert browse2["ok"]

    cancel = _post("/login/cancel", {"session_id": sid})
    assert cancel["ok"]


def test_concurrent_profile_access():
    print("\n=== concurrent profile access ===")
    site = "concurrent-test"
    start = _post("/login/start", {"site": site})
    assert start["ok"]
    sid = start["data"]["session_id"]

    results = []
    errors = []

    def worker():
        try:
            r = _post("/browse", {"session_id": sid, "url": "https://example.com"})
            results.append(r)
        except Exception as e:
            errors.append(str(e))

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    print(f"concurrent requests: {len(results)} success, {len(errors)} errors")
    assert len(errors) == 0, f"errors: {errors}"
    for r in results:
        assert r["ok"], f"bad result: {r}"

    cancel = _post("/login/cancel", {"session_id": sid})
    assert cancel["ok"]


def test_ephemeral_session_isolation():
    print("\n=== ephemeral session isolation ===")
    sid1 = "ephemeral-a"
    sid2 = "ephemeral-b"
    r1 = _post("/browse", {"session_id": sid1, "url": "https://example.com"})
    r2 = _post("/browse", {"session_id": sid2, "url": "https://example.com"})
    assert r1["ok"] and r2["ok"]
    sessions = _post("/session/list", {})
    print("sessions:", sessions)
    ids = [s["session_id"] for s in sessions["data"]["sessions"]]
    assert sid1 in ids
    assert sid2 in ids

    _post("/session/delete", {"session_id": sid1})
    sessions_after = _post("/session/list", {})
    ids_after = [s["session_id"] for s in sessions_after["data"]["sessions"]]
    assert sid1 not in ids_after
    assert sid2 in ids_after

    _post("/session/delete", {"session_id": sid2})


def main():
    if not API_KEY:
        print("WARNING: BROWSER_API_KEY not set; tests may fail with 401")

    test_session_list_after_operations()
    test_login_start_status_cancel()
    test_session_vs_profile_lifetime()
    test_profile_refresh()
    test_concurrent_profile_access()
    test_ephemeral_session_isolation()

    print("\n=== Phase 1.5 API validation passed ===")
    print("Note: full restart persistence (Tests 1-4) requires manual worker restart.")
    print("      Re-run this script after restart to verify profile reuse.")


if __name__ == "__main__":
    main()
