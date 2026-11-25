# -*- coding: utf-8 -*-
"""会话签名测试：伪造/篡改/过期必须被拒，旧格式只放行一次。"""
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, session  # noqa: E402

SECRET = "test-session-secret"


class SessionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._cfg = config.PATH
        config.PATH = os.path.join(self.tmp, "config.json")
        with open(config.PATH, "w", encoding="utf-8") as f:
            json.dump({"internal_token": SECRET}, f)
        self._env = os.environ.pop("BOARD_SESSION_SECRET", None)
        self._cli = os.environ.pop("QL_INTERNAL_TOKEN", None)

    def tearDown(self):
        config.PATH = self._cfg
        for k, v in (("BOARD_SESSION_SECRET", self._env), ("QL_INTERNAL_TOKEN", self._cli)):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_roundtrip(self):
        v = session.issue("ou_abc")
        self.assertTrue(v.startswith("ou_abc."))
        open_id, state = session.verify(v)
        self.assertEqual(("ou_abc", "ok"), (open_id, state))

    def test_tampered_open_id_rejected(self):
        v = session.issue("ou_abc")
        _, ts, sig = v.rsplit(".", 2)
        forged = f"ou_admin.{ts}.{sig}"        # 换成别人的 id，签名不变
        self.assertEqual(("", "bad"), session.verify(forged))

    def test_tampered_timestamp_rejected(self):
        v = session.issue("ou_abc")
        open_id, _, sig = v.rsplit(".", 2)
        self.assertEqual(("", "bad"),
                         session.verify(f"{open_id}.{int(time.time()) + 999}.{sig}"))

    def test_expired_rejected(self):
        old = int(time.time()) - session.MAX_AGE_S - 60
        v = session.issue("ou_abc", now=old)
        self.assertEqual(("", "bad"), session.verify(v))

    def test_legacy_plain_id_is_flagged(self):
        """旧明文值只标 legacy，由调用方决定是否换发。"""
        self.assertEqual(("ou_abc", "legacy"), session.verify("ou_abc"))

    def test_garbage_rejected(self):
        for v in ("", "abc", "ou_a.b.c.d", "uid=1", "ou_abc.zzz.sig"):
            self.assertEqual("bad", session.verify(v)[1], msg=v)

    def test_no_secret_rejects_everything(self):
        with open(config.PATH, "w", encoding="utf-8") as f:
            json.dump({}, f)
        os.environ.pop("QL_INTERNAL_TOKEN", None)
        os.environ.pop("BOARD_SESSION_SECRET", None)
        self.assertEqual("", session.issue("ou_abc"))
        self.assertEqual("bad", session.verify("ou_abc")[1])

    def test_secure_flag_only_on_https(self):
        self.assertNotIn("Secure", session.cookie_header("v"))
        self.assertIn("Secure", session.cookie_header("v", secure=True))
        self.assertIn("HttpOnly", session.cookie_header("v"))


if __name__ == "__main__":
    unittest.main()
