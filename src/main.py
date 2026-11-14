# -*- coding: utf-8 -*-
"""
PUBG Map Overlay Ex — Windows 桌面投影浮层（EXE 形态，非网页）

核心行为复刻旧 PUBGMapOverlay-Etalien.exe：点位**直接投影到游戏内 M 地图上**，
而不是在一张独立底图上标点。

旧 EXE 取证结论（REA 反汇编 + calibration 基准图像素测量）：
  * 完整地图（按 M 打开、未缩放）是屏幕正中的正方形，边长 = 屏幕高度 H：
        left = (W − H)/2,  top = 0,  side = H,  8×8 网格每格 H/8
    erangel.v2.png (2560×1440) 网格线实测 x=560+k·180 (k=0..8)、y=k·180，
    峰值梯度 53~65 vs 邻域 12~24；地图区亮度 157.5 vs 两侧留白 30.2 / 64.0。
    即矩形只由分辨率决定，与地图内容无关 —— 无需识别。
  * 抓屏链路 GetClientRect → CreateCompatibleBitmap → BitBlt (0x1400060d5)；
    校准流程 0x140005af0 把「完整地图基准」存入 this+0x7C8。字符串表中的
    「同步位置异常 · 已恢复完整地图位置」「同步失败 · 已保留上次地图位置」
    说明识别只用于跟踪玩家在完整地图上的放大 / 平移，基础矩形本身是固定的。
  * 0x140042be0 的 4:3 / 16:9 / 16:10 比例表（0.7441W / 0.5462H / 0.9833W）
    是右下角小地图（迫击炮测距选区）用的，与 M 大地图是两套，切勿混用。

因此本程序**不做任何图像识别 / 抓屏**：点位按固定坐标直接画到 side=H 的矩形上，
分辨率变化自动重算。仅当 HUD 缩放与标准不同时，可在设置里手动补偿 scale/offset。

开源实现 / 排版复用：
  * gnlenfn/pubg_sanhok_map —— main.py + build.py + assets + config.ini、托盘、
    全局热键、点击穿透、ttk.Notebook 分栏设置
  * SUONSUN9527/pubg-buddy —— 两个独立透明浮窗、图层开关、只显示当前图有的类别、
    36px 折叠圆徽章、描边线条图标、滚轮缩放
  * adzpm/pubg-map overlay —— 托盘透明度预设带勾选 / 版本号 / 热键占用提示 /
    Escape 隐藏 / 单实例激活 / skipTaskbar

数据：pubgmap.top 公开只读接口，6 地图 / 7 类别 / 456 点位，随 EXE 打包。
  每张图展示哪些类别由 `map_type_marker_types` 决定（维寒迪还有熊洞/撬棍房/实验营地，
  蒂斯顿用安全门替代密室）；点位图标用 `marker_types.icon_url` 的官方 SVG，
  渲染参数照抄网页 Leaflet：iconSize 32×32、iconAnchor 底部中心。
纯屏幕上层绘制，不读游戏内存、不注入、不抓包、运行时不联网。
"""
import configparser
import ctypes
import json
import math
import os
import queue
import re
import subprocess
import sys
import threading
import time
import traceback
from ctypes import wintypes
from pathlib import Path

import tkinter as tk
from tkinter import ttk

from PIL import Image, ImageChops, ImageDraw, ImageGrab, ImageOps, ImageTk

try:
    import win32gui
    import win32con
except ImportError:
    win32gui = None
    win32con = None

APP_NAME = "PUBG-MapRuler"
# 版本号划分（2026-11-15 用户要求，不再逐次递增）：
#   主版本 X.0.0 = 重大重构 / 不兼容变更
#   次版本 4.x.0 = 新功能批次（一批功能合起来发一版）
#   修订   4.0.x = bug 修复、体验微调
# v4.0.0 = 帮助滑出面板 + 真自定义热键（捕获态实时预览）功能批次
APP_VER = "4.0.0"
APP_TITLE = "PUBG 地图投影浮层"

# ---------------------------------------------------------------- Win32
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)

WM_HOTKEY, WM_COMMAND, WM_CLOSE, WM_DESTROY = 0x0312, 0x0111, 0x0010, 0x0002
WM_APP = 0x8000
WM_TRAY, WM_SHOW_SELF, WM_REBIND, WM_DISPLAYCHANGE = (WM_APP + 1, WM_APP + 2,
                                                      WM_APP + 3, 0x007E)
WM_RBUTTONUP, WM_LBUTTONUP = 0x0205, 0x0202

MOD_ALT, MOD_CONTROL, MOD_NOREPEAT = 0x0001, 0x0002, 0x4000
VK_OEM_COMMA = 0xBC
VK_RBUTTON, VK_LBUTTON = 0x02, 0x01

WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
GWL_EXSTYLE = -20
LWA_COLORKEY, LWA_ALPHA = 0x00000001, 0x00000002
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010
HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
NIM_ADD, NIM_DELETE = 0, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x01, 0x02, 0x04

MF_STRING, MF_SEPARATOR, MF_POPUP = 0x0000, 0x0800, 0x0010
MF_GRAYED, MF_CHECKED = 0x0001, 0x0008

M_TOGGLE, M_SETTINGS, M_GRID, M_EXIT = 1001, 1002, 1005, 1004
M_MAP_BASE, M_OPACITY_BASE = 2000, 3000
OPACITY_LEVELS = (55, 70, 85, 100)

# 显隐热键**由用户自定义**——修饰键限定左/右 Alt、左/右 Ctrl 四选一
# （RegisterHotKey 的 MOD_ALT/MOD_CONTROL 只认家族），主键任意
# （字母/数字/F1~F12/常用符号）。配置格式 "Side+Main"，如 "RAlt+,"、
# "LCtrl+F2"。触发瞬间用 GetAsyncKeyState 查物理按键区分左右边。
_HOTKEY_SIDE_VK = {"LAlt": 0xA4, "RAlt": 0xA5,
                   "LCtrl": 0xA2, "RCtrl": 0xA3}   # VK_L/RMENU, VK_L/RCONTROL
_HOTKEY_TEXT = {"LAlt": "左Alt", "RAlt": "右Alt",
                "LCtrl": "左Ctrl", "RCtrl": "右Ctrl"}
# 主键 keysym → VK（字母/数字/F 键按公式算，这里只列符号类）
_HOTKEY_MAIN_VK = {",": 0xBC, ".": 0xBE, "/": 0xBF, ";": 0xBA, "'": 0xDE,
                   "[": 0xDB, "]": 0xDD, "-": 0xBD, "=": 0xBB, "\\": 0xDC,
                   "`": 0xC0, "Space": 0x20, "Enter": 0x0D, "Tab": 0x09}
# 主键 VK → token（捕获态轮询用；字母/数字/F 键由公式生成，其余来自 _HOTKEY_MAIN_VK）
_HK_POLL_MAIN = {}
for _i in range(26):
    _HK_POLL_MAIN[0x41 + _i] = chr(ord("A") + _i)
for _i in range(10):
    _HK_POLL_MAIN[0x30 + _i] = chr(ord("0") + _i)
for _n in range(1, 25):
    _HK_POLL_MAIN[0x6F + _n] = f"F{_n}"
for _tok, _vk in _HOTKEY_MAIN_VK.items():
    _HK_POLL_MAIN[_vk] = _tok


def parse_toggle_hotkey(val):
    """配置值 → 规范 spec "Side+Main"。非法/旧格式回落 RAlt+,。"""
    v = str(val or "").strip()
    parts = v.split("+", 1)
    if len(parts) == 2 and parts[0] in _HOTKEY_SIDE_VK and parts[1].strip():
        return f"{parts[0]}+{parts[1].strip()}"
    return "RAlt+,"


def hotkey_main_vk(main):
    """主键 token → VK 码；不认识的键返回 None。"""
    m = main.strip()
    if len(m) == 1:
        ch = m.upper()
        if "A" <= ch <= "Z" or "0" <= ch <= "9":
            return ord(ch)
    ml = m.lower()
    if ml.startswith("f") and ml[1:].isdigit():
        n = int(ml[1:])
        if 1 <= n <= 24:
            return 0x6F + n            # VK_F1 = 0x70
    return _HOTKEY_MAIN_VK.get(m)


def hotkey_disp(spec):
    """spec → 显示文案，如「右Alt + ,」「左Ctrl + F2」。"""
    v = parse_toggle_hotkey(spec)
    side, main = v.split("+", 1)
    return f"{_HOTKEY_TEXT[side]} + {main}"


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HICON),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.RegisterClassW.restype = wintypes.ATOM
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
user32.CreateWindowExW.restype = wintypes.HWND
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                  wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int,
                                  wintypes.UINT, wintypes.UINT]
user32.RegisterHotKey.restype = wintypes.BOOL
user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                               wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = ctypes.c_int
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                wintypes.WPARAM, wintypes.LPARAM]
user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowW.restype = wintypes.HWND
user32.GetParent.argtypes = [wintypes.HWND]
user32.GetParent.restype = wintypes.HWND
user32.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR,
                               wintypes.LPCWSTR, wintypes.UINT]
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE


def enable_dpi_awareness():
    """源码态兜底：在建窗口前把进程设成 PerMonitorV2。

    EXE 态靠的是打包时 --manifest（src/app.manifest）在进程创建时就声明
    PMv2 —— DPI 感知只能设一次，PyInstaller 默认 manifest 抢先设成 unaware
    之后这里再调会被系统拒绝（这正是以前「源码正常、EXE 副屏错位」的根因：
    主屏 125% / 副屏 150% 混合 DPI 下，unaware 窗口的坐标、内容、点击三者
    对不上）。    两种态现在都是 PMv2，坐标空间统一为物理像素。"""
    try:
        # 注意：这个 API 在 user32.dll，不在 shcore —— 以前写成 shcore 一直
        # 抛 AttributeError 静默降级到下面的系统感知，副屏（150%屏）上错位。
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def screen_metrics():
    return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)  # SM_CXSCREEN / SM_CYSCREEN


def virtual_screen():
    """整个虚拟桌面（多显示器时 = 所有屏的外接矩形）。"""
    return (user32.GetSystemMetrics(76), user32.GetSystemMetrics(77),
            user32.GetSystemMetrics(78), user32.GetSystemMetrics(79))


def secondary_monitor():
    """返回第一块**非主屏**显示器的矩形 (l, t, r, b)；只有一块屏时返回 None。

    主屏的虚拟坐标原点恒为 (0,0)，其余屏按相对位置排布。"""
    from ctypes import wintypes as _wt
    rects = []

    def _cb(hmon, hdc, lprect, lparam):
        r = lprect.contents
        rects.append((r.left, r.top, r.right, r.bottom))
        return True

    MONITORENUMPROC = ctypes.WINFUNCTYPE(
        ctypes.c_int, _wt.HMONITOR, _wt.HDC,
        ctypes.POINTER(_wt.RECT), _wt.LPARAM)
    try:
        if not user32.EnumDisplayMonitors(0, 0, MONITORENUMPROC(_cb), 0):
            return None
    except Exception:
        return None
    for r in rects:
        if (r[0], r[1]) != (0, 0):
            return r
    return None


def dpi_scale():
    """Tk 画布单位是逻辑像素，系统按 DPI 放大；网页的 32px 要换算成逻辑单位。"""
    try:
        return max(1.0, user32.GetDpiForSystem() / 96.0)
    except Exception:
        return 1.0


