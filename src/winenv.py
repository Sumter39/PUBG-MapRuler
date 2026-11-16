# -*- coding: utf-8 -*-
"""Win32 进程环境：ctypes 绑定、消息/样式常量、热键解析、DPI 与屏幕几何。"""
import ctypes
from ctypes import wintypes

try:
    import win32gui
    import win32con
except ImportError:
    win32gui = None
    win32con = None


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
VK_LBUTTON = 0x01

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
