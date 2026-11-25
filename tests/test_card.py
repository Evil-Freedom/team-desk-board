# -*- coding: utf-8 -*-
"""卡片日报：口径、转义、结构、完整性标注。"""
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import attendance, card, config, db  # noqa: E402


class CardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._db, self._cfg = db.PATH, config.PATH
        db.PATH = os.path.join(self.tmp, "board.db")
        config.PATH = os.path.join(self.tmp, "config.json")
        db.init()
        with open(config.PATH, "w", encoding="utf-8") as f:
            json.dump({"department": "运营部"}, f, ensure_ascii=False)

    def tearDown(self):
        db.PATH, config.PATH = self._db, self._cfg

    def _seed(self, full=False, empty=False):
        if empty:
            return
        people = [("ou_a", "张三", "normal"), ("ou_b", "李四", "normal"),
                  ("ou_c", "王五", "normal"), ("ou_d", "赵六", "leave"),
                  ("ou_e", "<script>&Co", "away")]
        now = int(time.time())
        with db.session() as conn:
            for oid, name, st in people:
                conn.execute("INSERT INTO members (open_id,name,department,status) "
                             "VALUES (?,?,?,'运营部')", (oid, name, st)) if False else \
                    conn.execute("INSERT INTO members (open_id,name,department,status) "
                                 "VALUES (?,?,?,?)", (oid, name, "运营部", st))
            # 只有一个人有考勤记录且已确认
            conn.execute("""INSERT INTO attendance_records
                (attendance_date, employee_no, open_id, check_in_record_id,
                 check_in_result, check_in_time, fetched_at, source, run_id)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                         (attendance.today(), "X1", "ou_a", "r1", "Normal", 1, 1, "team", "r"))
            # 授权能力：full=True 时五个人都可用
            for oid, _n, _s in (people if full else people[:1]):
                conn.execute("""INSERT OR REPLACE INTO users
                    (open_id,name,access_token,refresh_token,expires_at,
                     refresh_expires_at,authorized_at,scope)
                    VALUES (?,?,?,?,?,?,?,?)""",
                             (oid, "x", "at", "rt", now + 3600, now + 86400, now,
                              "offline_access attendance:task:readonly"))

    def test_stats_follow_existing_definitions(self):
        self._seed()
        d = card.collect()
        self.assertEqual(5, d["total"])       # 应到 = members 行数
        self.assertEqual(3, d["normal"])      # status=normal
        self.assertEqual(1, d["leave"])
        self.assertEqual(1, d["away"])
        self.assertEqual(1, d["checked"])     # 只有 ou_a 有真实记录
        self.assertEqual(2, d["pending"])     # 其余两个在岗的没记录

    def test_incomplete_data_is_labelled(self):
        self._seed()
        c = card.build()
        sub = c["header"]["subtitle"]["content"]
        self.assertIn("数据未完整", sub)
        self.assertIn("1/5", sub)             # 覆盖 1 / 应到 5

    def test_complete_data_when_everyone_authorized(self):
        self._seed(full=True)
        d = card.collect()
        self.assertTrue(d["complete"])
        self.assertIn("数据完整", card.build()["header"]["subtitle"]["content"])

    def test_pending_is_not_called_absent(self):
        """卡片正文不能出现缺勤/旷工——查不到 ≠ 没来上班。"""
        self._seed()
        text = json.dumps(card.build(), ensure_ascii=False)
        self.assertNotIn("缺勤", text)
        self.assertNotIn("旷工", text)

    def test_body_keeps_legacy_daily_wording(self):
        """正文必须是旧日报那几句，不能换成另一套说法。"""
        self._seed()
        c = card.build()
        body = c["body"]["elements"][0]["content"]
        self.assertIn("在岗 3 人", body)      # 3 normal + 1 leave + 1 away
        self.assertIn("休假 1", body)
        self.assertIn("出差 1", body)
        self.assertIn("休假：赵六", body)
        self.assertIn("出差：", body)
        self.assertIn("在办：", body)

    def test_all_here_wording_when_no_absence(self):
        with db.session() as conn:
            for i in (1, 2):
                conn.execute("INSERT INTO members (open_id,name,department,status) "
                             "VALUES (?,?,?,?)", (f"ou_x{i}", f"人{i}", "运营部", "normal"))
        body = card.build()["body"]["elements"][0]["content"]
        self.assertIn("今天全员在岗", body)

    def test_special_chars_escaped(self):
        self._seed()
        text = json.dumps(card.build(), ensure_ascii=False)
        self.assertNotIn("<script>", text)
        self.assertIn("&lt;script&gt;", text)

    def test_card_structure(self):
        self._seed()
        c = card.build()
        self.assertEqual("2.0", c["schema"])
        self.assertEqual("plain_text", c["header"]["title"]["tag"])
        self.assertIn("今日状态", c["header"]["title"]["content"])
        els = c["body"]["elements"]
        self.assertEqual("markdown", els[0]["tag"])       # 正文（旧日报文案）
        self.assertTrue(any(e["tag"] == "hr" for e in els))
        btns = [e for e in els if e["tag"] == "button"]
        self.assertEqual(1, len(btns))
        self.assertEqual("https://xbyyb.duckdns.org", btns[0]["url"])
        self.assertEqual("fill", btns[0]["width"])        # 移动端铺满更好点

    def test_card_is_json_serialisable(self):
        self._seed()
        s = json.dumps(card.build(), ensure_ascii=False)
        self.assertGreater(len(s), 200)
        self.assertEqual(card.build()["schema"], json.loads(s)["schema"])

    def test_empty_members_does_not_claim_zero(self):
        self._seed(empty=True)
        c = card.build()
        self.assertIn("尚未同步", c["header"]["subtitle"]["content"])

    def test_text_fallback_matches_card_body(self):
        """文本兜底必须和卡片正文说同样的话（同源 legacy_lines）。"""
        self._seed()
        t = card.build_text()
        self.assertIn("https://xbyyb.duckdns.org", t)
        self.assertIn("在岗 3 人", t)
        body = card.build()["body"]["elements"][0]["content"]
        for line in card.legacy_lines():
            self.assertIn(line, body)
            self.assertIn(line, t)

    def test_no_personal_evidence_in_card(self):
        """群里只给汇总：不出现 record_id、打卡时间、设备信息。"""
        self._seed()
        text = json.dumps(card.build(), ensure_ascii=False)
        for bad in ("record_id", "check_in_time", "device_id", "ou_a", "X1"):
            self.assertNotIn(bad, text)


if __name__ == "__main__":
    unittest.main()
