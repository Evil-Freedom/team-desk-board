#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""状态墙服务入口。

    python  server.py     # 前台，控制台有输出
    pythonw server.py     # 后台，无窗口（计划任务用这个）

pythonw 下没有 stdout，所以日志一律落到 logs/server.log，
不依赖控制台重定向——这样计划任务可以直接调 pythonw，不用 bat/vbs 包一层。
"""
import os
import socket
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from http.server import ThreadingHTTPServer

from app import api, db, scheduler

BASE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(BASE, "logs", "server.log")


def _log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    try:
        print(line)              # 有控制台时同时打出来
    except Exception:
        pass                     # pythonw 下 stdout 为 None，忽略
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("223.5.5.5", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def main():
    db.init()
    scheduler.scheduler.start()

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", 8900))
    local = "127.0.0.1" if host == "0.0.0.0" else host
    ip = lan_ip()

    _log(f"启动 host={host} port={port}")
    _log(f"本机 http://{local}:{port}/   局域网 http://{ip}:{port}/")

    try:
        ThreadingHTTPServer((host, port), api.Handler).serve_forever()
    except KeyboardInterrupt:
        scheduler.scheduler.stop()
        _log("收到中断，已停止")
    except Exception as e:
        _log(f"启动失败：{e!r}")
        raise


if __name__ == "__main__":
    main()
