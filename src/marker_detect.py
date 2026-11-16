# -*- coding: utf-8 -*-
"""标记检测：定位 + 分类。

设计（2026-11-16 与用户共同确定 + 实机验证）：
1. 定位 = 暗盘 × 白色内容**互相约束**（真标记 = 暗盘 + 盘上白色数字/箭头，
   两个证据缺一不可；上一版的独立白块源一帧 648 个候选，互相约束后仅存真标记）。
2. 分类 = 盘上白色内容：
   · 数字 1~4（实机屏幕模板 NCC）→ 队伍号（**队伍分类唯一标准**）
   · 箭头（几何法：对称轴扫描 + 端部散布裁决指向，不用模板 —— 箭头形状
     固定且轴对称，8px 尺度下模板没有区分度）→ 连线延伸方向
3. 成链不在本模块：数字/箭头身份交由 trackprobe.detect_teams 的三重约束
   （端点同数字、箭头单向指向、连线存在）裁决成链。
"""
import math
import os
import sys
from collections import deque

import numpy as np
from PIL import Image

# ── 定位参数 ──
# 暗盘 BFS 面积（stride-2 口径）
DISC_A0, DISC_A1 = 6, 120
# 白色内容块面积（全分辨率口径，数字/箭头都是 5~90px 的小白块）
DIGIT_A0, DIGIT_A1 = 4, 90
# 白色内容与暗盘的最大距离（互相约束半径）
NODE_R = 10

# ── 分类参数 ──
# 盘上白色内容窗口半径（标记盘直径 ~22px，内容在中心 ±10px 内）
WIN_R = 10
# **环内**白色内容半径（硬约束）：数字/箭头画在盘心，环内半径约 8px。
# 方窗四角探出环外，会把环外的地形白斑（岩石反光/建筑）漏进来 ——
# zoom8_yellow (944,1045) 实测失败案例，圆形掩膜把环外像素直接清零。
CONTENT_R = 8
# 白色内容阈值（UI 纯白：高 V 低 S）
WHITE_V, WHITE_S = 150, 80
# 分类门槛（NCC 口径，实机标定：真数字 0.56~1.0；
# pak 垃圾模板/拉伸蹭分场景 ≤0.55。屏幕样本入库后真数字普遍 0.8+）
DIGIT_MIN_NCC = 0.55
# 归一化匹配的纵横比失真上限：内容宽高比与模板宽高比之比超过它即拒绝。
# 归一化匹配把模板拉伸到内容 bbox —— 瘦高的"1"模板被拉成 9×8 宽块照样
# 蹭到 0.71 分（zoom8_polyline D 点实测），纵横比是防拉伸作弊的硬约束。
ASPECT_TOL = 1.8
# ── 箭头几何识别参数（对称轴法，不用模板）──
# 箭头（chevron/箭镞）是**轴对称图形**且形状固定：
#   1. 对称轴：像素投到轴坐标系，统计镜像点是否存在 → 匹配比例 = 对称分
#   2. 指向：轴只有方向（mod 180°）。尖端一侧沿轴收成一点（垂向散布小），
#      尾部是两条臂/平底（垂向散布大）→ 散布小的一端即指向。
ARROW_AXIS_STEP = 2      # 对称轴粗扫步长（°，轴方向 mod 180°）
ARROW_MIRROR_TOL = 1.35  # 镜像匹配容差（px，8px 小图形容忍 1px 级抖动）
ARROW_MIN_SYM = 0.60     # 对称分门槛（匹配像素比例）
ARROW_TIP_BAND = 0.30    # 指向裁决带：轴两端各取该比例的沿轴像素
ARROW_CONTRAST = 1.18    # 尾端散布/尖端散布 下限（数字近对称图形会被拦掉）
ARROW_TIE_EPS = 0.035    # 多条对称轴近并列阈值（全部输出交连线裁决）
# 箭头内容尺寸门（white_mask 主块面积，px）：真 chevron 是固定屏幕尺寸 UI，
# 18 样本实测 22~34px；大号队友数字（37~90px 的"1/2/4"字形斜笔/起笔）和
# 碎白块（≤19px）都会蹭出假箭头，两端堵死。
ARROW_MIN_PX = 20
ARROW_MAX_PX = 34

