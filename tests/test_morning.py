# -*- coding: utf-8 -*-
"""晨间统采与日报触发的测试。时钟全部由参数注入，绝不改系统时间。

覆盖任务书要求的动态时间边界：
    08:40 / 08:43 / 08:48 → 08:50，08:55 → 08:57（事件 + 2 分钟）
    早于目标时间不提前跑；晚于目标时间顺延；同日只发一次。
"""
import os
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import api, attendance, config, db, digest, morning  # noqa: E402

DAY = "2026-11-25"


def at(h, m, s=0):
    return datetime(2026, 11, 25, h, m, s)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._db_path, self._cfg_path = db.PATH, config.PATH
        db.PATH = os.path.join(self.tmp, "board.db")
        config.PATH = os.path.join(self.tmp, "config.json")
        db.init()
        self._orig_sync = api.sync_all
        self._orig_send = digest.send_once

    def tearDown(self):
        db.PATH, config.PATH = self._db_path, self._cfg_path
        api.sync_all = self._orig_sync
        digest.send_once = self._orig_send


class ScheduleTest(Base):
    def test_target_time_table(self):
        """任务书给的四行例子的可执行版本。"""
        cases = [(at(8, 40), at(8, 50)), (at(8, 43), at(8, 50)),
                 (at(8, 48), at(8, 50)), (at(8, 55), at(8, 57))]
        for now, expect in cases:
            self.assertEqual(expect.timestamp(), morning.target_at(now, now.timestamp()),
                             msg=f"{now:%H:%M} 应算到 {expect:%H:%M}")

    def test_early_event_does_not_run_earlier_than_target(self):
        now = at(7, 30)
        self.assertEqual(at(8, 50).timestamp(), morning.target_at(now, now.timestamp()))

    def test_plan_is_unique_per_day(self):
        a = morning.plan("checkin_unknown", now=at(8, 40))
        b = morning.plan("checkin_unknown", now=at(8, 45))
        self.assertEqual(a["run_id"], b["run_id"])
        with db.session() as conn:
            n = conn.execute("SELECT COUNT(*) c FROM morning_runs").fetchone()["c"]
        self.assertEqual(1, n)

    def test_not_due_before_target(self):
        morning.plan("checkin_unknown", now=at(8, 40))
        self.assertIsNone(morning.due(now=at(8, 49)))
        self.assertIsNotNone(morning.due(now=at(8, 50)))

    def test_late_event_shifts_target(self):
        run = morning.plan("checkin_unknown", now=at(8, 55))
        self.assertEqual(at(8, 57).timestamp(), run["scheduled_at"])
        self.assertIsNone(morning.due(now=at(8, 56)))
        self.assertIsNotNone(morning.due(now=at(8, 57)))

    def test_retry_window_then_stop(self):
        morning.plan("checkin_unknown", now=at(8, 40))
        # 第一次失败
        run = morning.get_run(DAY)
        morning._update(DAY, attempt=1, completed_at=None, digest_status="failed",
                        attendance_sync_status="failed")
        # 08:50 这一刻还有一次机会（+5 分钟那次之前不该跑）
        self.assertIsNone(morning.due(now=at(8, 51)))
        self.assertIsNotNone(morning.due(now=at(8, 55)))
        # 超过全部重试次数就不再轮询
        morning._update(DAY, attempt=len(morning.RETRY_MINUTES))
        self.assertIsNone(morning.due(now=at(9, 10)))
        del run

    def test_give_up_marks_unknown(self):
        morning.plan("checkin_unknown", now=at(8, 40))
        self.assertIsNotNone(morning.mark_given_up(now=at(9, 40)))
        run = morning.get_run(DAY)
        self.assertEqual("unknown", run["attendance_sync_status"])
        self.assertEqual("skipped", run["digest_status"])


class RunTest(Base):
    def _stub(self, att_status="ok"):
        calls = {"sync": 0, "digest": 0}

        def fake_sync(**kw):
            calls["sync"] += 1
            return {"attendance": {"ok": att_status != "failed", "status": att_status}}

        def fake_send(**kw):
            calls["digest"] += 1
            return {"ok": True}

        api.sync_all = fake_sync
        digest.send_once = fake_send
        return calls

    def test_digest_only_after_attendance_ok(self):
        calls = self._stub("ok")
        run = morning.plan("checkin_unknown", now=at(8, 40))
        morning.run_once(run, now=at(8, 50))
        self.assertEqual(1, calls["sync"])
        self.assertEqual(1, calls["digest"])
        self.assertEqual("sent", morning.get_run(DAY)["digest_status"])

    def test_no_digest_when_attendance_failed(self):
        calls = self._stub("failed")
        run = morning.plan("checkin_unknown", now=at(8, 40))
        morning.run_once(run, now=at(8, 50))
        self.assertEqual(0, calls["digest"])   # 采集失败不发错误摘要
        self.assertEqual("pending", morning.get_run(DAY)["digest_status"])

    def test_completed_run_is_not_due_again(self):
        self._stub("ok")
        run = morning.plan("checkin_unknown", now=at(8, 40))
        morning.run_once(run, now=at(8, 50))
        self.assertIsNone(morning.due(now=at(8, 55)))

    def test_sync_exception_does_not_kill_scheduler(self):
        def boom(**kw):
            raise RuntimeError("boom")
        api.sync_all = boom
        run = morning.plan("checkin_unknown", now=at(8, 40))
        r = morning.run_once(run, now=at(8, 50))
        self.assertFalse(r["ok"])
        self.assertEqual("failed", morning.get_run(DAY)["board_sync_status"])


class DigestIdempotencyTest(Base):
    def test_claim_twice_only_once(self):
        morning.plan("checkin_unknown", now=at(8, 40))
        self.assertTrue(digest._claim(DAY))
        self.assertFalse(digest._claim(DAY))     # 第二次认领失败 = 不会二次发送

    def test_fixed_nine_oclock_is_gone(self):
        """due_now 必须永远 False，否则等于把 9 点定时偷偷放回来。"""
        self.assertFalse(digest.due_now())


    def test_fallback_creates_run_after_target(self):
        """没有设备事件时，到点也要自建 run，否则当天整日无日报。"""
        from datetime import datetime
        day = datetime(2026, 11, 26, 8, 55)
        self.assertIsNone(morning.get_run("2026-11-26"))
        run = morning.ensure_fallback(now=day)
        self.assertIsNotNone(run)
        self.assertEqual("fallback", run["trigger_source"])

    def test_no_fallback_before_target(self):
        from datetime import datetime
        self.assertIsNone(morning.ensure_fallback(now=datetime(2026, 11, 26, 8, 10)))
        self.assertIsNone(morning.get_run("2026-11-26"))

    def test_fallback_does_not_duplicate(self):
        from datetime import datetime
        morning.ensure_fallback(now=datetime(2026, 11, 26, 9, 0))
        self.assertIsNone(morning.ensure_fallback(now=datetime(2026, 11, 26, 9, 10)))
        with db.session() as conn:
            n = conn.execute("SELECT COUNT(*) c FROM morning_runs "
                             "WHERE business_date='2026-11-26'").fetchone()["c"]
        self.assertEqual(1, n)


if __name__ == "__main__":
    unittest.main()
