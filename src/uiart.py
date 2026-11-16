# -*- coding: utf-8 -*-
"""面板视觉常量与抗锯齿绘制原语（PIL 4× 超采样 → PhotoImage 缓存）。"""
from PIL import Image, ImageDraw, ImageTk


# ---------------------------------------------------------------- 视觉常量
BG, BAR, LINE, CY, TXT = "#0a1116", "#16262e", "#243b45", "#22d3ee", "#dbe9ee"
DIM, DIM2, CHIP, GOLD = "#6d8996", "#435965", "#101d24", "#fbbf24"
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


def hex_rgb(c):
    c = str(c).lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
