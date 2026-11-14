<h1 align="center">PUBG MapRuler</h1>

<p align="center">PUBG 地图测距 &amp; 点位浮层。单个 EXE、常驻托盘：官方点位直接叠进按 M 打开的游戏地图，画线即得两点直线距离。</p>

<p align="center">
  <img src="https://img.shields.io/badge/Platform-Windows-0078D6.svg" alt="Platform: Windows">
  <img src="https://img.shields.io/badge/Python-3.8%2B-3776AB.svg" alt="Python: 3.8+">
  <img src="https://img.shields.io/badge/version-4.0.0-green.svg" alt="version">
</p>

- **点位投影**：6 张地图 / 7 类别 / 456 个点位，固定公式对齐，按类别开关
- **测距**：读你在游戏地图上画的线，四名队友各一行，逐帧识别、缩放平移自动适配

> 只读屏幕像素：不读内存、不注入、不 hook、运行时不联网。第三方 overlay
> 可能违反游戏服务条款，用自己账号，后果自负。

## 快速开始

```bash
pip install pyinstaller pywin32
python build.py          # 产出 dist/PUBG-MapRuler.exe
```

运行 `dist\PUBG-MapRuler.exe`，游戏设为无边框窗口。默认热键 `右Alt + ,`
显示/隐藏面板；按 M 开地图 → `Alt + 右键` 画轨迹 → 点面板「测量」。

## 文档

- [使用说明](docs/usage.md) —— 构建、操作、行为约定、已知限制
- [技术设计](docs/design.md) —— 架构、投影公式、识别链路、数据来源
