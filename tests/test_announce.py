# -*- coding: utf-8 -*-
"""上线公告：前置条件、幂等、不可重复。"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import announce, attendance, config, db, notify  # noqa: E402


class AnnounceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._db, self._cfg = db.PATH, config.PATH
        db.PATH = os.path.join(self.tmp, "board.db")
        config.PATH = os.path.join(self.tmp, "config.json")
        db.init()
        with open(config.PATH, "w", encoding="utf-8") as f:
            f.write('{"digest_chat_id":"oc_test","allow_departments":["运营部"]}')
        self._send = notify.send_to_chat_ex
        self.sent = []

        def fake(chat_id, text):
            self.sent.append((chat_id, text))
            return True, None, "om_msg_1"

        notify.send_to_chat_ex = fake
        for i in range(3):
            with db.session() as conn:
                conn.execute("INSERT INTO members (open_id,name,department,status) "
                             "VALUES (?,?,?,'normal')", (f"ou_{i}", f"人{i}", "运营部"))

    def tearDown(self):
        db.PATH, config.PATH = self._db, self._cfg
        notify.send_to_chat_ex = self._send

    def _attendance(self, full):
        """造当天的考勤记录；full=False 时只覆盖 1 人（模拟 per_user）。"""
        with db.session() as conn:
            for i in (range(3) if full else [0]):
                conn.execute("""INSERT INTO attendance_records
                    (attendance_date, employee_no, open_id, check_in_record_id,
                     check_in_result, check_in_time, fetched_at, source, run_id)
                    VALUES (?,?,?,?,?,?,?,?,?)""",
                             (attendance.today(), f"X{i}", f"ou_{i}", "r", "Normal",
                              1, 1, "team" if full else "per_user", "r1"))

    def test_preflight_blocks_when_coverage_incomplete(self):
        self._attendance(full=False)
        pf = announce.preflight()
        self.assertFalse(pf["ok"])
        self.assertFalse(pf["checks"]["attendance_full"])

    def test_preflight_blocks_without_morning_run(self):
        self._attendance(full=True)
        self.assertFalse(announce.preflight()["checks"]["morning_ran"])

    def test_refuses_to_send_when_preflight_fails(self):
        self._attendance(full=False)
        r = announce.send_launch()
        self.assertFalse(r["ok"])
        self.assertEqual("preflight_failed", r["reason"])
        self.assertEqual([], self.sent)          # 一条都不能发出去

    def test_send_once_then_idempotent(self):
        self._attendance(full=True)
        with db.session() as conn:
            conn.execute("""INSERT INTO morning_runs
                (business_date, run_id, attendance_sync_status, digest_status,
                 completed_at) VALUES (?,?,?,?,?)""",
                         (attendance.today(), "r1", "ok", "sent", 1))
        r1 = announce.send_launch()
        self.assertTrue(r1["ok"], msg=str(r1))
        self.assertEqual("om_msg_1", r1["message_id"])
        r2 = announce.send_launch()
        self.assertTrue(r2["ok"])
        self.assertIn("已发送", r2["reason"])
        self.assertEqual(1, len(self.sent))      # 只发了一次

    def test_sending_state_is_not_resent(self):
        with db.session() as conn:
            conn.execute("INSERT INTO announcements (announcement_key,status,created_at) "
                         "VALUES (?,?,?)", (announce.KEY, "SENDING", 1))
        ok, row, why = announce._claim(announce.KEY)
        self.assertFalse(ok)
        self.assertIn("未确认", why)

    def test_failed_can_retry(self):
        with db.session() as conn:
            conn.execute("INSERT INTO announcements (announcement_key,status,created_at) "
                         "VALUES (?,?,?)", (announce.KEY, "FAILED", 1))
        ok, _, why = announce._claim(announce.KEY)
        self.assertTrue(ok)
        self.assertEqual("失败重试", why)


if __name__ == "__main__":
    unittest.main()
