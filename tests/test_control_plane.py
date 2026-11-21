"""Regression tests for real persistence and HTTP handlers, without a live game.

The fake subprocess is explicitly test-only; it never seeds the shipped app.
"""
import base64
import io
import json
import os
import struct
import sys
import tempfile
import threading
import time
import unittest
import zlib
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lab.core import Invalid, Store, decode_png, normalize_options, validate_scenario
from lab.manager import Manager
from lab.server import Handler, report_html

TOKEN = "unit-test-admin-token-1234567890"


class MemorySocket:
    def __init__(self, data):
        self.input = io.BytesIO(data)
        self.output = bytearray()

    def makefile(self, mode, *args):
        return self.input

    def sendall(self, data):
        self.output.extend(data)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def enqueue(self, **kwargs):
        return self.store.enqueue({"tests": ["http"], **kwargs})


class StoreTest(Fixture):
    def test_options_reject_arbitrary_target_and_unknown_test(self):
        with self.assertRaises(Invalid):
            normalize_options({"tests": ["https://localhost/admin"]})
        self.assertNotIn("target", normalize_options({"target": "http://localhost"}))

    def test_validation_limits_repeat_timeout_and_nan(self):
        for options in ({"repeat": True}, {"repeat": 11}, {"timeout": 1}, {"browser": "evil"}, {"tests": []}):
            with self.subTest(options=options), self.assertRaises(Invalid):
                normalize_options(options)

    def test_fifo_claim_and_terminal_cancel(self):
        first, second = self.enqueue(), self.enqueue()
        self.assertEqual(self.store.claim()["id"], first["id"])
        self.store.cancel(second["id"])
        self.assertEqual(self.store.run(second["id"])["status"], "cancelled")
        self.assertIsNone(self.store.claim())

    def test_queue_capacity(self):
        for _ in range(10):
            self.enqueue()
        with self.assertRaises(Invalid):
            self.enqueue()

    def test_claim_is_atomic_across_threads(self):
        self.enqueue()
        claimed = []
        threads = [threading.Thread(target=lambda: claimed.append(self.store.claim())) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(r is not None for r in claimed), 1)

    def test_restart_recovers_running_keeps_queue(self):
        first, second = self.enqueue(), self.enqueue()
        self.store.claim()
        self.store.recover()
        self.assertEqual(self.store.run(first["id"])["status"], "interrupted")
        self.assertEqual(self.store.run(second["id"])["status"], "queued")

    def test_sessions_are_hashed_and_logout_revokes(self):
        token = self.store.session()
        self.assertTrue(self.store.authenticated(token))
        with self.store.db() as db:
            self.assertNotEqual(db.execute("SELECT hash FROM sessions").fetchone()[0], token)
        self.store.logout(token)
        self.assertFalse(self.store.authenticated(token))

    def test_events_incremental(self):
        run = self.enqueue()
        self.store.event(run["id"], "one", {"message": "Первый"})
        self.store.event(run["id"], "two", {})
        first = self.store.events(run["id"])
        self.assertEqual(len(first), 2)
        self.assertEqual(self.store.events(run["id"], first[0]["seq"])[0]["type"], "two")

    def test_schedule_tbilisi_utc_conversion(self):
        at = datetime(2026, 11, 21, 4, 59, tzinfo=timezone.utc)
        due = Store.next_daily(9, 0, 240, at)
        self.assertEqual(due, "2026-11-21T05:00:00.000+00:00")
        after = Store.next_daily(9, 0, 240, at + timedelta(minutes=1))
        self.assertEqual(after, "2026-11-22T05:00:00.000+00:00")

    def test_schedule_enqueues_once_after_downtime(self):
        sid = self.store.add_schedule({"hour": 9, "minute": 0, "offset": 240, "options": {"tests": ["http"]}})
        with self.store.db() as db:
            db.execute("UPDATE schedules SET next_at=? WHERE id=?", ("2026-11-19T05:00:00.000+00:00", sid))
        at = datetime(2026, 11, 21, 6, 0, tzinfo=timezone.utc)
        self.store.tick(at)
        self.store.tick(at)
        self.assertEqual(len(self.store.runs()), 1)
        self.assertEqual(self.store.schedules()[0]["next_at"], "2026-11-22T05:00:00.000+00:00")

    def test_paused_schedule_does_not_launch(self):
        sid = self.store.add_schedule({"hour": 9, "minute": 0})
        self.store.toggle_schedule(sid, False)
        self.store.tick(datetime(2030, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(self.store.runs(), [])

    def test_stats_skip_excluded_and_profiles_separated(self):
        for status, profile in [("passed", "desktop"), ("failed", "desktop"), ("skipped", "desktop"), ("passed", "mobile")]:
            run = self.enqueue(profile=profile)
            self.store.update_run(run["id"], status="passed", results=[{"id": "http", "status": status, "duration": 2}])
        stats = self.store.stats()["tests"]
        desktop = next(t for t in stats if t["profile"] == "desktop")
        self.assertEqual(desktop["pass_rate"], 50)
        self.assertTrue(desktop["unstable"])
        self.assertEqual(desktop["skipped"], 1)
        self.assertEqual(len(stats), 2)

    def test_scenario_requires_assert_and_known_template(self):
        with self.assertRaises(Invalid):
            validate_scenario({"name": "N", "steps": [{"action": "reload"}]}, {})
        with self.assertRaises(Invalid):
            validate_scenario({"name": "N", "steps": [{"action": "assert", "template": "missing"}]}, {})
        result = validate_scenario({"name": "N", "steps": [{"action": "assert", "template": "ready"}]}, {"ready": {}})
        self.assertEqual(result["steps"][0]["template"], "ready")

    def test_invalid_drag_coordinates(self):
        with self.assertRaises(Invalid):
            validate_scenario({"name": "N", "steps": [{"action": "drag", "coords": [0, 0, 1, float("nan")]},
                {"action": "assert", "template": "ready"}]}, {"ready": {}})

    def test_png_validation(self):
        with self.assertRaises(Invalid):
            decode_png("data:image/png;base64,AAAA")
        def chunk(kind, payload):
            return struct.pack(">I",len(payload)) + kind + payload + struct.pack(">I",zlib.crc32(kind+payload) & 0xffffffff)
        data = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB",20,20,8,2,0,0,0)) + chunk(b"IDAT",zlib.compress((b"\0"+b"\xff\0\0"*20)*20)) + chunk(b"IEND",b"")
        self.assertEqual(decode_png("data:image/png;base64," + base64.b64encode(data).decode()), data)

    def test_report_escapes_untrusted_browser_errors(self):
        run = self.enqueue()
        run["results"] = [{"id": "http", "name": "<script>", "status": "failed", "message": "<img onerror=alert(1)>", "duration": 1}]
        result = report_html(run).decode()
        self.assertNotIn("<img onerror", result)
        self.assertIn("&lt;script&gt;", result)


class HttpTest(Fixture):
    def setUp(self):
        super().setUp()
        self.server = SimpleNamespace(store=self.store, admin_token=TOKEN, public=True,
            secure_cookie=False, auth_lock=threading.Lock(), login_attempts=__import__("collections").defaultdict(__import__("collections").deque),
            streams=threading.BoundedSemaphore(6))

    def request(self, path, body=None, token=None, origin=None, cookie=None, extra=None):
        method = "POST" if body is not None else "GET"
        data = json.dumps(body).encode() if body is not None else b""
        headers = {"Host": "localhost:8080", "Connection": "close"}
        if body is not None:
            headers.update({"Content-Type": "application/json", "Content-Length": str(len(data))})
        if token:
            headers["Authorization"] = "Bearer " + token
        if origin:
            headers["Origin"] = origin
        if cookie:
            headers["Cookie"] = cookie
        headers.update(extra or {})
        head = f"{method} {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k,v in headers.items()) + "\r\n"
        sock = MemorySocket(head.encode() + data)
        Handler(sock, ("127.0.0.1", 1234), self.server)
        header, payload = bytes(sock.output).split(b"\r\n\r\n", 1)
        status = int(header.split(b" ")[1])
        return status, header.decode(), payload

    def test_public_read_but_no_anonymous_write(self):
        self.assertEqual(self.request("/api/runs")[0], 200)
        self.assertEqual(self.request("/api/runs", {"tests": ["http"]})[0], 401)
        self.assertEqual(self.request("/api/runs", {"tests": ["http"]}, token=TOKEN)[0], 201)

    def test_cookie_login_secure_flags_logout(self):
        self.server.secure_cookie = True
        status, headers, _ = self.request("/api/login", {"token": TOKEN})
        self.assertEqual(status, 200)
        self.assertIn("HttpOnly; SameSite=Strict; Secure", headers)
        cookie = next(line.split(": ", 1)[1].split(";", 1)[0] for line in headers.split("\r\n") if line.startswith("Set-Cookie"))
        self.assertTrue(json.loads(self.request("/api/session", cookie=cookie)[2])["admin"])
        self.assertEqual(self.request("/api/logout", {}, cookie=cookie)[0], 200)
        self.assertFalse(json.loads(self.request("/api/session", cookie=cookie)[2])["admin"])

    def test_cross_origin_blocked(self):
        self.assertEqual(self.request("/api/runs", {"tests": ["http"]}, token=TOKEN, origin="https://evil.example")[0], 403)
        self.assertEqual(self.request("/api/login", {"token": TOKEN}, origin="https://evil.example")[0], 403)

    def test_login_throttle(self):
        for _ in range(10):
            self.assertEqual(self.request("/api/login", {"token": "bad"})[0], 401)
        self.assertEqual(self.request("/api/login", {"token": TOKEN})[0], 429)

    def test_private_mode_requires_login(self):
        self.server.public = False
        self.assertEqual(self.request("/api/runs")[0], 401)
        self.assertEqual(self.request("/api/runs", token=TOKEN)[0], 200)
        self.assertEqual(self.request("/healthz")[0], 200)

    def test_artifact_traversal_and_spec_are_blocked(self):
        run = self.enqueue()
        folder = self.store.root / "runs" / run["id"]
        folder.mkdir()
        (folder / "spec.json").write_text("private")
        for name in ("spec.json", "../../lab.sqlite3", "%2e%2e/%2e%2e/lab.sqlite3"):
            self.assertEqual(self.request(f"/api/runs/{run['id']}/artifacts/{name}")[0], 404)

    def test_video_range_and_suffix_range(self):
        run = self.enqueue()
        folder = self.store.root / "runs" / run["id"]
        folder.mkdir()
        (folder / "video.webm").write_bytes(b"0123456789")
        path = f"/api/runs/{run['id']}/artifacts/video.webm"
        status, headers, data = self.request(path, extra={"Range": "bytes=2-4"})
        self.assertEqual((status, data), (206, b"234"))
        self.assertIn("bytes 2-4/10", headers)
        self.assertEqual(self.request(path, extra={"Range": "bytes=-3"})[2], b"789")
        self.assertEqual(self.request(path, extra={"Range": "bytes=100-"})[0], 416)

    def test_failed_only_retry(self):
        run = self.store.enqueue({"tests": ["http", "canvas"]})
        self.store.update_run(run["id"], status="failed", results=[{"id": "http", "status": "passed"}, {"id": "canvas", "status": "failed"}])
        status, _, payload = self.request(f"/api/runs/{run['id']}/retry", {"failed_only": True}, token=TOKEN)
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(payload)["options"]["tests"], ["canvas"])

    def test_unknown_routes_are_not_success(self):
        self.assertEqual(self.request("/api/missing")[0], 404)
        self.assertEqual(self.request("/api/runs/deadbeefdeadbeef")[0], 404)

    def test_cancel_queued_over_http(self):
        run = self.enqueue()
        status, _, payload = self.request(f"/api/runs/{run['id']}/cancel", {}, token=TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)["status"], "cancelled")


