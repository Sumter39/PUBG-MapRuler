# -*- coding: utf-8 -*-
"""原生消息循环宿主：托盘图标、全局热键、单实例守卫。"""
import ctypes
import os
import queue
import threading

from winenv import (kernel32, user32, wintypes, WNDCLASSW, WNDPROC,
                    win32con, win32gui, MOD_ALT, MOD_CONTROL,
                    MOD_NOREPEAT, VK_OEM_COMMA, MF_STRING, MF_SEPARATOR,
                    MF_POPUP, MF_GRAYED, MF_CHECKED, NIM_ADD, NIM_DELETE,
                    NIF_MESSAGE, NIF_ICON, NIF_TIP, WM_TRAY, WM_SHOW_SELF,
                    WM_REBIND, WM_DISPLAYCHANGE, WM_HOTKEY, WM_COMMAND,
                    WM_CLOSE, WM_DESTROY, WM_RBUTTONUP, WM_LBUTTONUP,
                    parse_toggle_hotkey, hotkey_main_vk, hotkey_disp,
                    _HOTKEY_SIDE_VK)
from appconfig import APP_NAME, APP_TITLE, APP_VER


# ---- 托盘菜单命令 ID 与透明度档位 ----
M_TOGGLE, M_SETTINGS, M_GRID, M_EXIT = 1001, 1002, 1005, 1004
M_MAP_BASE, M_OPACITY_BASE = 2000, 3000
OPACITY_LEVELS = (55, 70, 85, 100)


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