# 模板资源根：PyInstaller --onefile 下模块在 sys._MEIPASS 里，
# __file__/../assets 会指到解包目录的上级（%TEMP%）——必须走 _MEIPASS。
# 资源由 build.py --add-data 打包在 _MEIPASS/assets/。
if getattr(sys, "frozen", False):
    _ASSETS = os.path.join(sys._MEIPASS, "assets", "markers")
else:
    _ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "..", "assets", "markers")

_TPL = {}


# ════════════════════════ 定位 ════════════════════════

def _blobs(mask, return_pts=False):
    """8 邻接连通块，返回 [(质心x, 质心y, 面积[, 块内点]), ...]。纯 BFS。"""
    ys, xs = np.nonzero(mask)
    seen = np.zeros_like(mask)
    out = []
    pts = list(zip(ys.tolist(), xs.tolist()))
    pset = set(pts)
    for y0, x0 in pts:
        if seen[y0, x0]:
            continue
        q = deque([(y0, x0)])
        seen[y0, x0] = True
        comp = []
        while q:
            y, x = q.popleft()
            comp.append((y, x))
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    p = (y + dy, x + dx)
                    if p in pset and not seen[p[0], p[1]]:
                        seen[p[0], p[1]] = True
                        q.append(p)
        n = len(comp)
        cy = sum(p[0] for p in comp) / n
        cx = sum(p[1] for p in comp) / n
        out.append((cx, cy, n, comp) if return_pts else (cx, cy, n))
    return out


def find_nodes(im):
    """圆盘定位：暗盘核心 × 白色内容**互相约束**。

    上一版教训：白色块源独立收集时地形亮斑/文字全进来（一帧 648 个候选），
    径向结构校验拦不住「亮斑周围恰好有暗色」的组合。真标记的定义就是
    「暗盘 + 盘上的白色数字/箭头」：
      · 白色候选半径 NODE_R 内必须有一个暗盘证据
      · 暗盘候选同样要求附近有白色内容（否则只是地形暗斑）
    返回 [(x, y), ...]（已去重，同盘的白块源与暗盘源都保留，相差 1~5px，
    由分类/成链阶段选择）。
    """
    g = np.asarray(im.convert("L")).astype(np.float32)
    H, W = g.shape
    dark = g < 100
    white = g > 175
    # 排除上下 UI 带
    top, bot = int(H * 0.05), int(H * 0.91)
    dark[:top, :] = False; dark[bot:, :] = False
    white[:top, :] = False; white[bot:, :] = False

    # 暗盘证据（stride-2 面积口径）
    disc_cands = []
    for cx, cy, n in _blobs(dark[::2, ::2]):
        if DISC_A0 <= n <= DISC_A1:
            disc_cands.append((cx * 2, cy * 2))

    # 白色内容块
    white_blobs = [(cx, cy, n) for cx, cy, n in _blobs(white)
                   if DIGIT_A0 <= n <= DIGIT_A1]

    # ── 互相约束 ──
    nodes = []
    used = set()
    r2 = NODE_R ** 2
    for wx, wy, wn in white_blobs:
        best, bd = None, r2
        for di, (dx, dy) in enumerate(disc_cands):
            d2 = (wx - dx) ** 2 + (wy - dy) ** 2
            if d2 < bd:
                best, bd = di, d2
        if best is None:
            continue                      # 白色内容旁没有暗盘 → 地形亮点
        used.add(best)
        nodes.append((wx, wy))
    for di, (dx, dy) in enumerate(disc_cands):
        if di in used:
            continue                      # 已被白色源覆盖
        ok = False                        # 纯暗盘候选：附近必须有白色内容
        for wx, wy, wn in white_blobs:
            if (wx - dx) ** 2 + (wy - dy) ** 2 < r2:
                ok = True
                break
        if ok:
            nodes.append((dx, dy))

    final = []
    for cx, cy in nodes:
        if all((cx - ox) ** 2 + (cy - oy) ** 2 >= 5 ** 2 for ox, oy in final):
            final.append((int(round(cx)), int(round(cy))))
    return final


