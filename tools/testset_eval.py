# -*- coding: utf-8 -*-
"""识别回归测试集：跑 testset/ 下的样本，与 expected.json 对账。

用法：
    python tools/testset_eval.py

判定规则（见 docs/design.md「测试集与回归」）：
  · 槽位必须精确匹配 —— 多认一队、少认一队都算失败（颜色归槽是本项目
    反复出错的点，宁可严）
  · 距离允许 ±tolerance（expected.json 里配，默认 2%）
  · zoom 允许 ±5%（防网格识别退化）
  · consistency 组要求同一条轨迹跨缩放档的距离一致（±tolerance）
退出码 0 = 全部通过，1 = 有失败，2 = 测试集缺失。
"""
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from gridprobe import GridProbe      # noqa: E402
from trackprobe import TrackProbe    # noqa: E402
from PIL import Image                # noqa: E402

SET_DIR = ROOT / "testset"
KM_SIZE = 8.0                        # Rondo


def run(frame):
    """返回 {队号字符串: 距离 m} 与网格结果。

    与主程序「测量」的识别闸门保持一致：比例尺没量到（conf < 0.12 或「估算」）
    时不算距离——没有可信比例尺，像素长度换成米就是瞎猜。
    """
    im = Image.open(SET_DIR / "frames" / frame).convert("RGB")
    # 正方形帧 side=边长；整屏矩形帧（v4.0.12 起抓图=整屏 2560×1440）
    # side 必须是屏高（与主程序 self.rect[2]=H×scale 同一口径），
    # 用宽度会把 zoom 算成 2.4 倍。帧的 side 可在 expected.json 覆盖。
    side = float(min(im.size)) if im.size[0] == im.size[1] else float(im.size[1])
    grid = GridProbe.probe(im, KM_SIZE, side)
    got = {}
    if grid.get("conf", 0.0) >= 0.12 and grid.get("source") != "估算":
        for ti, pts in TrackProbe.detect_teams(im, 4).items():
            got[str(ti)] = math.dist(pts[0], pts[-1]) * grid["mpp"]
    return got, grid


def main():
    path = SET_DIR / "expected.json"
    if not path.exists():
        print("缺少 testset/expected.json —— 测试集被 Git 忽略，需按 docs/design.md 重建")
        return 2
    spec = json.loads(path.read_text(encoding="utf-8"))
    tol = float(spec.get("tolerance", 0.02))

    print("=== testset: %d cases ===" % len(spec["cases"]))
    passed = 0
    measured = {}
    for case in spec["cases"]:
        got, grid = run(case["frame"])
        measured[case["frame"]] = got
        want = case["expect"]
        why = []
        zerr = abs(grid["zoom"] - case["zoom"]) / max(1e-6, case["zoom"])
        if zerr > 0.05:
            why.append("zoom %.2f vs %.2f" % (grid["zoom"], case["zoom"]))
        # check="grid"：只校验比例尺（缩放档 + 是否量到），不校验距离。
        # 用于「比例尺能认出、但轨迹本身还没人工确认」的帧。
        if case.get("check") == "grid" and grid["source"] == "估算":
            why.append("比例尺未量到")
        # grid="none"：该帧本来就没有网格（赛前预览 / 过曝蒙层），
        # 要求比例尺必须回退成「估算」——不许把地形纹理当成格线。
        if case.get("grid") == "none" and grid["source"] != "估算":
            why.append("网格应判无，实得 %s %.3f" % (grid["source"], grid["mpp"]))
        if case.get("check") != "grid":
            if set(got) != set(want):
                why.append("槽位 %s != 期望 %s" % (sorted(got), sorted(want)))
            for team, want_m in want.items():
                if team in got:
                    err = abs(got[team] - want_m) / max(1e-6, want_m)
                    if err > tol:
                        why.append("t%s %.1fm vs %.1fm (%.1f%%)"
                                   % (team, got[team], want_m, err * 100))
        ok = not why
        passed += ok
        show = " ".join("t%s %.1fm" % (t, v) for t, v in sorted(got.items())) or "无轨迹"
        print("%s  %-28s %s%s" % ("PASS" if ok else "FAIL", case["frame"], show,
                                  "" if ok else "   <- " + "; ".join(why)))

    checks = spec.get("consistency", [])
    if checks:
        print("--- 跨档一致性 ---")
    for c in checks:
        vals = [measured.get(f, {}).get(c["team"]) for f in c["frames"]]
        if any(v is None for v in vals):
            print("FAIL  %s 缺少某档结果 %s" % (c["note"], vals))
            continue
        spread = (max(vals) - min(vals)) / max(1e-6, min(vals))
        ok = spread <= tol
        print("%s  %s  %s 差 %.1f%%"
              % ("PASS" if ok else "FAIL", c["note"],
                 " / ".join("%.1fm" % v for v in vals), spread * 100))
        if ok:
            passed += 1

    total = len(spec["cases"]) + len(checks)
    print("---\n%d/%d 通过 (%.1f%%)" % (passed, total, passed * 100.0 / total))
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
