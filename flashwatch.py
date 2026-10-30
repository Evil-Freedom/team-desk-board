# -*- coding: utf-8 -*-
"""闪窗记录器 v2：记录窗口所属进程 + 父进程链，定位是谁拉起这个窗口的。

重点盯 cmd.exe / conhost.exe / powershell 这类控制台窗口。
每写一条立刻 flush，随时可读。
"""
import ctypes
import ctypes.wintypes as wt
import io
import os
import time

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

EVENT_OBJECT_SHOW = 0x8002
WINEVENT_OUTOFCONTEXT = 0x0000
DURATION = 30 * 60            # 挂 30 分钟

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "_flashlog.txt")

SKIP_CLASS = {"IME", "MSCTFIME UI", "tooltips_class32", "GDI+ Hook Window Class",
              "ForegroundStaging"}
NOISE = {"wetype_renderer.exe", "dwm.exe", "WorkBuddy.exe", "TextInputHost.exe"}


def proc_info(pid):
    """返回 (进程名, 父PID)。"""
    if not pid:
        return "?", 0
    h = kernel32.OpenProcess(0x1000, False, pid)      # QUERY_LIMITED_INFORMATION
    if not h:
        return "?", 0
    buf = ctypes.create_unicode_buffer(260)
    size = ctypes.c_ulong(260)
    ok = kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
    kernel32.CloseHandle(h)
    name = buf.value.split("\\")[-1] if (ok and buf.value) else "?"

    # 父 PID：用 NtQueryInformationProcess 太重，这里改用 Toolhelp 快照查
    return name, parent_of(pid)


_snap_cache = {"t": 0.0, "map": {}}


def parent_map():
    """0.5 秒缓存一次全进程父子关系，避免每次都建快照。"""
    now = time.time()
    if now - _snap_cache["t"] < 0.5 and _snap_cache["map"]:
        return _snap_cache["map"]

    TH32CS_SNAPPROCESS = 0x00000002

    class PE32(ctypes.Structure):
        _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD),
                    ("th32ProcessID", wt.DWORD), ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                    ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
                    ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", wt.LONG),
                    ("dwFlags", wt.DWORD), ("szExeFile", wt.WCHAR * 260)]

    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    m = {}
    if snap != -1:
        pe = PE32()
        pe.dwSize = ctypes.sizeof(PE32)
        if kernel32.Process32FirstW(snap, ctypes.byref(pe)):
            while True:
                m[pe.th32ProcessID] = (pe.szExeFile, pe.th32ParentProcessID)
                if not kernel32.Process32NextW(snap, ctypes.byref(pe)):
                    break
        kernel32.CloseHandle(snap)
    _snap_cache["t"] = now
    _snap_cache["map"] = m
    return m


def parent_of(pid):
    m = parent_map()
    return m.get(pid, ("?", 0))[1]


def chain(pid, depth=3):
    """往上追 depth 层父进程，返回 "cmd.exe ← explorer.exe ← …"。"""
    m = parent_map()
    out = []
    cur = pid
    for _ in range(depth):
        item = m.get(cur)
        if not item:
            break
        pname, ppid = item
        out.append(pname)
        if ppid <= 0 or ppid == cur:
            break
        cur = ppid
    return " ← ".join(out) if out else "?"


def class_name(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def title(hwnd):
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, 512)
    return buf.value.strip()


WINPROC = ctypes.WINFUNCTYPE(
    None, wt.HANDLE, wt.DWORD, wt.HWND, wt.LONG, wt.LONG, wt.DWORD, wt.DWORD)

f = io.open(OUT, "w", encoding="utf-8")


def w(line):
    f.write(line + "\n")
    f.flush()


w("# 闪窗记录 v2  开始 %s  计划 %d 分钟" % (time.strftime("%Y-%m-%d %H:%M:%S"), DURATION // 60))
w("# 时间 | 窗口进程 | 窗口类 | 标题 | 进程链（自身 ← 父 ← 祖父）")


def on_event(_h, event, hwnd, id_object, id_child, _t, _m):
    if event != EVENT_OBJECT_SHOW or id_object != 0 or id_child != 0:
        return
    if not user32.IsWindow(hwnd):
        return
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    cls = class_name(hwnd)
    if cls in SKIP_CLASS:
        return
    name, _ppid = proc_info(pid.value)
    ti = title(hwnd)
    if name in NOISE:
        return
    w("%s | %-22s | %-22s | %-40s | %s"
      % (time.strftime("%H:%M:%S"), name, cls[:22], ti[:40] or "(无标题)", chain(pid.value)))


cb = WINPROC(on_event)
hook = user32.SetWinEventHook(EVENT_OBJECT_SHOW, EVENT_OBJECT_SHOW,
                              0, cb, 0, 0, WINEVENT_OUTOFCONTEXT)
if not hook:
    w("!! 钩子注册失败")
    f.close()
    raise SystemExit(1)

end = time.time() + DURATION
msg = wt.MSG()
last_ping = time.time()
while time.time() < end:
    while user32.PeekMessageW(ctypes.byref(msg), 0, 0, 0, 1):
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))
    if time.time() - last_ping > 300:
        last_ping = time.time()
        w("# --- 心跳 %s ---" % time.strftime("%H:%M:%S"))
    time.sleep(0.02)

user32.UnhookWinEvent(hook)
w("# 结束 %s" % time.strftime("%H:%M:%S"))
f.close()
print("done")
