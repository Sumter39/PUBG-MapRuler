# -*- coding: utf-8 -*-
"""
PUBG-MapRuler — Windows 桌面投影浮层（EXE 形态，非网页）

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


import ctypes
import json
import math
import os
import queue
import re
import threading
import time
from pathlib import Path

import tkinter as tk
from tkinter import ttk

from PIL import Image, ImageGrab, ImageOps

from appconfig import (APP_VER, GRID_TTL, SQUAD_COLORS, SQUAD_N, Config,
                       resource, user_config_dir)
from gridprobe import GridProbe
from trackprobe import TrackProbe
from uiart import (BG, BAR, CAP_COLS, CAP_H, CAP_ROW, CAP_W, CHIP,
                   CHIP_SIZE, CY, DIM, DIM2, FOOT_H, FONT, GOLD, HELP_W,
                   KEYCOLOR, KEYREF, LINE, MEAS_TITLE_H, M_ROW_H, MONO,
                   PAD, PW, RoundArt, S, TITLE_H, LAYER_Y, TXT)
from winenv import (user32, win32gui, GWL_EXSTYLE, HWND_TOPMOST,
                    HWND_NOTOPMOST, LWA_COLORKEY, LWA_ALPHA, SWP_NOSIZE,
                    SWP_NOMOVE, SWP_NOACTIVATE, WS_EX_LAYERED,
                    WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW, WS_EX_TRANSPARENT,
                    VK_LBUTTON, parse_toggle_hotkey, hotkey_disp,
                    _HK_POLL_MAIN, _HOTKEY_TEXT, screen_metrics,
                    virtual_screen, secondary_monitor, dpi_scale)
from winhost import NativeHost


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
            # 早期某次拖动把用户面板位置连同主屏坐标一起固化进了配置）
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
        self.enabled = set()
        self.scale = cfg.getfloat("Projection", "scale", 1.0)
        self.off_x = cfg.getint("Projection", "offsetX", 0)
        self.off_y = cfg.getint("Projection", "offsetY", 0)
        self.show_grid = bool(cfg.getint("Projection", "showGrid", 0))
        self.dot = cfg.getint("Projection", "dotSize", 5)
        self.rect = (0, 0, 0, 0)      # left, top, side, screenH
        self.regions = []
        self._last_metrics = (0, 0)

        # 读轨迹：网格比例尺缓存 + 4 个槽位（点「识别」时从整块地图一次性读出）
        self.grid = None
        self.grid_key = None
        self.grid_ts = 0.0
        self.measure = [None] * self.slots  # {"m": 米, "a": (x,y), "b": (x,y),
                                            #  "pts": [...], "straight": bool}

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
        # 实验证实：普通 Tk 窗口的位图恒为逻辑尺寸，
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

        截图范围 = 整个主屏（v4.0.12）。放大后地图会铺满整屏，只抓中央
        side×side 方块会把伸到两侧的轨迹端点裁掉。整屏抓完不做缩放，
        网格识别自己从中间裁方块（见 `GridProbe._canvas`）。

        抓屏通道两级（见 `_grab_map_core`）：DXGI 桌面复制优先（HDR 屏拿
        scRGB 原始帧，颜色正确，不做任何拉伸）；失败退回 GDI ImageGrab
        （HDR 屏会被 DWM 色调映射压爆，只能事后 autocontrast 找回对比度，
        且削顶信息已不可恢复）。"""
        try:
            self._proj_alpha(0.0)
            try:
                time.sleep(0.035)
                im, hdr = self._grab_map_core()
            finally:
                self._proj_alpha(self.alpha)
            if im is None:
                return None
            im = im.convert("RGB")          # 整屏矩形，不 resize 成方块
            # GDI 兜底在 HDR 屏上整体过曝（老版本实测 7.5% 像素削顶到 255），
            # 直方图拉伸把对比度找回；DXGI 帧颜色本来正确，拉伸反而破坏色相。
            if not hdr:
                im = ImageOps.autocontrast(im, cutoff=(1.0, 0.6))
            # 抓图当场压高光（v4.0.8）：合成出来的帧本来就大面积削顶，存下来
            # 的截图和肉眼看到的不是一回事。**这一步在保存之前做**，落盘的帧
            # 和面板预览都是正常的图。原始帧留一份给识别 —— 压高光是为了让
            # 截图好看，识别链路的阈值是按原始像素标定的，不该跟着一起变。
            wr = GridProbe.white_ratio(im)
            self._raw_frame = None
            if wr >= 0.10:
                self._raw_frame = im
                im, p_lo, p_hi = GridProbe.exposure_stretch(im)
                try:
                    with open(user_config_dir() / "dxgi.log", "a",
                              encoding="utf-8") as f:
                        f.write("%s capture stretch: 削顶 %.1f%% -> %.1f%% "
                                "分位 %d~%d\n"
                                % (time.strftime("%m-%d %H:%M:%S"),
                                   wr * 100, GridProbe.white_ratio(im) * 100,
                                   p_lo, p_hi))
                except Exception:
                    pass
            return im
        except Exception:
            return None

    def _map_box_phys(self):
        """抓图框 = 整个主屏的物理像素（v4.0.12）。

        旧版只抓屏幕中央的 side×side 方块；地图放大铺满整屏后，伸到方块
        两侧的轨迹端点会被裁掉（实测 zoom16 那条青线左端点压在 x=0，
        四象限判据直接否掉 → 无轨迹）。整屏抓图把两侧也纳入，端点不再被裁。

        注意 `side`（= 屏高 × scale）**不跟着改成屏宽**：它是比例尺基准，
        zoom = 大格像素 / (side/km)，而完整地图的方块边长仍是屏高。
        """
        W, H = screen_metrics()
        return (0, 0, W, H)

    @staticmethod
    def _median_v(im):
        """画面亮度中位数（V 通道）。

        过曝帧的中间调整体抬到 216~232，正常地图只有 140~150 —— 比「削顶
        比例」更能分开：浅色地图（雪地/沙漠高光）削顶比例也可能到 56%，
        但中间调仍然丰富、网格和标记都认得出。"""
        h = im.convert("HSV").getchannel("V").histogram()
        total = sum(h)
        acc = 0
        for i, c in enumerate(h):
            acc += c
            if acc >= total / 2:
                return i
        return 255

    def _dump_raw(self, dxgi_im, pbox):
        """PUBGMAP_DUMP_RAW=1 时，把 DXGI 帧和同刻 GDI 帧各存一份。

        「肉眼正常、截图过曝」只有把两条通道的原图摆在一起才判得了：
        raw_dxgi.png / raw_gdi.png 放数据目录，亮度写进 dxgi.log。
        只在调试开关下执行，正常运行时零开销。"""
        try:
            d = user_config_dir()
            dxgi_im.save(d / "raw_dxgi.png")
            alt = self._gdi_map_raw(pbox)
            if alt is not None:
                alt.save(d / "raw_gdi.png")
            with open(d / "dxgi.log", "a", encoding="utf-8") as f:
                f.write("%s raw dump dxgi_medV=%d gdi_medV=%s\n"
                        % (time.strftime("%m-%d %H:%M:%S"),
                           self._median_v(dxgi_im),
                           self._median_v(alt) if alt is not None else "-"))
        except Exception:
            pass

    def _gdi_map_raw(self, pbox):
        """过曝对比用：GDI 全屏抓一次再裁地图方块（不做比例缓存，慢但独立）。"""
        try:
            full = ImageGrab.grab(all_screens=False)
            W, _H = screen_metrics()
            r = full.width / max(1.0, float(W))
            box = tuple(int(round(v * r)) for v in pbox)
            return full.crop(box)
        except Exception:
            return None

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
                if os.environ.get("PUBGMAP_DUMP_RAW"):
                    self._dump_raw(img, pbox)
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
            # 传进来的是「压过高光」的截图（截图增强版）而识别不认时，
            # 退回原始帧再试一次：压高光是为了截图好看，不能让能量到的
            # 比例尺跟着丢。
            raw = getattr(self, "_raw_frame", None)
            if raw is not None and raw is not im \
                    and (self.grid.get("conf", 0.0) < 0.12
                         or self.grid.get("source") == "估算"):
                alt = GridProbe.probe(raw, km, side)
                if alt.get("conf", 0.0) >= 0.12 and alt.get("source") != "估算":
                    self.grid = alt
        self.grid_key = key
        self.grid_ts = time.time()
        # 排障用：设 PUBGMAP_PROBE=1 才把每帧的网格量测追加到 grid.log。
        # 注意地图缩放是**连续**的（滚轮 + 0.15 s 插值），没有离散档位表，
        # 所以这里只作事后核查，不参与识别决策。
        if os.environ.get("PUBGMAP_PROBE"):
            try:
                p = user_config_dir() / "grid.log"
                with open(p, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"grab": self._last_grab, "side": side,
                                        "km": km, **self.grid},
                                       ensure_ascii=False) + "\n")
                if p.stat().st_size > 1024 * 1024:      # 只留最近 4000 行
                    keep = p.read_text(encoding="utf-8").splitlines()[-4000:]
                    p.write_text("\n".join(keep) + "\n", encoding="utf-8")
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
                    # 截图增强（抓图时压过高光）只用于落盘与预览；
                    # 识别一律吃原始像素，避免改动已标定的颜色阈值。
                    ana = getattr(self, "_raw_frame", None) or im
                    grid = self._ensure_grid(force=True, im=ana)
                    if grid.get("conf", 0.0) < 0.12 \
                            or grid.get("source") == "估算":
                        # 量不到网格 = 不是地图画面（大厅/结算/观战）、网格被
                        # 白色蒙层冲淡，或缩放极端。比例尺不可信时不出距离。
                        self._scan_reason = "无网格"
                        found = [None] * self.slots
                    else:
                        self._scan_reason = ""
                        found = self._detect_measure(ana, grid["mpp"])
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
            # 失败原因直接写在按钮上：过曝 / 无网格 / 无轨迹，别让用户猜
            stxt, scol = getattr(self, "_scan_reason", "") or "无轨迹", GOLD
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
            col = self.squad[i]
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
        _, _, p_esc, p_main = self._hk_prev
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
