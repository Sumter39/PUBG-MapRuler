# -*- coding: utf-8 -*-
"""网格测距：从屏幕像素读 PUBG M 地图自带网格，换算「米/逻辑像素」。"""
import math

import numpy as np
from PIL import Image, ImageChops


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

    # 细线二值化阈值（v4.0.13：12 → 5，理由见 _verify 与 docs/design.md）
    VERIFY_THR = 5

    # 去曝光曲线（v4.0.7）：高光软肩 + gamma。桌面复制拿到的帧常常大片
    # 削顶（实机 01:55 帧 31% 的像素三通道全 ≥245，网格白线与浅色地形压成
    # 同一片白），削顶不可逆，但中间调还能拉开。这张 LUT 把 0.72 以上的
    # 高光按二次曲线压下来、再对中间调做 gamma 1.28 提对比，纯白占比从
    # 31% 降到 0%，同一帧的网格从「无网格」变回 288 px。三个通道用同一条
    # 曲线，色相基本不变，不影响队伍色分带。
    #
    # 用法有两条：抓图时按削顶程度决定是否套用（见 MainApp._grab_map，
    # 保存下来的截图就不再是过曝的）；识别通路在常规探测失败时再兜底一次。
    _DEHAZE_T = 0.72
    _DEHAZE_K = 1.55
    _DEHAZE_G = 1.28

    @classmethod
    def _dehaze_lut(cls):
        lut = getattr(cls, "_LUT", None)
        if lut is None:
            lut = []
            for i in range(256):
                x = i / 255.0
                s = x - cls._DEHAZE_K * (x - cls._DEHAZE_T) ** 2 \
                    if x > cls._DEHAZE_T else x
                s = min(1.0, max(0.0, s))
                lut.append(int(round((s ** cls._DEHAZE_G) * 255)))
            cls._LUT = lut
        return lut

    @classmethod
    def exposure_stretch(cls, im, lo=1.0, hi=99.0):
        """抓图时的曝光拉伸：按亮度分位把整幅拉到 0~255，色相不变。

        和分通道 autocontrast 的区别：三个通道用**同一条** LUT（增益/偏移
        一致），所以不会偏色 —— 队伍标记的橙/青/黄不会因为拉伸互相串色。
        削顶的像素拉不回来，但整幅被白蒙层压扁的中间调能拉回全量程，存下来
        的截图才和肉眼看到的一致。

        返回 (新图, p_lo, p_hi)。
        """
        rgb = im.convert("RGB")
        h = rgb.convert("L").histogram()
        total = max(1, sum(h))
        acc, p_lo, p_hi = 0, 0, 255
        for i, c in enumerate(h):
            acc += c
            if acc >= total * lo / 100.0:
                p_lo = i
                break
        acc = 0
        for i, c in enumerate(h):
            acc += c
            if acc >= total * hi / 100.0:
                p_hi = i
                break
        if p_hi - p_lo < 30:                  # 已经铺满量程，别硬拉
            return rgb, p_lo, p_hi
        gain = 255.0 / (p_hi - p_lo)
        lut = [max(0, min(255, int(round(i * gain - p_lo * gain))))
               for i in range(256)]
        return rgb.point(lut * 3), p_lo, p_hi

    @staticmethod
    def white_ratio(im):
        """三通道都 ≥245 的像素占比 —— 真·削顶，判断要不要压高光就用它。

        取三通道逐像素最小值（`darker` 两次），再看这个「最暗通道」有多少
        像素 ≥245：三个通道同时贴顶才是削顶，单通道到 255（纯蓝天空、纯黄
        标记）不算。
        """
        r, g, b = im.convert("RGB").split()
        m = ImageChops.darker(ImageChops.darker(r, g), b)
        h = m.histogram()
        return sum(h[245:]) / float(max(1, sum(h)))

    @classmethod
    def _canvas(cls, im, dehaze=False, full=False):
        """去掉边缘罗盘 / 队友栏后的地图画幅（剖面与直线校验共用同一块）。

        full=False（默认）：抓图是整屏矩形（2560×1440），按**居中**取
        min(W,H) 的方块；正方形帧时 cx=cy=0，与旧版逐像素一致。
        full=True：整幅只去掉上下 UI 带（与 marks 同口径），供 probe 的
        整幅兜底 —— 超深档 100m 格整屏只有 3 条线，居中方块会把两侧的
        线裁掉。

        dehaze=True 时先过一遍去曝光曲线：实机抓帧常常大片削顶（31% 的像素
        三通道全 ≥245），网格白线与浅色地形压成同一片白。削顶不可逆，但把
        高光压回可用范围后，弱网格才找得回来。**只在常规探测失败时才用**
        （见 probe 的兜底），因为压高光也会削弱某些浅色地形上的白线对比度。
        """
        W, H = im.size
        if full:
            rgb = im.convert("RGB").crop((0, int(H * 0.05), W, int(H * 0.91)))
        else:
            S = min(W, H)
            pad = int(S * cls.PAD_RATIO)
            cx, cy = (W - S) // 2, (H - S) // 2
            rgb = im.convert("RGB").crop((cx + pad, cy + pad,
                                          cx + S - pad, cy + S - pad))
        if dehaze:
            rgb = rgb.point(cls._dehaze_lut() * 3)
        return rgb

    @classmethod
    def _profiles_from(cls, rgb):
        """B 通道 + 亮度两套剖面。

        实测：1 km 黑线在蓝通道被压低约 100/255，而亮度里 B 只占 0.114，
        按亮度算只剩 ~11 的落差，雪地/水面上容易淹掉；反过来 100 m 白线在
        亮度上最明显。所以黑线优先看 B，白线优先看亮度，互为备选。

        关于「用网格线的真实颜色建轴」（v4.0.9 试过，结论是不成立，别再走）：
        `MapGridWidget` 的 CDO 给出 `ColorSmallGrid = (0.887,0.887,0.887)`
        中性灰白、`ColorLargeGrid = (0.034,0.034,0.122) A=0.75` 近黑蓝。但实机
        帧逐像素量「线像素 − 左右各 4 px 均值」后发现增量本身是**中性**的：
        亮线归一化 (+0.88~+1.00, +0.88~+1.00, +1.00)、暗线 (−1.00, −0.99,
        −0.95)，三通道同向等幅、极差只占幅度 3%~9%。线只是把局部地形整体
        抬亮/压暗，不改色相（半透明叠加 + 色调映射），所以颜色距离没有区分力
        ——绿草地上的线是 #A4A187、雪地 #E4EDFF、沙漠 #FFF9D8，离固定色都远。
        细节见 docs/design.md「网格线颜色实测」。
        """

        def prof(img):
            # tobytes() 比 getdata() 快且不触发 Pillow 的弃用告警（单通道图即 0~255）
            col = img.resize((img.width, 1), Image.BOX).tobytes()
            row = img.resize((1, img.height), Image.BOX).tobytes()
            return col, row

        return prof(rgb.split()[2]), prof(rgb.convert("L"))

    @classmethod
    def _verify(cls, rgb, period, sign, ratio=0.55):
        """核对某个周期是否真的对应「横平竖直、贯穿地图」的直线（v4.0.6）。

        前面几步都是把二维画面压成一维剖面求周期，地形纹理一旦有条纹/斑块
        的重复感，剖面里就出现假周期（实测：赛前地图蒙层下锁到 13.09 px，
        被当成 100 m 格 → 397 px 的线算成 3031 m，实际约 2206 m）。而地图
        网格的本质是**直**线：竖线在整列上同色、横线在整行上同色，且贯穿
        整张地图；地形条纹做不到同一条像素列上「一条接一条」。

        做法：对候选周期，把「细线响应」（本像素比左右各 4 px 的均值亮/暗
        多少）二值化，按列（或行）求平均得到 0~1 的贯穿率，再在整周期内
        找最佳相位，取所有直线上贯穿率的中位数。

        实测 20 张留帧：真网格 0.67~0.97（最差是 450 m 那帧的浅色地形），
        假周期 0.16~0.41。门槛 0.55 两侧都留了余量。
        """
        W, H = rgb.size
        # 细线二值化阈值（v4.0.13：12 → 5）。放大后格子线落在海洋/浅色
        # 地形上时，「线 − 左右4px均值」的响应只有 5~10，被 12 卡掉 →
        # 整列贯穿率 0.488 过不了 0.55 门槛，明明格子在却判「无网格」。
        # 实测 thr=5：真周期 0.63~0.83、地形假周期(13~17px) 0.31~0.41，
        # 0.55 门槛两侧仍有余量，扶回淡线不误认纹理。
        thr = cls.VERIFY_THR
        top = 0.0
        for source in ("L", "B"):
            src = rgb.convert("L") if source == "L" else rgb.split()[2]
            for vertical in (True, False):
                if vertical:
                    a = ImageChops.offset(src, 4, 0)
                    b = ImageChops.offset(src, -4, 0)
                else:
                    a = ImageChops.offset(src, 0, 4)
                    b = ImageChops.offset(src, 0, -4)
                nb = ImageChops.add(a, b, scale=2.0)
                resp = (ImageChops.subtract(src, nb) if sign > 0
                        else ImageChops.subtract(nb, src))
                binm = resp.point(lambda v: 255 if v >= thr else 0)
                one = (binm.resize((W, 1), Image.BOX) if vertical
                       else binm.resize((1, H), Image.BOX))
                prof = [v / 255.0 for v in one.tobytes()]
                n = len(prof)
                best = 0.0
                for ph in range(max(1, int(round(period)))):
                    vals = []
                    k = 0
                    while ph + period * k <= n - 1:
                        i = int(round(ph + period * k))
                        vals.append(max(prof[j] for j in (i - 1, i, i + 1)
                                        if 0 <= j < n))
                        k += 1
                    if len(vals) < 2:
                        break
                    c = sum(vals) / len(vals)
                    if c > best:
                        best = c
                if best > top:
                    top = best
                    if top >= ratio:
                        return top
        return top

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
    def _refit(cls, peaks, period, tol=2.2, rounds=3):
        """把峰集收缩到「与周期一致」的内点，再用内点重拟合（v4.0.7）。

        剖面里真线往往只有 4 根，地形噪声峰却有十几根。旧版 `_solve` 把两者
        平等喂进最小二乘：rms 被噪声拉到 40+、内点率掉到 0.2，真周期直接被否。
        实测过曝帧里 114/402/690/978 四条真线精确 288 间距（峰值 13~28），
        却被判「无网格」——信息没丢，是拟合器把噪声当成了等权样本。

        现在改成：按粗周期给每个峰算 k=round((x−a)/p)，只留 |残差| ≤ tol 的
        内点，用内点做 LS 精修，迭代两三轮。返回
        (周期, 相位, 内点 rms, 内点数, 跨度, 内点幅度中位数)。
        """
        if period <= 0:
            return None
        xs = [p for _, p in peaks]
        if len(xs) < 2:
            return None
        a = float(min(xs))
        b = float(period)
        picked = []
        for _ in range(max(1, rounds)):
            picked = []
            for amp, x in peaks:
                k = int(round((x - a) / b))
                if abs(x - (a + b * k)) <= tol:
                    picked.append((k, x, amp))
            if len(picked) < 2:
                return None
            n = len(picked)
            sk = sum(k for k, _, _ in picked)
            sx = sum(x for _, x, _ in picked)
            skk = sum(k * k for k, _, _ in picked)
            skx = sum(k * x for k, x, _ in picked)
            den = n * skk - sk * sk
            if abs(den) < 1e-9:
                return None
            b_new = (n * skx - sk * sx) / den
            if b_new <= 0:
                return None
            a_new = (sx - b_new * sk) / n
            a, b = a_new, b_new
        devs = [abs(x - (a + b * k)) for k, x, _ in picked]
        rms = (sum(dv * dv for dv in devs) / max(1, len(devs))) ** 0.5
        ks = [k for k, _, _ in picked]
        span = b * (max(ks) - min(ks)) if len(set(ks)) > 1 else 0.0
        amps = sorted(p for _, _, p in picked)
        amp_med = amps[len(amps) // 2]
        return b, a, rms, len(picked), span, amp_med

    @classmethod
    def _solve(cls, d, peaks, min_period, dur, limit=3):
        """给定峰位集合，投票 + 拟合求基本周期。"""
        if len(peaks) < 2:
            return None
        xs = [p for _, p in peaks]

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

        # 逐个候选周期算「占位率」再择优（v4.0.7）：
        # 占位率 = 落在点阵上的峰数 / 这段跨度里点阵该有的位置数。
        # 真周期所有位置都有线（占位率≈1）；真周期的 1/3、1/4 这种「公约数」
        # 虽然也能罩住同样的峰，但会空出 2/3 的位置，占位率掉到 0.3~0.5。
        # 旧版取「升序第一个能拟合的」，过曝帧上先撞到 96 px（288 的公约数）
        # 就把真周期顶掉了。
        best = []
        for per, cnt in sorted(cand.items()):
            if per < min_period * 0.97:
                continue
            rough = cls._fit(xs, float(per))
            if not rough:
                continue
            r = cls._refit(peaks, rough[0])
            if not r:
                continue
            period, _a, rms, n_in, span, amp_in = r
            if period < min_period * 0.97:
                continue
            if n_in >= 3:
                if rms > 1.1 or span < period * 2.0:
                    continue
            else:                                   # 只剩两条线：证据很弱
                # 深放大时 1km 大格整屏只有 2 根，且渲染细、幅度降到 32~39
                # （v3.9.18 实测：720px 间距两根线 amp 32/39 被 45 卡死 →
                # 网格量不到 → 测距被闸门拦成「无轨迹」）。地形噪声峰只有
                # 6~8，26 仍分得干净；两线间距即周期，靠比值校验兜底。
                if rms > 0.9 or amp_in < 26 or span < period * 0.95:
                    continue
            sites = max(1, int(round(span / period)) + 1)
            occupancy = n_in / float(sites)
            score = occupancy * min(1.0, n_in / 4.0)
            conf = (min(1.0, amp_in / 40.0) * min(1.0, n_in / 6.0)
                    * max(0.0, 1.0 - rms / 2.0) * min(1.0, span / dur)
                    * min(1.0, occupancy + 0.25))
            best.append((score, period, conf, n_in, span))
        best.sort(key=lambda x: -x[0])
        out = []
        for row in best:                            # 周期相差 <3% 视为同一个，只留最高分
            if all(abs(row[1] - q[1]) > 0.03 * row[1] for q in out):
                out.append(row)
            if len(out) >= limit:
                break
        return [(p, c, n, s) for _, p, c, n, s in out]

    @classmethod
    def _lattice(cls, d, sign, min_period, limit=3):
        """在整条剖面里找候选周期（不预设缩放），按占位率从高到低返回。

        剖面里除了网格线，还有幅度只有真线十分之一左右的地形小峰；它们会把
        拟合搅乱（真线明明在、却拟合不出来）。所以先用「强峰」求解，不够再
        逐级放宽。完全没有网格时强峰幅度也很低（< 25），直接判失败。

        v4.0.7 起返回**多个**候选：占位率最高的未必是真周期（过曝帧上
        288 的公约数 96 会先拿高分），交给 `_verify` 用「横平竖直」去裁决。
        """
        raw = cls._peaks(d, sign, 6.0)
        if len(raw) < 2:
            return []
        top = max(v for v, _ in raw)
        if top < 18.0:                     # 没有像样的线，别猜
            return []                       # （v3.9.14: 22→18，白细线实测 21.4
                                            #   被 22 卡死，改由 _solve 自身校验兜底）
        dur = float(max(1, len(d)))
        for frac in (0.45, 0.22):
            peaks = [pk for pk in raw if pk[0] >= max(6.5, top * frac)]
            if len(peaks) < 2:
                continue
            r = cls._solve(d, peaks, min_period, dur, limit=limit)
            if r:
                return r
        return []


    @classmethod
    def probe(cls, im, km_size, side):
        """常规探测 → 去过曝 → 整幅兜底，三条路都拿不到才算「无网格」。

        常规通路对所有历史帧已验证过，不能随便动；过曝帧上它拿不到网格，
        这时才把高光压回可用范围重测（v4.0.7）。

        整幅兜底（v4.0.14）：抓图改为整屏矩形后，默认 _canvas 居中取
        min(W,H) 方块；zoom42 超深档 100m 格 = 768px，方块裁完只剩 2 条
        竖线、幅度 19.6 被两线分支的 amp_in≥26 卡掉 → 判「无网格」。但
        整幅宽 2560px 上有 3 条 768px 间距的竖线（718/1486/2254，_verify
        0.98），网格线明明在、只是被居中裁掉了。常规+去曝都失败时，对
        宽屏帧整幅（只去掉上下 UI 带）再探一次；附加约束 minor≥120px，
        防止整幅里冒出的地形假周期（13~17px）被当成小格。
        """
        r = cls._probe(im, km_size, side, dehaze=False)
        if r.get("conf", 0.0) >= 0.12 and r.get("source") != "估算":
            return r
        r2 = cls._probe(im, km_size, side, dehaze=True)
        if r2.get("conf", 0.0) >= 0.12 and r2.get("source") != "估算":
            return r2
        W, H = im.size
        if W > H:                       # 只有整屏矩形帧才需要整幅兜底
            for dehaze in (False, True):
                r3 = cls._probe(im, km_size, side, dehaze=dehaze, full=True)
                if (r3.get("conf", 0.0) >= 0.12
                        and r3.get("source") != "估算"
                        and r3.get("minor", 0.0) >= 120.0):
                    return r3
        return r

    @classmethod
    def _probe(cls, im, km_size, side, dehaze=False, full=False):
        side = max(1.0, float(side))
        rgb = cls._canvas(im, dehaze=dehaze, full=full)
        (bc, br), (lc, lr) = cls._profiles_from(rgb)
        sB = (cls._signed(bc), cls._signed(br))
        sL = (cls._signed(lc), cls._signed(lr))

        # 满图时 1 km = side/kmSize 像素。但 M 地图面板不保证铺满整块方屏
        # （维寒迪实测：整图在屏时大格 180 px，而 side/kmSize = 240 px），
        # 所以这个值只当「量级参考」，不拿来当前置下限。
        base = side / max(1.0, float(km_size))

        vseen = {}

        def verified(period, sign):
            """同一次 probe 里同一周期只校验一次（4 组响应图不便宜）。"""
            k = (round(period, 2), sign)
            if k not in vseen:
                vseen[k] = cls._verify(rgb, period, sign)
            return vseen[k]

        def scan(profs, sign, min_period):
            """取「通过直线校验」的最佳候选。

            旧版每个剖面只取占位率最高的一个，校验不过就整队放弃；过曝帧上
            最高的往往是真周期的公约数（96 = 288/3），一校验不过就把真周期
            一起丢掉。现在逐候选校验，再从通过者里挑基本周期。

            挑法（v4.0.7）：占位率 ≥0.8 且内点 ≥4 的候选里取**最小**周期。
            144 和 288 常常同时可拟合（后者是前者的整倍数），只看占位率会
            因为 144 少认一根线而偏向 288 —— 距离直接翻倍。反过来 288 是真
            周期时，144 的点阵会空一半位置，占位率掉到 0.5，进不了这个组，
            自然落到 288。

            （v4.0.9 试过改成「先按贯穿率分组再取最小」：432 = 1.5×288 的
            贯穿率 0.71 确实低于 288 的 0.88，本意是踢掉它；但同一规则把
            zoom24 / 012411 / 012415 三张真值 432 的帧压成 288，净损失更大，
            已回退。见 docs/design.md「网格线颜色」一节。）
            """
            cands = []
            for pr in profs:
                for cand in cls._lattice(pr, sign, min_period, limit=3):
                    if verified(cand[0], sign) < 0.55:
                        continue
                    cands.append(cand)
            if not cands:
                return None
            solid = []
            for p, c, n_in, span in cands:
                sites = max(1, int(round(span / p)) + 1)
                if n_in >= 4 and n_in / float(sites) >= 0.8:
                    solid.append((p, c, n_in, span))
            if solid:
                return min(solid, key=lambda c: c[0])
            return max(cands, key=lambda c: c[1])

        # 峰值搜索用很小的下限（只看「有没有周期性」），识别后再用满图间距
        # 做一次量级校验 —— 下限卡太死会把「面板没铺满方屏」的正常情况判死。
        dark = scan(sB, -1, 24.0) or scan(sL, -1, 24.0)   # 黑粗线 = 1 km
        bright = scan(sL, 1, 10.0) or scan(sB, 1, 10.0)   # 白细线 = 100 m
        Pd = dark[0] if dark else 0.0
        Pb = bright[0] if bright else 0.0
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

        # 最后一关：周期必须落在**真的横平竖直、贯穿地图**的直线上。
        # 一维剖面对任何有重复感的地形纹理都会给出周期（实测赛前蒙层下
        # 锁到 13.09 px，被当成 100 m 格 → 同一条线从 2.2 km 变 3.0 km），
        # 而网格线的本质是直：竖线整列同色、横线整行同色。贯穿率不达标
        # 就当没量到，宁可显示「无网格」也不给一个能算出离谱距离的比例尺。
        dv = verified(Pd, -1) if Pd else 0.0
        bv = verified(Pb, 1) if Pb else 0.0
        if Pd and dv < 0.55:
            Pd, dark = 0.0, None
        if Pb and bv < 0.55:
            Pb, bright = 0.0, None


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
            # 地图最小就是满图，mpp 不可能比满图解析值更大。满图时 100 m
            # 细格只有 side/(kmSize×10) ≈ 18 px 且被浅色地形淹得很淡，
            # 剖面容易被 ~13 px 的地形纹理顶替（实测 conf 0.37 → mpp 7.64，
            # 同一条线从 2.2 km 被算成 3.0 km）。低置信又超上限的一律回退。
            full_mpp = float(km_size) * 1000.0 / side
            if mpp > full_mpp * 1.05 and conf < 0.55:
                return dict(mpp=full_mpp, source="估算", conf=0.0,
                            major=0.0, minor=0.0, km=km_size, zoom=1.0)
            span = Pd if Pd else (Pb * 10.0 if Pb else 0.0)
            return dict(mpp=mpp, source=src, conf=conf,
                        major=Pd, minor=Pb, km=km_size,
                        zoom=max(0.0, span / base) if span else 1.0)

        # 网格被关掉 / 不是 M 地图 -> 解析式兜底（等价于假设未缩放）
        return dict(mpp=(float(km_size) * 1000.0) / side, source="估算",
                    conf=0.0, major=0.0, minor=0.0, km=km_size, zoom=1.0)
