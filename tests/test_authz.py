# -*- coding: utf-8 -*-
"""权限隔离测试：双层闸门（登录 + 部门）与敏感接口的角色边界。

这一组测的是「谁能看到什么」，不是功能。任何一条挂掉都说明存在越权。
"""
import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import api, config, db  # noqa: E402

DEPT = "运营部"


class AuthzTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._db, self._cfg = db.PATH, config.PATH
        db.PATH = os.path.join(self.tmp, "board.db")
        config.PATH = os.path.join(self.tmp, "config.json")
        db.init()
        self._write_config(["运营部"], ["管理员"])
        self._seed()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), api.Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        db.PATH, config.PATH = self._db, self._cfg

    def _write_config(self, allow, admins):
        with open(config.PATH, "w", encoding="utf-8") as f:
            json.dump({"department": DEPT, "allow_departments": allow,
                       "todo_admins": admins, "digest_chat_id": "",
                       "internal_token": "x"}, f, ensure_ascii=False)

    def _seed(self):
        now = int(time.time())
        people = [
            ("ou_member", "普通成员", DEPT),
            ("ou_admin", "管理员", DEPT),
            ("ou_other", "外人", "市场部"),
            ("ou_nodept", "无部门", ""),
        ]
        with db.session() as conn:
            for oid, name, dept in people:
                conn.execute("INSERT INTO members (open_id,name,department,status) "
                             "VALUES (?,?,?,'normal')", (oid, name, dept))
            for oid, name, _ in people:
                conn.execute("""INSERT INTO users
                    (open_id,name,access_token,refresh_token,expires_at,
                     refresh_expires_at,authorized_at)
                    VALUES (?,?,'at','rt',?,?,?)""",
                             (oid, name, now + 3600, now + 86400, now))
            conn.execute("INSERT INTO todos (id,open_id,title,status,source) "
                         "VALUES ('fs_1','ou_member','私密待办','open','feishu')")

    # ── 工具 ────────────────────────────────────────────────────────
    def req(self, path, open_id=None, method="GET", body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {}
        if open_id:
            headers["Cookie"] = f"uid={open_id}"
        payload = None
        if body is not None:
            payload = json.dumps(body)
            headers["Content-Type"] = "application/json"
        conn.request(method, path, payload, headers)
        r = conn.getresponse()
        raw = r.read()
        conn.close()
        try:
            return r.status, json.loads(raw or b"{}")
        except Exception:
            return r.status, {"raw": raw[:80]}

    # ── 用例 ────────────────────────────────────────────────────────
    def test_anonymous_gets_401(self):
        status, _ = self.req("/api/board")
        self.assertEqual(401, status)

    def test_other_department_gets_403(self):
        status, body = self.req("/api/board", "ou_other")
        self.assertEqual(403, status)
        self.assertIn("运营部", body.get("error", ""))

    def test_missing_department_is_denied(self):
        """通讯录没同步到部门时不能猜，只能拒。"""
        status, _ = self.req("/api/board", "ou_nodept")
        self.assertEqual(403, status)

    def test_empty_whitelist_denies_everyone(self):
        """配置丢了白名单时必须收紧，而不是放开。"""
        self._write_config([], ["管理员"])
        status, _ = self.req("/api/board", "ou_admin")
        self.assertEqual(403, status)

    def test_member_can_read_board(self):
        status, body = self.req("/api/board", "ou_member")
        self.assertEqual(200, status)
        self.assertTrue(body.get("members"))

    def test_member_cannot_read_admin_only_endpoints(self):
        for path in ("/api/users", "/api/config", "/api/absences"):
            status, _ = self.req(path, "ou_member")
            self.assertEqual(403, status, msg=path)

    def test_admin_can_read_admin_only_endpoints(self):
        for path in ("/api/users", "/api/config", "/api/absences"):
            status, _ = self.req(path, "ou_admin")
            self.assertEqual(200, status, msg=path)

    def test_member_cannot_list_others_todos(self):
        status, _ = self.req("/api/todos?open_id=ou_admin", "ou_member")
        self.assertEqual(403, status)

    def test_member_can_list_own_todos(self):
        status, body = self.req("/api/todos?open_id=ou_member", "ou_member")
        self.assertEqual(200, status)
        self.assertEqual(1, len(body.get("items") or []))

    def test_member_cannot_write_config(self):
        status, _ = self.req("/api/config", "ou_member", "POST", {"department": "改名"})
        self.assertEqual(403, status)

    def test_member_cannot_trigger_full_sync(self):
        status, _ = self.req("/api/sync", "ou_member", "POST", {})
        self.assertEqual(403, status)

    def test_other_department_cannot_write_todos(self):
        status, _ = self.req("/api/todos", "ou_other", "POST",
                             {"open_id": "ou_member", "title": "x"})
        self.assertEqual(403, status)

    def test_sensitive_files_not_downloadable(self):
        for path in ("/board.db", "/config.json", "/app/api.py",
                     "/logs/server.log", "/.git/config"):
            status, body = self.req(path, "ou_admin")
            # 未登录会被 302 到登录页；登录后必须是 404，绝不能返回内容
            self.assertIn(status, (302, 404), msg=path)
            self.assertNotIn(b"SQLite", str(body).encode(), msg=path)

    def test_demo_page_is_open(self):
        status, _ = self.req("/demo")
        self.assertEqual(200, status)


if __name__ == "__main__":
    unittest.main()