# ════════════════════════ 分类 ════════════════════════

def _alpha(path):
    a = np.asarray(Image.open(path))[:, :, 3] > 128
    ys, xs = np.nonzero(a)
    return a[ys.min():ys.max() + 1, xs.min():xs.max() + 1]


def _templates():
    """懒加载分类模板。

    digits: {数字: [alpha 模板, ...]} —— pak 里 mapmarker_0N.u 解出来是
    噪声（RGB 全乱、alpha 是无字形结构的实心块），弃用；改用**实机截图
    采集样本**（assets/markers/screen_digits/，从已人工核验的测试集帧抠出）。
    数字是固定屏幕尺寸 UI，屏幕口径对屏幕口径零失真；路线端数字小
    （2×8~6×8），玩家标数字大（4×13~9×15），多样本互相补盲区。
    箭头不走模板（几何法，见 match_arrow）。
    """
    if not _TPL:
        digits = {}
        sdir = os.path.join(_ASSETS, "screen_digits")
        if os.path.isdir(sdir):
            for fn in sorted(os.listdir(sdir)):
                if fn.startswith("scr_d") and fn.endswith(".png"):
                    d = int(fn[5])
                    digits.setdefault(d, []).append(_alpha(os.path.join(sdir, fn)))
        _TPL["digits"] = digits
    return _TPL


def white_mask(im, cx, cy):
    """盘上白色内容掩膜，只保留**环内**（半径 CONTENT_R 圆内）的白色块。

    用户定死的规则：只识别圆环内部的白色内容。落地为两层几何约束：
    1. 圆形掩膜：盘心半径 CONTENT_R 外的白色像素一律清零 —— 环外的
       地形白斑（岩石反光/建筑/玩家名）从根上进不来；
    2. 块选择：环内若有多块（环描边残块 / 数字与描边粘连），取**离盘心
       最近**的 ≥3px 块 —— 内容永远画在盘心，与块大小无关。
    返回 (内容块掩膜, 主块面积, 环内总块数)。
    """
    x0, y0 = cx - WIN_R, cy - WIN_R
    crop = im.crop((x0, y0, x0 + 2 * WIN_R + 1, y0 + 2 * WIN_R + 1))
    hsv = np.asarray(crop.convert("HSV"))
    raw = (hsv[:, :, 2] > WHITE_V) & (hsv[:, :, 1] < WHITE_S)
    # 环内硬约束：CONTENT_R 外清零
    yy, xx = np.mgrid[0:raw.shape[0], 0:raw.shape[1]]
    raw &= (xx - WIN_R) ** 2 + (yy - WIN_R) ** 2 <= CONTENT_R ** 2
    blobs = _blobs(raw, return_pts=True)
    if not blobs:
        return np.zeros_like(raw), 0, 0
    near = [b for b in blobs if b[2] >= 3]
    if not near:
        return np.zeros_like(raw), 0, 0
    main = min(near, key=lambda b: (b[0] - WIN_R) ** 2 + (b[1] - WIN_R) ** 2)
    out = np.zeros_like(raw)
    for py, px in main[3]:
        out[py, px] = True
    return out, main[2], len(blobs)


def _ncc(a, b):
    """零均值归一化互相关。对细笔画的形状差异远比二值 IoU 灵敏：
    pak 垃圾模板蹭 IoU 0.71 的场景，NCC 只有 0.5 以下；真数字 0.8~1.0。"""
    a = a - a.mean()
    b = b - b.mean()
    d = math.sqrt(float((a * a).sum()) * float((b * b).sum()))
    return float((a * b).sum()) / d if d > 0 else 0.0


