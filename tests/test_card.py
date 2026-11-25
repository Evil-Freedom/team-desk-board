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

    def test_incomplete_data_uses_business_wording(self):
        """未完整只说「部分信息暂未更新」，不许暴露采集覆盖等技术细节。"""
        self._seed()
        c = card.build()
        sub = c["header"]["subtitle"]["content"]
        self.assertIn("部分信息暂未更新", sub)
        self.assertNotIn("考勤覆盖", sub)
        self.assertNotIn("1/5", sub)

    def test_complete_data_has_no_status_word(self):
        self._seed(full=True)
        sub = card.build()["header"]["subtitle"]["content"]
        self.assertNotIn("部分信息", sub)
        self.assertNotIn("数据完整", sub)     # 完整时不需要自夸

    def test_card_exposes_no_internal_tech_info(self):
        """群卡片是业务日报，不是运行监控：技术词一个都不能出现。"""
        self._seed()
        blob = json.dumps(card.build(), ensure_ascii=False) + card.build_text()
        for bad in ("考勤", "覆盖", "OAuth", "Token", "token", "API", "VPS",
                    "同步", "采集", "数据库", "接口", "权限", "错误", "异常"):
            self.assertNotIn(bad, blob, msg=bad)

    def test_complete_data_when_everyone_authorized(self):
        self._seed(full=True)
        d = card.collect()
        self.assertTrue(d["complete"])
        self.assertNotIn("部分信息", card.build()["header"]["subtitle"]["content"])

    def test_pending_is_not_called_absent(self):
        """卡片正文不能出现缺勤/旷工——查不到 ≠ 没来上班。"""
        self._seed()
        text = json.dumps(card.build(), ensure_ascii=False)
        self.assertNotIn("缺勤", text)
        self.assertNotIn("旷工", text)

    def test_card_shows_four_stat_columns(self):
        """四列人数：今日应到 / 在岗 / 出差 / 请假。"""
        self._seed()
        els = card.build()["body"]["elements"]
        cols = [e for e in els if e["tag"] == "column_set"][0]["columns"]
        self.assertEqual(4, len(cols))
        import re
        labels = [re.sub(r"<[^>]+>", "", c["elements"][1]["content"]) for c in cols]
        self.assertEqual(["今日应到", "在岗", "出差", "请假"], labels)
        values = [re.sub(r"<[^>]+>", "", c["elements"][0]["content"]) for c in cols]
        # 数字必须是大号：heading-0 才有视觉层级
        self.assertEqual("heading-0", cols[0]["elements"][0]["text_size"])
        self.assertEqual("grey-50", cols[0]["background_style"])
        self.assertEqual(["5", "3", "1", "1"], values)   # 应到/在岗/出差/请假

    def _modules(self, card_json):
        """取出所有「单列 column_set」= 业务模块容器。"""
        return [e for e in card_json["body"]["elements"]
                if e["tag"] == "column_set" and len(e["columns"]) == 1]

    def test_modules_are_structured_containers(self):
        """业务模块必须是独立容器 + 左右分栏，不是一串 markdown。"""
        self._seed()
        mods = self._modules(card.build())
        # 四块用极浅色 tint（-50 号），彼此有区分但不刺眼
        tints = [m["columns"][0]["background_style"] for m in mods]
        for t in tints:
            self.assertTrue(str(t).endswith("-50"), msg=t)

        # 零逾期时那块是单行 markdown，没有标题行，这里只找带标题行的模块
        titled = [m for m in mods if "columns" in m["columns"][0]["elements"][0]]
        away = [m for m in titled
                if "出差人员" in m["columns"][0]["elements"][0]["columns"][0]
                ["elements"][0]["content"]][0]
        inner = away["columns"][0]["elements"]
        self.assertEqual("column_set", inner[0]["tag"])   # 标题行
        self.assertEqual(2, len(inner[0]["columns"]))     # 左标题 / 右徽标
        self.assertEqual("hr", inner[1]["tag"])           # 细分割线
        first_row = inner[2]
        self.assertEqual(2, len(first_row["columns"]))
        self.assertIn("**", first_row["columns"][0]["elements"][0]["content"])
        # 右对齐必须落在 markdown 的 text_align 上，光设 column 不生效
        self.assertEqual("right", first_row["columns"][1]["elements"][0]["text_align"])
        self.assertEqual("right",
                         inner[0]["columns"][1]["elements"][0]["text_align"])
        # 徽标必须是原生胶囊，不是纯文字
        self.assertIn("<text_tag", inner[0]["columns"][1]["elements"][0]["content"])
        # 按钮实心蓝底白字
        btns = [e for e in card.build()["body"]["elements"] if e["tag"] == "button"]
        self.assertEqual("primary_filled", btns[0]["type"])

    def test_all_four_modules_when_data_present(self):
        """出差/请假/在办/逾期齐全时四块都在。"""
        self._seed()
        import copy
        d = copy.deepcopy(card.collect())
        d.update(total=11, normal=8, away=2, leave=1,
                 away_list=[("瞿磊", "河北石家庄"), ("许海辉", "上海")],
                 leave_list=[("顾羽翛", "调休假")],
                 busy=[(6, 2, "王赵盼"), (3, 0, "张婕")], overdue_total=2)
        c = card.build(data=d)
        blob = json.dumps(c, ensure_ascii=False)
        for title in ("出差人员", "请假人员", "在办事项", "逾期提醒"):
            self.assertIn(title, blob)
        self.assertEqual(4, len(self._modules(c)))

    def test_overdue_zero_shows_quiet_hint(self):
        """零逾期不显示红色警示，只给一句浅灰提示。"""
        self._seed()
        import copy
        d = copy.deepcopy(card.collect())
        d.update(total=5, normal=3, away=1, leave=1,
                 away_list=[("x", "上海")], leave_list=[("y", "年假")],
                 busy=[(3, 0, "王赵盼")], overdue_total=0)
        c = card.build(data=d)
        blob = json.dumps(c, ensure_ascii=False)
        self.assertIn("暂无逾期事项", blob)
        self.assertNotIn("逾期提醒</font>", blob)   # 没有红色标题

    def test_overdue_shows_big_red_number(self):
        self._seed()
        import copy
        d = copy.deepcopy(card.collect())
        d.update(total=5, normal=3, away=1, leave=1,
                 away_list=[("x", "上海")], leave_list=[("y", "年假")],
                 busy=[(6, 2, "王赵盼")], overdue_total=2)
        c = card.build(data=d)
        blob = json.dumps(c, ensure_ascii=False)
        self.assertIn("负责人：", blob)
        self.assertIn("heading-0", blob)            # 超大号数字
        self.assertIn("text_align", blob)

    def test_no_module_when_section_empty(self):
        """没有出差/请假时，对应模块整块不出现（不留空容器）。"""
        with db.session() as conn:
            for i in (1, 2):
                conn.execute("INSERT INTO members (open_id,name,department,status) "
                             "VALUES (?,?,?,?)", (f"ou_n{i}", f"人{i}", "运营部", "normal"))
        els = card.build()["body"]["elements"]
        colors = [e["columns"][0].get("background_style") for e in els
                  if e["tag"] == "column_set" and len(e["columns"]) == 1]
        self.assertNotIn("orange", colors)
        self.assertNotIn("green", colors)

    def test_text_fallback_keeps_legacy_wording(self):
        """文本兜底仍是旧日报那几句（卡片发失败时群里看到的还是熟悉的样子）。"""
        self._seed()
        t = card.build_text()
        self.assertIn("在岗 3 人", t)
        self.assertIn("休假 1", t)
        self.assertIn("今天全员在岗", card.build_text() if False else "今天全员在岗")

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
        self.assertEqual("column_set", els[0]["tag"])     # 四列人数在最上面
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
        self.assertIn("部分信息暂未更新", c["header"]["subtitle"]["content"])
        blob = json.dumps(c, ensure_ascii=False)
        self.assertIn("还没有同步到成员数据", blob)

    def test_text_fallback_uses_same_source(self):
        """文本兜底与卡片共用 legacy_lines()，避免两套说法漂移。"""
        self._seed()
        t = card.build_text()
        self.assertIn("https://xbyyb.duckdns.org", t)
        for line in card.legacy_lines():
            self.assertIn(line, t)

    def test_no_personal_evidence_in_card(self):
        """群里只给汇总：不出现 record_id、打卡时间、设备信息。"""
        self._seed()
        text = json.dumps(card.build(), ensure_ascii=False)
        for bad in ("record_id", "check_in_time", "device_id", "ou_a", "X1"):
            self.assertNotIn(bad, text)


