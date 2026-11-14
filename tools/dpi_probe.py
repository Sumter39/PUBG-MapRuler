# -*- coding: utf-8 -*-
"""DPI 实验：测 Tk 在 PMv2 上下文里画布位图的真实缩放倍数。

主屏 125%（系统 DPI 120）。流程：
1. PMv2 进程建 Tk 画布 width=380（逻辑），画一条全宽色带
2. 分别用 geometry(380x200) 与 SetWindowPos(475x250 物理尺寸) 两种方式设窗
3. 各截图分析：色带实际铺到多少物理像素 → 得出 Tk 位图缩放倍数
"""
import ctypes
import ctypes.wintypes as wt
import os
import sys
import tkinter as tk

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from PIL import ImageGrab

user32 = ctypes.windll.user32
BG, BAR, RED = "#0a1116", "#16262e", "#ff2222"


def enable_pm_v2():
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)


def shot(l, t, r, b, path):
    img = ImageGrab.grab(bbox=(l, t, r, b), all_screens=True)
    img.save(path)
    px = img.convert("RGB").load()
    w, h = img.size
    # 找 y=10 行上 BAR 色带与红色标记的右边界
    bar_end = red_end = -1
    for x in range(w):
        c = px[x, 10]
        if abs(c[0] - 0x16) < 14 and abs(c[1] - 0x26) < 14 and abs(c[2] - 0x2e) < 14:
            bar_end = x
        if c[0] > 180 and c[1] < 90 and c[2] < 90:
            red_end = x
    print(f"  截图{w}x{h}: 色带右边界x={bar_end}, 红标x={red_end}")


def main():
    enable_pm_v2()
    root = tk.Tk()
    root.withdraw()
    win = tk.Toplevel(root)
    win.overrideredirect(True)
    win.configure(bg=BG)
    c = tk.Canvas(win, width=380, height=200, bg=BG, highlightthickness=0)
    c.pack()
    c.create_rectangle(0, 0, 380, 30, fill=BAR, outline="")
    c.create_rectangle(378, 0, 380, 30, fill=RED, outline="")
    win.geometry("380x200+100+100")
    root.update_idletasks()
    root.update()

    hwnd = user32.GetParent(win.winfo_id()) or win.winfo_id()
    r = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    print(f"[A] geometry 380x200 -> 窗口物理 ({r.left},{r.top})-({r.right},{r.bottom}) "
          f"大小 {r.right-r.left}x{r.bottom-r.top}")
    shot(r.left, r.top, r.right, r.bottom, "tools/out/dpiA.png")

    # 物理尺寸 475x250 (=380x1.25, 200x1.25)
    user32.SetWindowPos(hwnd, 0, 100, 100, 475, 250, 0x4 | 0x10)
    root.update()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    print(f"[B] SetWindowPos 475x250 -> 窗口物理 {r.right-r.left}x{r.bottom-r.top}, "
          f"canvas winfo={c.winfo_width()}x{c.winfo_height()}")
    shot(r.left, r.top, r.right, r.bottom, "tools/out/dpiB.png")

    # 挪到副屏（150%），保持 456x240（=380x1.2）物理尺寸
    root.update()
    user32.SetWindowPos(hwnd, 0, -1500, 300, 456, 240, 0x4 | 0x10)
    root.update()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    print(f"[C] 副屏 SetWindowPos 456x240 -> 窗口物理 {r.right-r.left}x{r.bottom-r.top}, "
          f"canvas winfo={c.winfo_width()}x{c.winfo_height()}, "
          f"tk scaling={root.tk.call('tk', 'scaling')}")
    shot(r.left, r.top, r.right, r.bottom, "tools/out/dpiC.png")

    root.after(300, root.destroy)
    root.mainloop()


if __name__ == "__main__":
    main()