def _ncc_template(wm, tmpl):
    """内容 bbox 对齐（模板缩放到内容框，灰度双线性）+ ±2px 滑动，取最大 NCC。

    纵横比校验仍是硬前置：瘦高"1"模板拉成宽块照样蹭分的漏洞，靠
    ASPECT_TOL 堵死（形变超 1.8 倍直接拒绝）。
    """
    H, W = wm.shape
    ys, xs = np.nonzero(wm)
    if len(xs) < 4:
        return 0.0
    y0, y1 = ys.min(), ys.max() + 1
    x0, x1 = xs.min(), xs.max() + 1
    ch, cw = y1 - y0, x1 - x0
    # 数字"1"屏幕口径只有 2px 宽（竖条），宽下限不能 ≥3
    if ch < 3 or cw < 2 or ch > 2 * WIN_R or cw > 2 * WIN_R:
        return 0.0
    ah, aw = tmpl.shape
    ca, ta = cw / ch, aw / ah
    if max(ca / ta, ta / ca) > ASPECT_TOL:
        return 0.0
    sw = max(2, min(2 * WIN_R, int(round(aw * ch / ah))))
    t = np.asarray(Image.fromarray((tmpl * 255).astype(np.uint8))
                   .resize((sw, ch), Image.BILINEAR)).astype(np.float32) / 255.0
    w = wm.astype(np.float32)
    best = 0.0
    for dy in range(max(0, y0 - 2), min(H - ch, y0 + 3)):
        for dx in range(max(0, x0 - sw), min(W - sw, x0 + 3)):
            best = max(best, _ncc(w[dy:dy + ch, dx:dx + sw], t))
    return best


def match_digit(wm):
    """数字匹配（NCC，多样本取最大）。返回 (digit 1~4, ncc) 或 None。"""
    samples = _templates().get("digits") or {}
    best = None
    for d, ts in samples.items():
        for t in ts:
            v = _ncc_template(wm, t)
            if best is None or v > best[1]:
                best = (d, v)
    if best is None or best[1] < DIGIT_MIN_NCC:
        return None
    return best


