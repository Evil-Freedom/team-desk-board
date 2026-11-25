# -*- coding: utf-8 -*-
"""统一考勤采集的单元测试。全部离线：飞书访问层被替身顶掉。

为什么用 tempfile 而不是内存库：
    采集逻辑依赖真实的 (attendance_date, employee_no) 主键冲突行为，
    内存库在部分 Python 版本上会被不同连接看到不同的实例，测幂等会假通过。
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import attendance, config, db  # noqa: E402


def payload(*rows):
    """按飞书 user_task_results 的真实层级造数据。"""
    return {"user_task_results": [{
        "employee_name": "某人",
        "records": [{
            "check_in_record_id": r.get("record_id", ""),
            "check_in_result": r.get("result", ""),
            "check_in_record": {"check_time": str(r.get("time", "")),
                                "user_id": r.get("no", "X0001")},
            "check_out_record_id": "",
            "check_out_result": "",
            "check_out_record": {},
        } for r in rows],
    }]}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._db_path, self._cfg_path = db.PATH, config.PATH
        db.PATH = os.path.join(self.tmp, "board.db")
        config.PATH = os.path.join(self.tmp, "config.json")
        db.init()
        # per_user 路径要用应用凭证刷新成员 token，缺了会直接判失败
        with open(config.PATH, "w", encoding="utf-8") as f:
            json.dump({"app_id": "cli_test", "app_secret": "secret"}, f)
        self._orig_app = attendance.feishu.attendance_by_app
        self._orig_user = attendance.feishu.attendance_by_user

    def tearDown(self):
        db.PATH, config.PATH = self._db_path, self._cfg_path
        attendance.feishu.attendance_by_app = self._orig_app
        attendance.feishu.attendance_by_user = self._orig_user

    def add_member(self, open_id, name, status="normal"):
        with db.session() as conn:
            conn.execute("INSERT INTO members (open_id, name, status) VALUES (?,?,?)",
                         (open_id, name, status))


class ParseTest(Base):
    def test_parse_keeps_only_business_fields(self):
        rows = attendance.parse_user_tasks(payload(
            {"record_id": "r1", "result": "Normal", "time": 1795567208, "no": "X0050"}))
        self.assertEqual(1, len(rows))
        self.assertEqual("X0050", rows[0]["employee_no"])
        self.assertEqual("r1", rows[0]["check_in_record_id"])
        # 隐私字段一概不进结构
        self.assertNotIn("photo_urls", rows[0])
        self.assertNotIn("device_id", rows[0])

    def test_parse_millisecond_timestamp(self):
        rows = attendance.parse_user_tasks(payload(
            {"record_id": "r2", "result": "Normal", "time": 1795567208000}))
        self.assertEqual(1795567208, rows[0]["check_in_time"])

    def test_parse_empty_payload(self):
        self.assertEqual([], attendance.parse_user_tasks({}))
        self.assertEqual([], attendance.parse_user_tasks(None))


class ClassifyTest(Base):
    def test_states(self):
        ok = {"check_in_record_id": "r", "check_in_result": "Normal", "check_in_time": 1}
        self.assertEqual(attendance.CHECKED_IN, attendance.classify(ok))
        self.assertEqual(attendance.CHECKIN_PENDING, attendance.classify(None))
        self.assertEqual(attendance.NOT_YET,
                         attendance.classify({"check_in_result": "Todo"}))
        self.assertEqual(attendance.ABSENT_CONFIRMED,
                         attendance.classify({"check_in_result": "Absent"}))
        # 有 result 但没有 record_id：服务器还没写记录，不能算成功
        self.assertEqual(attendance.CHECKIN_UNKNOWN,
                         attendance.classify({"check_in_result": "Normal"}))

    def test_leave_day_is_not_applicable(self):
        self.assertEqual(attendance.NOT_APPLICABLE, attendance.classify(None, "leave"))
        self.assertEqual(attendance.NOT_APPLICABLE, attendance.classify(None, "away"))


class SyncTest(Base):
    def stub_app(self, data=None, err=None, capture=None):
        def fake(nos, day):
            if capture is not None:
                capture["nos"] = nos
            return (data, err) if err else (data, None)
        attendance.feishu.attendance_by_app = fake

    def stub_user(self, data=None, err=None):
        attendance.feishu.attendance_by_user = lambda token, day: (data, err)

    def stub_users(self, users):
        """per_user 模式下要遍历已授权成员。"""
        from app import oauth
        self._orig_list, self._orig_tok = oauth.list_users, oauth.get_valid_token
        oauth.list_users = lambda: [{"open_id": u, "name": "x"} for u in users]
        oauth.get_valid_token = lambda open_id, aid, sec: ("tok-" + open_id, None)
        self.addCleanup(lambda: setattr(oauth, "list_users", self._orig_list))
        self.addCleanup(lambda: setattr(oauth, "get_valid_token", self._orig_tok))

    def test_per_user_mode_without_mapping(self):
        self.add_member("ou_a", "甲")
        self.stub_users(["ou_a"])
        self.stub_user(payload({"record_id": "r9", "result": "Normal",
                                "time": 1795567208, "no": "X0050"}))
        r = attendance.sync(day="2026-11-25", run_id="run1")
        self.assertTrue(r["ok"])
        self.assertEqual("per_user", r["mode"])      # 必须如实标注不是全员
        self.assertEqual(1, r["count"])
        # 顺便记住映射，下次就能用应用身份批量查
        self.assertEqual({"ou_a": "X0050"}, attendance.identity_map())

    def test_team_mode_when_mapping_exists(self):
        self.add_member("ou_a", "甲")
        attendance.save_identity("ou_a", "X0050", "test")
        cap = {}
        self.stub_app(payload({"record_id": "r1", "result": "Normal",
                               "time": 1795567208, "no": "X0050"}), capture=cap)
        r = attendance.sync(day="2026-11-25")
        self.assertEqual("team", r["mode"])
        self.assertEqual(["X0050"], cap["nos"])

    def test_repeat_sync_is_idempotent(self):
        self.add_member("ou_a", "甲")
        attendance.save_identity("ou_a", "X0050", "test")
        self.stub_app(payload({"record_id": "r1", "result": "Normal", "time": 1795567208}))
        attendance.sync(day="2026-11-25")
        attendance.sync(day="2026-11-25")
        with db.session() as conn:
            n = conn.execute("SELECT COUNT(*) c FROM attendance_records").fetchone()["c"]
        self.assertEqual(1, n)      # 主键 (日期, 工号) 保证不会出第二行

    def test_api_error_marks_failed_without_fake_data(self):
        self.add_member("ou_a", "甲")
        attendance.save_identity("ou_a", "X0050", "test")
        self.stub_app(err="no permission")
        r = attendance.sync(day="2026-11-25")
        self.assertFalse(r["ok"])
        self.assertEqual("failed", r["status"])
        self.assertEqual({}, attendance.records_for_day("2026-11-25"))

    def test_per_user_without_authorized_member_fails_loudly(self):
        self.add_member("ou_a", "甲")
        self.stub_users([])
        r = attendance.sync(day="2026-11-25")
        self.assertFalse(r["ok"])
        self.assertEqual("failed", r["status"])

    def test_find_self_is_exact_match_only(self):
        self.add_member("ou_a", "甲")
        attendance.save_identity("ou_a", "X0050", "test")
        self.stub_app(payload({"record_id": "r1", "result": "Normal",
                               "time": 1795567208, "no": "X0050"}))
        attendance.sync(day="2026-11-25")

        self.assertIsNotNone(attendance.find_self(open_id="ou_a", day="2026-11-25"))
        self.assertIsNone(attendance.find_self(open_id="ou_b", day="2026-11-25"))
        self.assertIsNone(attendance.find_self(employee_no="X9999", day="2026-11-25"))


if __name__ == "__main__":
    unittest.main()
