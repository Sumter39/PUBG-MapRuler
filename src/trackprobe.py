# -*- coding: utf-8 -*-
"""轨迹识别：从地图截图认游戏自画的标号轨迹（按队伍）。"""
import math

import numpy as np
from PIL import Image, ImageChops

import marker_detect as TP


# ---------------------------------------------------------------- 轨迹识别
class TrackProbe:
    """从地图截图里认**游戏自己画的**标号轨迹，不碰玩家输入。

    实机形态（PUBG 地图 Alt+右键标记）：每个标记是一个**队伍色**圆环 + 深色底 +
    白色数字，相邻标记之间由同色细线连起来。颜色以**屏幕像素实测**为准
    （2026-11-15，Normal 色盲模式）：1 号黄 #FFD700 / 2 号橙 #FF9643 /
    3 号青 #00DCDC / 4 号绿 #3CE070。旧版按蓝图 GetTeamColor 的线性色常量
    写死成「1黄2红3蓝4绿」，与 sRGB 屏幕像素不符，2 号橙被 1 号黄窗口吞掉。
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

    @classmethod
    def mask_for(cls, im, ti, line=False, hsv=None):
        """第 ti 号队伍（0 基）的颜色掩膜——**HSV 色相分类**，返回 bool ndarray。

        hsv 可选：uint8 (H, W, 3) 数组，4 队共用同一次 `im.convert("HSV")`
        （整屏 2560×1440 转一次约 0.3s）。不给时对 im 现转。
        阈值语义与旧 PIL 链（point 严格 > / <，逐通道与）完全一致。

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
        if hsv is None:
            hsv = np.asarray(im.convert("HSV"))
        h, s, v = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
        if ti == 1:
            # 2 号橙：实机色相窗口 H11~30（h1/h2），不再是旧的「红环绕」。
            # 纯红 H>245 只留作兼容兜底（其他色盲模式 / 旧版本标记），
            # 它和橙窗口不相交，不会把 1 号黄抢回来。
            hue = ((h > h1) & (h < h2)) | (h > 245)
            return hue & (s > s_t) & (v > v_t)
        return (h > h1) & (h < h2) & (s > s_t) & (v > v_t)

    # 每队（0基）HSV 掩膜参数（8bit：H = 色相角 × 255/360）：
    # (H 下限, H 上限, 标记 S 下限, 标记 V 下限)。掩膜只筛颜色，环打分再筛形态。
    _HSV_BANDS = {
        # v4.0.1：实机重新分带。旧版 1 号黄 H11~44 把 2 号橙 (H≈20) 一并
        # 吃进去，2 号红窗口 (H<12 或 >245) 在实机一个像素都没有 → 所有
        # 橙线轨迹都被记到 1 号槽，2 号永远空。按取样中位色相切开：
        #   1 号黄 H30~52（实测 35~42）/ 2 号橙 H11~30（实测 16~22）
        # 交界 30 两侧各留一点余量，抗锯齿混色不至于漏环。
        # 1号黄：覆盖削顶纯黄 (255,255,0) H≈42 与金黄 (255,215,0) H≈36；
        # 地形土黄 V=74% 进不来，秸秆高光由环形态打分 + _linked 兜底。
        0: (30, 52, 139, 223),
        # 2号橙：实机 (255,150,67) H≈19~(255,174,40) H≈26；旧的纯红
        # (H<12 / >245) 只在 mask_for 里作兼容兜底，不再当主窗口。
        1: (11, 30, 139, 177),
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
    #
    # v4.0.15：0.30 → 0.24。实机 zoom16 弱环帧（草地深绿背景上的 1px 黄
    # 描边圈）实测 outer=0.261，被 0.30 卡在门外 → 端点丢失 → 无轨迹。
    # 降 0.24 不放宽「像不像环」的判据本身：仍有 inner≤0.20 + 四象限均衡
    # ≥0.20 + 真链五层校验三道关；全测试集 + 飞机假链帧验证无误报。
    # 本质：同一固定标记在雪地 outer 0.9、草地 0.26，单一硬阈值必顾此失彼。
    GATE_OUTER, GATE_INNER = 0.24, 0.20
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
    def _ring_scan_np(cls, mask):
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
        pad = cls.RING_OUT
        # 只在「有队伍色像素」的包围盒（外扩 RING_OUT）内扫描。整屏抓图后
        # 两侧黑边没有任何掩膜点，46 次跨度累加全花在空数据上（实测一队
        # 0.6s）。裁完再算，正方形帧 bbox 通常就是整幅，结果逐像素不变。
        yy, xx = np.nonzero(a)
        if not len(xx):
            return []
        OH, OW = a.shape
        x0 = max(0, int(xx.min()) - pad)
        x1 = min(OW, int(xx.max()) + 1 + pad)
        y0 = max(0, int(yy.min()) - pad)
        y1 = min(OH, int(yy.max()) + 1 + pad)
        a = a[y0:y1, x0:x1]
        H, W = a.shape
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
        return [(x + x0, y + y0) for x, y in spread]   # 还原到整屏坐标

    @classmethod
    def _ring_scan_border(cls, ml):
        """**贴着测距框边缘的环**单独扫一遍（v4.0.11）。

        整图扫描的四象限判据要求环在四个方向都有覆盖；环一旦被画面边缘裁掉
        一侧（放大后轨迹端点正好压在框边上，实测左边只剩 74 px），象限覆盖
        直接为 0，真标记被整条丢掉 → 只剩一个端点 → 无轨迹。用户遇到的就是
        这个：同一个轨迹缩小能测 354 m，放大就「无轨迹」。

        做法：只取边框带内（中心仍在图内）的位置，环带改成**按框内有效点数
        归一化**（而不是除以完整的环点数），于是半圆也能算覆盖率；再用
        「有效点占比 ≥45%」挡住只剩一小段弧的垃圾候选。

        前置短路：环中心距掩膜点最多 RING_OUT px，掩膜点外扩 RING_OUT 后
        若完全不落在边框带内，边框带内的中心连一个掩膜点都够不着，候选
        必为空——直接跳过整段 gather（常规帧每队省几百 ms 的大头）。
        """
        m = np.asarray(ml) > 0
        H, W = m.shape
        pad = cls.RING_OUT + 1
        if H <= 3 * pad or W <= 3 * pad:
            return []
        band = cls.RING_OUT
        if not (m[:pad + band].any() or m[H - pad - band:].any()
                or m[:, :pad + band].any() or m[:, W - pad - band:].any()):
            return []
        ring = cls._offsets(cls.RING_IN, cls.RING_OUT)
        core = cls._offsets(0, cls.CORE_R)
        rdx = np.array([d[0] for d in ring], np.int16)
        rdy = np.array([d[1] for d in ring], np.int16)
        cdx = np.array([d[0] for d in core], np.int16)
        cdy = np.array([d[1] for d in core], np.int16)

        rows = np.arange(0, H, 2)
        cols = np.arange(0, W, 2)
        top = rows[rows < pad]
        bot = rows[rows >= H - pad]
        left = cols[cols < pad]
        right = cols[cols >= W - pad]
        inner_rows = np.arange(pad, H - pad, 2)
        cy = np.concatenate([
            np.repeat(top, len(cols)), np.repeat(bot, len(cols)),
            np.repeat(inner_rows, len(left)), np.repeat(inner_rows, len(right)),
        ]).astype(np.int32)
        cx = np.concatenate([
            np.tile(cols, len(top)), np.tile(cols, len(bot)),
            np.tile(left, len(inner_rows)), np.tile(right, len(inner_rows)),
        ]).astype(np.int32)
        if not len(cx):
            return []

        def score_at(cy, cx, dxs, dys):
            gy = cy[:, None] + dys[None, :]
            gx = cx[:, None] + dxs[None, :]
            ok = (gy >= 0) & (gy < H) & (gx >= 0) & (gx < W)
            hit = m[np.clip(gy, 0, H - 1), np.clip(gx, 0, W - 1)] & ok
            return hit.sum(1), ok.sum(1)

        scored = []
        step = 4096                                  # 分块，避免大数组爆内存
        for i in range(0, len(cx), step):
            sl = slice(i, i + step)
            hs, vs = score_at(cy[sl], cx[sl], rdx, rdy)
            cs, _ = score_at(cy[sl], cx[sl], cdx, cdy)
            with np.errstate(invalid="ignore", divide="ignore"):
                outer = hs / np.maximum(1, vs)
                frac = vs / float(len(rdx))
                inner = cs / float(len(cdx))
            keep = (outer >= cls.GATE_OUTER) & (inner <= cls.GATE_INNER) \
                & (frac >= 0.45)
            for k in np.nonzero(keep)[0]:
                scored.append((float(outer[k]), int(cx[i + k]), int(cy[i + k])))
        if not scored:
            return []
        scored.sort(reverse=True)
        spread, sep2 = [], cls.RING_OUT * cls.RING_OUT
        for _s, x, y in scored:
            if all((x - ox) ** 2 + (y - oy) ** 2 >= sep2 for ox, oy in spread):
                spread.append((x, y))
            if len(spread) >= 16:
                break
        return spread

    # 收敛去重半径（v4.0.14）：v4.0.3 起 20px，只为防「同一个环被精修认出
    # 两个偏心中心」。但放大帧上飞机图标的「(1)(1) 电波」符号会给出一串间距
    # 25~29px 的伪环（实测 known_false_chain：880/902/927 x 445~473），20px
    # 拦不住，被 _chains 串成 37m 假链多认一队。真标记最小间距远大于 36px
    # （zoom8 那帧同色两标记 509.93px），36 只合并图标内伪环、不碰真点。
    MERGE_R = 36

    @classmethod
    def marks(cls, mask, cell=8, thr=18, top=48, sep=22):
        """粗网格出候选 → 环打分定位。

        单独按粗值排序会被地图底部的黄色 UI 条带挤掉真标号（它们又长又亮），
        所以候选先做一遍**按距离的抑制**（`sep` px 内只留最强的一个），
        让候选铺开到整张图，再取前 `top` 个细打分。

        细打分在每个候选 ±16px 窗口内对环响应图取 argmax（窗口全局最优，
        等价于完美爬山）。v3.9.10 的单起点十字爬山会卡在环形打分的同心
        局部极值上（切线方向），偏心 10px 的候选永远收敛不到真中心 →
        标记丢失 → 「无轨迹」，v3.9.11 改为窗口 argmax 一劳永逸。

        mask 传 bool ndarray（mask_for 的返回值）或 PIL 掩膜（色盲退路
        mask() 的返回值），两条入口在顶部归一化成同一组 (arr, ml)。"""
        if isinstance(mask, np.ndarray):
            arr = mask
            W, H = int(arr.shape[1]), int(arr.shape[0])
            ml = Image.fromarray(arr.astype(np.uint8) * 255)
        else:
            ml = mask.convert("L")
            arr = np.asarray(ml) > 0
            W, H = int(mask.size[0]), int(mask.size[1])
        small = ml.resize((W // cell, H // cell), Image.BOX)
        sa = np.asarray(small)
        # 局部极大（四邻域均 ≥）+ 阈值粗筛，向量化与逐格扫描同序（行优先）
        cand = []
        if sa.shape[0] > 2 and sa.shape[1] > 2:
            inner = sa[1:-1, 1:-1]
            loc = (inner >= thr) & (inner >= sa[1:-1, :-2]) \
                & (inner >= sa[1:-1, 2:]) & (inner >= sa[:-2, 1:-1]) \
                & (inner >= sa[2:, 1:-1])
            for i, j in zip(*np.nonzero(loc)):
                cand.append(((j + 1) * cell + cell // 2,
                             (i + 1) * cell + cell // 2, int(sa[i + 1, j + 1])))
        cand.sort(key=lambda p: -p[2])
        spread = []
        for x, y, v in cand:
            if all((x - cx) ** 2 + (y - cy) ** 2 >= sep ** 2 for cx, cy, _ in spread):
                spread.append((x, y, v))

        pts = [(x, y) for x, y, _v in spread[:top]]
        # 环形度整图扫描（v3.9.15）：密度粗筛在放大态会漏细环（环描边
        # 1~2px，8×8 格均值趋近 0，密度排序被麦田大色块碾压），按环形度
        # 直接出候选补上。两路候选 20px 去重后一起进窗口精修。
        # 环形度扫描要整幅前缀和数组（几十 MB），系统内存吃紧时会 MemoryError
        # （实测内存占用 96% 时 2 MB 都申请不到）。两条 numpy 路径都只做增益，
        # 失败就退回密度粗筛候选，识别链路不能因此挂掉。
        try:
            if int(arr.sum()) < 40:                    # 空掩膜直接短路
                return []
            ring_pts = cls._ring_scan_np(ml)
            # 贴边的环单独补一遍：整图扫描要求四象限都有覆盖，被框边裁掉
            # 一侧的标记会被整条丢掉（放大后端点压在框边 → 无轨迹）。
            # 这一路已经按「框内有效点数」归一化过，不再进 ±16 窗口精修
            # （精修同样按固定象限数打分，贴边点会被再次否掉）。
            border_pts = cls._ring_scan_border(ml)
        except MemoryError:
            ring_pts, border_pts = [], []
        merged = []
        for x, y in list(pts) + list(ring_pts):
            if all((x - ox) ** 2 + (y - oy) ** 2 >= cls.MERGE_R ** 2
                   for ox, oy in merged):
                merged.append((x, y))
        pts = merged
        try:
            out = cls._marks_np(ml, pts)
        except MemoryError:
            out = pts              # 退化：不经窗口精修，直接用合并后的粗候选
        # 上下 UI 条带排除：抓帧带上游戏自己的 UI（顶部 FPS/UTC 蓝字、
        # 底部公里/小时进度条），它们的高饱和像素凑得出「环」也串得出
        # 假链（v3.9.14 实测：顶部两个蓝假点串出 126m 假测距）。地图
        # 标记极少贴到上下 5%/9% 边缘，代价可忽略。
        out = [(x, y) for x, y in out
               if H * 0.05 < y < H * 0.91]
        # 贴边环按原样并入（它们没经过窗口精修，坐标就是边界扫描的最优点）
        out = out + [(x, y) for x, y in border_pts if H * 0.05 < y < H * 0.91]
        # 收敛去重：不同候选可能落到同一个环（半径用 MERGE_R，见类注释）
        final = []
        for x, y in out:
            if all((x - ox) ** 2 + (y - oy) ** 2 >= cls.MERGE_R ** 2
                   for ox, oy in final):
                final.append((x, y))
        return final

    @classmethod
    def _marks_np(cls, mask, pts):
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

    # 连线判定门槛（v4.0.3 定，v4.0.15 微调 COVER/SEG，见 docs/design.md）。
    # 都是「较高但不要求 100%」：允许地形遮挡/抗锯齿造成的小缺失，
    # 但不允许线整段消失。真链/假边实测区间见 design.md 的表。
    GATE_DEPART = 0.70    # 端点出线（真链 ≥0.91，假边 0.18）
    # v4.0.15：0.85 → 0.83。实机弱环帧真线覆盖 0.843（地形吃掉几个像素），
    # 差 0.007 被否。端点出线(1.0/0.82)、最长缺口(7.5px)、细描边(1.0) 三层
    # 仍满分，这三层才是假链的主要防线；0.83 留的余量对真链够、对伪直连
    # （实测 0.28~0.65）仍远。
    GATE_COVER = 0.83
    GATE_GAP_PX = 12.0    # 最长连续缺口（真链 ≤5px，伪直连 32.6px）
    # v4.0.15：0.65 → 0.60。同帧终点前一小段命中 0.625 被否；折线拐点处
    # 单段本就偏低（11% 分散缺失 ≈0.71），0.60 给到极端地形的余量。
    GATE_SEG = 0.60
    MAX_LINK = 0.72       # 单段连线最长 = 画面对角线 × 该系数（约 1460px @1440²）

    @classmethod
    def _linked(cls, px, W, H, a, b, ratio=0.72):
        """两点之间是否被**一条真实连线**连着，而非「两点共线」。

        判据按本质到辅助分五层（v4.0.3）：
          ① 端点出线：沿对方方向 12~32px 命中率 ≥0.70。
          ② 全局覆盖：整条线命中率 ≥0.85 —— 允许 15% 的像素因地形遮挡、
             抗锯齿、半透明混色缺失，不要求 100%。
          ③ 最长连续缺口 ≤12px：这是「缺失分散」与「线断了」的分界。
             真链小缝只有 2.5~5px；孤立人物点造成的伪直连有 32.6px 空档。
          ④ 分段连续：按 ~40px 切段，每段 ≥0.65（给局部遮挡留余量：
             模拟 11% 分散缺失后单段 ≈0.71，0.70 会卡边）。
          ⑤ 细描边：法向 ±5/±7px 不命中，far_ok ≥0.72。

        只设一个全局高阈值不够：那条伪直连的全局覆盖正好是 0.89，
        和「89% 有连线的真线」在全局口径上无法区分；靠 ③④ 才能分开。
        """
        ax, ay = a
        bx, by = b
        d = math.hypot(bx - ax, by - ay)
        if d < 24:
            return False
        n = max(16, int(d / 2.5))
        dx, dy = bx - ax, by - ay
        ux, uy = dx / d, dy / d                # 主线单位方向
        nx, ny = -uy, ux                       # 主线法向

        def band(x, y, vx, vy, half=3):
            """沿法向 ±half 查掩膜：只认宽度 ≤ 2*half+1 的窄带。

            旧版用 7×7 方块（49 个像素），等于允许采样点偏离 3px 还能命中，
            旁边的地形斑块很容易被算成连线 —— 伪直连 A→C 因此有 0.894。
            换成法向窄带（沿 vx,vy 走、横向只查 7 个像素）后同一帧：
            真线 0.981→0.952（几乎无损，线本来就压在带内），
            伪直连 0.894→0.648（过不了 GATE_COVER 0.85）。
            """
            wx, wy = -vy, vx                   # 该方向的法向
            for s in range(-half, half + 1):
                xx = int(round(x + wx * s))
                yy = int(round(y + wy * s))
                if 0 <= xx < W and 0 <= yy < H and px[xx, yy]:
                    return True
            return False

        # ① 端点出线（从 r=12 起，避开半径 11 的标记环本身）
        def depart(P, Q):
            # 用 P→Q 自己的长度归一化。旧版误用主线长度 d：A→B 时单位
            # 向量只有 0.27 长，r=12..32 实际只走 3~9px，全落在标记环上。
            # 再在 ±30° 内取最佳方向：折线拐点处，出线方向是相邻段的走向，
            # 不一定沿着 P→Q（004041 帧 A 点沿 P→Q 只有 0.55，偏 15° 就满分）。
            L = math.hypot(Q[0] - P[0], Q[1] - P[1]) or 1.0
            vx, vy = (Q[0] - P[0]) / L, (Q[1] - P[1]) / L
            best = 0.0
            for deg in (-30, -15, 0, 15, 30):
                t = math.radians(deg)
                wx = vx * math.cos(t) - vy * math.sin(t)
                wy = vx * math.sin(t) + vy * math.cos(t)
                hits = [band(int(round(P[0] + wx * r)), int(round(P[1] + wy * r)),
                             wx, wy) for r in range(12, 33, 2)]
                best = max(best, sum(hits) / len(hits))
            return best
        if depart(a, b) < cls.GATE_DEPART or depart(b, a) < cls.GATE_DEPART:
            return False

        mask_hit = far_ok = far_total = 0
        oks = []
        for i in range(n + 1):
            t = i / n
            x = int(round(ax + dx * t))
            y = int(round(ay + dy * t))
            ok = band(x, y, ux, uy)
            oks.append(ok)
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
        # ② 全局覆盖：不要求 100%，但缺得太多就不是一条线
        cover = mask_hit / (n + 1)
        if cover < cls.GATE_COVER:
            return False
        # ③ 最长连续缺口：区分「零散小缝」与「整段没线」
        run = max_run = 0
        for ok in oks:
            run = 0 if ok else run + 1
            max_run = max(max_run, run)
        if max_run * d / n > cls.GATE_GAP_PX:
            return False
        # ④ 分段连续：任一段断了就不是一条线，全局平均再高也不认
        K = max(4, int(d / 40))
        seg_hit, seg_tot = [0] * K, [0] * K
        for i, ok in enumerate(oks):
            k = min(K - 1, i * K // len(oks))
            seg_tot[k] += 1
            seg_hit[k] += ok
        if any(seg_hit[k] / max(1, seg_tot[k]) < cls.GATE_SEG for k in range(K)):
            return False
        # ⑤ 细描边（远点不足 3 个时退回纯掩膜口径）
        if far_total >= 3:
            return far_ok / far_total >= ratio
        return cover >= ratio

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
                # 上限别按固定 1000px：放大后一段真实连线就能到 1020px
                # （实测 zoom16 那条青线 1020px，被 1000 卡掉 → 无轨迹）。
                # 按画面对角线归一，防止的是「跨整幅的荒谬长边」，
                # 真假分辨本来就交给 _linked 的五层判据。
                if d < 22 or d > cls.MAX_LINK * math.hypot(W, H):
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

    # 「先阈值化挑点」这一步由 marks() 的 HSV + 环形度打分完成：候选必须
    # 是队伍色圆环（四象限均衡 / 空心内核），实心色块和地形边界都进不来。
    # 不额外按「实心 vs 空心」砍点 —— 实机里画线端点也可能是带白色数字的
    # 实心标记（231937 帧橙线两端就是），按内核暗度硬筛会把真轨迹删掉。
    # 人物点与轨迹点的区别只能由**两点之间有没有一段完整的线**来定，
    # 交给 _linked 的端点出线 + 分段连续性判定。

    @classmethod
    def detect_teams(cls, im, slots=4):
        """按队伍色各认各的标号轨迹：返回 {队伍序号(0基): 最长链}。

        同一队友可能画了多段标记，取首尾距离最长的一段（最可能是当前
        有效的测距段）。没认到的队伍不出现在返回值里。

        现行管线（2026-11-16 与用户共同确定，旧算法只留环打分定位零件）：
        1. 定位 = 队伍色环打分（颜色只用来找环，不用来分类队伍）∪ 暗盘
        2. 分类 = 盘上白色内容**双身份**（marker_detect）：
           数字 1~4（队伍号）/ 箭头（连线延伸方向）。屏幕口径数字只有
           4×8px，与拉伸箭头模板 IoU 不可分（0.69 vs 0.75），单点强分类
           必错 —— 两种身份都保留，裁决交给成链几何约束。
        3. 成链 = 三重约束的 DFS：
           a. 链两端必须持有**相同数字**（队伍分类唯一标准 + 假链过滤）
           b. 中间节点的箭头方向与连线方向一致（±25°）
           c. 两点间连线确实存在（段支持率，**阈值从松** 0.60 —— 方向与
              数字先验已经排除了绝大多数假边，连线只是加强证据）
        4. 队伍 = 链端点数字 - 1（数字 n → 槽位 n-1，0 基）
        """
        out = {}
        hsv = np.asarray(im.convert("HSV"))   # (H, W, 3) uint8，定位/连线掩膜共用
        nslots = min(4, slots)

        # ── 1. 定位：环打分（定位用）为主 ──
        # find_nodes 暗盘候选只做补充：地形暗斑太多，全量进分类会刷出
        # 几百个假数字（两端同号凑假链）。仅当环候选 <2 时启用。
        cands = []
        for ti in range(nslots):
            cands.extend(cls.marks(cls.mask_for(im, ti, hsv=hsv)))
        pts = []
        for cx, cy in cands:
            if all((cx - ox) ** 2 + (cy - oy) ** 2 >= 8 ** 2 for ox, oy in pts):
                pts.append((cx, cy))
        if len(pts) < 2:
            try:
                extra = TP.find_nodes(im)
                for cx, cy in extra:
                    if all((cx - ox) ** 2 + (cy - oy) ** 2 >= 8 ** 2
                           for ox, oy in pts):
                        pts.append((cx, cy))
            except Exception:
                pass
        if len(pts) < 2:
            return out

        # ── 2. 内容双身份分类 ──
        try:
            ids = TP.classify(im, pts)
        except Exception:
            return out
        idx = {p: i for i, p in enumerate(pts)}
        nodes = [idx[p] for p in ids if p in idx]
        if len(nodes) < 2:
            return out

        # ── 3. 线掩膜 + 段支持率 ──
        # 邻接判定用 la = 队伍线 ∪ 细结构白线；穿线判定只用队伍线
        # （la_team）。白色 UI 线兜底必须做**细结构过滤**：亮地形
        # （白沙岩/岩石反光）整片 V>200 低饱和，一帧上百万白像素，
        # 不过滤会伪造邻接（玩家标记互连）并让终点穿线判定误命中；
        # 真细白线 9×9 局部密度只有 0.2~0.4，地形整片 ≥0.8。
        masks = [cls.mask_for(im, ti, line=True, hsv=hsv) for ti in range(nslots)]
        la_team = masks[0]
        for m in masks[1:]:
            la_team = la_team | m
        la = la_team
        wl = (hsv[:, :, 2] > 200) & (hsv[:, :, 1] < 60)
        if wl.any():
            H2, W2 = la.shape
            iw = np.zeros((H2 + 1, W2 + 1), np.int32)
            iw[1:, 1:] = np.cumsum(np.cumsum(wl, axis=0), axis=1)
            ysc = np.arange(H2)[:, None]
            xsc = np.arange(W2)[None, :]
            ya = np.clip(ysc - 4, 0, H2)
            yb = np.clip(ysc + 5, 0, H2)
            xa = np.clip(xsc - 4, 0, W2)
            xb = np.clip(xsc + 5, 0, W2)
            box = iw[yb, xb] - iw[ya, xb] - iw[yb, xa] + iw[ya, xa]
            dens = box / np.maximum(1, (yb - ya) * (xb - xa))
            la = la | (wl & (dens <= 0.5))
        H2, W2 = la.shape
        ii = np.zeros((H2 + 1, W2 + 1), np.int32)
        ii[1:, 1:] = np.cumsum(np.cumsum(la, axis=0), axis=1)

        def seg_support(a, b):
            """线段存在性（细线判据）：采样点 5×5 框内队伍色命中比例
            0 < fill ≤ SEG_FILL_MAX 才算命中。

            真连线是 2~3px 细线（斜穿 5×5 框命中 ~0.2~0.6）；队友大盘
            （40px 实心同队色）内部 fill=1.0 —— zoom8_green_line 的四个
            大盘互相紧挨，旧 >0 判据让盘体伪边串成假链。盘腹采样点
            miss，但真边大部分点在细线上，support 仍 ≥ 阈值；大盘互连
            伪边整条都在盘腹 → 死。

            采样点一次性向量化：坐标公式、int() 截断（坐标非负，astype
            同为向零截断）、clip 边界与 fill 判据逐点同旧循环。
            """
            d = math.hypot(b[0] - a[0], b[1] - a[1])
            n = max(6, int(d // 4))
            t = np.arange(n + 1) / n
            x = (a[0] + (b[0] - a[0]) * t).astype(np.int64)
            y = (a[1] + (b[1] - a[1]) * t).astype(np.int64)
            xa = np.clip(x - 2, 0, W2)
            xb = np.clip(x + 3, 0, W2)
            ya = np.clip(y - 2, 0, H2)
            yb = np.clip(y + 3, 0, H2)
            fill = (ii[yb, xb] - ii[ya, xb] - ii[yb, xa] + ii[ya, xa]) \
                / np.maximum(1, (yb - ya) * (xb - xa))
            hit = int(((fill > 0) & (fill <= cls.SEG_FILL_MAX)).sum())
            return hit / (n + 1)

        max_d = cls.MAX_LINK * math.hypot(W2, H2)
        adj = {i: set() for i in nodes}
        for i in nodes:
            for j in nodes:
                if j <= i:
                    continue
                d = math.hypot(pts[i][0] - pts[j][0], pts[i][1] - pts[j][1])
                if d < 24 or d > max_d:
                    continue
                # 连线存在性（形态学加强，阈值从松 0.60）
                if seg_support(pts[i], pts[j]) >= cls.SEG_SUPPORT:
                    adj[i].add(j)
                    adj[j].add(i)

        # ── 4. 有向图组链：连线存在（形态学加强）+ 箭头**单向**指向 ──
        # 箭头语义（用户钦定 + zoom4 实测 40° vs 实际 37°）：每个环只指向
        # **下一个**环，不知道上一个环在哪 —— 单向，不允许 ±180°。
        # 链结构：起点是箭头节点（第一个路径点，无数字），中间箭头节点
        # 逐段指向前方，**终点必须是数字节点**（数字定队伍）。
        def ang(u, v):
            return math.degrees(math.atan2(pts[v][1] - pts[u][1],
                                           pts[v][0] - pts[u][0])) % 360.0

        def arrow_of(node):
            """node 的全部箭头身份角度（屏幕方位角，0=右 90=下）。"""
            return [val for kind, val, _ in ids[pts[node]] if kind == "arrow"]

        def arrow_hit(node, a0):
            """node 的某箭头指向 a0 方向（±容差）。"""
            for val in arrow_of(node):
                diff = abs(val - a0)
                diff = min(diff, 360.0 - diff)
                if diff <= cls.ARROW_TOL_DEG:
                    return True
            return False

        def line_continues(nxt, cur):
            """链是否越过 nxt 继续前进（穿线点判定，邻接方向锥版）。

            终点判据（用户钦定）：玩家/队友位置标记恰好**压在轨迹线
            中间**，线从它身上穿过去；真结尾的线到盘为止。判据：存在
            邻接边 (nxt, j)（j ≠ cur）其方向与行进方向 ang(cur→nxt)
            夹角 ≤ PASS_TOL_DEG → 线越过 nxt 继续走。直线穿过的队友
            标记（出边 ≈ 行进方向）与折线拐点（出边偏转 ≤55°，
            zoom8_polyline D 点实测入 67° 出 113°）都算穿线；只指向
            身后（≈180° 反向入线）不算 —— 真结尾的所有邻接边都指向
            身后。用邻接边几何而不是箭头身份：数字斜笔蹭出的 bogus
            箭头（zoom8_green E 点 155° 恰好对准入线反方向）不会误杀
            真终点。
            """
            a0 = ang(cur, nxt)
            for j in adj[nxt]:
                if j == cur:
                    continue
                diff = abs(ang(nxt, j) - a0)
                diff = min(diff, 360.0 - diff)
                if diff <= cls.PASS_TOL_DEG:
                    return True
            return False

        def digits(node, u):
            """node 在入边 u→node 下的**有效终点数字**。

            仲裁规则（用户钦定，两道闸）：
            1. 穿线裁决：线越过节点继续延伸 → 它是穿线点（队友位置标记
               压在线中间 / 中间点被数字模板蹭分，zoom8_polyline D 点），
               数字身份作废、链继续走；线到盘为止 → 才轮到数字当结尾。
            2. 双身份裁决：节点自身有箭头身份、且箭头指向锥内**存在邻接
               边**（zoom8_green n4：真 chevron 358° 对准出边 354.7°）→
               它是方向中间节点，数字是模板蹭分（n4 bogus digit3@0.582），
               作废。真终点数字往往也有字形蹭出的箭头（n5 digit"4" 蹭出
               178° 恰好对准入边方向），会误杀 —— 所以只作废**弱数字**
               （NCC < DIGIT_STRONG；真数字屏幕模板口径普遍 0.8+，
               bogus 蹭分 ≤0.6）。
            """
            ds = [(val, sc) for kind, val, sc in ids[pts[node]]
                  if kind == "digit"]
            if not ds:
                return []
            if line_continues(node, u):
                return []              # 线穿过去 → 中间点，数字作废
            strong = any(sc >= cls.DIGIT_STRONG for _, sc in ds)
            if not strong and arrow_of(node):
                for j in adj[node]:
                    if arrow_hit(node, ang(node, j)):
                        return []      # 箭头锥内有连线 → 方向节点，数字作废
            return ds

        # 有向边：u→v = 两点间连线存在 且 u 的箭头指向 v。
        # 例外（链起点，用户钦定 stacked_marker 实测结构）：玩家位置
        # 大盘（链首）没有箭头 —— 线直接从大盘连到下一个标记。但
        # 起点资格必须是**强 digit**（真位置大盘的大号数字 NCC 0.8+）：
        # 放开到任意节点会让名牌/大盘群污染块（zoom4 n6 digit2@0.582）
        # 和反向蹭分链（zoom10p7_faint [n6→n1]）起假链。
        directed = {i: set() for i in nodes}
        for i in nodes:
            for j in adj[i]:
                if arrow_hit(i, ang(i, j)):
                    directed[i].add(j)

        def strong_digit(i):
            return any(sc >= cls.DIGIT_STRONG
                       for kind, _, sc in ids[pts[i]] if kind == "digit")

        chains = []                   # (path节点, [(digit, score)], 有箭头驱动, dist)
        max_len = 5                   # 链长上限（防爆炸；终点数字不算中间点）

        def dfs(cur, path):
            if len(path) > max_len:
                return
            if len(path) > 1 or not strong_digit(cur):
                nxts = directed[cur]
            else:                     # 强 digit 起点：第一步免箭头
                nxts = adj[cur]
            for nxt in nxts:
                if nxt in path:
                    continue
                new_path = path + [nxt]
                ds = digits(nxt, cur)
                if ds:
                    # 收链：数字一定是结尾 —— 终点数字定队伍，不再延伸
                    d = math.hypot(pts[nxt][0] - pts[path[0]][0],
                                   pts[nxt][1] - pts[path[0]][1])
                    if d >= 45:
                        drive = any(arrow_hit(new_path[k], ang(new_path[k], new_path[k + 1]))
                                    for k in range(len(new_path) - 1))
                        chains.append((new_path, ds, drive, d))
                    continue
                # nxt 是中间节点：必然有箭头（否则不会有出边），继续走
                dfs(nxt, new_path)

        for s in nodes:
            dfs(s, [s])

        # 链级方向仲裁：同一条几何路径（节点集相同）双向都能成链
        # （无箭头起点让链可反向走），真方向只有一条 ——
        #   1. 有箭头沿前进方向驱动的链优先（真链的箭头指向下一环）；
        #   2. 都没有箭头驱动时（位置大盘 → 数字终点），取终点数字
        #      分数更高的方向（zoom8_stacked：正向收 digit2@1.0，
        #      反向收 digit1@0.566 —— 反向是终点蹭分）。
        groups = {}
        for path, ds, drive, d in chains:
            key = frozenset(pts[i] for i in path)
            groups.setdefault(key, []).append((path, ds, drive, d))
        cand = {}                     # digit -> [(drive, dist, path)]
        for lst in groups.values():
            driven = [c for c in lst if c[2]]
            if driven:
                pick = max(driven, key=lambda c: c[3])
            else:
                pick = max(lst, key=lambda c: max(s for _, s in c[1]))
            path, ds, drive, d = pick
            for dn, _ in ds:
                cand.setdefault(dn, []).append((drive, d, path))
        # digit 槽竞争（跨路径）：有箭头驱动的链优先 —— 箭头指向是
        # 用户钦定的前进证据；无驱动链（纯连线）只在没有任何驱动链
        # 时兜底（zoom10p7_faint：真链 [n5→n6] drive 399px 压过反向
        # 无驱动 554px；zoom4：[n3→n4] drive 258px 压过名牌污染起点
        # [n6→n3→n4] drive 366px —— 优先级高于 dist）。
        best = {}                     # digit -> (dist, chain)
        for dn, lst in cand.items():
            pool = [c for c in lst if c[0]] or lst
            d, path = max((c[1], c[2]) for c in pool)
            best[dn] = (d, [pts[k] for k in path])

        return {dn - 1: c for dn, (_, c) in best.items()}   # 数字 n → 槽位 n-1

    # 成链参数：连线验证阈值从松（方向/数字先验已排除绝大多数假边）；
    # 箭头方向容差（zoom4 实测: 匹配 45° vs 连线 37°，误差 8°）；
    # 穿线判定容差（zoom8_polyline D 拐点实测：入 67° 出 113°，偏转 46°）
    SEG_SUPPORT = 0.60
    # 细线判据上限：采样 5×5 框内队伍色命中比例 ≤0.7 视为「细线」，
    # 大于它是实心色块（队友大盘本体），不算连线证据（见 seg_support）
    SEG_FILL_MAX = 0.70
    ARROW_TOL_DEG = 25.0
    PASS_TOL_DEG = 55.0
    # 双身份仲裁的强数字下限：真数字（屏幕模板 NCC）普遍 0.8+，
    # bogus 蹭分（zoom8_green n4 digit3）≤0.6 —— 低于它且箭头锥内
    # 有连线时数字作废（见 digits()）。
    DIGIT_STRONG = 0.70

    @classmethod
    def detect(cls, im, max_chains=4):
        """（旧路径）黄色通认：返回按位置排序的若干条链。
        色盲模式 ≠ Normal 时标记色变体不同，退回这条通道。"""
        mask = cls.mask(im)
        pts = cls.marks(mask)
        if len(pts) < 2:
            return []
        return cls._chains(pts, mask, max_chains)
