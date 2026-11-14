# -*- coding: utf-8 -*-
"""面板行为验证：跨屏挪动 + 按钮点击命中。
用法: python behavior_check.py
"""
import ctypes
import ctypes.wintypes as wt
import subprocess
import sys
import time

from PIL import ImageGrab

user32 = ctypes.windll.user32


def enable_pm_v2():
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)


def find_panel():
    """找到面板窗口（TkTopLevel, title='tk'），返回 (hwnd, l, t, r, b)。"""
    res = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(hwnd, _lp):
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buf, 256)
        tbuf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, tbuf, 256)
        if buf.value == "TkTopLevel" and tbuf.value == "tk":
            r = wt.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            res.append((hwnd, r.left, r.top, r.right, r.bottom))
        return True

    user32.EnumWindows(cb, 0)
    return res[0] if res else None


def click(x, y):
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.15)
    ctypes.windll.user32.mouse_event(2, 0, 0, 0, 0)   # LEFTDOWN
    time.sleep(0.08)
    ctypes.windll.user32.mouse_event(4, 0, 0, 0, 0)   # LEFTUP
    time.sleep(0.4)


def grab(hwnd, tag):
    rc = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rc))
    img = ImageGrab.grab(bbox=(rc.left, rc.top, rc.right, rc.bottom),
                         all_screens=True)
    p = f"tools/out/behavior_{tag}_{rc.right-rc.left}x{rc.bottom-rc.top}.png"
    img.save(p)
    print(f"[{tag}] rect=({rc.left},{rc.top})-({rc.right},{rc.bottom}) "
          f"size={rc.right-rc.left}x{rc.bottom-rc.top} -> {p}")


def main():
    enable_pm_v2()
    # 仅当面板处于隐藏态时才唤回（PostMessage WM_SHOW_SELF = WM_APP+2 给宿主窗口），
    # 不能按 Alt+, —— 那是「显示/隐藏」切换键，面板开着时会被切成隐藏。
    probe = find_panel()
    if probe and not user32.IsWindowVisible(probe[0]):
        host = user32.FindWindowW("PUBGMapRulerHost", None)
        if host:
            user32.PostMessageW(host, 0x8000 + 2, 0, 0)   # WM_APP+2 = WM_SHOW_SELF
            time.sleep(0.8)
    p = find_panel()
    if not p:
        print("NO_PANEL")
        return
    hwnd, l, t, r, b = p
    grab(hwnd, "0_副屏初始")

    # 1) 跨屏挪到主屏 (100,100)，等待 watch_metrics 一个周期
    user32.SetWindowPos(hwnd, 0, 100, 100, 0, 0, 0x5)  # NOSIZE|NOZORDER
    time.sleep(2.5)
    grab(hwnd, "1_挪到主屏")

    # 2) 点「折叠」按钮（标题栏右侧第一个：cx = PW-S(56), cy = S(20)）
    _, l, t, r, b = find_panel()
    click(l + (r - l) - 84, t + 30)
    grab(hwnd, "2_点折叠后")

    # 3) 点折叠芯片（左端「+」图标附近）→ 展开
    _, l, t, r, b = find_panel()
    click(l + 30, t + 27)
    time.sleep(0.5)
    grab(hwnd, "3_点芯片展开后")


if __name__ == "__main__":
    main()
