# -*- coding: utf-8 -*-
"""应用元数据、资源定位与便携模式配置。"""
import configparser
import os
import sys
from pathlib import Path


APP_NAME = "PUBG-MapRuler"
# 版本号划分（2026-11-15 用户要求，不再逐次递增）：
#   主版本 X.0.0 = 重大重构 / 不兼容变更
#   次版本 4.x.0 = 新功能批次（一批功能合起来发一版）
#   修订   4.0.x = bug 修复、体验微调
# v4.0.0 = 帮助滑出面板 + 真自定义热键（捕获态实时预览）功能批次
APP_VER = "5.0.0"
APP_TITLE = "PUBG 地图投影浮层"


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
        # 旧版 clickThrough 是「整窗锁死」、panelScreen 只存不读，语义均已废弃，
        # 读进来就丢掉
        if self.cp.has_section("Window"):
            self.cp.remove_option("Window", "clickThrough")
            self.cp.remove_option("Window", "panelScreen")
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
        # v4.0.1：旧逆向线性色组与实机不符（1黄2红3蓝4绿 → 1黄2橙3青4绿）。
        # 配置里存的旧默认会一直盖住新值，且识别分带已按实机色相重划，
        # 必须整组刷新并落盘。只认「四个都是旧默认」，用户自定义过的不碰。
        if tuple(self.cp.get("Squad", f"c{i}", fallback="").upper()
                 for i in (1, 2, 3, 4)) == (
                     "#F4B300", "#DC0000", "#0061E5", "#068E00"):
            for i in range(4):
                self.cp.set("Squad", f"c{i + 1}", SQUAD_COLORS[i])
            self.save()
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


# ---- 队伍配色与识别缓存常量 ----
GRID_TTL = 6.0                    # 网格识别结果有效期（秒），Alt 按下会强制刷新
SQUAD_N = 4                       # 固定 4 个槽位（即使不满也不增减）
SQUAD_MAX = 10                    # 编号色表最多备到 10 号
# 队伍编号色。1~4 号用**实机截图取样**（2026-11-15，Rondo / Normal 色盲模式）。
# 旧值来自 UiHelperFunctionsBp.GetTeamColor 蓝图字节码里的 EX_FloatConst，
# 那是线性色，未经 sRGB 编码直接 ×255 得到，和屏幕像素对不上：2 号被写成
# 红、3 号写成深蓝，而实机是橙和青；识别用的 HSV 窗口按旧值划，2 号橙线
# 全落进 1 号黄槽。改成屏幕真值后，面板色与识别分带同源。
# 5~10 号在蓝图中没有对应常量，仍为实机估色（阵亡态反推），仅观赛多队时用。
# 想微调就改配置 [Squad] c1..c10，程序不在运行时取色。
SQUAD_COLORS = (
    "#FFD700",   # 1 黄（实机削顶纯黄 255,255,0）
    "#FF9643",   # 2 橙（实机 255,150,67）
    "#00DCDC",   # 3 青（实机纯青 0,255,255）
    "#3CE070",   # 4 绿（实机 60,224,112）
    "#FF5164",   # 5 红（阵亡态反推）
    "#FF71C3",   # 6 粉（阵亡态反推）
    "#6C92FF",   # 7 靛（阵亡态反推）
    "#8FE8FF",   # 8 浅青（实测原色）
    "#3FD8F0",   # 9 青（实测原色）
    "#4A5BD8",   # 10 深蓝（阵亡态反推）
)
