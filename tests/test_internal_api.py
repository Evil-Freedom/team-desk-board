# -*- coding: utf-8 -*-
"""内部接口的安全边界测试。

这一组不是「功能测试」，而是「越权测试」：
    不签名 / 签名错 / 重放 / 未绑定设备 / 想读别人，
任何一条能通过都意味着 QuickLaunch 能拿到不属于它的考勤数据。
"""
import hashlib
import hmac
import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import uuid
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import api, attendance, config, db  # noqa: E402

SECRET = "test-internal-secret"


def sign(secret, ts, nonce, method, path, body=b""):
    digest = hashlib.sha256(body or b"").hexdigest()
    msg = f"{ts}\n{nonce}\n{method.upper()}\n{path}\n{digest}".encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


class InternalApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._db_path, self._cfg_path = db.PATH, config.PATH
        db.PATH = os.path.join(self.tmp, "board.db")
        config.PATH = os.path.join(self.tmp, "config.json")
        db.init()
        with open(config.PATH, "w", encoding="utf-8") as f:
            json.dump({"internal_token": SECRET,
                       "quicklaunch": {"device_bindings": {"dev_1": "ou_self"}}}, f)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), api.Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

        # 造一条本人记录 + 一条别人的记录，验证「读不到别人」
        with db.session() as conn:
            conn.execute("INSERT INTO members (open_id, name, status) VALUES ('ou_self','本人','normal')")
            conn.execute("INSERT INTO members (open_id, name, status) VALUES ('ou_other','他人','normal')")
        for oid, no in (("ou_self", "X0050"), ("ou_other", "X0077")):
            with db.session() as conn:
                conn.execute("""INSERT INTO attendance_records
                    (attendance_date, employee_no, open_id, check_in_record_id,
                     check_in_result, check_in_time, fetched_at, source, run_id)
                    VALUES (?,?,?,?,?,?,?,?,?)""",
                             (attendance.today(), no, oid, "rec-" + no, "Normal",
                              int(time.time()) - 300, int(time.time()), "bot", "r1"))

    def tearDown(self):
        self.server.shutdown()
        db.PATH, config.PATH = self._db_path, self._cfg_path

    # ── 工具 ────────────────────────────────────────────────────────
    def get(self, path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", path, headers=headers or {})
        r = conn.getresponse()
        body = json.loads(r.read() or b"{}")
        conn.close()
        return r.status, body

    def signed(self, path, method="GET", body=b"", ts=None, nonce=None, secret=SECRET):
        ts = str(ts or int(time.time()))
        nonce = nonce or uuid.uuid4().hex
        return {"X-QL-Timestamp": ts, "X-QL-Nonce": nonce,
                "X-QL-Signature": sign(secret, ts, nonce, method, path, body),
                "Content-Type": "application/json"}

    def self_path(self, device_id="dev_1"):
        return f"/internal/attendance/self?attendance_date={attendance.today()}&device_id={device_id}"

    # ── 用例 ────────────────────────────────────────────────────────
    def test_rejects_without_signature(self):
        status, body = self.get(self.self_path())
        self.assertEqual(401, status)
        self.assertFalse(body.get("ok"))

    def test_rejects_wrong_secret(self):
        p = self.self_path()
        status, _ = self.get(p, self.signed(p, secret="wrong"))
        self.assertEqual(401, status)

    def test_rejects_expired_timestamp(self):
        p = self.self_path()
        status, _ = self.get(p, self.signed(p, ts=int(time.time()) - 3600))
        self.assertEqual(401, status)

    def test_rejects_replayed_nonce(self):
        p = self.self_path()
        h = self.signed(p)
        first, _ = self.get(p, h)
        second, _ = self.get(p, h)
        self.assertEqual(200, first)
        self.assertEqual(401, second)       # 同一个 nonce 不能再用

    def test_rejects_unbound_device(self):
        p = self.self_path("dev_unknown")
        status, body = self.get(p, self.signed(p))
        self.assertEqual(403, status)
        self.assertEqual("device_not_bound", body.get("error"))

    def test_returns_only_bound_user_record(self):
        p = self.self_path()
        status, body = self.get(p, self.signed(p))
        self.assertEqual(200, status)
        self.assertEqual("X0050", body["employee_no"])   # 绑定的那个人
        self.assertEqual("rec-X0050", body["record_id"])
        self.assertNotIn("X0077", json.dumps(body))

    def test_no_parameter_can_switch_user(self):
        """带 open_id / employee_no 参数也必须无效——服务端只认绑定关系。"""
        p = self.self_path() + "&open_id=ou_other&employee_no=X0077"
        status, body = self.get(p, self.signed(p))
        self.assertEqual(200, status)
        self.assertEqual("X0050", body["employee_no"])

    def test_missing_record_returns_found_false(self):
        p = f"/internal/attendance/self?attendance_date=2020-01-01&device_id=dev_1"
        status, body = self.get(p, self.signed(p))
        self.assertEqual(200, status)
        self.assertFalse(body["found"])     # 待同步，不是缺勤

    def test_plan_endpoint_is_idempotent(self):
        body = json.dumps({"device_id": "dev_1", "event_time": int(time.time()),
                           "type": "checkin_unknown"}).encode()
        headers = self.signed("/internal/morning/plan", "POST", body)
        results = []
        for _ in range(2):
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            h = dict(headers)
            h["X-QL-Nonce"] = uuid.uuid4().hex
            h["X-QL-Signature"] = sign(SECRET, h["X-QL-Timestamp"], h["X-QL-Nonce"],
                                       "POST", "/internal/morning/plan", body)
            conn.request("POST", "/internal/morning/plan", body, h)
            r = conn.getresponse()
            results.append(json.loads(r.read()))
            conn.close()
        self.assertEqual(results[0]["run_id"], results[1]["run_id"])
        with db.session() as conn:
            n = conn.execute("SELECT COUNT(*) c FROM morning_runs").fetchone()["c"]
        self.assertEqual(1, n)

    def test_plan_rejects_unbound_device(self):
        body = json.dumps({"device_id": "nope", "event_time": int(time.time())}).encode()
        headers = self.signed("/internal/morning/plan", "POST", body)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/internal/morning/plan", body, headers)
        r = conn.getresponse()
        self.assertEqual(403, r.status)
        conn.close()


if __name__ == "__main__":
    unittest.main()
