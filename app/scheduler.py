# -*- coding: utf-8 -*-
"""定时调度。只负责按间隔调用同步，不处理数据。

用线程 + sleep 而不是 APScheduler：
本机装不了第三方包（pip 被网络策略拦截），标准库足够跑 11 人规模的定时任务。
若将来环境允许，把这个文件换成 APScheduler 的 BlockingScheduler 即可，
外部接口保持不变（start/stop）。
"""
import threading
import time

from . import api, config, db, digest


class Scheduler:
    def __init__(self):
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        # 启动即同步一次，避免首屏空白
        while not self._stop.is_set():
            try:
                api.sync_all()
            except Exception as e:
                db.log(False, "scheduler", repr(e))

            # 每日摘要：过了设定时间且今天没发过就发一条
            try:
                if digest.due_now():
                    digest.send_daily()
            except Exception as e:
                db.log(False, "digest", repr(e))
            minutes = max(1, int(config.load().get("sync_minutes") or 5))
            self._stop.wait(minutes * 60)


scheduler = Scheduler()
