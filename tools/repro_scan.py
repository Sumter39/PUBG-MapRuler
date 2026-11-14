# -*- coding: utf-8 -*-
"""离线复现 last_scan.png 的识别链路（临时脚本，用完即删）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import main as M  # noqa: E402

frame = Path(r"D:/tmp/tmp/PUBG-MapRuler/dist/PUBG-MapRuler-Data/last_scan.png")
im = M.Image.open(frame)
print("frame size:", im.size, im.mode)

for side in (im.size[0], im.size[1], 2560.0, 1440.0):
    if side > max(im.size):
        continue
    grid = M.GridProbe.probe(im, 8.0, float(side))
    print(f"side={side}: {grid}")

side = 2560.0
grid = M.GridProbe.probe(im, 8.0, side)
mpp = grid.get("mpp", 0)
print("\n--- TrackProbe.detect_teams(im, 4) ---")
teams = M.TrackProbe.detect_teams(im, 4)
print("teams:", {k: len(v) for k, v in teams.items()} if teams else "NONE")
for ti, pts in (teams or {}).items():
    import math
    m = math.hypot(pts[-1][0] - pts[0][0], pts[-1][1] - pts[0][1]) * mpp
    print(f"  team {ti}: {len(pts)} pts, first={pts[0]}, last={pts[-1]}, dist={m:.0f} m")

print("\n--- detect (黄通认) ---")
det = M.TrackProbe.detect(im, 4)
print("chains:", [len(p) for p in det] if det else "NONE")
