# 技术设计

## 架构

单进程，两个原生窗口 + 一个 Tk 面板：

```
src/main.py     单入口：Win32 宿主（托盘/热键/消息循环）+ 控制面板 + 点位投影层 + 识别
src/dxgigrab.py DXGI 桌面复制抓屏（HDR 屏颜色正确），GDI ImageGrab 兜底
src/app.manifest  PerMonitorV2 DPI 感知声明
build.py        PyInstaller 打包：assets/ 与 data/ 随包，通过 sys._MEIPASS 读取
```

## 点位投影

完整地图（按 M 打开）是屏幕正中、边长等于屏高的正方形，纯固定公式对齐，
不识别、不抓屏：

```
side = H    left = (W − H)/2    top = 0    1 km 网格 = H/8
x = (lng+180)/360 × side        y = (lat+90)/180 × side
```

该矩形来自旧 EXE 逆向取证：基准图 9 条竖网格线精确落在 `x = 560 + k·180`
（2560×1440），反汇编确认识别仅用于跟踪放大/平移，基础矩形固定。
换分辨率时矩形随 H 自动缩放，无需配置。放大/平移后的测距识别按网格
实时重测比例尺。

## 测距识别链路

```
抓帧（DXGI 桌面复制优先；抓屏前把自己的图层 alpha 归零，帧内只剩游戏）
→ HSV 色相掩膜（每队一个色相窗口；色相对亮度拉伸/削顶不敏感）
→ 环形度整图扫描出候选（四象限均衡，挡直边/大色块）
→ 窗口精修环打分
→ 串链：相邻标记间存在细直描边才算连线（直线采样 + 法向细描边校验）
→ 共线判定：直线量距离；折线认出但距离留空
```

- 颜色分类用 **HSV 色相窗口**：屏幕标记色随曝光/削顶/背景混合漂移
  （蓝→纯青、黄→削顶纯黄），固定 RGB 阈值不可靠，色相一个区间覆盖全部变体。
- 比例尺来自游戏自带网格：1 km 黑粗线 + 100 m 白细线，剖面找峰 + 间距投票
  + 最小二乘精修；置信不足回退 `kmSize × 1000 / side`（结果标「估算」）。
- 队伍颜色逆向自 `UiHelperFunctionsBp.GetTeamColor`（Normal case0）：
  1 黄 `#F4B300` / 2 红 `#DC0000` / 3 蓝 `#0061E5` / 4 绿 `#068E00`。

## 窗口与输入

- **面板几何由 Win32 全权接管**：尺寸位置只走 `SetWindowPos`，Tk 不参与
  布局传播（`pack_propagate(False)`）；子类化窗口过程吞掉 `WM_DPICHANGED`，
  混合 DPI（如主屏 125% / 副屏 150%）下面板物理像素 1:1，跨屏不重排不模糊。
- **分层窗口收不到键盘焦点**：`overrideredirect` 透明面板上 Tk 的
  `focus_force()` + `bind_all("<KeyPress>")` 永远等不到按键。热键捕获因此用
  `GetAsyncKeyState` 轮询 + 自维护状态表做按下沿检测，与焦点无关；录制期间
  先注销旧热键，提交/取消后恢复。
- 显隐热键用 `RegisterHotKey`，触发时以 `GetAsyncKeyState` 查物理按键区分
  左右边；运行时改绑走自定义消息 `WM_REBIND`。

## 目录结构

```
PUBG-MapRuler/
├── build.py / README.md / docs/
├── src/                          运行时代码（见上）
├── assets/                       icon.ico + 点位类别图标 PNG（随 EXE 打包）
├── data/pubg-overlay-data.json   6 地图 / 7 类别 / 456 点位 / kmSize（随 EXE 打包）
├── tools/                        自研开发/诊断脚本
│   ├── pak_extract.py            自研 pak 索引解析 + Oodle 解压（只读）
│   ├── repro_scan.py             last_scan.png 离线复现识别链路
│   ├── vtable_check.py           SDK 头文件 Vtbl 全序核对
│   └── …                         面板像素 / 行为 / 截图 / DPI 验证
├── vendor/                       第三方，原样存放
│   ├── ooz/                      oo2core_9 DLL + 上游源码副本
│   └── unpaker/                  Unpaker.CLI pak 工具套件
└── dist/                         构建产物（不入库）
```

划分原则：`tools/` 只放自研，`vendor/` 只放第三方。

## 数据来源

全部来自 `pubgmap.top` 的 Supabase 公开只读接口，离线打包：

| 项 | 来源 |
| --- | --- |
| 456 点位 / 6 地图 / 7 类别 | `markers` / `map_types` / `marker_types` / `map_type_marker_types` |
| 点位图标 | `marker_types.icon_url` 官方 SVG，按 Leaflet 参数 32×32、底部中心锚点栅格化 |
| `kmSize` | 各图实际边长（Erangel/Miramar/Taego/Rondo/Deston 8 km，Vikendi 6 km） |

每张图展示的类别由 `map_type_marker_types` 关联表决定，所以各图不同：
维寒迪独有熊洞/撬棍房/实验营地，蒂斯顿用安全门替代密室。坐标为社区人工
标注。官方轮换地图缺 Sanhok / Karakin / Paramo（来源站未收录）。

## 免责声明

- 仅供个人学习自用。第三方 overlay 可能违反游戏服务条款，用自己账号，后果自负。
- **只读屏幕像素**：不读游戏内存、不注入、不 hook、不抓包、运行时不联网。
- 不做任何预测或位置推算——屏幕上的信息就是本程序的全部输入。
