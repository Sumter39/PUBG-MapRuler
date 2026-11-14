# -*- coding: utf-8 -*-
"""面板视觉验证：PMv2 感知进程里枚举目标进程的顶层窗口，
取物理窗口矩形，逐窗口截图保存。用法：
    python capture_check.py <exe进程名或PID> [输出目录]
"""
import ctypes
import ctypes.wintypes as wt
import os
import sys

from PIL import ImageGrab

user32 = ctypes.windll.user32
DPI_AWARE = ctypes.c_void_p(-4)  # PER_MONITOR_AWARE_V2


def enable_pm_v2():
    try:
        user32.SetProcessDpiAwarenessContext(DPI_AWARE)
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass


def find_windows(target):
    """返回 [(hwnd, class, title, l, t, r, b)]，仅可见顶层窗口。"""
    hits = []
    pids = set()
    if target.isdigit():
        pids.add(int(target))
    else:
        import subprocess
        out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {target}",
                              "/FO", "CSV", "/NH"],
                             capture_output=True, text=True).stdout
        for line in out.splitlines():
            parts = [p.strip('"') for p in line.split('","')]
            if len(parts) > 1 and parts[1].isdigit():
                pids.add(int(parts[1]))

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(hwnd, _lp):
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value not in pids or not user32.IsWindowVisible(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buf, 256)
        tbuf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, tbuf, 256)
        rect = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        hits.append((hwnd, buf.value, tbuf.value,
                     rect.left, rect.top, rect.right, rect.bottom))
        return True

    user32.EnumWindows(cb, 0)
    return hits


def main():
    enable_pm_v2()
    target = sys.argv[1] if len(sys.argv) > 1 else "PUBG-MapRuler.exe"
    outdir = sys.argv[2] if len(sys.argv) > 2 else "."
    os.makedirs(outdir, exist_ok=True)
    wins = find_windows(target)
    if not wins:
        print("NO_WINDOW")
        return
    for i, (hwnd, cls, title, l, t, r, b) in enumerate(wins):
        w, h = r - l, b - t
        print(f"[{i}] hwnd=0x{hwnd:X} class={cls!r} title={title!r} "
              f"rect=({l},{t})-({r},{b}) size={w}x{h}")
        if w <= 0 or h <= 0:
            continue
        img = ImageGrab.grab(bbox=(l, t, r, b), all_screens=True)
        path = os.path.join(outdir, f"win{i}_{w}x{h}.png")
        img.save(path)
        print("    saved", path)


if __name__ == "__main__":
    main()
