"""离线批量量比例尺：读一个目录里的地图截图，逐张量 100 m 对应多少像素。

不抓屏、不碰游戏进程，随时可跑，也不受实时抓屏链路影响。

    python tools/zoom_batch.py testset/frames
    python tools/zoom_batch.py D:/shots/minimap --km 8 --rect 0,0,1080,1080

默认把每张图居中裁成最大正方形（整块屏幕就是地图时不用给 --rect）。
输出逐图明细 + 按 ±3% 归并的汇总（只用于看一组图量得是否一致、有没有失败帧）。

注意地图缩放是**连续**的（滚轮 + 0.15 s 插值），没有离散档位表，
所以这里不判断「是不是 2 的整数倍」，那是已作废的旧假设。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from gridprobe import GridProbe       # noqa: E402
from PIL import Image                 # noqa: E402

EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    root = Path(argv[0])
    km = 8.0
    rect = None
    args = argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--km":
            km = float(args[i + 1]); i += 2
        elif args[i] == "--rect":
            rect = tuple(int(v) for v in args[i + 1].split(",")); i += 2
        else:
            i += 1
    if not root.is_dir():
        print("不是目录: %s" % root)
        return 2

    files = sorted(p for p in root.rglob("*") if p.suffix.lower() in EXTS)
    if not files:
        print("目录里没有图片: %s" % root)
        return 1

    rows = []
    for p in files:
        try:
            im = Image.open(p)
            im.load()
        except Exception as e:                       # noqa: BLE001
            print("  %-40s 读不出: %s" % (p.name, e))
            continue
        im = im.convert("RGB")
        w, h = im.size
        if rect:
            box = (rect[0], rect[1], rect[0] + rect[2], rect[1] + rect[3])
        else:
            s = min(w, h)
            box = ((w - s) // 2, (h - s) // 2, (w + s) // 2, (h + s) // 2)
        im = im.crop(box)
        side = float(im.size[0])
        grid = GridProbe.probe(im, km, side)
        ok = grid.get("conf", 0.0) >= 0.12 and grid.get("source") != "估算"
        per = grid["minor"] * 10.0 if grid.get("minor") else 0.0
        rows.append((p.name, side, per, grid.get("mpp", 0.0),
                     grid.get("conf", 0.0), ok, grid.get("source", "")))
        flag = "" if ok else "  <失败>"
        print("  %-42s side=%4.0f 100m=%7.1fpx mpp=%.4f conf=%.3f %s%s"
              % (p.name[:42], side, per, grid.get("mpp", 0.0),
                 grid.get("conf", 0.0), grid.get("source", ""), flag))

    good = [r for r in rows if r[5] and r[2] > 0]
    if not good:
        print("\n没有一张量出网格。")
        return 1

    print("\n=== 档位表（按 100 m 像素归并，±3%% 同档）===")
    buckets = []
    for name, side, per, mpp, conf, ok, src in sorted(good, key=lambda r: r[2]):
        for b in buckets:
            if abs(b["per"] - per) <= b["per"] * 0.03:
                b["n"] += 1
                b["names"].append(name)
                break
        else:
            buckets.append(dict(per=per, n=1, names=[name], mpp=mpp))
    lo = buckets[0]["per"]
    prev = None
    for b in buckets:
        r = b["per"] / lo
        q = (b["per"] / prev) if prev else 1.0
        prev = b["per"]
        # 只报与上一组的倍率，不做「是否 2 的幂」判断：缩放连续，没有离散档位
        mark = "  上一组 x%.2f" % q if q != 1.0 else ""
        print("  %7.1f px/100m   基准x%-5.2f  mpp=%.4f  %d 张%s"
              % (b["per"], r, b["mpp"], b["n"], mark))
        print("           %s" % ", ".join(b["names"][:4]))
    print("\n基准（最小）= %.1f px/100m，共 %d 组，%d/%d 张可量"
          % (lo, len(buckets), len(good), len(rows)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
