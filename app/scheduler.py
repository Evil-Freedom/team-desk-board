# -*- coding: utf-8 -*-
"""定时调度。只负责按间隔调用同步，不处理数据。

两条独立节奏，为什么要分开：
    · 业务同步（人/审批/任务/表格/群待办）5 分钟一轮，图的是看板别太久不更新；
    · 晨间统采要卡在 08:50 之后的分钟级窗口，5 分钟粒度会把它推到 08:55，
      而且中间那几次重试（+5/+15 分钟）就没法对齐。
    合在一起只能二选一，所以拆成两个线程，各自 sleep 各自的间隔。

9:00 固定日报已经取消：日报由 morning.tick() 在统采完成后触发，
这里不再判断时间点。如果谁把 digest.due_now() 加回来，群里就会在
「数据没采到」的情况下也收到摘要——那正是本次要修的问题。
"""
import threading
import time

from . import api, config, db, morning


class Scheduler:
    def __init__(self):
        self._stop = threading.Event()
        self._thread = None
        self._morning_thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="ql-board-sync")
        self._morning_thread = threading.Thread(target=self._morning_loop, daemon=True,
                                                name="ql-board-morning")
        self._thread.start()
        self._morning_thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        # 启动即同步一次，避免首屏空白
        while not self._stop.is_set():
            try:
                api.sync_all()
            except Exception as e:
                db.log(False, "scheduler", repr(e)[:300])

            minutes = max(1, int(config.load().get("sync_minutes") or 5))
            self._stop.wait(minutes * 60)

    def _morning_loop(self):
        """30 秒一次的心跳：只看库里状态，不联网，成本可以忽略。"""
        while not self._stop.is_set():
            try:
                if morning.tick():
                    db.log(True, "scheduler", "晨间统采已执行")
                else:
                    morning.mark_given_up()
            except Exception as e:
                # 线程不能死：一次异常不代表今天都不用采了
                db.log(False, "scheduler", f"morning tick: {e!r}"[:300])
            self._stop.wait(30)


scheduler = Scheduler()