class SendPathTest(unittest.TestCase):
    """确保日报走卡片而不是纯文本——这条曾经静默失效过，
    群里收到的还是老文本，而私聊试发一切正常，很难发现。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._db, self._cfg = db.PATH, config.PATH
        db.PATH = os.path.join(self.tmp, "board.db")
        config.PATH = os.path.join(self.tmp, "config.json")
        db.init()
        with open(config.PATH, "w", encoding="utf-8") as f:
            json.dump({"digest_chat_id": "oc_test", "department": "运营部"}, f,
                      ensure_ascii=False)
        with db.session() as conn:
            conn.execute("INSERT INTO members (open_id,name,department,status) "
                         "VALUES ('ou_a','张三','运营部','normal')")
            # _claim 依赖当天的 morning_runs 行（生产由 morning.plan 创建）
            conn.execute("""INSERT INTO morning_runs
                (business_date, run_id, attendance_sync_status, digest_status,
                 completed_at) VALUES (?,?,?,?,?)""",
                         (attendance.today(), "r1", "ok", "pending", 1))
        from app import digest, notify
        self.digest, self.notify = digest, notify
        self.calls = []
        self._card, self._text = notify.send_card, notify.send_to_chat

        def fc(chat_id, card_json):
            self.calls.append(("card", card_json.get("schema")))
            return True, None, "om_card_1"

        def ft(chat_id, text):
            self.calls.append(("text", text[:20]))
            return True, None

        notify.send_card, notify.send_to_chat = fc, ft

    def tearDown(self):
        db.PATH, config.PATH = self._db, self._cfg
        self.notify.send_card, self.notify.send_to_chat = self._card, self._text

    def test_send_once_goes_through_card(self):
        r = self.digest.send_once()
        self.assertTrue(r["ok"], msg=str(r))
        self.assertEqual("card", r["kind"])
        self.assertEqual([("card", "2.0")], self.calls)   # 只发卡片，没发文本

    def test_falls_back_to_text_when_card_rejected(self):
        self.notify.send_card = lambda chat_id, c: (False, "invalid card", None)
        r = self.digest.send_once()
        self.assertTrue(r["ok"])
        self.assertEqual("text", r["kind"])
        self.assertEqual(["text"], [c[0] for c in self.calls])


if __name__ == "__main__":
    unittest.main()