# ---------------------------------------------------------------- 抗锯齿绘制
class RoundArt:
    """用 PIL 4× 超采样预渲染圆角矩形与圆点。

    Tk Canvas 在 Windows 上走 GDI，圆弧/多边形没有抗锯齿，胶囊两端会呈
    阶梯状（就是反馈里「一点一点的、圆角不连续」）。这里以 4 倍尺寸绘制再
    LANCZOS 缩回 1x，边缘平滑；文字仍交给 Tk 原生渲染，保持笔画清晰。
    结果按 (尺寸, 颜色, 线宽) 缓存成 PhotoImage，反复绘制只算一次。
    """
    SS = 4                      # 超采样倍数

    def __init__(self, master):
        self.master = master
        self._cache = {}
        self._icon_src = {}

    @staticmethod
    def _rgba(c):
        if not c:
            return None
        c = str(c).lstrip("#")
        try:
            if len(c) == 6:
                return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16), 255)
            if len(c) == 8:
                return (int(c[0:2], 16), int(c[2:4], 16),
                        int(c[4:6], 16), int(c[6:8], 16))
        except ValueError:
            pass
        return (255, 255, 255, 255)

    def _finish(self, im, w, h, key):
        ph = ImageTk.PhotoImage(
            im.resize((max(1, int(w)), max(1, int(h))), Image.LANCZOS),
            master=self.master)
        self._cache[key] = ph
        return ph

    def pill(self, w, h, fill, outline=None, ow=1, radius=None):
        """圆角胶囊/圆角矩形；radius=None 表示两端全圆（胶囊）。"""
        w, h = max(2, int(w)), max(2, int(h))
        key = ("pill", w, h, fill, outline, int(ow), radius)
        ph = self._cache.get(key)
        if ph is not None:
            return ph
        S = self.SS
        W, H = w * S, h * S
        im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        r = (H // 2) if radius is None else int(radius) * S
        r = max(0, min(r, H // 2, W // 2))
        d.rounded_rectangle((0, 0, W - 1, H - 1), radius=r,
                            fill=self._rgba(fill),
                            outline=self._rgba(outline) if outline else None,
                            width=max(1, int(ow)) * S if outline else 0)
        return self._finish(im, w, h, key)

    def dot(self, size, fill=None, outline=None, ow=1):
        """圆形（实心 / 空心圆环）。"""
        size = max(2, int(size))
        key = ("dot", size, fill, outline, int(ow))
        ph = self._cache.get(key)
        if ph is not None:
            return ph
        S = self.SS
        D = size * S
        im = Image.new("RGBA", (D, D), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        d.ellipse((0, 0, D - 1, D - 1),
                  fill=self._rgba(fill) if fill else None,
                  outline=self._rgba(outline) if outline else None,
                  width=max(1, int(ow)) * S if outline else 0)
        return self._finish(im, size, size, key)

    def pin(self, icon_path, size, dim=False, keyed=True):
        """真图标：直接使用网站 marker_types.icon_url 的官方 SVG（已栅格化为 PNG）。

        网页端是 Leaflet `L.icon({iconSize:[32,32], iconAnchor:[16,32]})`，
        即水滴针尖端对准坐标点；这里缩放到目标尺寸后原样贴图，不做二次绘制。

        `dim=True` 出「未选中」版：抽掉颜色再压暗。图例默认全关，光靠琥珀色描边
        区分不够，图标本身也灰掉，一眼就能看出哪些类别没开。

        `keyed=True`（投影层专用）：透明区填 KEYCOLOR、alpha 压成 0/255，
        供 colorkey 抠图消黑边；但面板没有 colorkey，深色方片会原样画在
        圆底上（就是「图标重叠」的来源）——面板必须用 keyed=False，
        保留真 alpha，只显示水滴形状。"""
        size = max(8, int(size))
        key = ("pin", str(icon_path), size, bool(dim), bool(keyed))
        ph = self._cache.get(key)
        if ph is not None:
            return ph
        src = self._icon_src.get(str(icon_path))
        if src is None:
            src = Image.open(str(icon_path)).convert("RGBA")
            self._icon_src[str(icon_path)] = src
        im = src.resize((size, size), Image.LANCZOS)
        if dim:
            r, g, b, a = im.split()
            gray = Image.merge("RGB", (r, g, b)).convert("L").point(
                lambda v: int(v * 0.52))
            im = Image.merge("RGBA", (gray, gray, gray, a))
        if keyed:
            # 消黑边：投影层用 colorkey 抠透明，半透明像素会被画成「图标 × 近黑键色」
            # 的混合色，比键色深一点点的像素全都显示出来，看上去就是图标外一圈黑边。
            # 统一把 alpha 压成 0/255 两档，再整片叠在键色上：要么全色、要么全透明。
            a = im.getchannel("A").point(lambda v: 255 if v >= 150 else 0)
            solid = Image.new("RGBA", im.size, (*hex_rgb(KEYCOLOR), 255))
            im = Image.composite(im, solid, a).convert("RGB")
        return self._finish(im, size, size, key)


# ---------------------------------------------------------------- 网格测距
class GridProbe:
    """从屏幕像素里读出 PUBG M 地图自带网格，换算「米/逻辑像素」。

    只读屏幕像素（GDI 抓屏），不接触游戏进程、不读内存、不注入。

    两种网格线（用户口径，也是实测结果）：
      * 黑色粗线 = 1 km（1000 m）大格
      * 白色细线 = 100 m 小格
    颜色就是分类依据，**与缩放无关** —— 不需要假设大格在屏幕上是多少像素，
    所以滚轮放大到 360 / 720 px、甚至整屏看不到大格时同样成立。

    周期求法：峰间距投票 → 升序试第一个能拟合上的 → 最小二乘精修到亚像素。
    精度：只看单间隔时误差 = 间距误差/间距（18 px 小格 ±1 px 就是 5.6%），
    用几十条线一起拟合后相对误差约 0.1%，因此小格可以直接用。

    缩放约束：颜色分类本身与缩放无关；像素下限只取满图间距的 0.45 倍当量级
    参考（面板可能不铺满方屏），主要靠「大格与小格必然差 10 倍」互相校正
    「只检到隔一条线」的 2 倍误判。
    """

    MAJOR_M = 1000.0
    MINOR_M = 100.0
    PAD_RATIO = 0.09

    @classmethod
    def _profiles(cls, im):
        """B 通道 + 亮度两套剖面。

        实测：1 km 黑线在蓝通道被压低约 100/255，而亮度里 B 只占 0.114，
        按亮度算只剩 ~11 的落差，雪地/水面上容易淹掉；反过来 100 m 白线在
        亮度上最明显。所以黑线优先看 B，白线优先看亮度，互为备选。
        """
        S = min(im.size)
        pad = int(S * cls.PAD_RATIO)          # 去掉边缘罗盘 / 队友栏
        rgb = im.convert("RGB").crop((pad, pad, S - pad, S - pad))

        def prof(img):
            # tobytes() 比 getdata() 快且不触发 Pillow 的弃用告警（单通道图即 0~255）
            col = img.resize((img.width, 1), Image.BOX).tobytes()
            row = img.resize((1, img.height), Image.BOX).tobytes()
            return col, row

        return prof(rgb.split()[2]), prof(rgb.convert("L"))

    @staticmethod
    def _signed(prof, r=20):
        """减去局部均值：暗线为负、亮线为正，抵消大片地形的明暗。

        用前缀和算窗口均值（O(n)）。线只有 1~3 px，对 41 px 窗口均值的影响
        不到 5，不影响判断；换成均值是为了让一次测量能跑 4 条剖面还不卡。
        """
        n = len(prof)
        pre = [0.0] * (n + 1)
        for i, v in enumerate(prof):
            pre[i + 1] = pre[i] + v
        out = [0.0] * n
        for i in range(n):
            a, b = max(0, i - r), min(n, i + r + 1)
            out[i] = prof[i] - (pre[b] - pre[a]) / (b - a)
        return out

    @staticmethod
    def _peaks(d, sign, min_v, tol=0.8):
        """找局部峰。放大后线条会糊成「平台」，所以要认平台并取中点，
        否则同值像素互相不满足「严格大于」而被整条丢掉。"""
        n = len(d)
        vals = [d[i] * sign for i in range(n)]
        out = []
        i = 1
        while i < n - 1:
            v = vals[i]
            if v < min_v:
                i += 1
                continue
            if v + tol < vals[i - 1]:
                i += 1
                continue
            j = i
            while j + 1 < n and abs(vals[j + 1] - v) <= tol:
                j += 1
            if j + 1 < n and vals[j + 1] > v + tol:
                i = j + 1
                continue
            c = (i + j) // 2
            amp = max(vals[i:j + 1])
            if out and c - out[-1][1] <= 3:
                if amp > out[-1][0]:
                    out[-1] = (amp, c)
            else:
                out.append((amp, c))
            i = j + 1
        return out

    @staticmethod
    def _fit(xs, p):
        """把峰位拟合成 x = a + b*k，返回 (周期, 残差 rms, 内点数, 跨度)。"""
        if p <= 0 or len(xs) < 2:
            return None
        off = min(xs)
        a, b = off, p
        for _ in range(3):
            groups = {}
            for x in xs:
                k = int(round((x - a) / b))
                groups.setdefault(k, []).append(x)
            pts = [(k, sum(v) / len(v)) for k, v in sorted(groups.items())]
            if len(pts) < 2:
                return None
            n = len(pts)
            sk = sum(k for k, _ in pts)
            sx = sum(x for _, x in pts)
            skk = sum(k * k for k, _ in pts)
            skx = sum(k * x for k, x in pts)
            den = n * skk - sk * sk
            if abs(den) < 1e-9:
                return None
            b = (n * skx - sk * sx) / den
            a = (sx - b * sk) / n
            if b <= 0:
                return None
        groups = {}
        for x in xs:
            groups.setdefault(int(round((x - a) / b)), []).append(x)
        devs, ks = [], []
        for k, v in groups.items():
            devs.extend(abs(x - (a + b * k)) for x in v)
            ks.append(k)
        inl = sum(1 for dv in devs if dv <= 1.6)
        rms = (sum(dv * dv for dv in devs) / max(1, len(devs))) ** 0.5
        span = b * (max(ks) - min(ks)) if ks else 0.0
        return b, rms, inl, span

    @classmethod
    def _solve(cls, d, peaks, min_period, dur):
        """给定峰位集合，投票 + 拟合求基本周期。"""
        if len(peaks) < 2:
            return None
        xs = [p for _, p in peaks]
        amps = sorted(v for v, _ in peaks)
        amp_med = amps[len(amps) // 2]

        from collections import Counter
        cand = Counter()
        m = len(xs)
        for a in range(m):
            for b in range(a + 1, min(a + 48, m)):
                g = xs[b] - xs[a]
                if g < min_period:
                    continue
                for dd in (-1, 0, 1):
                    cand[g + dd] += 1

        # 升序试：第一个通过校验的就是基本周期（更大的都是它的整数倍）
        for per, cnt in sorted(cand.items()):
            if per < min_period * 0.97 or cnt < 1:
                continue
            fit = cls._fit(xs, float(per))
            if not fit:
                continue
            period, rms, inl, span = fit
            if period < min_period * 0.97:
                continue
            n = len(xs)
            if n >= 4:
                if rms > 1.1 or inl < 0.6 * n or span < period * 2.2:
                    continue
            elif n == 3:
                if rms > 0.7 or inl < 3 or span < period * 1.9:
                    continue
            else:                                   # 只剩两条线：证据很弱
                # 深放大时 1km 大格整屏只有 2 根，且渲染细、幅度降到 32~39
                # （v3.9.18 实测：720px 间距两根线 amp 32/39 被 45 卡死 →
                # 网格量不到 → 测距被闸门拦成「无轨迹」）。地形噪声峰只有
                # 6~8，26 仍分得干净；两线间距即周期，靠比值校验兜底。
                if rms > 0.9 or amp_med < 26 or span < period * 0.95:
                    continue
            conf = (min(1.0, amp_med / 40.0) * min(1.0, n / 6.0)
                    * max(0.0, 1.0 - rms / 2.0) * min(1.0, span / dur))
            return period, conf, inl, span
        return None

    @classmethod
    def _lattice(cls, d, sign, min_period):
        """在整条剖面里找基本周期（不预设缩放）。

        剖面里除了网格线，还有幅度只有真线十分之一左右的地形小峰；它们会把
        拟合搅乱（真线明明在、却拟合不出来）。所以先用「强峰」求解，不够再
        逐级放宽。完全没有网格时强峰幅度也很低（< 25），直接判失败。
        """
        raw = cls._peaks(d, sign, 6.0)
        if len(raw) < 2:
            return None
        top = max(v for v, _ in raw)
        if top < 18.0:                     # 没有像样的线，别猜
            return None                     # （v3.9.14: 22→18，白细线实测 21.4
                                            #   被 22 卡死，改由 _solve 自身校验兜底）
        dur = float(max(1, len(d)))
        for frac in (0.45, 0.22):
            peaks = [pk for pk in raw if pk[0] >= max(6.5, top * frac)]
            if len(peaks) < 2:
                continue
            r = cls._solve(d, peaks, min_period, dur)
            if r:
                return r
        return None

    @classmethod
    def _guided_minor(cls, profs, Pd):
        """大格已知时按 Pd/10 的期望位置核小格（双重判定的加强路径）。

        小格白线只有 1~2px，整宽平均后幅度常低于独立求解门槛（实测 21.4
        差 22.0 一口气）；但大格可信时小格峰必然落在 k*Pd/10 ±4px 处，
        直接按期望间距拟合，内点率/残差达标才认（用户口径：黑大格 1km、
        白细格 100m，两者必须互相印证）。返回 (Pb, conf, inl, span) 或 None。"""
        expect = Pd / 10.0
        best = None
        for d in profs:
            raw = cls._peaks(d, 1, 4.0)
            if len(raw) < 4:
                continue
            xs = [p for _, p in raw]
            pos, k = [], 1
            while expect * k < len(d):
                c = expect * k
                near = [x for x in xs if abs(x - c) <= 4.0]
                if near:
                    pos.append(min(near, key=lambda x: abs(x - c)))
                k += 1
            n = len(pos)
            if n < 5:
                continue
            fit = cls._fit(pos, expect)
            if not fit:
                continue
            period, rms, inl, span = fit
            if not (expect * 0.8 <= period <= expect * 1.25):
                continue
            if rms > 1.2 or inl < 0.6 * n or span < period * 4:
                continue
            conf = (0.4 * min(1.0, inl / float(n))
                    * max(0.0, 1.0 - rms / 2.0) * min(1.0, span / max(1.0, len(d))))
            if best is None or conf > best[1]:
                best = (period, conf, inl, span)
        return best

    @classmethod
    def probe(cls, im, km_size, side):
        side = max(1.0, float(side))
        (bc, br), (lc, lr) = cls._profiles(im)
        sB = (cls._signed(bc), cls._signed(br))
        sL = (cls._signed(lc), cls._signed(lr))

        # 满图时 1 km = side/kmSize 像素。但 M 地图面板不保证铺满整块方屏
        # （维寒迪实测：整图在屏时大格 180 px，而 side/kmSize = 240 px），
        # 所以这个值只当「量级参考」，不拿来当前置下限。
        base = side / max(1.0, float(km_size))

        def scan(profs, sign, min_period):
            best = None
            for pr in profs:
                r = cls._lattice(pr, sign, min_period)
                if r and (best is None or r[1] > best[1]):
                    best = r
            return best

        # 峰值搜索用很小的下限（只看「有没有周期性」），识别后再用满图间距
        # 做一次量级校验 —— 下限卡太死会把「面板没铺满方屏」的正常情况判死。
        dark = scan(sB, -1, 24.0) or scan(sL, -1, 24.0)   # 黑粗线 = 1 km
        bright = scan(sL, 1, 10.0) or scan(sB, 1, 10.0)   # 白细线 = 100 m
        Pd = dark[0] if dark else 0.0
        Pb = bright[0] if bright else 0.0
        # 双重判定加强路径：大格已可信、小格独立求解失败时，按 Pd/10 的
        # 期望位置去核小格峰（小格线太细，整宽平均后幅度常差一口气）
        if Pd and not Pb:
            g = cls._guided_minor((sL[0], sL[1], sB[0], sB[1]), Pd)
            if g:
                Pb, bright = g[0], (g[0], g[1], g[2], g[3])
        # 大格不会比满图间距的 40% 还小（面板再小也不是这个量级）：小了就是
        # 锁到了半周期或地形噪声，宁可不要
        if Pd and Pd < base * 0.40:
            Pd, dark = 0.0, None
        if Pb and Pb < base * 0.035:
            Pb, bright = 0.0, None
        if not Pd and not Pb:
            dark = bright = None

        # 大格必是小格的 10 倍；只检到「隔一条」时会出现 20 / 5 倍，按此校正
        if Pd and Pb:
            r = Pd / Pb
            if 17.0 <= r <= 23.0:
                Pd /= 2.0
            elif 4.2 <= r <= 5.8:
                Pb /= 2.0
            elif not (7.5 <= r <= 13.0):
                Pd, Pb = (Pd, 0.0) if (dark and dark[1] >= bright[1]) else (0.0, Pb)

        cands = []
        if Pd:
            cands.append((cls.MAJOR_M / Pd, dark[1], dark[2], "1 km", Pd, Pb))
        if Pb:
            cands.append((cls.MINOR_M / Pb, bright[1], bright[2], "100 m", Pd, Pb))
        # 置信太低的一律不认：结算页 / 观战这类没有网格的画面也会凑出「周期」，
        # 认了会让面板显示一个离谱的比例尺，宁可用解析式兜底
        cands = [c for c in cands if c[1] >= 0.12]

        if cands:
            # 两者都在且互证：按参与拟合的线数加权平均，误差再降一半
            if len(cands) == 2 and 7.5 <= Pd / Pb <= 13.0:
                w1, w2 = cands[0][2], cands[1][2]
                mpp = (cands[0][0] * w1 + cands[1][0] * w2) / max(1, w1 + w2)
                conf = min(1.0, max(cands[0][1], cands[1][1]) + 0.15)
                src = "1 km"
            else:
                best = max(cands, key=lambda c: c[1] * (1 + 0.02 * c[2]))
                mpp, conf, src = best[0], best[1], best[3]
            span = Pd if Pd else (Pb * 10.0 if Pb else 0.0)
            return dict(mpp=mpp, source=src, conf=conf,
                        major=Pd, minor=Pb, km=km_size,
                        zoom=max(0.0, span / base) if span else 1.0)

        # 网格被关掉 / 不是 M 地图 -> 解析式兜底（等价于假设未缩放）
        return dict(mpp=(float(km_size) * 1000.0) / side, source="估算",
                    conf=0.0, major=0.0, minor=0.0, km=km_size, zoom=1.0)


# ---------------------------------------------------------------- 轨迹识别
class TrackProbe:
    """从地图截图里认**游戏自己画的**标号轨迹，不碰玩家输入。

    实机形态（PUBG 地图 Alt+右键标记）：每个标记是一个**队伍色**圆环 + 深色底 +
    白色数字，相邻标记之间由同色细线连起来。标记颜色来自游戏蓝图
    UiHelperFunctionsBp.GetTeamColor(队伍序号)（逆向真值，Normal 色盲模式），
    队伍序号 → 颜色（用户实测口径）：1 号黄 #F4B300 / 2 号红 #DC0000 /
    3 号蓝 #0061E5 / 4 号绿 #068E00；
    色盲模式（ColorBlindType）只决定取哪一组变体常量。

    认法就是「按颜色找环 → 找线 → 串成链」：

      ① 颜色掩膜：每个队伍色一套通道不等式（HSV 色锥写法，全程 PIL 的 C 实现，
         比逐像素 Python 快两个数量级）；红色掩膜要抠掉黄色，避免黄环被抢；
      ② 环打分：半径 6~11 的圆环里同色点占比 ≥ 0.35，且半径 0~3 的中心是暗的
         —— 这一条把"实心色块""彩色地形"这些干扰项直接排除；
      ③ 连线判定：两点连线上逐点采样，同色覆盖率 ≥ 0.6 才算"被线连着"；
      ④ 串链：按连通关系分组，再从一端按最近邻走成有序点列。
    """

    @staticmethod
    def mask(im):
        r, g, b = im.split()
        m1 = r.point(lambda v: 255 if v > 165 else 0).convert("1")
        m2 = g.point(lambda v: 255 if v > 140 else 0).convert("1")
        m3 = b.point(lambda v: 255 if v < 125 else 0).convert("1")
        m4 = ImageChops.subtract(r, b, 1.0, -60).point(
            lambda v: 255 if v > 0 else 0).convert("1")
        m5 = ImageChops.subtract(g, b, 1.0, -45).point(
            lambda v: 255 if v > 0 else 0).convert("1")
        m = ImageChops.logical_and(ImageChops.logical_and(m1, m2),
                                   ImageChops.logical_and(m3, m4))
        return ImageChops.logical_and(m, m5)

    # 队伍标记色 = UiHelperFunctionsBp.GetTeamColor 逆向真值（Normal 色盲模式）
    # 序号即队伍号：1 黄 / 2 红 / 3 蓝 / 4 绿（用户实测口径，v3.9.14 修正）
    TEAM_RGB = ((244, 179, 0), (220, 0, 0), (0, 97, 229), (6, 142, 0))

    @staticmethod
    def _and(*ms):
        m = ms[0]
        for x in ms[1:]:
            m = ImageChops.logical_and(m, x)
        return m

    @classmethod
    def mask_for(cls, im, ti, line=False):
        """第 ti 号队伍（0 基）的颜色掩膜——**HSV 色相分类**。

        RGB 不等式盒的教训（一晚三轮调参）：屏幕上的标记色随曝光/削顶/
        背景混合逐帧漂移——蓝 #0061E5 → 纯青 (0,255,255)，黄 #F4B300 →
        削顶 (255,255,0) → 橙化 (255,173,45)，固定 RGB 阈值永远在追。
        色相 H 对亮度拉伸/削顶不敏感，一个区间覆盖所有变体；S 管饱和度
        （标记是 UI 叠加纯色），V 管明度（地形土黄 V≤84% 天然出局）。

        line=True 出**串链判定**掩膜（v3.9.26 起：宽松色相口径 + 形态严判）：
        相邻标记之间只要存在一条细直描边就算连线——形态由 _linked 负责
        （沿两环连线直线采样 + 法向 ±5/±7px 细描边校验，区域色块/宽道路
        在法向检查里出局），颜色在这里只做粗筛。连线与标记共用色相窗口，
        差异只在 S/V：S≥45%（半透明混色像素能过，登机阶段队友绿线混色
        S 掉到 54%，旧严格 S≥75% 把线拦腰截断）、V≥78%（**UI 叠加线永远
        满亮度**，实测黄/橙/青/绿真线核心 V=255；地形深绿植被 (75,148,93)
        V=55~63% 出局——v3.9.26 回归抓到的 3 组假绿链）。"""
        h1, h2, s_t, v_t = cls._HSV_BANDS[ti]
        if line:
            s_t, v_t = cls.LINE_S, cls.LINE_V
        h, s, v = im.convert("HSV").split()
        if ti == 1:                          # 红：H 环绕（<17° 或 >346°）
            hue = ImageChops.logical_or(cls._lo(h, h1), cls._hi(h, h2))
            return cls._and(hue, cls._hi(s, s_t), cls._hi(v, v_t))
        return cls._and(cls._hi(h, h1), cls._lo(h, h2),
                        cls._hi(s, s_t), cls._hi(v, v_t))

    @staticmethod
    def _hi(ch, t):
        return ch.point(lambda x: 255 if x > t else 0).convert("1")

    @staticmethod
    def _lo(ch, t):
        return ch.point(lambda x: 255 if x < t else 0).convert("1")

    # 每队（0基）HSV 掩膜参数（8bit：H = 色相角 × 255/360）：
    # (H 下限, H 上限, 标记 S 下限, 标记 V 下限)。掩膜只筛颜色，环打分再筛形态。
    _HSV_BANDS = {
        # 1号黄 H15~60°：覆盖 (244,179,0)/(255,255,0)削顶/(255,173,45)橙化/
        # (255,106,26)压哑；地形土黄 (188,132,57) V=74% 进不来，秸秆高光
        # (255,200,100) 由环形态打分 + _linked 连线兜底。
        0: (11, 44, 139, 223),
        # 2号红 H<17° 或 >346°（环绕，mask_for 里特殊处理）：色相与黄(≥15°)
        # 天然不相交，不再需要反选宽黄掩膜。
        1: (12, 245, 139, 177),
        # 3号蓝 H179~218°：覆盖 #0061E5(H215°) 与削顶纯青 (0,255,255)(H180°)；
        # 环描边抗锯齿混色 (86,160,164) S=47% V=64% 也要收进来，否则环覆盖
        # 率过不了 GATE_OUTER。暗水域 V<59% 出局，亮水假环靠形态打分兜底。
        # 上限 155 挡蓝色安全区（v3.9.25）：登机/跳伞阶段安全区填色
        # (91,131,255) H=226°(PIL 157~161) 整片过旧掩膜 → 42 万 px + 假环
        # + 蓝区自身过连线掩膜 → 55m 假链。真标记蓝家族 H≤216°(PIL≤153)。
        2: (123, 155, 114, 150),
        # 4号绿 H90~150°：S 85 收半透明队友标记（v3.9.25：登机阶段队友绿
        # 标记（圈+数字）环描边实测 S 仅 36~52%，旧 75% 全灭 → 绿链漏识别；
        # 连线掩膜 S=114 不动，队友绿线 (47,255,139) S=82% 绰绰有余）。
        3: (63, 107, 85, 101),
    }
    # 连线掩膜的 S/V 门槛（与 _HSV_BANDS 的标记门槛分开）
    LINE_S, LINE_V = 114, 200

    RING_OUT, RING_IN = 11, 6
    CORE_R = 3
    # 环门槛：outer 命中率下限 / inner 命中率上限。环描边在缩放/抗锯齿下
    # 只能盖住环带 ~40%（实测 0.37），0.35 余量太薄，降到 0.30（v3.9.13）；
    # 防误判靠 hollow 内核 + 串链时 _linked 细描边校验兜底。
    GATE_OUTER, GATE_INNER = 0.30, 0.20
    # 四象限均衡门槛（v3.9.15）：真环四面都有描边，直边/大色块边界只有
    # 半边 —— 实测真标记 min_quad=0.36~0.41，地形直边 0.00~0.22。它不
    # 参与「像不像环」的打分，只在出候选/收尾时挡直边假点。
    GATE_QUAD = 0.20

    @classmethod
    def _offsets(cls, r1, r2):
        key = (r1, r2)
        cache = cls.__dict__.get("_off_cache")
        if cache is None:
            cache = {}
            setattr(cls, "_off_cache", cache)
        if key not in cache:
            off = []
            for dy in range(-r2, r2 + 1):
                for dx in range(-r2, r2 + 1):
                    d2 = dx * dx + dy * dy
                    if r1 * r1 <= d2 <= r2 * r2:
                        off.append((dx, dy))
            cache[key] = off
        return cache[key]

    @classmethod
    def _ring_scan_np(cls, np, mask):
        """整图向量化环响应扫描：直接按「环形度」出候选（v3.9.15）。

        放大态下标记环描边只有 1~2px，8×8 粗格的密度门槛（thr）下环所在
        格子均值趋近 0，而密度排序里麦田大色块永远在前 —— 真标记根本进
        不了候选（v3.9.15 放大态「无轨迹」根因，实测环打分本身 0.44 远超
        门槛，死的是粗筛漏斗）。改为整图算环响应：outer（环带命中率）+
        inner（内核命中率）+ 四象限均衡 min_quad（真环四面都有描边，直边
        只有半边），过门槛的点按分数 spread 出候选。与密度候选互补：
        密度路径抓大而亮的标记，本路径抓细而弱的环。

        mask 传 L 图（marks 已转过，避免整幅 1→L 再来一遍）；
        int16 + 预分配缓冲：2560×1440 整图 46 次跨度累加，int32 临时数组
        每个十几 MB，4 队扫下来 4s 全耗在内存搬运上。"""
        if mask.mode != "L":
            mask = mask.convert("L")
        a = (np.asarray(mask, dtype=np.int16) > 0)
        H, W = a.shape
        pad = cls.RING_OUT
        row = np.zeros((H, W + 2 * pad + 1), np.int16)
        row[:, pad + 1:pad + 1 + W] = a.cumsum(axis=1, dtype=np.int16)
        # 四象限环带命中（与 _marks_np 同一套前缀和口径）：
        # q0=上左 q1=上右 q2=下左 q3=下右（dx=0 归右、dy=0 归下，与跨度处理一致）
        qcnt = [0, 0, 0, 0]
        for dx, dy in cls._offsets(cls.RING_IN, cls.RING_OUT):
            qcnt[(2 if dy >= 0 else 0) + (1 if dx >= 0 else 0)] += 1
        quads = [np.zeros((H, W), np.int16) for _ in range(4)]
        core = np.zeros((H, W), np.int16)
        segR = np.empty((H, W), np.int16)
        segL = np.empty((H, W), np.int16)
        for dy in range(-pad, pad + 1):
            t2 = pad * pad - dy * dy
            if t2 < 0:
                continue
            b2 = cls.RING_IN * cls.RING_IN - dy * dy
            if b2 > t2:
                continue
            hw = int(math.isqrt(t2))
            lw = int(math.ceil(math.sqrt(b2))) if b2 > 0 else 0
            lm = lw if lw > 0 else 1
            np.subtract(row[:, pad + hw + 1:pad + hw + 1 + W],
                        row[:, pad + lw:pad + lw + W], out=segR)
            np.subtract(row[:, pad - lm + 1:pad - lm + 1 + W],
                        row[:, pad - hw:pad - hw + W], out=segL)
            # 行偏移：环像素在 y+dy 行，剖面行平移 dy 再累加（seg[r] 是
            # 中心 r-dy 的响应）——漏了这个错位，真环会被稀释、边缘 UI 反而命中
            qi = 2 if dy >= 0 else 0
            if dy >= 0:
                quads[qi + 1][0:H - dy] += segR[dy:]
                quads[qi][0:H - dy] += segL[dy:]
            else:
                quads[qi + 1][-dy:] += segR[:H + dy]
                quads[qi][-dy:] += segL[:H + dy]
        seg = np.empty((H, W), np.int16)
        for dy in range(-cls.CORE_R, cls.CORE_R + 1):
            hw = int(math.isqrt(cls.CORE_R * cls.CORE_R - dy * dy))
            np.subtract(row[:, pad + hw + 1:pad + hw + 1 + W],
                        row[:, pad - hw:pad - hw + W], out=seg)
            if dy >= 0:
                core[0:H - dy] += seg[dy:]
            else:
                core[-dy:] += seg[:H + dy]
        ring_n = len(cls._offsets(cls.RING_IN, cls.RING_OUT))
        core_n = len(cls._offsets(0, cls.CORE_R))
        outer = (quads[0] + quads[1] + quads[2] + quads[3]).astype(np.float32) / ring_n
        inner = core.astype(np.float32) / core_n
        mnq = np.minimum(np.minimum(
            quads[0].astype(np.float32) / max(1, qcnt[0]),
            quads[1].astype(np.float32) / max(1, qcnt[1])),
            np.minimum(
            quads[2].astype(np.float32) / max(1, qcnt[2]),
            quads[3].astype(np.float32) / max(1, qcnt[3])))
        score = outer - 1.2 * inner
        cond = (outer >= cls.GATE_OUTER) & (inner <= cls.GATE_INNER) \
            & (mnq >= cls.GATE_QUAD)
        ys, xs = np.nonzero(cond)
        if not len(xs):
            return []
        cand = sorted(zip(xs.tolist(), ys.tolist()),
                      key=lambda p: -score[p[1], p[0]])
        spread, sep2 = [], cls.RING_OUT * cls.RING_OUT
        for x, y in cand:
            if all((x - cx) ** 2 + (y - cy) ** 2 >= sep2 for cx, cy in spread):
                spread.append((x, y))
            if len(spread) >= 48:
                break
        return spread

    @classmethod
    def marks(cls, mask, cell=8, thr=18, top=48, sep=22):
        """粗网格出候选 → 环打分定位。

        单独按粗值排序会被地图底部的黄色 UI 条带挤掉真标号（它们又长又亮），
        所以候选先做一遍**按距离的抑制**（`sep` px 内只留最强的一个），
        让候选铺开到整张图，再取前 `top` 个细打分。

        细打分在每个候选 ±16px 窗口内对环响应图取 argmax（窗口全局最优，
        等价于完美爬山）。v3.9.10 的单起点十字爬山会卡在环形打分的同心
        局部极值上（切线方向），偏心 10px 的候选永远收敛不到真中心 →
        标记丢失 → 「无轨迹」，v3.9.11 改为窗口 argmax 一劳永逸。"""
        small = mask.convert("L").resize((mask.width // cell, mask.height // cell),
                                         Image.BOX)
        w, h = small.size
        sp = small.load()
        W, H = mask.size
        cand = []
        for y in range(1, h - 1):
            for x in range(1, w - 1):
                v = sp[x, y]
                if v < thr:
                    continue
                if v >= sp[x - 1, y] and v >= sp[x + 1, y] \
                        and v >= sp[x, y - 1] and v >= sp[x, y + 1]:
                    cand.append((x * cell + cell // 2, y * cell + cell // 2, v))
        cand.sort(key=lambda p: -p[2])
        spread = []
        for x, y, v in cand:
            if all((x - cx) ** 2 + (y - cy) ** 2 >= sep ** 2 for cx, cy, _ in spread):
                spread.append((x, y, v))

        pts = [(x, y) for x, y, _v in spread[:top]]
        # 环形度整图扫描（v3.9.15）：密度粗筛在放大态会漏细环（环描边
        # 1~2px，8×8 格均值趋近 0，密度排序被麦田大色块碾压），按环形度
        # 直接出候选补上。两路候选 20px 去重后一起进窗口精修。
        # L 图只转一次，两条 numpy 路径共用（整幅 1→L 每次上百毫秒）。
        ml = mask.convert("L")
        try:
            import numpy as np
            if int((np.asarray(ml) > 0).sum()) < 40:   # 空掩膜直接短路
                return []
            ring_pts = cls._ring_scan_np(np, ml)
        except Exception:
            ring_pts = []
        merged = []
        for x, y in list(pts) + list(ring_pts):
            if all((x - ox) ** 2 + (y - oy) ** 2 >= 20 ** 2
                   for ox, oy in merged):
                merged.append((x, y))
        pts = merged
        try:
            import numpy as np
            out = cls._marks_np(np, ml, pts)
        except Exception:
            out = cls._marks_py(mask, pts)
        # 上下 UI 条带排除：抓帧带上游戏自己的 UI（顶部 FPS/UTC 蓝字、
        # 底部公里/小时进度条），它们的高饱和像素凑得出「环」也串得出
        # 假链（v3.9.14 实测：顶部两个蓝假点串出 126m 假测距）。地图
        # 标记极少贴到上下 5%/9% 边缘，代价可忽略。
        out = [(x, y) for x, y in out
               if H * 0.05 < y < H * 0.91]
        # 收敛去重：不同候选可能落到同一个环
        final = []
        for x, y in out:
            if all((x - ox) ** 2 + (y - oy) ** 2 >= 20 ** 2 for ox, oy in final):
                final.append((x, y))
        return final

    @classmethod
    def _marks_np(cls, np, mask, pts):
        """窗口环打分（numpy 快路径）：每个候选 ±16px 窗口内精确求最优环心。

        环打分与逐点统计完全同义：outer = 环带(RING_IN²..RING_OUT²)掩膜
        命中率，inner = 内核(≤CORE_R)命中率。环带每行有左右两个对称跨度，
        水平方向用零填充 + 行前缀和，边界/部分跨度天然正确。
        mask 传 L 图（marks 已转过）。"""
        if mask.mode != "L":
            mask = mask.convert("L")
        a = (np.asarray(mask, dtype=np.int32) > 0)
        H, W = a.shape
        pad = cls.RING_OUT
        row = np.zeros((H, W + 2 * pad + 1), np.int32)
        row[:, pad + 1:pad + 1 + W] = a.cumsum(axis=1, dtype=np.int32)
        ring_n = len(cls._offsets(cls.RING_IN, cls.RING_OUT))
        core_n = len(cls._offsets(0, cls.CORE_R))
        core_r = cls.CORE_R
        ring_in = cls.RING_IN
        # 四象限命中数（口径与 _ring_scan_np 一致：dx=0 归右、dy=0 归下）
        qcnt = [0, 0, 0, 0]
        for dx, dy in cls._offsets(ring_in, pad):
            qcnt[(2 if dy >= 0 else 0) + (1 if dx >= 0 else 0)] += 1
        gate_quad = cls.GATE_QUAD

        out = []
        for x, y in pts:
            x0, x1 = max(0, x - 16), min(W - 1, x + 16)
            y0, y1 = max(0, y - 16), min(H - 1, y + 16)
            w, h = x1 - x0 + 1, y1 - y0 + 1
            ring = np.zeros((h, w), np.int32)
            core = np.zeros((h, w), np.int32)
            quad = [np.zeros((h, w), np.int32) for _ in range(4)]
            for dy in range(-core_r, core_r + 1):        # 内核圆盘
                ty0, ty1 = max(y0, -dy), min(y1, H - 1 - dy)
                if ty0 > ty1:
                    continue
                hw = int(math.isqrt(core_r * core_r - dy * dy))
                r0, r1 = ty0 + dy, ty1 + dy + 1
                core[ty0 - y0:ty1 - y0 + 1] += (
                    row[r0:r1, x0 + pad + hw + 1:x0 + pad + hw + 1 + w]
                    - row[r0:r1, x0 + pad - hw:x0 + pad - hw + w])
            for dy in range(-pad, pad + 1):              # 环带（左右两跨度）
                t2 = pad * pad - dy * dy
                if t2 < 0:
                    continue
                b2 = ring_in * ring_in - dy * dy
                if b2 > t2:
                    continue
                hw = int(math.isqrt(t2))
                lw = int(math.ceil(math.sqrt(b2))) if b2 > 0 else 0
                ty0, ty1 = max(y0, -dy), min(y1, H - 1 - dy)
                if ty0 > ty1:
                    continue
                r0, r1 = ty0 + dy, ty1 + dy + 1
                segR = (row[r0:r1, x0 + pad + hw + 1:x0 + pad + hw + 1 + w]
                        - row[r0:r1, x0 + pad + lw:x0 + pad + lw + w])
                lm = lw if lw > 0 else 1                 # dx=0 归右跨度，防双计
                segL = (row[r0:r1, x0 + pad - lm + 1:x0 + pad - lm + 1 + w]
                        - row[r0:r1, x0 + pad - hw:x0 + pad - hw + w])
                ring[ty0 - y0:ty1 - y0 + 1] += segR
                ring[ty0 - y0:ty1 - y0 + 1] += segL
                # 右跨度 → q1/q3，左跨度 → q0/q2（与 _ring_scan_np 同口径）
                qi = 2 if dy >= 0 else 0
                quad[qi + 1][ty0 - y0:ty1 - y0 + 1] += segR
                quad[qi][ty0 - y0:ty1 - y0 + 1] += segL
            outer = ring.astype(np.float32) / ring_n
            inner = core.astype(np.float32) / core_n
            score = outer - 1.2 * inner
            # 先过门槛（outer/inner/四象限均衡）再取窗口最优：窗口里常有
            # score 更高的直边（0.5~0.65 > 环 0.44），argmax 会落在边上，
            # 直接对 argmax 处验门槛会把真环整候选否掉
            mnq_map = np.minimum(np.minimum(
                quad[0].astype(np.float32) / max(1, qcnt[0]),
                quad[1].astype(np.float32) / max(1, qcnt[1])),
                np.minimum(
                quad[2].astype(np.float32) / max(1, qcnt[2]),
                quad[3].astype(np.float32) / max(1, qcnt[3])))
            ok = (outer >= cls.GATE_OUTER) & (inner <= cls.GATE_INNER) \
                & (mnq_map >= gate_quad)
            if ok.any():
                s = np.where(ok, score, np.float32(-9.0))
                iy, ix = np.unravel_index(int(np.argmax(s)), s.shape)
                out.append((x0 + int(ix), y0 + int(iy)))
        return out

    @classmethod
    def _marks_py(cls, mask, pts):
        """纯 Python 兜底（numpy 不可用时）：逐点环打分 + 单起点爬山。"""
        px = mask.load()
        W, H = mask.size
        offsets = cls._offsets(cls.RING_IN, cls.RING_OUT)
        core = cls._offsets(0, cls.CORE_R)
        qcnt = [0, 0, 0, 0]
        for dx, dy in offsets:
            qcnt[(2 if dy >= 0 else 0) + (1 if dx >= 0 else 0)] += 1

        def score(cx, cy):
            q = [0, 0, 0, 0]
            hit = 0
            for dx, dy in offsets:
                if 0 <= cx + dx < W and 0 <= cy + dy < H and px[cx + dx, cy + dy]:
                    hit += 1
                    q[(2 if dy >= 0 else 0) + (1 if dx >= 0 else 0)] += 1
            outer = hit / max(1, len(offsets))
            hit = sum(1 for dx, dy in core
                      if 0 <= cx + dx < W and 0 <= cy + dy < H and px[cx + dx, cy + dy])
            mnq = min(qk / max(1, nk) for qk, nk in zip(q, qcnt))
            return (outer - 1.2 * hit / max(1, len(core)), outer,
                    hit / max(1, len(core)), mnq)

        out = []
        for x, y in pts:
            if any((x - ox) ** 2 + (y - oy) ** 2 < 20 ** 2 for ox, oy in out):
                continue
            best = (-9.0, x, y, 0.0)
            for step in (4, 2):
                for dx, dy in ((0, 0), (step, 0), (-step, 0), (0, step), (0, -step)):
                    s = score(best[1] + dx, best[2] + dy)[0]
                    if s > best[0]:
                        best = (s, best[1] + dx, best[2] + dy, 0.0)
            s, o, c, mnq = score(best[1], best[2])
            if o >= cls.GATE_OUTER and c <= cls.GATE_INNER \
                    and mnq >= cls.GATE_QUAD:
                out.append((best[1], best[2]))
        return out

    @classmethod
    def _linked(cls, px, W, H, a, b, ratio=0.62):
        """两点之间是否有一条真实连线：沿直线采样查掩膜。

        v3.9.11 加「细描边校验」：标记连线是 2~4px 的细描边，垂直于线方向
        ±5/±7px 必须出掩膜；而区域填充的边界（如蓝圈边缘，颜色与蓝色标记
        几乎一致、且整条弧线连续）一侧是整片色块，法向偏移仍落在掩膜里。
        只校验离两端 >13px 的采样点（端点附近是标号圆环，法向会撞上圆弧），
        远点不足 3 个时退回纯掩膜口径。"""
        ax, ay = a
        bx, by = b
        d = math.hypot(bx - ax, by - ay)
        n = max(16, int(d / 2.5))
        dx, dy = bx - ax, by - ay
        if d > 0:
            nx, ny = -dy / d, dx / d            # 单位法向
        else:
            nx = ny = 0.0

        def on(x, y):
            # ±3px 容差（v3.9.14 从 ±2 放宽）：1px 细线跨地形时抗锯齿会出
            # 1~2px 的小缺口，±2 搭不到 → 命中率 0.61 差 0.62 门槛一口气
            for ox in (-3, -2, -1, 0, 1, 2, 3):
                for oy in (-3, -2, -1, 0, 1, 2, 3):
                    xx, yy = x + ox, y + oy
                    if 0 <= xx < W and 0 <= yy < H and px[xx, yy]:
                        return True
            return False

        mask_hit = far_ok = far_total = 0
        for i in range(n + 1):
            t = i / n
            x = int(round(ax + dx * t))
            y = int(round(ay + dy * t))
            ok = on(x, y)
            mask_hit += ok
            if math.hypot(x - ax, y - ay) > 13 \
                    and math.hypot(x - bx, y - by) > 13:
                far_total += 1
                if ok:
                    edge = False
                    for s in (5, 7):
                        for sign in (1, -1):
                            xx = int(round(x + nx * s * sign))
                            yy = int(round(y + ny * s * sign))
                            if 0 <= xx < W and 0 <= yy < H and px[xx, yy]:
                                edge = True
                                break
                        if edge:
                            break
                    if not edge:
                        far_ok += 1
        if far_total >= 3:
            return far_ok / far_total >= ratio
        return mask_hit / (n + 1) >= ratio

    @classmethod
    def _chains(cls, pts, mask, max_chains=4):
        """在给定掩膜上把候选点串成有序链（连通分组 + 最近邻排序）。"""
        px = mask.load()
        W, H = mask.size
        n = len(pts)
        adj = {i: set() for i in range(n)}
        for i in range(n):
            for j in range(i + 1, n):
                d = math.hypot(pts[i][0] - pts[j][0], pts[i][1] - pts[j][1])
                if d < 22 or d > 1000:
                    continue
                if cls._linked(px, W, H, pts[i], pts[j]):
                    adj[i].add(j)
                    adj[j].add(i)
        seen, chains = set(), []
        for i in range(n):
            if i in seen or not adj[i]:
                continue
            comp, stack = [], [i]
            seen.add(i)
            while stack:
                k = stack.pop()
                comp.append(k)
                for m in adj[k]:
                    if m not in seen:
                        seen.add(m)
                        stack.append(m)
            # 从一端（度最小的点）按最近邻走成有序链
            end = min(comp, key=lambda k: len(adj[k]))
            order, left = [end], set(comp) - {end}
            while left:
                cur = order[-1]
                nxt = min(left, key=lambda k: (pts[k][0] - pts[cur][0]) ** 2
                          + (pts[k][1] - pts[cur][1]) ** 2)
                order.append(nxt)
                left.discard(nxt)
            chains.append([pts[k] for k in order])
        chains.sort(key=lambda c: (c[0][1], c[0][0]))
        return chains[:max_chains]

    @classmethod
    def detect_teams(cls, im, slots=4):
        """按队伍色各认各的标号轨迹：返回 {队伍序号(0基): 最长链}。

        同一队友可能画了多段标记，取首尾距离最长的一段（最可能是当前
        有效的测距段）。没认到的队伍不出现在返回值里。
        """
        out = {}
        for ti in range(min(4, slots)):
            mask = cls.mask_for(im, ti)
            pts = cls.marks(mask)
            if len(pts) < 2:
                continue
            # 连线判定用严格「标记线」掩膜：孤立标记（周围没有游戏画的
            # 连线）串不成链，链里每两点之间都必须有真实连线。
            chains = cls._chains(pts, cls.mask_for(im, ti, line=True), 4)
            if chains:
                out[ti] = max(chains, key=lambda c: math.hypot(
                    c[-1][0] - c[0][0], c[-1][1] - c[0][1]))
        return out

    @classmethod
    def detect(cls, im, max_chains=4):
        """（旧路径）黄色通认：返回按位置排序的若干条链。
        色盲模式 ≠ Normal 时标记色变体不同，退回这条通道。"""
        mask = cls.mask(im)
        pts = cls.marks(mask)
        if len(pts) < 2:
            return []
        return cls._chains(pts, mask, max_chains)


def hex_rgb(c):
    c = str(c).lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


# ---------------------------------------------------------------- 资源 / 配置
def app_root() -> Path:
    # PUBGMAP_DEV_FROZEN=1 让开发态也走「打包后」的资源路径分支，
    # 这样验证冻结态行为不必每次真的编译一次 EXE。
    if getattr(sys, "frozen", False) or os.environ.get("PUBGMAP_DEV_FROZEN") == "1":
        return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return Path(__file__).resolve().parent.parent


def resource(rel: str) -> Path:
    return app_root() / rel


def user_config_dir() -> Path:
    """便携模式：数据跟着 EXE 走，在 EXE 同文件夹下自建 PUBG-MapRuler-Data（v4.0.0 前为 PUBGMapOverlayEx-Data，自动迁移）。

    不再写 %LOCALAPPDATA%（用户明确要求：项目数据不许落在 C 盘默认目录）。
    开发态（未打包）数据放项目根目录，与打包后的「EXE 旁边」语义一致。
    旧版目录（PUBGMapOverlayEx-Data）存在时自动改名迁移，配置不丢。"""
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).resolve().parent
    else:
        base = Path(__file__).resolve().parent.parent
    d = base / (APP_NAME + "-Data")
    if not d.exists():
        old = base / "PUBGMapOverlayEx-Data"      # v4.0.0 改名前的数据目录
        if old.exists():
            try:
                old.rename(d)
            except OSError:
                pass                              # 被占用则下次启动再迁
    # dxgigrab 等惰性模块通过这个环境变量拿数据目录（便携模式，不碰 C 盘）
    os.environ["PUBGMAP_DATA_DIR"] = str(d)
    return d


class Config:
    def __init__(self):
        self.dir = user_config_dir()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "settings.ini"
        self.cp = configparser.ConfigParser()
        self.cp.optionxform = str
        self.cp.read_dict({
            "Window": {"selectedMap": "Erangel", "x": "16", "y": "16",
                       "opacity": "100", "topmost": "1",
                       "collapsed": "0"},
            "Projection": {"scale": "1.000", "offsetX": "0", "offsetY": "0",
                           "showGrid": "0", "dotSize": "5"},
            # 队伍编号块颜色 + 槽位数。默认 4 人（常规四排），特殊模式可调到 10。
            # 颜色直接改这里，重启即生效，不必重新编译。
            "Squad": dict({"slots": "4"},
                          **{f"c{i + 1}": SQUAD_COLORS[i] for i in range(SQUAD_MAX)}),
            "Layers": {},
            # HDR 屏白电平手动校准（nit）。0 = 自动（DisplayConfig 查询，
            # 结果落配置目录 dxgi.log）。若浮层地图显示比游戏里明显偏亮/偏暗，
            # 把这里改成游戏设置里 HDR 的「SDR 亮度」滑条数值即可。
            "Grab": {"sdrWhite": "0"},
            # 单个可自定义显隐热键，取值 LAlt/RCtrl 等（见 _HOTKEY_SIDE_VK）
            "Hotkeys": {"toggle": "RAlt+,"},
            "About": {"version": APP_VER, "source": "https://pubgmap.top/map"},
        })
        try:
            if self.path.exists():
                self.cp.read(self.path, encoding="utf-8")
        except (configparser.Error, UnicodeDecodeError):
            pass
        # 旧版 clickThrough 是「整窗锁死」，语义已废弃，读进来就丢掉
        if self.cp.has_section("Window"):
            self.cp.remove_option("Window", "clickThrough")
        # 3.7 之前 1 号色记的是 #8D8A16（从压暗的画面里误取的），实机原色是
        # #FFFF00。检测到旧值就整组刷新，用户自己改过的颜色不碰。
        if self.cp.get("Squad", "c1", fallback="").upper() in ("#8D8A16", "#777527"):
            for i in range(SQUAD_MAX):
                self.cp.set("Squad", f"c{i + 1}", SQUAD_COLORS[i])
        # v3.9.4 时代程序默认是亮色组（c1=#FFFF00）。v3.9.5 起默认换成了
        # 逆向出的官方真值（黄#F4B300/蓝#0061E5/绿#068E00/红#DC0000），
        # 但配置里存的旧默认会一直盖住新值 → 检测到旧默认整组刷新。
        if self.cp.get("Squad", "c1", fallback="").upper() == "#FFFF00":
            for i in range(SQUAD_MAX):
                self.cp.set("Squad", f"c{i + 1}", SQUAD_COLORS[i])
        # v3.9.13 及以前队伍序号→颜色是「1黄2蓝3绿4红」（逆向时排错的），
        # 用户实测口径是 **1黄2红3蓝4绿**。检测到旧顺序的 2/3/4 号就交换，
        # 用户自定义过的颜色（不构成旧顺序特征）不碰。
        _old = tuple(self.cp.get("Squad", f"c{i}", fallback="").upper()
                     for i in (2, 3, 4))
        if _old == ("#0061E5", "#068E00", "#DC0000"):
            self.cp.set("Squad", "c2", SQUAD_COLORS[1])
            self.cp.set("Squad", "c3", SQUAD_COLORS[2])
            self.cp.set("Squad", "c4", SQUAD_COLORS[3])
        # v3.9.28：显隐热键默认改右 Alt。hkVer 是「用户点选过」的标记：
        # 未标记（旧版/自动迁移产生的 LAlt+, 等）一律刷新为 RAlt+,；
        # 用户在帮助面板自定义过（hkVer>=2，v3.9.29 写 3）此后尊重其选择。
        try:
            _hk_ver = int(self.cp.get("Hotkeys", "hkVer", fallback="0"))
        except ValueError:
            _hk_ver = 0
        if _hk_ver < 2:
            self.cp.set("Hotkeys", "toggle", "RAlt+,")
            self.cp.set("Hotkeys", "hkVer", "2")
        # 白电平手动校准 → 传给 dxgigrab（模块惰性导入，走环境变量最省事）
        try:
            v = float(self.cp.get("Grab", "sdrWhite", fallback="0") or 0)
            if v > 0:
                os.environ["PUBGMAP_SDR_WHITE_NITS"] = str(v)
        except Exception:
            pass

    def get(self, sec, key, fallback=None):
        return self.cp.get(sec, key, fallback=fallback)

    def getint(self, sec, key, fallback=0):
        try:
            return self.cp.getint(sec, key, fallback=fallback)
        except (ValueError, TypeError):
            return fallback

    def getfloat(self, sec, key, fallback=0.0):
        try:
            return self.cp.getfloat(sec, key, fallback=fallback)
        except (ValueError, TypeError):
            return fallback

    def set(self, sec, key, value):
        if not self.cp.has_section(sec):
            self.cp.add_section(sec)
        self.cp.set(sec, key, str(value))

    def save(self):
        with open(self.path, "w", encoding="utf-8") as f:
            self.cp.write(f)


# ---------------------------------------------------------------- 原生宿主
class NativeHost:
    CLASS_NAME = "PUBGMapRulerHost"

    def __init__(self, actions: queue.Queue, icon_path: str, map_names,
                 hotkey_key="RAlt"):
        self.q = actions
        self.icon_path = icon_path
        self.map_names = map_names
        self.hk_key = hotkey_key
        self.hwnd = None
        self.hicon = None
        self._proc = WNDPROC(self._wnd_proc)
        self._tray_ok = False
        self._registered = []
        self.failed = []

    def start(self):
        h_inst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._proc
        wc.hInstance = h_inst
        wc.lpszClassName = self.CLASS_NAME
        if not user32.RegisterClassW(ctypes.byref(wc)):
            if ctypes.get_last_error() != 1410:
                return False
        self.hwnd = user32.CreateWindowExW(
            0, self.CLASS_NAME, self.CLASS_NAME, 0,
            0, 0, 0, 0, 0, None, h_inst, None)
        if not self.hwnd:
            return False
        # 单个显隐热键（v3.9.29 用户自定义）：修饰键家族 + 任意主键；
        # 左右边在触发时用 GetAsyncKeyState 区分
        self._register_hk()
        self._init_tray()
        threading.Thread(target=self._loop, daemon=True).start()
        return True

    def _register_hk(self):
        """按当前 hk_spec 注册热键（失败记入 failed，托盘菜单可见）。"""
        for hid in self._registered:
            try:
                user32.UnregisterHotKey(self.hwnd, hid)
            except Exception:
                pass
        self._registered, self.failed = [], []
        spec = parse_toggle_hotkey(self.hk_key)
        side, main = spec.split("+", 1)
        mod = MOD_CONTROL if "Ctrl" in side else MOD_ALT
        vk = hotkey_main_vk(main) or VK_OEM_COMMA
        if user32.RegisterHotKey(self.hwnd, 1, mod | MOD_NOREPEAT, vk):
            self._registered.append(1)
        else:
            self.failed.append(hotkey_disp(spec))

    def _init_tray(self):
        if not (win32gui and self.icon_path and os.path.exists(self.icon_path)):
            return
        try:
            self.hicon = win32gui.LoadImage(
                0, self.icon_path, win32con.IMAGE_ICON, 0, 0, 0x0010 | 0x0040)
            hint = f"{hotkey_disp(self.hk_key)} 显隐" if not self.failed \
                else "热键被占用，可在帮助面板换一个"
            nid = (self.hwnd, 0, NIF_MESSAGE | NIF_ICON | NIF_TIP,
                   WM_TRAY, self.hicon, f"PUBG 地图投影 · {hint}")
            win32gui.Shell_NotifyIcon(NIM_ADD, nid)
            self._tray_ok = True
        except Exception:
            self._tray_ok = False

    def _show_menu(self, opacity_pct, grid_on=False):
        if not win32gui:
            return
        try:
            menu = win32gui.CreatePopupMenu()
            win32gui.AppendMenu(menu, MF_STRING, M_TOGGLE,
                                f"显示 / 隐藏   {hotkey_disp(self.hk_key)}")
            win32gui.AppendMenu(menu, MF_STRING, M_SETTINGS, "设置")
            win32gui.AppendMenu(menu, MF_STRING | (MF_CHECKED if grid_on else 0),
                                M_GRID, "8×8 网格辅助线")
            win32gui.AppendMenu(menu, MF_SEPARATOR, 0, "")
            sub = win32gui.CreatePopupMenu()
            for i, name in enumerate(self.map_names):
                win32gui.AppendMenu(sub, MF_STRING, M_MAP_BASE + 1 + i, name)
            win32gui.AppendMenu(menu, MF_POPUP, sub, "切换地图")
            ops = win32gui.CreatePopupMenu()
            for i, pct in enumerate(OPACITY_LEVELS):
                flags = MF_STRING | (MF_CHECKED if pct == opacity_pct else 0)
                win32gui.AppendMenu(ops, flags, M_OPACITY_BASE + 1 + i, f"{pct}%")
            win32gui.AppendMenu(menu, MF_POPUP, ops, "透明度")
            win32gui.AppendMenu(menu, MF_SEPARATOR, 0, "")
            for txt in self.failed:
                win32gui.AppendMenu(menu, MF_STRING | MF_GRAYED, 0, f"热键 {txt} 被占用")
            win32gui.AppendMenu(menu, MF_STRING | MF_GRAYED, 0, f"版本 {APP_VER} · 本地数据")
            win32gui.AppendMenu(menu, MF_SEPARATOR, 0, "")
            win32gui.AppendMenu(menu, MF_STRING, M_EXIT, "退出")
            x, y = win32gui.GetCursorPos()
            user32.SetForegroundWindow(self.hwnd)
            win32gui.TrackPopupMenu(menu, win32con.TPM_LEFTALIGN | win32con.TPM_RIGHTBUTTON,
                                    x, y, 0, self.hwnd, None)
            user32.PostMessageW(self.hwnd, 0, 0, 0)
            win32gui.DestroyMenu(menu)
        except Exception:
            pass

    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_HOTKEY:
                # 单热键 + 左右区分（v3.9.29）：RegisterHotKey 只认家族修饰键，
                # 这里查物理按键——配置哪边就哪边生效。只拒绝「明确按了对边」
                # 的情况；两边都没查到（罕见时序）仍然放行，避免漏触发。
                side = parse_toggle_hotkey(self.hk_key).split("+", 1)[0]
                side_vk = _HOTKEY_SIDE_VK[side]
                other = 0xA3 if "Ctrl" in side else 0xA5    # 对边的 VK
                if (user32.GetAsyncKeyState(other) & 0x8000
                        and not user32.GetAsyncKeyState(side_vk) & 0x8000):
                    return 0
                self.q.put(("hotkey", "toggle"))
                return 0
            elif msg == WM_REBIND:                 # 帮助面板换了热键 → 重注册
                self._register_hk()
                self._update_tray_tip()
                return 0
            elif msg == WM_DISPLAYCHANGE:      # 分辨率 / DPI 变化 → 重算投影矩形
                self.q.put(("resync", None))
                return 0
            elif msg == WM_TRAY:
                low = lparam & 0xFFFF
                if low == WM_RBUTTONUP:
                    self.q.put(("tray_menu", None))
                elif low == WM_LBUTTONUP:
                    self.q.put(("hotkey", "toggle"))
                return 0
            elif msg == WM_SHOW_SELF:
                self.q.put(("hotkey", "show"))
                return 0
            elif msg == WM_COMMAND:
                cmd = wparam & 0xFFFF
                if cmd == M_TOGGLE:
                    self.q.put(("hotkey", "toggle"))
                elif cmd == M_SETTINGS:
                    self.q.put(("hotkey", "settings"))
                elif cmd == M_GRID:
                    self.q.put(("hotkey", "grid"))
                elif cmd == M_EXIT:
                    self.q.put(("hotkey", "quit"))
                elif M_MAP_BASE < cmd <= M_MAP_BASE + len(self.map_names):
                    self.q.put(("map", cmd - M_MAP_BASE - 1))
                elif M_OPACITY_BASE < cmd <= M_OPACITY_BASE + len(OPACITY_LEVELS):
                    self.q.put(("opacity", OPACITY_LEVELS[cmd - M_OPACITY_BASE - 1]))
                return 0
            elif msg == WM_CLOSE:
                user32.DestroyWindow(hwnd)
                return 0
            elif msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
        except Exception:
            pass
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _loop(self):
        msg = wintypes.MSG()
        while True:
            if user32.GetMessageW(ctypes.byref(msg), None, 0, 0) <= 0:
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def set_hotkey(self, spec):
        """（tk 线程调用）改绑显隐热键：存新 spec（"Side+Main"）+ 投递重绑。"""
        self.hk_key = parse_toggle_hotkey(spec)
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_REBIND, 0, 0)

    def suspend_hotkey(self):
        """（tk 线程调用）热键捕获期间暂停注册——防止用户按到旧组合时
        WM_HOTKEY 把面板切走、打断录制。退出捕获态由 set_hotkey 恢复。"""
        if not self.hwnd:
            return
        for hid in list(self._registered):
            try:
                user32.UnregisterHotKey(self.hwnd, hid)
            except Exception:
                pass
        self._registered = []

    def _update_tray_tip(self):
        if not self._tray_ok or not win32gui:
            return
        try:
            hint = f"{hotkey_disp(self.hk_key)} 显隐" if not self.failed \
                else "热键被占用，可在帮助面板换一个"
            nid = (self.hwnd, 0, NIF_MESSAGE | NIF_ICON | NIF_TIP,
                   WM_TRAY, self.hicon, f"PUBG 地图投影 · {hint}")
            win32gui.Shell_NotifyIcon(4, nid)   # 4 = NIM_MODIFY
        except Exception:
            pass

    def stop(self):
        for hid in self._registered:
            user32.UnregisterHotKey(self.hwnd, hid)
        if self._tray_ok and win32gui:
            try:
                win32gui.Shell_NotifyIcon(NIM_DELETE, (self.hwnd, 0, 0, 0, 0, ""))
            except Exception:
                pass
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)


def single_instance_guard():
    kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    h = kernel32.CreateMutexW(None, False, APP_NAME + "_Singleton")
    if ctypes.get_last_error() == 183:
        hwnd = user32.FindWindowW(NativeHost.CLASS_NAME, NativeHost.CLASS_NAME)
        if hwnd:
            user32.PostMessageW(hwnd, WM_SHOW_SELF, 0, 0)
        else:
            user32.MessageBoxW(0, "PUBG 地图投影已在运行，按配置的全局热键呼出。",
                               APP_TITLE, 0x40)
        return None
    return h


# ---------------------------------------------------------------- 视觉常量
BG, BAR, LINE, CY, TXT = "#0a1116", "#16262e", "#243b45", "#22d3ee", "#dbe9ee"
DIM, DIM2, CHIP, OK, GOLD = "#6d8996", "#435965", "#101d24", "#34d399", "#fbbf24"
FONT, MONO = "Microsoft YaHei UI", "Consolas"
KEYCOLOR = "#0b0d10"                  # 投影层的透明键色（须与点位颜色都不同）
KEYREF = 0x100D0B                     # COLORREF = 0x00BBGGRR

PS = 1.5                             # 面板整体缩放：匹配 150% 副屏的系统密度，
                                     # 固定值跨屏不变（窗口=内容 1:1 物理渲染）


def S(v):
    """面板逻辑设计值 → 实际像素（含字号/内边距/图形尺寸）。"""
    return int(round(v * PS))


PW, PAD = S(380), S(16)              # 570 / 24：放宽、加大留白
HELP_W = S(310)                      # 帮助扩展面板宽度（主面板右侧滑出）
CW = PW - 2 * PAD                    # 522
TITLE_H = S(40)                      # 60
LAYER_Y = S(52)                      # 78：标题栏下直接是类别图例：对齐参数不再展示
CAP_COLS = 4                         # 网页图例是「图标 + 下方名称」的瓦片，这里 4 列排布
CAP_W = (CW - (CAP_COLS - 1) * S(8)) // CAP_COLS
CAP_H, CAP_ROW = S(74), S(80)        # 111 / 120：60px 圆底 + 图标 + 下方名称
FOOT_H, CHIP_SIZE = S(34), S(36)     # 51 / 54

# 测距区：每条槽位只有编号块 + 距离条 + 数字，不写任何名称文字
MEAS_TITLE_H, M_ROW_H = S(26), S(25)  # 39 / 38
GRID_TTL = 6.0                    # 网格识别结果有效期（秒），Alt 按下会强制刷新
SQUAD_N = 4                       # 固定 4 个槽位（即使不满也不增减）
SQUAD_MAX = 10                    # 编号色表最多备到 10 号
# 队伍编号色。1~4 号是硬核逆向真值：解密 PUBG ui_bps.pak 索引 → Oodle 解压
# UiHelperFunctionsBp 蓝图字节码 → GetTeamColor 系列函数的 EX_FloatConst 常量
# （Normal 色盲模式 case0，线性色 ×255）。每色还带 Protanopia/Deuteranopia/
# Tritanopia 三个变体，印证了「蓝在色盲下不变、绿红互换」的设计。
# 5~10 号在蓝图中没有对应常量，仍为实机估色（阵亡态反推），仅观赛多队时用。
# 想微调就改配置 [Squad] c1..c10，程序不在运行时取色。
SQUAD_COLORS = (
    "#F4B300",   # 1 黄（逆向真值 0.956,0.701,0.0）
    "#DC0000",   # 2 红（逆向真值 0.863,0.0,0.0）
    "#0061E5",   # 3 蓝（逆向真值 0.0,0.381,0.896）
    "#068E00",   # 4 绿（逆向真值 0.023,0.558,0.0）
    "#FF5164",   # 5 红（阵亡态反推）
    "#FF71C3",   # 6 粉（阵亡态反推）
    "#6C92FF",   # 7 靛（阵亡态反推）
    "#8FE8FF",   # 8 浅青（实测原色）
    "#3FD8F0",   # 9 青（实测原色）
    "#4A5BD8",   # 10 深蓝（阵亡态反推）
)
M_COLORS = SQUAD_COLORS


# ---------------------------------------------------------------- 主应用
class App:
    def __init__(self, cfg: Config, data: dict):
        self.cfg, self.data = cfg, data
        self.types = {t["id"]: t for t in data["markerTypes"]}
        self.maps = data["maps"]
        self.mi = next((i for i, m in enumerate(self.maps)
                        if m["slug"] == cfg.get("Window", "selectedMap", "Erangel")), 0)

        self.q = queue.Queue()
        self.hk_key = parse_toggle_hotkey(cfg.get("Hotkeys", "toggle", "RAlt+,"))
        self.native = NativeHost(self.q, str(resource("assets/icon.ico")),
                                 [m["name"] for m in self.maps],
                                 hotkey_key=self.hk_key)
        self.settings_win = None
        # 帮助不是独立弹窗：点「?」后从主面板右侧滑出、连在主框上（v3.9.28）
        self.help_open = False
        self._help_w = PW                 # 当前面板总宽（含滑出动画）

        self.alpha = min(1.0, max(0.35, cfg.getint("Window", "opacity", 100) / 100.0))
        self.topmost = bool(cfg.getint("Window", "topmost", 1))
        self.slots = SQUAD_N
        self.squad = [cfg.get("Squad", f"c{i + 1}", SQUAD_COLORS[i]) or SQUAD_COLORS[i]
                      for i in range(self.slots)]
        self.collapsed = bool(cfg.getint("Window", "collapsed", 0))
        self.shown = True
        # 面板位置规则（用户定死，2026-11-14）：**永远不许出现在主屏**。
        # 有副屏：默认放副屏左上角内侧；配置里存过的坐标只有完整落在副屏内
        # 才恢复（尊重用户拖过的位置），落在主屏/屏外的旧坐标一律丢弃回默认。
        # 只有单屏时才允许 16,16（主屏）——没有别的选择。
        # 拖动落点在主屏也会被弹回，见 _drag_done。
        sec = secondary_monitor()
        Wp, Hp = screen_metrics()
        xv, yv, wv, hv = virtual_screen()
        multi = (xv, yv, wv, hv) != (0, 0, Wp, Hp)
        if sec:
            defx, defy = sec[0] + 16, sec[1] + 16
        elif multi:
            # 枚举失败但虚拟桌面确实跨屏：按虚拟桌面与主屏的相对方位推副屏角
            if xv < 0 or yv < 0:
                defx, defy = xv + 16, yv + 16
            elif wv > Wp:
                defx, defy = Wp + 16, yv + 16
            else:
                defx, defy = xv + 16, Hp + 16
        else:
            defx, defy = 16, 16
        self.px = cfg.getint("Window", "x", defx)
        self.py = cfg.getint("Window", "y", defy)
        if multi and (0 <= self.px < Wp and 0 <= self.py < Hp):
            # 旧坐标落在主屏矩形内 → 一律丢弃（这就是「老在主屏出现」的根因：
            # 早期某次拖动把 panelScreen=user + 主屏坐标固化进了配置）
            self.px, self.py = defx, defy
        # 一次性定位诊断：secondary_monitor 在真实环境里返回了什么、最终选了哪。
        try:
            with open(user_config_dir() / "place.log", "a",
                      encoding="utf-8") as _f:
                _f.write("%.0f sec=%s def=%s,%s cfg=%s,%s final=%s,%s\n"
                         % (time.time(), sec, defx, defy,
                            cfg.getint("Window", "x", -1),
                            cfg.getint("Window", "y", -1),
                            self.px, self.py))
        except Exception:
            pass
        self.panel_screen = ("user"
                             if cfg.get("Window", "panelScreen", "auto") == "user"
                             else "auto")
        self.enabled = set()
        self.scale = cfg.getfloat("Projection", "scale", 1.0)
        self.off_x = cfg.getint("Projection", "offsetX", 0)
        self.off_y = cfg.getint("Projection", "offsetY", 0)
        self.show_grid = bool(cfg.getint("Projection", "showGrid", 0))
        self.dot = cfg.getint("Projection", "dotSize", 5)
        self.rect = (0, 0, 0, 0)      # left, top, side, screenH
        self.regions = []
        self._drag = None
        self._last_metrics = (0, 0)

        # 读轨迹：网格比例尺缓存 + 4 个槽位（点「识别」时从整块地图一次性读出）
        self.grid = None
        self.grid_key = None
        self.grid_ts = 0.0
        self.measure = [None] * self.slots  # {"m": 米, "a": (x,y), "b": (x,y),
                                            #  "pts": [...], "straight": bool}
        self._last_frame = None             # 上一次手动识别抓到的整块地图
        self._scan_ts = 0.0                 # 上一次识别的时间

        if os.environ.get("PUBGMAP_TRACE"):
            def _tr(m):
                with open(user_config_dir() / "trace.log", "a", encoding="utf-8") as f:
                    f.write("%.2f %s\n" % (time.time(), m))
            self._tr = _tr
            _tr("init start")
        self._build_windows()
        self._load_layers()
        self._bind_events()
        self.native.start()
        self._resync()
        self._apply_styles()
        self.draw()
        self.save()
        if os.environ.get("PUBGMAP_TRACE"):
            self._tr(f"drawn shown={self.shown} collapsed={self.collapsed} px={self.px} py={self.py}")
        self.root.after(40, self._pump)
        self.root.after(2000, self._watch_metrics)
        if os.environ.get("PUBGMAP_SELFTEST_SCAN"):
            self.root.after(2500, self.rescan)
        if os.environ.get("PUBGMAP_SELFTEST_PINS"):
            def _pins():
                avail = self.cur()["availableTypeIds"]
                if avail:
                    self.enabled = {avail[1] if len(avail) > 1 else avail[0]}
                    self.draw()
                    self.draw_proj()
            self.root.after(2500, _pins)
        if os.environ.get("PUBGMAP_PROBE"):
            self.root.after(1200, self._probe_once)

    def _probe_once(self):
        """调试钩子：PUBGMAP_PROBE=1 时启动后探测一次网格并写 probe.log。
        用来核对「源码态 DPI aware / EXE 态 DPI unaware」两种坐标空间是否一致。"""
        try:
            g = self._ensure_grid(force=True)
            (self.cfg.dir / "probe.log").write_text(json.dumps(
                {"grid": g, "metrics": self._last_metrics,
                 "rect": [round(v, 1) for v in self.rect],
                 "grab": getattr(self, "_last_grab", None)},
                ensure_ascii=False), encoding="utf-8")
            self.draw()
        except Exception:
            pass

    # ------------------------------------------------------------ 两个窗口
    def _build_windows(self):
        self.root = tk.Tk()
        self.root.withdraw()                  # 隐藏根，两个 Toplevel 才是真实窗口
        self.art = RoundArt(self.root)        # 圆角/圆点走 PIL 超采样，避免 GDI 锯齿
        icon = resource("assets/icon.ico")

        # 投影层：透明、点击穿透、与游戏地图矩形同尺寸同位置
        self.proj = tk.Toplevel(self.root)
        self.proj.overrideredirect(True)
        self.proj.configure(bg=KEYCOLOR)
        try:
            self.proj.wm_attributes("-transparentcolor", KEYCOLOR)
        except Exception:
            pass
        self.proj.attributes("-topmost", self.topmost)
        self.proj.title("PUBG Map Projection")
        self.pc = tk.Canvas(self.proj, bg=KEYCOLOR, highlightthickness=0, bd=0)
        self.pc.pack()
        # 第一阶段落地：此时线程仍是 PMv2（进程级），投影层 HWND 在 PMv2
        # 上下文里创建——坐标是物理像素，点位才能和游戏地图精确对齐。
        self.root.update_idletasks()

        # 控制面板：可交互，普通 PMv2 窗口（v3.9 曾改成 per-window UNAWARE，
        # 结果 Tk 位图被虚拟化缩成 0.8 倍、DWM 拉伸后永远填不满窗口——右侧/
        # 底部空出一截、按钮全被裁掉，就是「内容被裁剪」的根因，已回退）。
        # 实验证实（tools/dpi_probe.py）：普通 Tk 窗口的位图恒为逻辑尺寸，
        # DWM 会把它精确拉伸到 SetWindowPos 设的物理矩形——主屏副屏都严丝
        # 合缝；跨屏 tk scaling 不变、布局不重排。物理尺寸由 _place_panel
        # 按 所在屏DPI/96 接管，观感跨屏恒定。
        self.panel = tk.Toplevel(self.root)
        self.panel.overrideredirect(True)
        self.panel.configure(bg=BG)
        self.panel.attributes("-topmost", self.topmost)
        self._clamp_pos()
        # 不调 panel.geometry：面板几何由 _place_panel 用物理坐标全权接管，
        # Tk 的延迟重断言只会跟它打架。
        self.c = tk.Canvas(self.panel, width=PW, height=S(320), bg=BG,
                           highlightthickness=0, bd=0)
        self.c.pack()
        # v3.9.28：禁止 Toplevel 跟随子组件尺寸自发变化——否则画布宽度一改，
        # Tk 的延迟几何传播就和 _place_panel 的 SetWindowPos 打架（帮助面板
        # 滑出时整窗「抽搐」的根因）。窗口尺寸从此只由 _place_panel 接管。
        self.panel.pack_propagate(False)
        if icon.exists():
            try:
                self.panel.iconbitmap(str(icon))
                self.proj.iconbitmap(str(icon))
            except Exception:
                pass
        self.map_menu = tk.Menu(self.panel, tearoff=0, bg=BAR, fg=TXT,
                                activebackground="#12333d", activeforeground=CY,
                                bd=0, font=(FONT, S(9)))
        for i, mp in enumerate(self.maps):
            self.map_menu.add_command(label=mp['name'],
                                      command=lambda i=i: self.set_map(i))
        self.root.update_idletasks()
        # winfo_id() 是 Tk 的客户区子窗口；WS_EX_TRANSPARENT / LAYERED /
        # TOPMOST 必须设在父级顶层窗口上，否则穿透不生效、会吃掉游戏点击。
        self.hwnd_panel = (user32.GetParent(self.panel.winfo_id())
                           or self.panel.winfo_id())
        self.hwnd_proj = (user32.GetParent(self.proj.winfo_id())
                          or self.proj.winfo_id())
        self._freeze_panel_size()
        self._place_panel()

    def _freeze_panel_size(self):
        """固定面板像素大小：无论放在哪块屏，都是同一组像素、永不重排。

        进程是 PerMonitorV2，面板跨屏时 Windows 会发 WM_DPICHANGED 让 Tk
        重排缩放字体（文字忽大忽小、布局错乱）。这里子类化面板窗口过程把
        这条消息吞掉——跨屏只挪位置，内容纹丝不动。面板本身按物理像素
        1:1 渲染（EXE 带 PMv2 manifest，实测 DWM 对它不做整窗拉伸），
        所以窗口尺寸 = 画布逻辑尺寸，内容天然铺满窗口。"""
        try:
            WM_DPICHANGED, GWLP_WNDPROC = 0x02E0, -4
            GetWindowLongPtrW = user32.GetWindowLongPtrW
            SetWindowLongPtrW = user32.SetWindowLongPtrW
            GetWindowLongPtrW.restype = ctypes.c_ssize_t
            GetWindowLongPtrW.argtypes = [ctypes.c_ssize_t, ctypes.c_int]
            SetWindowLongPtrW.restype = ctypes.c_ssize_t
            SetWindowLongPtrW.argtypes = [ctypes.c_ssize_t, ctypes.c_int,
                                          ctypes.c_void_p]
            old = GetWindowLongPtrW(self.hwnd_panel, GWLP_WNDPROC)
            if not old:
                return
            user32.CallWindowProcW.restype = ctypes.c_ssize_t
            user32.CallWindowProcW.argtypes = [ctypes.c_ssize_t,
                                               ctypes.c_ssize_t, ctypes.c_uint,
                                               ctypes.c_size_t,
                                               ctypes.c_ssize_t]

            @ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_ssize_t,
                                ctypes.c_uint, ctypes.c_size_t,
                                ctypes.c_ssize_t)
            def proc(hwnd, msg, wp, lp):
                if msg == WM_DPICHANGED:
                    return 0            # 吞掉：不缩放、不重排
                return user32.CallWindowProcW(old, hwnd, msg, wp, lp)

            self._panel_dpi_oldproc = old
            self._panel_dpi_proc = proc  # 保活，防止回调被回收
            SetWindowLongPtrW(self.hwnd_panel, GWLP_WNDPROC, proc)
        except Exception:
            self._panel_dpi_proc = None

    def _apply_styles(self):
        if not win32gui:
            return
        # 投影层：永远穿透（完全不挡游戏点击）
        st = win32gui.GetWindowLong(self.hwnd_proj, GWL_EXSTYLE)
        st |= WS_EX_LAYERED | WS_EX_TOOLWINDOW
        st |= WS_EX_TRANSPARENT | WS_EX_NOACTIVATE
        win32gui.SetWindowLong(self.hwnd_proj, GWL_EXSTYLE, st)
        win32gui.SetLayeredWindowAttributes(
            self.hwnd_proj, KEYREF, int(self.alpha * 255),
            LWA_COLORKEY | LWA_ALPHA)
        # 控制面板：永远可点，绝不穿透（之前被「锁定」锁死是设计错误）
        st = win32gui.GetWindowLong(self.hwnd_panel, GWL_EXSTYLE)
        st |= WS_EX_LAYERED | WS_EX_TOOLWINDOW
        st &= ~(WS_EX_TRANSPARENT | WS_EX_NOACTIVATE)
        win32gui.SetWindowLong(self.hwnd_panel, GWL_EXSTYLE, st)
        # 控制面板固定 100% 不透明：半透明会让文字发虚
        win32gui.SetLayeredWindowAttributes(self.hwnd_panel, 0, 255, LWA_ALPHA)
        win32gui.SetWindowPos(self.hwnd_panel,
                              HWND_TOPMOST if self.topmost else HWND_NOTOPMOST,
                              0, 0, 0, 0,
                              SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE)
        self._place_projection()

    def _place_projection(self):
        """Win32 显式定位投影层到 (left, top, side, side)。

        Tk 的 geometry() 在 layered + colorkey 的 overrideredirect 窗口上会
        丢掉 +X+Y 滑回 (0,0)（实测投影层因此盖住左上角控制面板），这里兜底。
        坐标与 GetSystemMetrics 同一坐标系；DPI 缩放下由系统自动换算，
        物理上仍落在 side=H_物理、left=(W_物理−H_物理)/2 的居中正方形。"""
        if not win32gui:
            return
        l, t, s, _ = self.rect
        if s <= 0:
            return
        try:
            win32gui.SetWindowPos(self.hwnd_proj,
                                  HWND_TOPMOST if self.topmost else HWND_NOTOPMOST,
                                  int(l), int(t), max(1, int(s)), max(1, int(s)),
                                  SWP_NOACTIVATE)
        except Exception as exc:
            # 失败不致命：Tk geometry 已先设过位置
            if os.environ.get("PUBGMAP_TRACE"):
                self._tr("place_projection failed: %s" % exc)

    # ------------------------------------------------------------ 投影矩形
    def _resync(self):
        """固定解析式：完整地图（按 M 打开）是屏幕正中正方形，边长 = 屏幕高度 H。
        scale=1 / offset=0 时即 left=(W−H)/2, top=0, side=H，8×8 网格每格 H/8。
        只由分辨率决定，不读游戏画面、不做识别；分辨率变化自动重算。"""
        W, H = screen_metrics()
        side = H * self.scale
        left = (W - side) / 2 + self.off_x
        top = (H - side) / 2 + self.off_y
        self.rect = (left, top, side, H)
        self._last_metrics = (W, H)
        self.proj.geometry(f"{max(1,int(side))}x{max(1,int(side))}"
                           f"+{int(left)}+{int(top)}")
        self.pc.configure(width=max(1, int(side)), height=max(1, int(side)))
        self.draw_proj()
        self._place_projection()

    def _watch_metrics(self):
        if screen_metrics() != self._last_metrics:
            self._resync()
            self.draw()
        # 面板尺寸固定为物理像素（WM_DPICHANGED 已被吞），跨屏无需重设
        self.root.after(2000, self._watch_metrics)

    # ------------------------------------------------------------ 状态
    def cur(self):
        return self.maps[self.mi]

    def _load_layers(self):
        """每张图的类别不同（维寒迪 6 类、蒂斯顿用安全门替代密室），
        所以开关是「按类别」而不是「按图」记的：
        `layer_pref` 保存所有类别的偏好，`enabled` 只是本图实际可用那几类的投影。
        这样换图时不会把另一张图才有的类别写成 0。

        默认值 = 关：首次打开（配置里还没有任何一条记录）不选中任何类别，
        一个点位都不画，让用户自己点。点过之后就被写进 [Layers]，
        以后每次启动、每次换图都按上次的选择还原。"""
        pref = {}
        if self.cfg.cp.has_section("Layers"):
            for tid, val in self.cfg.cp.items("Layers"):
                pref[tid] = val == "1"
        self.layer_pref = pref
        self.enabled = {tid for tid in self.cur()["availableTypeIds"]
                        if pref.get(tid, False)}

    def save(self):
        self._clamp_pos()              # 绝不把屏外坐标写进配置
        w = self.cfg
        w.set("Window", "selectedMap", self.cur()["slug"])
        w.set("Window", "x", self.px)
        w.set("Window", "y", self.py)
        w.set("Window", "panelScreen", self.panel_screen)
        w.set("Window", "opacity", round(self.alpha * 100))
        w.set("Window", "topmost", int(self.topmost))
        w.set("Window", "collapsed", int(self.collapsed))
        w.set("Projection", "scale", f"{self.scale:.3f}")
        w.set("Projection", "offsetX", self.off_x)
        w.set("Projection", "offsetY", self.off_y)
        w.set("Projection", "showGrid", int(self.show_grid))
        w.set("Projection", "dotSize", self.dot)
        # 只更新「本图可用」的类别，其余保留原有偏好（否则换图会互相覆盖）
        avail = set(self.cur()["availableTypeIds"])
        for t in self.data["markerTypes"]:
            tid = t["id"]
            if tid in avail:
                self.layer_pref[tid] = tid in self.enabled
            w.set("Layers", tid, int(self.layer_pref.get(tid, False)))
        w.save()

    # ------------------------------------------------------------ 开关
    def set_topmost(self, on):
        self.topmost = on
        self.panel.attributes("-topmost", on)
        self.proj.attributes("-topmost", on)
        self._apply_styles()
        self.draw()
        self.save()

    def toggle_visible(self):
        self.shown = not self.shown
        for win in (self.panel, self.proj):
            (win.deiconify() if self.shown else win.withdraw())
        if self.shown:
            self._resync()
            self._apply_styles()
        self.save()

    def show(self):
        if not self.shown:
            self.toggle_visible()

    def set_opacity(self, pct):
        self.alpha = min(1.0, max(0.35, pct / 100.0))
        self._apply_styles()
        self.draw()

    def set_map(self, idx):
        if 0 <= idx < len(self.maps):
            self.mi = idx
            # 换图后图例跟着换：只保留这张图确实存在的类别，按已保存的类别偏好
            # 还原开关（没记录过的默认关）。网站也是按 map_type_id 重新查关联表再渲染图例。
            self.enabled = {tid for tid in self.cur()["availableTypeIds"]
                            if self.layer_pref.get(tid, False)}
            self.grid = None
            self.grid_key = None
            self.grid_ts = 0.0
            self.measure = [None] * self.slots    # 换图 → 旧的测距结果作废
            self.draw()
            self.draw_proj()
            self.save()

    def all_types(self, on):
        ids = set(self.cur()["availableTypeIds"])
        self.enabled = (self.enabled | ids) if on else (self.enabled - ids)
        self.draw()
        self.draw_proj()
        self.save()

    def toggle_collapse(self):
        self.collapsed = not self.collapsed
        if self.collapsed:
            self.help_open = False        # 折叠时帮助面板一并收起
            self._help_w = PW
            self._win_w = PW              # 窗口宽度同步收回，防滑出宽度残留
        self.draw()
        self.save()

    def adjust(self, scale=None, dx=0, dy=0):
        if scale is not None:
            self.scale = min(1.4, max(0.6, round(scale, 3)))
        self.off_x += dx
        self.off_y += dy
        self._resync()
        self.draw()
        self.save()

    # ------------------------------------------------------------ 绘制原语
    def reg(self, x1, y1, x2, y2, act, val=None):
        self.regions.append((x1, y1, x2, y2, act, val))

    def txt(self, x, y, s, fill=DIM, size=9, font=FONT, anchor="w", bold=False):
        self.c.create_text(x, y, text=s, fill=fill,
                           font=(font, S(size), "bold" if bold else "normal"),
                           anchor=anchor)

    def stroke_icon(self, kind, cx, cy, color):
        if kind == "minus":
            self.c.create_line(cx - S(5), cy, cx + S(5), cy, fill=color)
        elif kind == "cursor":
            self.c.create_polygon(cx - S(5), cy - S(7), cx - S(5), cy + S(6),
                                  cx - S(1), cy + S(2), cx + S(2), cy + S(7),
                                  cx + S(4), cy + S(5), cx + S(1), cy + S(1),
                                  cx + S(5), cy + S(1),
                                  fill="", outline=color)
        elif kind == "close":
            self.c.create_line(cx - S(4), cy - S(4), cx + S(4), cy + S(4),
                               fill=color)
            self.c.create_line(cx + S(4), cy - S(4), cx - S(4), cy + S(4),
                               fill=color)

    def icon_btn(self, x1, y1, x2, y2, kind, act, on=False):
        """小型图形按钮；每个按钮都靠悬停提示与短标签说明，不在 UI 堆说明文字。"""
        self.c.create_rectangle(x1, y1, x2, y2,
                                fill="#12333d" if on else CHIP,
                                outline=CY if on else LINE)
        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
        col = CY if on else TXT
        if kind == "grid":
            for k in (-S(5), 0, S(5)):
                self.c.create_line(cx + k, cy - S(6), cx + k, cy + S(6),
                                   fill=col)
                self.c.create_line(cx - S(6), cy + k, cx + S(6), cy + k,
                                   fill=col)
        elif kind == "help":
            # 圆角徽章：暗青底 + 青色「?」，与面板 chip 风格一致（悬停反馈
            # 走 _panel_hover 的亮度变化，不做额外高亮层）
            self.c.create_image(x1, y1, anchor="nw",
                                image=self.art.pill(x2 - x1, y2 - y1,
                                                    "#12333d", LINE, 1,
                                                    radius=S(6)))
            self.txt(cx, cy, "?", CY, 11, MONO, "center", bold=True)
        self.reg(x1, y1, x2, y2, act)

    # ------------------------------------------------------------ 投影层绘制
    PIN_PHYS = 32                          # 网页 Leaflet iconSize 就是 32×32

    def pin_size(self):
        return max(14, int(round(self.PIN_PHYS / dpi_scale())))

    def draw_proj(self):
        """只画点位图标，不画底图 —— 游戏自己的 M 地图就在下面。

        图标来源与网页完全一致：marker_types.icon_url 的官方 SVG，尺寸 32×32、
        锚点在底部中心（水滴尖端 = 坐标点），和 Leaflet iconAnchor:[16,32] 一致。
        """
        self.pc.delete("all")
        _l, _t, side, _H = self.rect
        if side <= 0:
            return
        pin = self.pin_size()
        if self.show_grid:                     # 8x8 网格辅助对齐（旧 EXE 基准图的网格）
            for k in range(9):
                p = side * k / 8
                self.pc.create_line(p, 0, p, side, fill="#18333c", width=1)
                self.pc.create_line(0, p, side, p, fill="#18333c", width=1)
        self.proj_hits = []
        for p in self.cur()["markers"]:
            if p["typeId"] not in self.enabled:
                continue
            t = self.types[p["typeId"]]
            x = (p["lng"] + 180) / 360 * side
            y = (p["lat"] + 90) / 180 * side
            self.pc.create_image(int(round(x - pin / 2)), int(round(y - pin)),
                                 anchor="nw",
                                 image=self.art.pin(resource(t["icon"]), pin))
            self.proj_hits.append((x, y, p, t))

    # ------------------------------------------------------------ 读轨迹
    def _proj_alpha(self, a):
        """投影层整体透明度（0 = 完全不可见）。

        抓屏期间临时归零，让我们的点位图标 / 轨迹线不进入截图 —— 见
        `_grab_map`：这是「只认游戏那一层」的实现手段。
        """
        if not win32gui:
            return
        try:
            win32gui.SetLayeredWindowAttributes(
                self.hwnd_proj, KEYREF,
                int(max(0.0, min(1.0, float(a))) * 255),
                LWA_COLORKEY | LWA_ALPHA)
        except Exception:
            pass

    def _grab_map(self):
        """抓投影层所在区域（只读屏幕像素）。

        排除自身图层的方式：投影层是**独立的分层窗口**（WS_EX_LAYERED +
        colorkey + alpha），DWM 合成时才和游戏画面叠在一起。抓屏拿到的是
        合成结果，所以只要在抓的那一瞬间把自己的 alpha 设成 0，截图里就
        只有游戏，没有任何我们画的点位图标 / 轨迹线 —— 比 SW_HIDE 更快、
        不闪、不触发重排，也不影响游戏本身（游戏窗口我们全程不碰）。

        截图范围只有「屏幕中央、边长 = 屏高×scale」的地图方块，不多抓。
        抓屏通道两级（见 `_grab_map_core`）：DXGI 桌面复制优先（HDR 屏拿
        scRGB 原始帧，颜色正确，不做任何拉伸）；失败退回 GDI ImageGrab
        （HDR 屏会被 DWM 色调映射压爆，只能事后 autocontrast 找回对比度，
        且削顶信息已不可恢复）。"""
        try:
            _l, _t, s, _h = self.rect
            s = max(64, int(s))
            self._proj_alpha(0.0)
            try:
                time.sleep(0.035)
                im, hdr = self._grab_map_core()
            finally:
                self._proj_alpha(self.alpha)
            if im is None:
                return None
            im = im.convert("RGB").resize((s, s), Image.LANCZOS)
            # GDI 兜底在 HDR 屏上整体过曝（老版本实测 7.5% 像素削顶到 255），
            # 直方图拉伸把对比度找回；DXGI 帧颜色本来正确，拉伸反而破坏色相。
            if not hdr:
                im = ImageOps.autocontrast(im, cutoff=(1.0, 0.6))
            return im
        except Exception:
            return None

    def _map_box_phys(self):
        """地图方块的物理像素框。坐标公式只依赖「屏幕中央 + 边长比例 +
        手动偏移」，与抓屏 DPI 无关（EXE 是 PerMonitorV2，本就是物理像素）。"""
        W, H = screen_metrics()
        side_px = H * self.scale
        left_px = (W - side_px) / 2 + self.off_x
        top_px = (H - side_px) / 2 + self.off_y
        return (max(0, int(left_px)), max(0, int(top_px)),
                min(W, int(left_px + side_px)),
                min(H, int(top_px + side_px)))

    def _grab_map_core(self):
        """抓地图方块，返回 (PIL Image, 颜色是否已正确)。

        ① DXGI 桌面复制（src/dxgigrab.py）：HDR 屏拿 FP16 scRGB 原始帧，
           色调映射后颜色正确；SDR 屏拿 8bit 帧同样直接用。只转换地图方块。
        ② GDI ImageGrab 兜底：EXE 为 DPI aware，抓屏即物理像素；但稳妥起见
           第一次仍抓全屏做「图片↔物理」比例标定，之后按框直接 bbox 抓取
           （比例异常 = 换屏/改缩放，自动重标定一次）。"""
        W, H = screen_metrics()
        pbox = self._map_box_phys()
        dbg = os.environ.get("PUBGMAP_SCAN_FRAME")
        if dbg and os.path.exists(dbg):
            return Image.open(dbg).convert("RGB"), True
        try:
            import dxgigrab
            img = dxgigrab.grab_full(crop=pbox)
            if img is not None:
                self._last_grab = (img.width, img.height,
                                   (0, 0, img.width, img.height))
                return img, True
        except Exception:
            pass
        r = getattr(self, "_gdi_ratio", None)
        if r is None:
            full = ImageGrab.grab(all_screens=False)
            self._gdi_ratio = r = full.width / max(1.0, float(W))
            box = (int(pbox[0] * r), int(pbox[1] * r),
                   int(pbox[2] * r), int(pbox[3] * r))
            self._last_grab = (full.width, full.height, box)
            return full.crop(box), False
        bbox = tuple(int(round(v * r)) for v in pbox)
        im = ImageGrab.grab(bbox=bbox, all_screens=False)
        if abs(im.width - (pbox[2] - pbox[0]) * r) > 8 \
                or abs(im.height - (pbox[3] - pbox[1]) * r) > 8:
            self._gdi_ratio = None          # 分辨率/缩放变了 → 重标定一次
            full = ImageGrab.grab(all_screens=False)
            self._gdi_ratio = r = full.width / max(1.0, float(W))
            box = tuple(int(round(v * r)) for v in pbox)
            self._last_grab = (full.width, full.height, box)
            return full.crop(box), False
        self._last_grab = (int(W * r), int(H * r), bbox)
        return im, False

    def _ensure_grid(self, force=False, im=None):
        """识别当前画面的网格比例尺。

        缓存键只包含「本程序的投影矩形」；但玩家滚轮缩放 / 平移**游戏地图**时
        这些都不变，所以再加一个有效期，且扫描轨迹时用同一帧强制重测一次。
        """
        side = max(1.0, self.rect[2])
        key = (self.mi, self._last_metrics, round(side, 1),
               round(self.scale, 3), self.off_x, self.off_y)
        fresh = (time.time() - getattr(self, "_grid_ts", 0.0)) < GRID_TTL
        if (not force and self.grid is not None
                and self.grid_key == key and fresh):
            return self.grid
        im = im if im is not None else self._grab_map()
        km = float(self.cur().get("kmSize", 8))
        if im is None:
            self.grid = dict(mpp=km * 1000.0 / side, source="估算",
                             major=0, minor=0, conf=0.0, km=km, zoom=1.0)
        else:
            self.grid = GridProbe.probe(im, km, side)
        self.grid_key = key
        self.grid_ts = time.time()
        if os.environ.get("PUBGMAP_PROBE"):
            try:
                with open(user_config_dir() / "grid.log", "a", encoding="utf-8") as f:
                    f.write(json.dumps({"grab": self._last_grab, "side": side,
                                        "km": km, **self.grid},
                                       ensure_ascii=False) + "\n")
            except Exception:
                pass
        return self.grid

    @staticmethod
    def _straight(pts, ratio=0.012, floor=2.5):
        """链上的标号点是否共线 —— 共线才算一条直线轨迹。

        中间任一标号点偏离首尾连线超过 max(2.5px, 弦长 1.2%) 就判成折线，
        折线按规则不量距离。两个点永远共线。
        """
        if len(pts) < 2:
            return False
        if len(pts) == 2:
            return True
        ax, ay = pts[0]
        bx, by = pts[-1]
        dx, dy = bx - ax, by - ay
        chord = math.hypot(dx, dy)
        if chord < 1e-6:
            return False
        for px, py in pts[1:-1]:
            if abs(dy * (px - ax) - dx * (py - ay)) / chord > max(floor, chord * ratio):
                return False
        return True

    def _cb_type(self):
        """读游戏配置的色盲模式（TslPersistantData 里的 ColorBlindType），缓存一次。

        0 = Normal，标记色就是 GetTeamColor 的 case0 真值；非 0 时标记会变
        成粉/橙等变体，本程序的按色识别只覆盖 Normal，其余退回黄色通认。"""
        v = getattr(self, "_cb_cache", None)
        if v is None:
            v = 0
            try:
                p = Path(os.environ.get("LOCALAPPDATA", "")) / \
                    "TslGame/Saved/Config/WindowsNoEditor/GameUserSettings.ini"
                mm = re.search(r"ColorBlindType=(\d)", p.read_text(
                    encoding="utf-8", errors="ignore"))
                if mm:
                    v = int(mm.group(1))
            except Exception:
                pass
            v = min(3, max(0, v))
            self._cb_cache = v
        return v

    def _archive_scan(self, im, grid, found):
        """扫描留帧归档 + 扫描日志（v3.9.22，用户要求）。

        · last_scan.png 照旧写（「最新一帧」语义，复现工具 PUBGMAP_SCAN_FRAME
          兼容不变）；
        · frames/scan-时间戳.png 额外存一份历史帧，只保留最近 20 份；
        · scan.log 每次扫描追加一行 JSON（时间/帧文件名/网格/各队结果），
          全量保留不清理——排查「为什么识别不了」时直接按时间对帧。
        """
        d = user_config_dir()
        name = ""
        try:
            fr = d / "frames"
            fr.mkdir(exist_ok=True)
            name = f"scan-{time.strftime('%Y%m%d-%H%M%S')}.png"
            im.save(fr / name)
            try:                            # 「最新一帧」语义保持不变
                im.save(d / "last_scan.png")
            except OSError:
                pass
            olds = sorted(fr.glob("scan-*.png"))
            for p in olds[:-20]:                # 只留最近 20 帧
                try:
                    p.unlink()
                except OSError:
                    pass
        except Exception:
            pass
        try:
            teams = []
            for ti, f in enumerate(found if found else []):
                if f:
                    teams.append({"team": ti, "m": round(f.get("m") or 0, 1),
                                  "npts": len(f.get("pts") or []),
                                  "a": f.get("a"), "b": f.get("b")})
            entry = {
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "frame": name or None,
                "grid": {k: grid.get(k) for k in
                         ("mpp", "source", "conf", "major", "minor", "zoom")},
                "teams": teams,
            }
            with open(d / "scan.log", "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def rescan(self):
        """手动「识别」：**异步**抓一帧整块地图 → 逐项识别 → 整组替换槽位。

        计算期间按钮转「计算中…」动效（见 _pump），完成后结果直接上屏；
        一条轨迹都没认到时按钮短暂显示「无轨迹」，不再是无声无息。
        只有点按钮时才跑，不做任何轮询：
          · 抓到的那一帧会留一份（内存 + 配置目录下的 `last_scan.png`），
            所以**关掉地图之后结果照样在**，不用把地图一直开着；
          · 认到几项就填几项，没认到的项一律写空（`—`），绝不沿用上一轮的旧值；
          · 比例尺也在这同一帧上重测一次，滚轮缩放过也不用管。
        """
        if getattr(self, "_scanning", False):
            return False                    # 正在算，忽略重复点击
        self._scanning = True
        self._scan_anim = 0
        self.draw()
        self.root.update_idletasks()        # 先把「计算中」画出来再进重活

        def work():
            found = None                    # None = 抓屏失败
            try:
                ctypes.windll.ole32.CoInitializeEx(None, 0x0)
            except Exception:
                pass
            try:
                im = self._grab_map()       # 自己的点位 alpha 归零，只有游戏
                if im is not None:
                    self._last_frame = im
                    self._scan_ts = time.time()
                    grid = self._ensure_grid(force=True, im=im)
                    if grid.get("conf", 0.0) < 0.12 or grid.get("source") == "估算":
                        # 没量到网格 = 不是地图画面（大厅/结算/观战）或缩放极端
                        found = [None] * self.slots
                    else:
                        found = self._detect_measure(im, grid["mpp"])
                    self._archive_scan(im, grid, found)
            finally:
                try:
                    ctypes.windll.ole32.CoUninitialize()
                except Exception:
                    pass
            self.q.put(("scan_done", found))

        threading.Thread(target=work, daemon=True).start()
        return True

    def _detect_measure(self, im, mpp):
        """在已抓好的整块地图上做轨迹识别，返回槽位结果列表（工作线程调用）。"""
        found = [None] * self.slots
        if self._cb_type() == 0:
            # 色盲模式 = Normal：标记色 = GetTeamColor(队伍序号) 逆向真值，
            # 按颜色各认各的，槽位 i 直接对应 i 号队友（不再是按位置排序）。
            # 只认「有连线」的轨迹；连线是纯色实线，首尾距离就是测距值，
            # 折线照样量（用户口径：有连线才算轨迹）。
            teams = TrackProbe.detect_teams(im, self.slots)
            for ti, pts in teams.items():
                if ti >= self.slots:
                    continue
                m = math.hypot(pts[-1][0] - pts[0][0],
                               pts[-1][1] - pts[0][1]) * mpp
                found[ti] = {"m": m, "a": pts[0], "b": pts[-1], "pts": pts,
                             "straight": self._straight(pts)}
        else:
            # 色盲模式 ≠ Normal：标记色变体没逆向，退回黄色通认 + 位置排序
            for i, pts in enumerate(TrackProbe.detect(im, self.slots)):
                if i >= self.slots:
                    break
                straight = self._straight(pts)
                m = None
                if straight and len(pts) >= 2:
                    m = math.hypot(pts[-1][0] - pts[0][0],
                                   pts[-1][1] - pts[0][1]) * mpp
                found[i] = {"m": m, "a": pts[0], "b": pts[-1], "pts": pts,
                            "straight": straight}
        return found

    # ------------------------------------------------------------ 控制面板
    def draw(self):
        if os.environ.get("PUBGMAP_TRACE"):
            try:
                sc = self.panel.tk.call("tk", "scaling")
                wd = user32.GetDpiForWindow(self.hwnd_panel)
                self._tr("draw scaling=%.4f winDpi=%s px=%s py=%s wx=%s wy=%s"
                         % (sc, wd, self.px, self.py,
                            self.panel.winfo_x(), self.panel.winfo_y()))
            except Exception:
                pass
        """面板只显示「选了什么」：地图名 + 类别图例 + 点数。
        图例的类别集合与顺序取自 map_type_marker_types，与网页完全一致；
        对齐参数、微调按钮全部隐藏 —— 默认就按 side=H 的全图最大矩形计算。"""
        self.c.delete("all")
        self.regions = []
        self._ui_w = w = int(getattr(self, "_help_w", PW))
        self.c.configure(width=w)
        if self.collapsed:
            self._draw_chip()
            return
        m = self.cur()
        # 类别集合与顺序完全照抄网站：map_type_marker_types 声明的顺序（按 sort_order）
        present = [self.types[i] for i in m["availableTypeIds"] if i in self.types]
        rows = max(1, math.ceil(len(present) / CAP_COLS))
        meas_y = LAYER_Y + rows * CAP_ROW
        H = meas_y + MEAS_TITLE_H + self.slots * M_ROW_H + FOOT_H
        self._ui_h = H
        self.c.configure(height=H)
        self._clamp_pos()
        # 窗口尺寸/位置全部由 _place_panel 用物理坐标接管；Tk geometry 的
        # 尺寸变更会延迟重断言内部坐标、覆盖外部定位，这里绝不再调它。
        self._place_panel()
        self.root.after_idle(self._place_panel)   # 赢过 Tk 的延迟几何处理

        # 标题栏：只有地图名和一个描边图标（隐藏）；折叠按钮已按需求删除
        self.c.create_rectangle(0, 0, PW, TITLE_H, fill=BAR, outline="")
        self.c.create_line(0, TITLE_H, PW, TITLE_H, fill=LINE)
        self.c.create_image(PAD, TITLE_H / 2 - S(4), anchor="nw",
                            image=self.art.dot(S(8), CY))
        self.txt(PAD + S(16), TITLE_H / 2, f"{m['name']}  ▾", TXT, 11)
        self.reg(PAD - S(4), S(4), PAD + S(96), TITLE_H - S(4), "mapmenu")
        cx = PW - S(30)   # 保持原「隐藏」按钮位置不动
        self.c.create_rectangle(cx - S(11), S(8), cx + S(11), S(32),
                                fill=CHIP, outline=LINE)
        self.stroke_icon("close", cx, TITLE_H / 2, DIM)
        self.reg(cx - S(11), S(8), cx + S(11), S(32), "close")

        # 类别图例：照抄网页 —— 圆形图标底（40px）+ 内部 24px 官方图标 + 下方名称，
        # 选中态用琥珀色描边与文字（网页是 #EAB308 边 / #FACC15 字），未选中为暗灰。
        for i, t in enumerate(present):
            col = i % CAP_COLS
            x1 = PAD + col * (CAP_W + S(8))
            y1 = LAYER_Y + (i // CAP_COLS) * CAP_ROW
            cxc = x1 + CAP_W / 2
            on = t["id"] in self.enabled
            self.c.create_image(cxc - S(20), y1, anchor="nw",
                                image=self.art.dot(S(40), "#3a3418" if on else "#1e293b",
                                                   "#eab308" if on else None, 2))
            self.c.create_image(cxc - S(12), y1 + S(8), anchor="nw",
                                image=self.art.pin(resource(t["icon"]), S(24),
                                                   dim=not on, keyed=False))
            self.txt(cxc, y1 + S(56), t["name"], "#facc15" if on else "#94a3b8", 10,
                     anchor="center")
            self.reg(x1, y1, x1 + CAP_W, y1 + CAP_H, "type", t["id"])

        # 测距区：四名队友按一号~四号排列，颜色 + 名称区分（不是纯色块）
        self.c.create_line(0, meas_y, PW, meas_y, fill=LINE)
        src = (self.grid or {}).get("source", "—")
        zoom = (self.grid or {}).get("zoom", 1.0)
        # 识别按钮：写「测距」两个字的文字按钮，带悬停/按下反馈（像真的按钮，
        # 靠亮暗变化表达，不用悬浮气泡）。点一下抓整块地图做一次识别。
        bx2, by1, by2 = PW - PAD, meas_y + S(2), meas_y + S(24)
        bx1 = bx2 - S(48)
        self._scan_rect = (bx1, by1, bx2, by2)
        self._scan_imgs = {
            "n": self.art.pill(S(48), S(22), "#12333d", CY, 1, radius=S(4)),
            "h": self.art.pill(S(48), S(22), "#17505f", CY, 1, radius=S(4)),
            "p": self.art.pill(S(48), S(22), "#0a2a33", CY, 1, radius=S(4)),
        }
        self._scan_state = "n"
        self._scan_pill = self.c.create_image(bx1, by1, anchor="nw",
                                              image=self._scan_imgs["n"])
        # 按钮文字三态：测距（常态）/ 计算中…（异步识别期间，带省略号动效）/
        # 无轨迹（识别完成但一条连线轨迹都没认到，短暂显示后复原）
        stxt, scol = "测量", CY
        if getattr(self, "_scanning", False):
            stxt = "计算中" + "." * (1 + (getattr(self, "_scan_anim", 0) // 8) % 3)
        elif time.time() - getattr(self, "_scan_msg_ts", 0.0) < 3.5:
            stxt, scol = "无轨迹", GOLD
        self._scan_txt = self.c.create_text((bx1 + bx2) / 2, by1 + S(11),
                                            text=stxt, fill=scol,
                                            font=(FONT, S(8), "bold"),
                                            anchor="center")
        self.reg(bx1, by1, bx2, by2, "scan")
        # 缩放倍数：滚轮放大后这里会跟着变，一眼看出比例尺在跟着走；
        # 满图（min zoom）时格子偏小属正常，就不显示，免得看着像出错
        if self.grid and src not in ("估算", "—") and zoom > 1.05:
            self.txt(bx1 - S(20), meas_y + S(13), f"×{zoom:.1f}", CY, 9, MONO, "e")

        # 四条槽位：编号块（游戏原色 + 白粗数字）+ 距离条 + 大号数字，不写名称。
        # 折线不量距离：那行只画一个折线小图形，数字位留空。
        vals = [r["m"] for r in self.measure if r and r.get("m")]
        vmax = max(vals) if vals else 0.0
        bx0, bx1 = PAD + S(26), PW - PAD - S(62)
        for i in range(self.slots):
            ry = meas_y + MEAS_TITLE_H + i * M_ROW_H
            col = self.squad[i] if i < len(self.squad) else M_COLORS[i]
            r = self.measure[i]
            # 编号块：照 PUBG 左下角队伍列表的样式（游戏原色底 + 白色粗数字）
            self.c.create_image(PAD, ry + (M_ROW_H - S(17)) / 2, anchor="nw",
                                image=self.art.pill(S(17), S(17), col, None, 0,
                                                    radius=S(1)))
            self.txt(PAD + S(8.5), ry + M_ROW_H / 2 + 0.5, str(i + 1), "#FFFFFF",
                     8, MONO, "center", bold=True)
            my = ry + M_ROW_H / 2 - 2
            self.c.create_image(bx0, my, anchor="nw",
                                image=self.art.pill(bx1 - bx0, S(5), "#122630",
                                                    None, 0, radius=S(2)))
            if r and r.get("m") and vmax > 0:
                bw = max(S(7), (bx1 - bx0) * min(1.0, r["m"] / vmax))
                self.c.create_image(bx0, my, anchor="nw",
                                    image=self.art.pill(int(bw), S(5), col,
                                                        None, 0, radius=S(2)))
                self.txt(PW - PAD - S(10), ry + M_ROW_H / 2, f"{int(round(r['m'])):,}",
                         TXT, 12, MONO, "e", bold=True)
                self.txt(PW - PAD, ry + M_ROW_H / 2 + 1, "m", DIM2, 8, MONO, "e")
            elif r:
                # 折线：只放一个折线图形，代表「这条不量」
                zx, zy = PW - PAD - S(22), ry + M_ROW_H / 2
                self.c.create_line(zx, zy + S(4), zx + S(5.5), zy - S(4),
                                   zx + S(11), zy + S(4), zx + S(16.5), zy - S(4),
                                   fill=GOLD, width=1)
            else:
                self.txt(PW - PAD, ry + M_ROW_H / 2, "—", DIM2, 11, MONO, "e")
            self.c.create_line(PAD, ry + M_ROW_H, PW - PAD, ry + M_ROW_H,
                               fill="#16262e")
            self.reg(0, ry, PW, ry + M_ROW_H, "mclear", i)

        # 状态栏：只有一个「?」按钮（不做任何点数统计）
        fy = H - FOOT_H
        self.c.create_rectangle(0, fy, PW, H, fill="#080f13", outline="")
        self.c.create_line(0, fy, PW, fy, fill=LINE)
        # 版本 + 安全声明：主面板左下角（v3.9.29 从帮助面板移过来）
        self.c.create_text(S(10), fy + FOOT_H / 2 + S(1),
                           text=f"v{APP_VER} · 不读内存 · 不注入",
                           fill=DIM2, font=(MONO, 8), anchor="w")
        self.icon_btn(PW - PAD - S(30), fy + S(5), PW - PAD, fy + S(29),
                      "help", "help")
        # 帮助扩展面板：画在主框右侧、无缝相连（点「?」滑出/收起）
        if self.help_open:
            self._draw_help_panel(H)

    def _draw_help_panel(self, H):
        """帮助内容区：主面板右侧 HELP_W 宽的连体面板（v3.9.28）。"""
        c, x0 = self.c, PW
        W2 = PW + HELP_W
        c.create_rectangle(x0, 0, W2, H, fill=BG, outline="")
        c.create_line(x0, 0, x0, H, fill=LINE)
        # 标题 + 关闭
        c.create_text(x0 + S(16), S(22), text="帮助", fill=TXT,
                      font=(FONT, 12), anchor="w")
        bx = W2 - S(28)
        c.create_line(bx - S(6), S(16), bx + S(6), S(28), fill=DIM, tags="helpx")
        c.create_line(bx + S(6), S(16), bx - S(6), S(28), fill=DIM, tags="helpx")
        self.reg(bx - S(12), S(10), bx + S(12), S(34), "helpx")
        # 三步骤（用户口径文案，v3.9.28）——纵向压缩，保证热键框+版本行
        # 在面板高度（4 槽位 H≈420px）内放得下
        steps = (
            ("按 M 打开游戏地图", "缩放平移会自动适配"),
            ("在地图上用 Alt+右键 画轨迹", "允许弯折"),
            ("点面板「测量」按钮", "量出折线起点到终点的直线距离"),
        )
        y = S(48)
        for i, (t1, t2) in enumerate(steps):
            c.create_image(x0 + S(16), y, anchor="nw",
                           image=self.art.dot(S(28), "#12333d", CY, 1))
            c.create_text(x0 + S(30), y + S(14), text=str(i + 1), fill=CY,
                          font=(MONO, 11, "bold"))
            c.create_text(x0 + S(56), y + S(9), text=t1, fill=TXT,
                          font=(FONT, 11), anchor="w")
            if t2:
                c.create_text(x0 + S(56), y + S(28), text=t2, fill=DIM,
                              font=(FONT, 9), anchor="w")
            y += S(52)
        # 全局热键：分组标题 + 功能行。行规格与主面板按钮一致，标题与行
        # 紧凑排布（间距 S(18)），行内左右文字同字号、按钮文字精确居中。
        c.create_line(x0, y + S(6), W2, y + S(6), fill=LINE)
        ty = y + S(26)                            # 「全局热键」标题中心
        c.create_text(x0 + S(16), ty, text="全局热键", fill=TXT,
                      font=(FONT, 12), anchor="w")
        ry = ty + S(18)                           # 功能行顶
        bh = S(24)
        c.create_text(x0 + S(16), ry + bh / 2, text="显示 / 隐藏面板",
                      fill=DIM, font=(FONT, 9), anchor="w")
        bw = S(84)
        bx0 = W2 - S(16) - bw
        c.create_image(bx0, ry, anchor="nw",
                       image=self.art.pill(bw, bh, "#12333d", CY, 1,
                                           radius=S(4)))
        # 框内文字：捕获态（实时预览/等待按键）/ 不合法按键提示 / 当前热键
        now = time.time()
        if getattr(self, "_hk_listening", False):
            t, col = getattr(self, "_hk_live", "") or "按下新组合键", GOLD
        elif now - getattr(self, "_hk_hint", 0) < 2.0:
            t, col = "需带 Alt 或 Ctrl", GOLD
        else:
            t, col = hotkey_disp(self.hk_key), CY
        c.create_text(bx0 + bw / 2, ry + bh / 2, text=t,
                      fill=col, font=(FONT, 9, "bold"), anchor="center")
        self.reg(bx0, ry, bx0 + bw, ry + bh, "hkbox")

    def toggle_help(self):
        """点「?」：帮助面板从主框右侧滑出 / 滑回（宽度动画）。

        v3.9.28 抽搐修复：内容只在动画前画一次（画布直接铺到目标全宽，
        窗口窄时右侧内容被自然裁掉），动画期间**只动原生窗口宽度**
        （SetWindowPos），不再每帧 delete("all") 重画 + 改画布宽度——
        那会触发 Tk 几何传播与 SetWindowPos 打架，整窗来回拽。"""
        if getattr(self, "_help_animating", False):
            return
        self._help_animating = True
        opening = not self.help_open
        w_final = PW + HELP_W if opening else PW
        if opening:
            self.help_open = True
            self._help_w = w_final          # 内容一次画到全宽
            self._win_w = PW                # 窗口仍保持主宽，draw() 别把它撑开
            self.draw()
        cur = int(getattr(self, "_win_w", PW))
        step = max(S(28), abs(w_final - cur) // 4)

        def tick():
            nonlocal cur
            if cur < w_final:
                cur = min(cur + step, w_final)
            elif cur > w_final:
                cur = max(cur - step, w_final)
            self._win_w = cur
            self._place_panel()
            if cur != w_final:
                self.root.after(16, tick)
                return
            # 动画结束：收起方向此时才撤内容（此刻窗口已经缩回主宽）
            if not opening:
                self.help_open = False
                self._help_w = PW
                self.draw()
            self._win_w = w_final
            self._help_animating = False

        tick()

    def _start_hk_capture(self):
        """点击热键框：进入「按下新组合键」捕获态（再点一次或 Esc 取消）。

        修饰键限定 Alt / Ctrl（左右边触发时区分），主键任意——这就是
        「热键由用户自定义」：用户想用什么键就按什么键。

        v3.9.31 修复「按什么都没反应」：不再依赖 Tk 焦点——overrideredirect
        的分层透明窗口 focus_force 在 Windows 上拿不到键盘焦点，bind_all
        永远收不到 KeyPress。改用 GetAsyncKeyState 轮询 + 按下沿检测，
        与焦点无关，全局任意位置按键都能录到。捕获期间先注销旧热键，
        防止按到旧组合把面板切走、打断录制。"""
        if getattr(self, "_hk_listening", False):
            self._stop_hk_capture()
            return
        self._hk_listening = True
        self._hk_prev = None                  # 首轮只记录基线，不触发
        self._hk_live = ""
        try:
            self.native.suspend_hotkey()
        except Exception:
            pass
        self._hk_poll()
        self.draw()

    def _stop_hk_capture(self):
        """退出捕获态：恢复旧热键注册。"""
        self._hk_listening = False
        self._hk_hint = 0.0
        self._hk_prev = None
        self._hk_live = ""
        try:
            self.native.set_hotkey(self.hk_key)   # WM_REBIND → _register_hk
        except Exception:
            pass
        self.draw()

    def _hk_poll(self):
        """捕获态 25ms 轮询：主键「按下沿」+ Alt/Ctrl 修饰状态 → 生成 spec。

        依赖自维护的上一轮状态表（GetAsyncKeyState 低位「自上次调用以来
        按下」会被其他进程干扰，不可靠）。修饰键按住 + 主键新按下即完成
        录制；单独按 Esc（无修饰）取消；裸按主键提示「需带 Alt 或 Ctrl」。"""
        if not getattr(self, "_hk_listening", False):
            return
        ga = user32.GetAsyncKeyState
        alt = bool(ga(0xA4) & 0x8000) or bool(ga(0xA5) & 0x8000)
        ctrl = bool(ga(0xA2) & 0x8000) or bool(ga(0xA3) & 0x8000)
        esc = bool(ga(0x1B) & 0x8000)
        main = {vk: bool(ga(vk) & 0x8000) for vk in _HK_POLL_MAIN}
        if self._hk_prev is None:
            self._hk_prev = (alt, ctrl, esc, main)
            return self.root.after(25, self._hk_poll)
        p_alt, p_ctrl, p_esc, p_main = self._hk_prev
        self._hk_prev = (alt, ctrl, esc, main)
        # 左右边判定一次，实时预览与提交共用（GetAsyncKeyState 直查物理边）
        side_alt = "RAlt" if (ga(0xA5) & 0x8000 and not ga(0xA4) & 0x8000) else "LAlt"
        side_ctrl = "RCtrl" if (ga(0xA3) & 0x8000 and not ga(0xA2) & 0x8000) else "LCtrl"
        # 实时预览：按住修饰键逐个显示（Ctrl 在前），松开清空回「按下新组合键」。
        # 两个家族同时按住时框内放不下左右全称，缩写不带边（提交仍按 Alt 优先）。
        labels = ([_HOTKEY_TEXT[side_ctrl]] if ctrl
                  else []) + ([_HOTKEY_TEXT[side_alt]] if alt else [])
        live = " + ".join(labels) if len(labels) < 2 else "Ctrl + Alt"
        if live != getattr(self, "_hk_live", ""):
            self._hk_live = live
            self.draw()
        if esc and not p_esc and not (alt or ctrl):
            self._stop_hk_capture()           # Esc = 取消
            return
        hit = next((vk for vk, d in main.items() if d and not p_main[vk]), None)
        if hit is None:
            return self.root.after(25, self._hk_poll)
        if not (alt or ctrl):
            self._hk_hint = time.time()       # 提示「需带 Alt 或 Ctrl」，2 秒
            self.draw()
            return self.root.after(25, self._hk_poll)
        tok = _HK_POLL_MAIN[hit]
        self.hk_key = f"{side_alt if alt else side_ctrl}+{tok}"
        self.cfg.set("Hotkeys", "toggle", self.hk_key)
        self.cfg.set("Hotkeys", "hkVer", "3")  # 标记用户自定义过，迁移不再覆盖
        self.save()
        self._hk_listening = False
        self._hk_hint = 0.0
        self._hk_prev = None
        self._hk_live = ""
        self.native.set_hotkey(self.hk_key)
        self.draw()

    def _draw_chip(self):
        self.c.configure(height=CHIP_SIZE)
        self._ui_h = CHIP_SIZE
        self._clamp_pos()
        self._place_panel()
        self.root.after_idle(self._place_panel)
        self.c.create_image(1, 1, anchor="nw",
                            image=self.art.dot(CHIP_SIZE - 2, BAR, CY, 2))
        self.c.create_line(S(10), S(18), S(26), S(18), fill=CY)
        self.c.create_line(S(18), S(10), S(18), S(26), fill=CY)

    # ------------------------------------------------------------ 交互
    def _bind_events(self):
        self.c.bind("<Button-1>", self._click)
        # v4.0.0：右键隐藏已移除——用户右键面板被意外隐藏、不符合预期。
        # 隐藏入口只剩显隐热键与右上角 ×。
        self.c.bind("<Motion>", self._panel_hover)
        # 右键测距不吃窗口事件：colorkey 透明区不给命中测试，统一由
        self.root.bind("<Escape>", lambda e: self.toggle_visible() if self.shown else None)

    def _clamp_pos(self):
        """面板位置夹回虚拟桌面内。某些全屏切换会被系统把窗口挪到画面外，
        若不夹回，一次回写就把屏外坐标固化进配置，面板就找不到了。
        多显示器时夹进整个虚拟桌面（面板放副屏是合法的）。
        面板按物理像素 1:1 渲染，PW/_ui_h 就是物理尺寸。"""
        try:
            xv, yv, wv, hv = virtual_screen()
        except Exception:
            return
        if wv <= 0 or hv <= 0:
            return
        vis_h = max(CHIP_SIZE, getattr(self, "_ui_h", CHIP_SIZE))
        self.px = max(xv, min(int(self.px), xv + wv - PW))
        self.py = max(yv, min(int(self.py), yv + hv - vis_h))

    def _click(self, e):
        if os.environ.get("PUBGMAP_TRACE"):
            hit = next((a for x1, y1, x2, y2, a, _v in self.regions
                        if x1 <= e.x <= x2 and y1 <= e.y <= y2), None)
            self._tr("click c=%d,%d root=%d,%d win=%d,%d -> %s" % (
                e.x, e.y, e.x_root, e.y_root,
                self.panel.winfo_rootx(), self.panel.winfo_rooty(), hit))
        if self.collapsed:
            # 折叠芯片：交给系统拖动；没拖动（= 只是点了一下）就展开
            if not self._native_drag():
                self.toggle_collapse()
            return
        for x1, y1, x2, y2, act, val in self.regions:
            if not (x1 <= e.x <= x2 and y1 <= e.y <= y2):
                continue
            if act == "mapmenu":
                # px/py 与 PAD/TITLE_H 同一坐标系（面板物理像素 1:1）
                self.map_menu.post(self.px + PAD, self.py + TITLE_H)
            elif act == "type":
                self.enabled.symmetric_difference_update({val})
                self.draw_proj()
                self.save()
            elif act == "settings":
                self.open_settings()
                return
            elif act == "help":
                self.toggle_help()
                return
            elif act == "helpx":
                self.toggle_help()
                return
            elif act == "hkbox":
                self._start_hk_capture()
                return
            elif act == "mclear":
                self.measure[val] = None   # 点某一行 = 清掉这一行
                self.draw()
            elif act == "scan":
                # 按下反馈：暗一拍再识别（识别会先隐藏图层抓屏，正好衔接）
                self._set_scan("p")
                self.root.after(110, self.rescan)
            elif act == "close":
                self.toggle_visible()
                return
            self.draw()
            return
        if e.y < TITLE_H:
            self._native_drag()             # 标题栏 = 拖动面板

    def _native_drag(self):
        """标题栏 / 折叠芯片交给 Windows 原生拖动（WM_NCLBUTTONDOWN + HTCAPTION）。

        以前用 Tk 的 B1-Motion + geometry() 自己挪窗口：事件一被合并就"追不上
        光标"，拖起来特别涩。系统 move loop 是按光标实时走的，绝对跟手。
        必须用 PostMessage 而非 SendMessage：SendMessage 会在本回调里同步进
        move loop，期间 Windows 泵消息让 Tcl 重入 Python 时线程状态为空，直接
        Fatal Python error 进程崩死（实测点一下标题栏就崩）。PostMessage 只
        投递不等待，move loop 由 Tcl 自己的消息循环接管，同样跟手且安全。
        返回 True 表示已请求系统拖动（结果由 _drag_done 异步确认）。
        """
        self._drag_before = (self.px, self.py)
        try:
            WM_NCLBUTTONDOWN, HTCAPTION = 0xA1, 2
            user32.ReleaseCapture()
            user32.PostMessageW(self.hwnd_panel, WM_NCLBUTTONDOWN, HTCAPTION, 0)
        except Exception:
            return False
        self.root.after(400, self._drag_done)
        return True

    def _place_panel(self, w=None):
        """把面板放到 self.px/py（物理坐标），窗口物理尺寸 = 画布逻辑尺寸。

        EXE 是 PMv2 感知进程，面板窗口按物理像素 1:1 渲染（DWM 不做整窗
        拉伸），所以窗口设 380×H 物理像素，内容正好铺满、四周无空窗。
        跨屏尺寸不变（固定像素），WM_DPICHANGED 已被 _freeze_panel_size
        吞掉，字体布局永不重排。帮助滑出时 w > PW。"""
        try:
            lh = getattr(self, "_ui_h", 240)   # winfo_* 受 DPI 虚拟化影响，用常量
            # 动画期间 _win_w（窗口实时宽度）优先于内容宽度 _ui_w
            ww = int(w) if w else int(getattr(self, "_win_w", 0)) \
                or int(getattr(self, "_ui_w", PW))
            user32.SetWindowPos(self.hwnd_panel, 0, int(self.px), int(self.py),
                                ww, int(lh), 0x4 | 0x10)
            if os.environ.get("PUBGMAP_TRACE"):
                self._tr("place px=%s py=%s size=%sx%s"
                         % (self.px, self.py, PW, lh))
        except Exception:
            pass

    def _drag_done(self):
        """原生拖动结束（左键已松开）后写回窗口位置。还在拖就再等一拍。"""
        try:
            if user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000:   # 左键还按着
                self.root.after(120, self._drag_done)
                return
            # Tk 的 winfo_x/y 受 DPI 虚拟化影响不可信；
            # PMv2 线程调 GetWindowRect 拿到的就是物理真值。
            l, t, _r, _b = win32gui.GetWindowRect(self.hwnd_panel)
            self.px, self.py = int(l), int(t)
            if os.environ.get("PUBGMAP_TRACE"):
                self._tr("drag_done phys=%s,%s" % (self.px, self.py))
            self._clamp_pos()
            self._place_panel()
        except Exception:
            return
        if (self.px, self.py) != getattr(self, "_drag_before", (self.px, self.py)):
            self.panel_screen = "user"      # 用户拖过 → 以后记住这个位置
            self.save()
        elif self.collapsed:
            self.toggle_collapse()          # 折叠芯片没拖动 = 点了一下，展开

    def _set_scan(self, st):
        """「测距」按钮三态：n=常态 / h=悬停变亮 / p=按下变暗。"""
        if st == getattr(self, "_scan_state", "n"):
            return
        self._scan_state = st
        try:
            self.c.itemconfig(self._scan_pill, image=self._scan_imgs[st])
            self.c.itemconfig(self._scan_txt,
                              fill="#e6fbff" if st == "h" else CY)
        except Exception:
            pass

    def _panel_hover(self, e):
        """「测距」按钮悬停变亮（按钮感）；标题栏图标不再有悬停气泡。"""
        if not self.collapsed:
            r = getattr(self, "_scan_rect", None)
            if r:
                inside = r[0] <= e.x <= r[2] and r[1] <= e.y <= r[3]
                if inside and self._scan_state == "n":
                    self._set_scan("h")
                elif not inside and self._scan_state != "n":
                    self._set_scan("n")
        return

    # ------------------------------------------------------------ 固定公式
    def reset_geometry(self):
        """恢复纯解析式 side=H / left=(W−H)/2 / top=0，不做任何抓屏或识别。"""
        self.scale = 1.0
        self.off_x = 0
        self.off_y = 0
        self._resync()
        self.draw()
        self.save()

    # ------------------------------------------------------------ 帮助（图形化）
    def open_settings(self):
        if self.settings_win is not None and self.settings_win.winfo_exists():
            self.settings_win.lift()
            return
        w = tk.Toplevel(self.root)
        self.settings_win = w
        w.title("设置")
        w.geometry("440x400")
        w.configure(bg=BG)
        w.attributes("-topmost", True)
        w.resizable(False, False)
        try:
            w.iconbitmap(str(resource("assets/icon.ico")))
        except Exception:
            pass

        style = ttk.Style(w)
        style.theme_use("clam")
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=TXT, font=(FONT, 9))
        style.configure("Header.TLabel", font=(FONT, 12, "bold"), foreground=CY)
        style.configure("TLabelframe", background=BG, bordercolor=LINE)
        style.configure("TLabelframe.Label", background=BG, foreground=CY,
                        font=(FONT, 9, "bold"))
        style.configure("TEntry", fieldbackground=CHIP, foreground=TXT,
                        insertcolor=TXT, borderwidth=0)
        style.configure("TCombobox", fieldbackground=CHIP, background=CHIP,
                        foreground=TXT, borderwidth=0, arrowcolor=TXT)
        style.map("TCombobox",
                  fieldbackground=[("readonly", CHIP)],
                  background=[("readonly", CHIP)],
                  foreground=[("readonly", TXT)])
        style.configure("TButton", background=CHIP, foreground=TXT,
                        borderwidth=0, font=(FONT, 9))
        style.map("TButton", background=[("active", "#12333d")])
        style.configure("TCheckbutton", background=BG, foreground=TXT, font=(FONT, 9))

        ttk.Label(w, text="设置", style="Header.TLabel").pack(anchor="w", padx=14,
                                                              pady=(12, 6))
        box = ttk.LabelFrame(w, text=" 显示 ", padding=10)
        box.pack(fill="x", padx=14)
        ttk.Label(box, text="不透明度").pack(anchor="w")
        s2 = tk.Scale(box, from_=40, to=100, orient="horizontal", bg=BG, fg=TXT,
                      troughcolor=CHIP, highlightthickness=0, length=368,
                      command=lambda v: self.set_opacity(int(v)))
        s2.set(round(self.alpha * 100))
        s2.pack(anchor="w")
        self._v_top = tk.BooleanVar(value=self.topmost)
        self._v_grid = tk.BooleanVar(value=self.show_grid)
        ttk.Checkbutton(box, text="始终置顶", variable=self._v_top,
                        command=lambda: self.set_topmost(self._v_top.get())
                        ).pack(anchor="w")
        ttk.Checkbutton(box, text="网格辅助线", variable=self._v_grid,
                        command=lambda: (setattr(self, "show_grid", self._v_grid.get()),
                                         self.draw_proj(), self.draw(), self.save())
                        ).pack(anchor="w")
        row0 = ttk.Frame(box)
        row0.pack(fill="x", pady=(6, 0))
        ttk.Button(row0, text="全部类别 开", width=12,
                   command=lambda: self.all_types(True)).pack(side="left")
        ttk.Button(row0, text="全部类别 关", width=12,
                   command=lambda: self.all_types(False)
                   ).pack(side="left", padx=6)

        box2 = ttk.LabelFrame(w, text=" 对齐补偿 ", padding=10)
        box2.pack(fill="x", padx=14, pady=10)
        W_scr, H_scr = self._last_metrics
        left, top_, side, _ = self.rect
        self._lbl_rect = ttk.Label(
            box2, text=f"{W_scr}×{H_scr}   left={int(left)}  top={int(top_)}  "
                       f"side={int(side)}", foreground=CY, font=(MONO, 8))
        self._lbl_rect.pack(anchor="w", pady=(0, 6))
        sc = tk.Scale(box2, from_=0.85, to=1.15, resolution=0.005,
                      orient="horizontal", bg=BG, fg=TXT, troughcolor=CHIP,
                      highlightthickness=0, length=368,
                      command=lambda v: self.adjust(scale=float(v)))
        sc.set(self.scale)
        sc.pack(anchor="w")
        row = ttk.Frame(box2)
        row.pack(fill="x", pady=6)
        for label, key in (("X", "off_x"), ("Y", "off_y")):
            ttk.Label(row, text=label).pack(side="left", padx=(0, 4))
            ent = ttk.Entry(row, width=6)
            ent.insert(0, str(getattr(self, key)))
            ent.pack(side="left", padx=(0, 6))
            ttk.Button(row, text="应用", width=4, command=lambda k=key, e=ent: (
                setattr(self, k, int(e.get() or 0)), self._resync(), self.save())
                       ).pack(side="left", padx=(0, 8))
        ttk.Button(row, text="恢复默认",
                   command=lambda: (self.reset_geometry(), sc.set(1.0))
                   ).pack(side="left")

        # v3.9.28：全局热键的修改移到主面板帮助侧栏（点热键框循环切换），
        # 设置窗不再重复放这份 UI。
        ttk.Button(w, text="关闭", command=w.destroy).pack(anchor="e", padx=14)
        w.protocol("WM_DELETE_WINDOW", w.destroy)

    # ------------------------------------------------------------ 事件泵
    def _pump(self):
        # 「计算中」动效：每 ~0.32s 换一次省略号，重绘按钮文字
        if getattr(self, "_scanning", False):
            self._scan_anim = getattr(self, "_scan_anim", 0) + 1
            if self._scan_anim % 8 == 0:
                self.draw()
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "scan_done":
                    # 测距完成：整组替换结果；一条都没认到 → 按钮短暂显示「无轨迹」
                    self._scanning = False
                    self._scan_state = "n"
                    self.measure = (val if val is not None
                                    else [None] * self.slots)
                    self._scan_msg_ts = (0.0 if any(r and r.get("m")
                                                    for r in self.measure)
                                         else time.time())
                    self.draw()
                elif kind == "hotkey":
                    if val == "toggle":
                        self.toggle_visible()
                    elif val == "show":
                        self.show()
                    elif val == "settings":
                        self.show()
                        self.open_settings()
                    elif val == "grid":
                        self.show_grid = not self.show_grid
                        self.draw_proj()
                        self.draw()
                        self.save()
                    elif val == "quit":
                        self.quit()
                        return
                elif kind == "tray_menu":
                    self.native._show_menu(round(self.alpha * 100),
                                           self.show_grid)
                elif kind == "map":
                    self.show()
                    self.set_map(val)
                elif kind == "opacity":
                    self.set_opacity(val)
                    self.save()
                elif kind == "resync":
                    self._resync()
                    self.draw()
        except queue.Empty:
            pass
        self.root.after(40, self._pump)

    def quit(self):
        self.save()
        try:
            self.native.stop()
        except Exception:
            pass
        self.root.destroy()
        os._exit(0)

    def run(self):
        if os.environ.get("PUBGMAP_TRACE"):
            self._tr("enter mainloop")
        self.root.mainloop()


def main():
    enable_dpi_awareness()
    mutex = single_instance_guard()
    if mutex is None:
        return
    cfg = Config()
    data = json.loads(resource("data/pubg-overlay-data.json").read_text(encoding="utf-8"))
    App(cfg, data).run()
    _ = mutex


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log = user_config_dir() / "startup-error.log"
        try:
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text(traceback.format_exc(), encoding="utf-8")
        except Exception:
            pass
        user32.MessageBoxW(0, f"启动失败，详情见：\n{log}", APP_TITLE, 0x10)
        sys.exit(1)