def match_arrow(wm):
    """箭头几何识别（对称轴 + 端部散布裁决），不用模板。

    用户定死：箭头形状固定且**轴对称**，用顶点/质心/对称性这类鲁棒几何
    量直接算方向，比小模板滑窗精确得多（模板在 8px 尺度没有区分度）。

    两步：
      1. 对称轴（mod 180°）：把环内白块像素投到候选轴坐标系
         (T=沿轴, S=垂轴)，每个像素的镜像 (T, -S) 若有像素存在（容差内）
         记一次命中，命中比例即对称分。真轴上形状两侧重合高分，错误方向
         形状错位低分。粗扫步长 ARROW_AXIS_STEP，再 ±3° 细化到 1°。
      2. 指向侧裁决：轴本身没有指向。尖端一侧沿轴收成一点（端部垂向
         散布小），尾部是两条臂/平底（端部垂向散布大）。取两端各
         ARROW_TIP_BAND 比例的像素带比垂向散布，小的一端即指向；
         散布对比不足 ARROW_CONTRAST 的（近对称图形，如数字 0/3/8/1）
         判非箭头。

    返回 [(角度°, 对称分), ...] 或 None。角度 = 指向的屏幕方位角
    （0=右, 90=下, 即 atan2(dy,dx) 度数）。多条近并列轴（差 ≤
    ARROW_TIE_EPS）全部输出，真角度由成链的连线存在性裁决。
    """
    ys, xs = np.nonzero(wm)
    if len(xs) < 6:
        return None
    ch = ys.max() - ys.min() + 1
    cw = xs.max() - xs.min() + 1
    if ch < 4 or cw < 4:
        return None
    if not (ARROW_MIN_PX <= len(xs) <= ARROW_MAX_PX):
        return None                          # 尺寸门外：大数字字形/碎块
    X = xs.astype(np.float32) - xs.mean()
    Y = ys.astype(np.float32) - ys.mean()

    def axis_frame(theta_deg):
        t = math.radians(theta_deg)
        c, s = math.cos(t), math.sin(t)
        return X * c + Y * s, -X * s + Y * c      # (T 沿轴, S 垂轴)

    def sym_score(T, S):
        """每个像素的镜像 (T,-S) 容差内有像素 → 命中；命中比例即对称分。"""
        d2 = (T[:, None] - T[None, :]) ** 2 + (S[:, None] + S[None, :]) ** 2
        return float((d2.min(axis=1) <= ARROW_MIRROR_TOL ** 2).mean())

    # 粗扫 0~179°（轴 mod 180）+ 细化
    coarse = [(sym_score(*axis_frame(a)), a)
              for a in range(0, 180, ARROW_AXIS_STEP)]
    top0 = max(s for s, _ in coarse)
    cands = [a for s, a in coarse if s >= top0 - ARROW_TIE_EPS]
    scored = []
    for a0 in cands:
        for a in range(a0 - ARROW_AXIS_STEP, a0 + ARROW_AXIS_STEP + 1):
            a %= 180
            T, S = axis_frame(a)
            scored.append((sym_score(T, S), a))
    top = max(s for s, _ in scored)

    def resolve(sym, a):
        """轴 → 指向角 + 指向裁决（散布小的一端是尖端）。"""
        T, S = axis_frame(a)
        t0, t1 = float(T.min()), float(T.max())
        span = max(t1 - t0, 1e-3)
        lo = T <= t0 + ARROW_TIP_BAND * span
        hi = T >= t1 - ARROW_TIP_BAND * span
        sp_lo = float(S[lo].max() - S[lo].min()) if lo.any() else 0.0
        sp_hi = float(S[hi].max() - S[hi].min()) if hi.any() else 0.0
        if sp_lo <= sp_hi:                        # 尖端在低 T 端 → 指向 -u
            return (a + 180) % 360, sp_hi, sp_lo
        return a % 360, sp_lo, sp_hi              # 尖端在高 T 端 → 指向 +u

    out = []
    seen = []
    for sym, a in sorted(scored, key=lambda p: -p[0]):
        if sym < top - ARROW_TIE_EPS or sym < ARROW_MIN_SYM:
            continue
        if any(min((a - b) % 180, (b - a) % 180) < 8 for b in seen):
            continue
        seen.append(a)
        ang, sp_tail, sp_tip = resolve(sym, a)
        if sp_tip < 1e-3 or sp_tail / sp_tip < ARROW_CONTRAST:
            continue                              # 近对称图形（数字等）→ 非箭头
        out.append((ang, sym))
    return out or None


def classify(im, nodes):
    """对每个候选节点分类盘上白色内容（环内白块 → 数字/箭头身份）。

    返回 dict {(x, y): [("digit", n, ncc), ("arrow", deg, ncc), ...]}。
    实机教训：屏幕口径下白色内容只有 2~9px，单点强分类必然出错 ——
    数字与箭头的最终裁决交给成链的几何约束：
      · 链的中间节点必须有箭头身份指向出段方向
      · 链的**终点必须是数字节点**（数字定队伍）
      · 相邻节点间连线必须存在（形态学加强，阈值从松）
    环内掩膜只保留离盘心最近的 ≥3px 白块（环外像素已被圆形掩膜清零）。
    """
    out = {}
    for (cx, cy) in nodes:
        wm, main_n, _ = white_mask(im, cx, cy)
        if main_n < 4:
            continue                      # 环内无白色内容
        ids = []
        d = match_digit(wm)
        if d is not None:
            ids.append(("digit", d[0], d[1]))
        for a in (match_arrow(wm) or []):
            ids.append(("arrow", a[0], a[1]))
        if ids:
            out[(cx, cy)] = ids
    return out
