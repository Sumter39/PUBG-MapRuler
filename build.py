# -*- coding: utf-8 -*-
"""打包成单文件 Windows EXE：python build.py

输出 dist/PUBG-MapRuler.exe（--onefile，无控制台窗口）。
资源 assets/ 与 data/ 会一起打进包内，通过 sys._MEIPASS 读取，离线可用。
"""
import os
from pathlib import Path
import PyInstaller.__main__

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
SEP = ";"

args = [
    str(ROOT / "src" / "main.py"),
    "--name=PUBG-MapRuler",
    "--onefile",
    "--noconsole",
    "--clean",
    "--noconfirm",
    f"--add-data={ROOT / 'assets'}{SEP}assets",
    f"--add-data={ROOT / 'data'}{SEP}data",
    f"--icon={ROOT / 'assets' / 'icon.ico'}",
    "--hidden-import=win32gui",
    "--hidden-import=win32con",
    "--hidden-import=win32api",
    # numpy 必须打包：dxgigrab 的 FP16→SDR 色调映射靠它做向量化运算
    "--exclude-module=cv2",
    "--exclude-module=matplotlib",
    "--exclude-module=scipy",
    "--exclude-module=pandas",
    "--exclude-module=PyQt5",
    "--distpath=dist",
    "--workpath=build",
    "--specpath=build",
    # PerMonitorV2 manifest：DPI 感知必须在进程创建时就生效（manifest 是唯一
    # 可靠入口）。运行时 enable_dpi_awareness() 对 EXE 无效——默认 manifest
    # 已把进程设成 DPI unaware，API 只允许设一次，后面再设会被拒绝。
    # 没有它，混合 DPI（主屏 125% / 副屏 150%）下副屏上内容与窗口错位。
    f"--manifest={ROOT / 'src' / 'app.manifest'}",
]
PyInstaller.__main__.run(args)
print("\n构建完成 ->", ROOT / "dist" / "PUBG-MapRuler.exe")
