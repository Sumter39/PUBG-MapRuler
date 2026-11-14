# -*- coding: utf-8 -*-
import os, sys, time
sys.path.insert(0, r"D:/tmp/tmp/PUBG-MapRuler/src")
os.environ["PUBGMAP_DATA_DIR"] = r"D:/tmp/tmp/PUBG-MapRuler/dist/PUBG-MapRuler-Data"
import dxgigrab
t0 = time.time()
img = dxgigrab.grab_full()
print("grab_full:", None if img is None else (img.size, img.mode), "%.2fs" % (time.time() - t0))
if img is not None:
    img.save(r"D:/tmp/tmp/PUBG-MapRuler/tools/dxgi_test_frame.png")