FAKE_WORKER = '''
import json, sys, time
from pathlib import Path
spec=json.loads(Path(sys.argv[1]).read_text())
folder=Path(spec['folder'])
mode=sys.argv[2] if len(sys.argv)>2 else 'passed'
results=[]
for repeat in range(1,spec['options']['repeat']+1):
 for test in spec['options']['tests']:
  row={'id':test,'name':'fixture check','repeat':repeat,'duration':0.1,'status':mode,'message':''}
  results.append(row)
  with (folder/'events.jsonl').open('a') as out: out.write(json.dumps({'type':'test_end',**row})+'\\n')
(folder/'result.json').write_text(json.dumps({'results':results}))
'''


class ManagerTest(Fixture):
    def test_worker_events_are_drained_before_finalize(self):
        fixture = Path(self.temp.name) / "fixture.py"
        fixture.write_text(FAKE_WORKER)
        run = self.enqueue(repeat=3)
        manager = Manager(self.store, [sys.executable, str(fixture)])
        manager.execute(self.store.claim())
        saved = self.store.run(run["id"])
        self.assertEqual(saved["status"], "passed")
        self.assertEqual(len(saved["results"]), 3)

    def test_worker_crash_is_error_not_pass(self):
        fixture = Path(self.temp.name) / "crash.py"
        fixture.write_text("raise RuntimeError('intentional fixture failure')")
        run = self.enqueue()
        Manager(self.store, [sys.executable, str(fixture)]).execute(self.store.claim())
        self.assertEqual(self.store.run(run["id"])["status"], "error")

    def test_cancel_running_terminates_process_group(self):
        fixture = Path(self.temp.name) / "wait.py"
        fixture.write_text("import sys,time; from pathlib import Path; import json; s=json.loads(Path(sys.argv[1]).read_text()); f=Path(s['folder']);\nwhile not (f/'cancel').exists(): time.sleep(.05)\n")
        run = self.enqueue()
        manager = Manager(self.store, [sys.executable, str(fixture)])
        thread = threading.Thread(target=lambda: manager.execute(self.store.claim()))
        thread.start()
        deadline = time.monotonic() + 5
        while manager.process is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.store.cancel(run["id"])
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.store.run(run["id"])["status"], "cancelled")

    def test_retention_preserves_run_metadata(self):
        run = self.enqueue()
        folder = self.store.root / "runs" / run["id"]
        folder.mkdir()
        (folder / "video.webm").write_bytes(b"test-only")
        self.store.update_run(run["id"], status="passed", finished="2020-01-01T00:00:00+00:00")
        Manager(self.store).prune()
        self.assertFalse(folder.exists())
        self.assertIsNotNone(self.store.run(run["id"]))


if __name__ == "__main__":
    unittest.main()
