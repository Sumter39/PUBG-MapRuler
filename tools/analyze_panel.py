# -*- coding: utf-8 -*-
"""面板截图像素分析：验证内容是否填满窗口、元素是否被裁。"""
import sys
from PIL import Image

BG = (0x0a, 0x11, 0x16)          # 面板背景
BAR = (0x16, 0x26, 0x2e)         # 标题栏


def close(c1, c2, tol=12):
    return all(abs(a - b) <= tol for a, b in zip(c1[:3], c2))


def analyze(path):
    im = Image.open(path).convert("RGB")
    w, h = im.size
    px = im.load()
    print(f"size={w}x{h}")

    # 右边缘一列 / 底边一行是否是背景色（BG=未铺满的空窗）
    col = [px[w - 3, y] for y in range(h)]
    row = [px[x, h - 3] for x in range(w)]
    print("右边缘列: 全BG=%s, 全BAR=%s" % (
        all(close(c, BG) for c in col), all(close(c, BAR) for c in col)))
    print("底边缘行: 全BG=%s" % all(close(c, BG) for c in row))

    # 找标题栏(BAR)横带的实际宽度和高度范围
    bar_cols = [x for x in range(w) if close(px[x, 6], BAR)]
    print("标题栏横带 y=6: x从%d到%d (宽%d)" % (
        min(bar_cols) if bar_cols else -1,
        max(bar_cols) if bar_cols else -1,
        (max(bar_cols) - min(bar_cols) + 1) if bar_cols else 0))

    # 每一行：非背景内容的右边界
    for y in range(0, h, max(1, h // 20)):
        xs = [x for x in range(w) if not close(px[x, y], BG)]
        if xs:
            print("y=%3d 内容x: %d..%d" % (y, min(xs), max(xs)))
        else:
            print("y=%3d 全BG" % y)

    # 找橙色(#FF8533)和蓝色(#3A6FD8)槽位块（第2、3行的编号方块）
    for name, target in (("orange2", (0xFF, 0x85, 0x33)),
                         ("blue3", (0x3A, 0x6F, 0xD8)),
                         ("yellow1", (0xFF, 0xFF, 0x00))):
        pts = [(x, y) for y in range(h) for x in range(0, 60)
               if close(px[x, y], target, 40)]
        if pts:
            ys = sorted(p[1] for p in pts)
            print("%s: x范围%d..%d y范围%d..%d 数量%d"
                  % (name, min(p[0] for p in pts), max(p[0] for p in pts),
                     ys[0], ys[-1], len(pts)))
        else:
            print("%s: 未找到" % name)


if __name__ == "__main__":
    analyze(sys.argv[1] if len(sys.argv) > 1
            else "tools/out/win1_456x350.png")
