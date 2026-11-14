# -*- coding: utf-8 -*-
"""DXGI 桌面复制抓屏 —— 从根源解决 HDR 屏 GDI 抓屏过曝问题。

背景：GDI 抓屏（ImageGrab/BitBlt）拿到的是 DWM 做完 SDR 色调映射的合成结果。
HDR 开启时中间调被抬亮、高光整体削顶——实测地图画面 B 通道 91.6% 像素顶着
255，事后任何恢复算法都救不回被削掉的信息。

根源方案：桌面复制 API（IDXGIOutputDuplication）在 HDR 模式下直接给
R16G16B16A16_FLOAT 的 scRGB 线性帧（1.0 = 80 nit），高亮 >1.0 原样保留不削顶，
按显示器 SDR 白电平（DisplayConfig API 查询，默认 80 nit）归一化 + gamma 编码，
还原出正确的 SDR 颜色。SDR 显示器下则返回 B8G8R8A8，直接用。

vtable 序号与接口 UUID 全部来自本机 Windows SDK 10.0.26100.0 头文件
（dxgi.h / dxgi1_2.h / d3d11.h）逐项解析，非手抄：
  IDXGIFactory1        = 770aae78-f26f-4dba-a829-253c83d1b387
  IDXGIOutput1         = 00cddea8-939b-4b83-a340-a685226666cc
  ID3D11Texture2D      = 6f15aaf2-d208-4e89-9ab4-489535d34f9c
  IDXGIFactory::EnumAdapters=7       IDXGIAdapter::EnumOutputs=7
  IDXGIOutput1::DuplicateOutput=22   IDXGIOutputDuplication::AcquireNextFrame=8
  IDXGIOutputDuplication::ReleaseFrame=14
  ID3D11Device::CreateTexture2D=5 / GetImmediateContext=40
  ID3D11Texture2D::GetDesc=10
  ID3D11DeviceContext::CopyResource=47 / Map=14 / Unmap=15

任何一步失败一律返回 None（调用方退回 GDI 抓屏）——本模块只增益，不冒险。
PUBGMAP_NO_DXGI=1 环境变量可整体停用。
"""
import ctypes
import os
import sys
import time
import traceback
from ctypes import wintypes

# ---------------------------------------------------------------- vtable 序号
VT_ENUM_ADAPTERS = 7
VT_ENUM_OUTPUTS = 7
VT_OUTPUT_GETDESC = 7
VT_DUPLICATE_OUTPUT = 22
VT_ACQUIRE_NEXT_FRAME = 8
VT_RELEASE_FRAME = 14
VT_CREATE_TEXTURE2D = 5
VT_GET_IMMEDIATE_CONTEXT = 40
VT_TEX_GETDESC = 10
VT_COPY_RESOURCE = 47
VT_MAP = 14
VT_UNMAP = 15


class GUID(ctypes.Structure):
    _fields_ = [('Data1', wintypes.DWORD), ('Data2', wintypes.WORD),
                ('Data3', wintypes.WORD), ('Data4', wintypes.BYTE * 8)]


def _guid(s):
    p = s.split('-')
    g = GUID()
    g.Data1 = int(p[0], 16)
    g.Data2 = int(p[1], 16)
    g.Data3 = int(p[2], 16)
    for i, part in enumerate((p[3], p[4])):
        for k in range(0, len(part), 2):
            g.Data4[i * 2 + k // 2] = int(part[k:k + 2], 16)
    return g


IID_IDXGIFactory1 = _guid('770aae78-f26f-4dba-a829-253c83d1b387')
IID_IDXGIOutput1 = _guid('00cddea8-939b-4b83-a340-a685226666cc')
IID_ID3D11Texture2D = _guid('6f15aaf2-d208-4e89-9ab4-489535d34f9c')


class DXGI_OUTPUT_DESC(ctypes.Structure):
    # 字段顺序必须与 SDK 头文件逐字一致（shared/dxgi.h，10.0.26100.0）：
    # DeviceName, DesktopCoordinates, AttachedToDesktop, Rotation, Monitor。
    # v3.9.14 及以前把 Rotation/Monitor 排在前面，size 104 ≠ 真实 96：
    # GetDesc 返回 S_OK 但代码在错位上读矩形（读到的是 Attached+Rotation+
    # HMONITOR 的字节），AttachedToDesktop 永远读不到真值 → 永远
    # no-output-on-primary → DXGI 从未真正工作过，一直静默回退 GDI。
    _fields_ = [('DeviceName', wintypes.WCHAR * 32),
                ('DesktopCoordinates', wintypes.RECT),
                ('AttachedToDesktop', wintypes.BOOL),
                ('Rotation', ctypes.c_int),
                ('Monitor', wintypes.HMONITOR)]


class TEX2D_DESC(ctypes.Structure):
    _fields_ = [('Width', wintypes.UINT), ('Height', wintypes.UINT),
                ('MipLevels', wintypes.UINT), ('ArraySize', wintypes.UINT),
                ('Format', ctypes.c_int),
                ('SampleCount', wintypes.UINT), ('SampleQuality', wintypes.UINT),
                ('Usage', ctypes.c_int),
                ('BindFlags', wintypes.UINT), ('CPUAccessFlags', wintypes.UINT),
                ('MiscFlags', wintypes.UINT)]


class MAPPED_SUBRESOURCE(ctypes.Structure):
    _fields_ = [('pData', ctypes.c_void_p), ('RowPitch', wintypes.UINT),
                ('DepthPitch', wintypes.UINT)]


class _PtrPos(ctypes.Structure):
    _fields_ = [('Visible', wintypes.BOOL), ('Position', wintypes.POINT)]


class FRAME_INFO(ctypes.Structure):
    _fields_ = [('LastPresentTime', wintypes.UINT),
                ('LastMouseUpdateTime', wintypes.UINT),
                ('AccumulatedFrames', wintypes.UINT),
                ('RectsCoalesced', wintypes.BOOL),
                ('ProtectedContentMaskedOut', wintypes.BOOL),
                ('PointerPosition', _PtrPos),
                ('TotalMetadataSize', wintypes.UINT),
                ('Size', wintypes.SIZE)]


class LUID(ctypes.Structure):
    _fields_ = [('LowPart', wintypes.DWORD), ('HighPart', wintypes.LONG)]


class _Rational(ctypes.Structure):
    _fields_ = [('Numerator', wintypes.UINT), ('Denominator', wintypes.UINT)]


class PATH_INFO(ctypes.Structure):
    class _Src(ctypes.Structure):
        _fields_ = [('adapterId', LUID), ('id', wintypes.UINT),
                    ('modeInfoIdx', wintypes.UINT), ('statusFlags', wintypes.UINT)]

    class _Tgt(ctypes.Structure):
        _fields_ = [('adapterId', LUID), ('id', wintypes.UINT),
                    ('modeInfoIdx', wintypes.UINT),
                    ('technology', wintypes.UINT), ('rotation', wintypes.UINT),
                    ('scaling', wintypes.UINT),
                    ('refreshRate', _Rational),
                    ('scanLineOrdering', wintypes.UINT),
                    ('targetAvailable', wintypes.BOOL),
                    ('statusFlags', wintypes.UINT)]

    _fields_ = [('sourceInfo', _Src), ('targetInfo', _Tgt),
                ('flags', wintypes.UINT)]


class SDR_WHITE_LEVEL(ctypes.Structure):
    class _Header(ctypes.Structure):
        _fields_ = [('type', wintypes.UINT), ('size', wintypes.UINT),
                    ('adapterId', LUID), ('id', wintypes.UINT)]

    _fields_ = [('header', _Header), ('SDRWhiteLevel', wintypes.ULONG)]


def _method(ptr, idx, restype, *argtypes):
    """取 COM 对象 vtable 第 idx 项，返回可直接调用的函数（首个参数 = 接口指针）。

    restype 一律降级为 c_long：ctypes 对 restype=HRESULT 的失败返回会自动抛
    OSError —— 而 EnumAdapters 枚举到末尾本来就约定返回 DXGI_ERROR_NOT_FOUND
    （正常终止信号），AcquireNextFrame 超时的 WAIT_TIMEOUT 同理；v3.9.14 里
    这些「正常失败码」全被 ctypes 炸成异常，整个 grab_full 直接全灭，DXGI
    永远静默回退 GDI（HDR 过白 + 识别异常的总根源，dxgi.log 的 exc:OSError
    就是它）。c_long 返回原始 hr 整数，由调用方自行判 != 0。"""
    if restype is ctypes.HRESULT:
        restype = ctypes.c_long
    vt = ctypes.cast(ctypes.c_void_p(ptr),
                     ctypes.POINTER(ctypes.c_void_p)).contents.value
    fn = ctypes.cast(ctypes.c_void_p(vt), ctypes.POINTER(ctypes.c_void_p))[idx]
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(fn)


def _qi(ptr, iid):
    out = ctypes.c_void_p()
    hr = _method(ptr, 0, ctypes.HRESULT, ctypes.POINTER(GUID),
                 ctypes.POINTER(ctypes.c_void_p))(ptr, ctypes.byref(iid),
                                                  ctypes.byref(out))
    if hr != 0:
        raise OSError('QueryInterface hr=0x%08X' % (hr & 0xFFFFFFFF))
    return out.value


def _release(ptr):
    if ptr:
        try:
            _method(ptr, 2, wintypes.ULONG)(ptr)
        except Exception:
            pass


def _log(msg):
    """诊断日志 → 数据目录 dxgi.log（便携模式，随 EXE 同文件夹；失败静默）。"""
    try:
        from pathlib import Path
        base = os.environ.get("PUBGMAP_DATA_DIR")
        if not base:
            return
        p = Path(base) / "dxgi.log"
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(time.strftime("%m-%d %H:%M:%S ") + msg + "\n")
    except Exception:
        pass


_SDR_CACHE = [0.0, 0.0]          # [白电平 nit, 上次查询时刻] TTY 缓存 30s
_LOG_TS = [0.0]                  # 日志节流


def _sdr_white_nits():
    """查询显示器 SDR 白电平（nit），返回 (nits, 来源)。失败回退 80（scRGB 参考白）。

    DISPLAYCONFIG_PATH_INFO=72B / DISPLAYCONFIG_MODE_INFO=64B（与 SDK 定义逐项
    对过字段布局）。SDRWhiteLevel 单位 = nits*1000/80。
    白电平取错会让整帧线性值归一化错档 → 显示过亮/过暗（v3.9.11 实测案例），
    所以：① 环境变量 PUBGMAP_SDR_WHITE_NITS 手动校准优先；② 查询结果缓存 30s
    并连同格式落 dxgi.log，出问题先看这份日志。"""
    env = os.environ.get("PUBGMAP_SDR_WHITE_NITS")
    if env:
        try:
            v = float(env)
            if v > 0:
                return v, "手动"
        except ValueError:
            pass
    now = time.time()
    if _SDR_CACHE[0] > 0 and now - _SDR_CACHE[1] < 30.0:
        return _SDR_CACHE[0], "缓存"
    try:
        u32 = ctypes.windll.user32
        flags = 1          # QDC_ONLY_ACTIVE_PATHS
        npath = wintypes.UINT()
        nmode = wintypes.UINT()
        if u32.GetDisplayConfigBufferSizes(flags, ctypes.byref(npath),
                                           ctypes.byref(nmode)) != 0:
            return 80.0, "默认(查询失败)"
        paths = (PATH_INFO * npath.value)()
        modes = (ctypes.c_char * 64 * nmode.value)()
        if u32.QueryDisplayConfig(flags, ctypes.byref(npath), paths,
                                  ctypes.byref(nmode), modes, None) != 0:
            return 80.0, "默认(查询失败)"
        for p in paths[:npath.value]:
            q = SDR_WHITE_LEVEL()
            q.header.type = 11      # DISPLAYCONFIG_DEVICE_INFO_GET_SDR_WHITE_LEVEL
            q.header.size = ctypes.sizeof(SDR_WHITE_LEVEL)
            q.header.adapterId = p.targetInfo.adapterId
            q.header.id = p.targetInfo.id
            if u32.DisplayConfigGetDeviceInfo(ctypes.byref(q)) == 0 \
                    and q.SDRWhiteLevel:
                v = max(80.0, q.SDRWhiteLevel * 80.0 / 1000.0)
                _SDR_CACHE[0], _SDR_CACHE[1] = v, now
                return v, "DisplayConfig"
    except Exception:
        pass
    return 80.0, "默认(异常)"


_FAIL_TS = [0.0]


def _fail(step, hr=0):
    """grab_full 失败出口统一落日志（10s 节流）。

    背景：DXGI 抓屏任何一步失败都会静默回退 GDI，dxgi.log 没有痕迹
    就无法判断「过亮/识别异常」到底走的哪条通道（v3.9.13 加）。"""
    if time.time() - _FAIL_TS[0] >= 10.0:
        _FAIL_TS[0] = time.time()
        _log("grab fail: %s (hr=0x%08X)" % (step, hr & 0xFFFFFFFF))
    return None


def grab_full(crop=None):
    """抓主显示器整帧，返回 PIL Image('RGB')；失败返回 None。

    crop = (l, t, r, b) 时只转换该区域（FP16 大帧省掉大部分转换时间）。"""
    if os.environ.get('PUBGMAP_NO_DXGI'):
        return None
    try:
        import numpy as np
        from PIL import Image
    except Exception:
        return None
    factory = adapter = output = out1 = dev = ctx = dup = tex = stag = res = None
    try:
        dxgi = ctypes.WinDLL('dxgi')
        d3d11 = ctypes.WinDLL('d3d11')

        f = ctypes.c_void_p()
        hr = dxgi.CreateDXGIFactory1(ctypes.byref(IID_IDXGIFactory1),
                                     ctypes.byref(f))
        if hr != 0:
            return _fail('CreateDXGIFactory1', hr)
        factory = f.value

        # 枚举所有输出，挑桌面坐标包含 (0,0) 的（主屏）
        best = None
        a = ctypes.c_void_p()
        ai = 0
        while _method(factory, VT_ENUM_ADAPTERS, ctypes.HRESULT,
                      wintypes.UINT, ctypes.POINTER(ctypes.c_void_p))(
                factory, ai, ctypes.byref(a)) == 0 and a.value:
            adapter = a.value
            o = ctypes.c_void_p()
            oi = 0
            while _method(adapter, VT_ENUM_OUTPUTS, ctypes.HRESULT,
                          wintypes.UINT, ctypes.POINTER(ctypes.c_void_p))(
                    adapter, oi, ctypes.byref(o)) == 0 and o.value:
                # r 必须先置 None：某个输出 GetDesc 失败/未附桌面时不能引用
                # 旧值或未定义值（v3.9.13 及以前这里 UnboundLocalError 直接
                # 炸掉整个 grab_full，静默回退 GDI —— dxgi.log 里那条
                # "exc:UnboundLocalError" 就是它）
                r = None
                desc = DXGI_OUTPUT_DESC()
                if _method(o.value, VT_OUTPUT_GETDESC, ctypes.HRESULT,
                           ctypes.POINTER(DXGI_OUTPUT_DESC))(
                        o.value, ctypes.byref(desc)) == 0 \
                        and desc.AttachedToDesktop:
                    r = desc.DesktopCoordinates
                if r is not None and r.left <= 0 < r.right \
                        and r.top <= 0 < r.bottom:
                    best = o.value
                    break
                _release(o.value)
                oi += 1
            if best is not None:
                break
            _release(adapter)
            adapter = None
            ai += 1
        if best is None:
            return _fail('no-output-on-primary')
        output = best
        # adapter 必须留活着传给 D3D11CreateDevice：它要的是 IDXGIAdapter，
        # 不是 IDXGIOutput（v3.9.15 前传 output → 按错误 vtable 调方法 → AV）

        d = ctypes.c_void_p()
        c = ctypes.c_void_p()               # 立即上下文（此前枚举从没走到
        fl = wintypes.UINT()                # 这里，未定义的 NameError 一直藏着）
        hr = d3d11.D3D11CreateDevice(ctypes.c_void_p(adapter), 0, None, 0,
                                     None, 0, 7, ctypes.byref(d),
                                     ctypes.byref(fl), ctypes.byref(c))
        if hr != 0:
            return _fail('D3D11CreateDevice', hr)
        _release(adapter)                   # 设备已建好，adapter 即可释放
        adapter = None                      # （置 None 防 finally 二次 Release）
        dev = d.value
        ctx = c.value

        out1_ptr = ctypes.c_void_p(_qi(output, IID_IDXGIOutput1))
        _release(output)
        output = None
        out1 = out1_ptr.value

        dup_p = ctypes.c_void_p()
        hr = _method(out1, VT_DUPLICATE_OUTPUT, ctypes.HRESULT,
                     ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(
            out1, dev, ctypes.byref(dup_p))
        if hr != 0:
            return _fail('DuplicateOutput', hr)
        dup = dup_p.value

        # 等一帧新画面并拷贝读回。HDR/独占切换瞬间桌面复制可能给「全黑帧」
        # （v3.9.16 实测：HDR 下 FP16 帧全黑 → 上层误报无轨迹）。黑帧丢弃
        # 重试，连续 3 帧仍黑则失败返回 None，让上层走 GDI 兜底。
        WAIT_TIMEOUT = 0x887A0027
        raw = None
        stride = 0
        fmt = 0
        w = h = 0
        stag = None
        stag_key = None
        for _attempt in range(3):
            info = FRAME_INFO()
            rsrc = ctypes.c_void_p()
            got = False
            for _ in range(8):
                hr = _method(dup, VT_ACQUIRE_NEXT_FRAME, ctypes.HRESULT,
                             wintypes.UINT, ctypes.POINTER(FRAME_INFO),
                             ctypes.POINTER(ctypes.c_void_p))(
                    dup, 45, ctypes.byref(info), ctypes.byref(rsrc))
                if hr == 0:
                    got = True
                    break
                if (hr & 0xFFFFFFFF) != WAIT_TIMEOUT:
                    return _fail('AcquireNextFrame', hr)
            if not got or not rsrc.value:
                return _fail('no-frame-timeout')

            tex = _qi(rsrc.value, IID_ID3D11Texture2D)
            _release(rsrc.value)
            rsrc = None

            td = TEX2D_DESC()
            _method(tex, VT_TEX_GETDESC, None, ctypes.POINTER(TEX2D_DESC))(
                tex, ctypes.byref(td))
            w, h = td.Width, td.Height
            if not (64 <= w <= 16384 and 64 <= h <= 16384):
                return _fail('bad-size %dx%d' % (w, h))

            # staging 按尺寸/格式缓存：黑帧重试时不重复建纹理
            if stag_key != (w, h, td.Format):
                if stag is not None:
                    _release(stag)
                    stag = None
                sd = TEX2D_DESC()
                sd.Width, sd.Height, sd.MipLevels, sd.ArraySize = w, h, 1, 1
                sd.Format = td.Format
                sd.SampleCount, sd.SampleQuality = 1, 0
                sd.Usage = 3                       # D3D11_USAGE_STAGING
                sd.BindFlags = 0
                sd.CPUAccessFlags = 0x20000        # D3D11_CPU_ACCESS_READ
                sd.MiscFlags = 0
                st = ctypes.c_void_p()
                hr = _method(dev, VT_CREATE_TEXTURE2D, ctypes.HRESULT,
                             ctypes.POINTER(TEX2D_DESC), ctypes.c_void_p,
                             ctypes.POINTER(ctypes.c_void_p))(
                    dev, ctypes.byref(sd), None, ctypes.byref(st))
                if hr != 0:
                    return _fail('CreateTexture2D', hr)
                stag = st.value
                stag_key = (w, h, td.Format)

            _method(ctx, VT_COPY_RESOURCE, None, ctypes.c_void_p,
                    ctypes.c_void_p)(ctx, stag, tex)
            mp = MAPPED_SUBRESOURCE()
            # Map(this, pResource, Subresource, MapType, MapFlags, pMappedResource)
            # —— v3.9.15 前漏声明 MapFlags，6 个实参对 5 形参直接 TypeError
            hr = _method(ctx, VT_MAP, ctypes.HRESULT, ctypes.c_void_p,
                         wintypes.UINT, wintypes.UINT, wintypes.UINT,
                         ctypes.POINTER(MAPPED_SUBRESOURCE))(
                ctx, stag, 0, 1, 0, ctypes.byref(mp))
            if hr != 0 or not mp.pData:
                return _fail('Map', hr)
            raw = ctypes.string_at(mp.pData, mp.RowPitch * h)
            _method(ctx, VT_UNMAP, None, ctypes.c_void_p,
                    wintypes.UINT)(ctx, stag, 0)
            _method(dup, VT_RELEASE_FRAME, None)(dup)

            if _frame_black(raw, mp.RowPitch, w, h, td.Format):
                raw = None                         # 黑帧：丢弃，重试下一帧
                continue
            stride = mp.RowPitch
            fmt = td.Format
            break

        if raw is None:
            return _fail('black-frame x3')

        if fmt == 10:                # R16G16B16A16_FLOAT —— HDR 原始帧
            return _tonemap_fp16(np, Image, raw, stride, w, h, crop, fmt)
        if fmt == 87:                # B8G8R8A8_UNORM —— SDR 桌面
            _log_frame(fmt, 0.0)
            if crop:
                l, t, rr, b = crop
                l = max(0, min(w - 1, l)); rr = max(l + 1, min(w, rr))
                t = max(0, min(h - 1, t)); b = max(t + 1, min(h, b))
                rows = b''.join(
                    raw[y * stride + l * 4: y * stride + rr * 4]
                    for y in range(t, b))
                w, h = rr - l, b - t
            else:
                rows = b''.join(raw[y * stride: y * stride + w * 4]
                                for y in range(h))
            # 内存字节序是 B,G,R,A——rawmode 必须写 BGRA。v3.9.15 用裸
            # 'RGBA' 读把 R/B 互换了（海水变黄绿、黄轨迹变青蓝），
            # HDR 时代只走 FP16 路径从未触发，桌面切 SDR 后暴露。
            return Image.frombytes('RGBA', (w, h), rows, 'raw', 'BGRA') \
                        .convert('RGB')
        return _fail('unknown-format %d' % fmt)
    except Exception as e:
        # 带上 traceback 末帧：纯 %r 看不出错误发生在哪一步
        loc = traceback.extract_tb(sys.exc_info()[2])[-1]
        return _fail('exc:%r @%s:%d' % (e, os.path.basename(loc.filename),
                                        loc.lineno))
    finally:
        for p in (stag, tex, dup, out1, ctx, dev, adapter, factory):
            _release(p)


def _frame_black(raw, stride, w, h, fmt):
    """整帧近全黑检测（HDR/独占切换或游戏异常时桌面复制可能给全零帧）。

    v3.9.16 实测：HDR 下拿到 fmt=FP16 的全黑帧 → 上层裁剪后自然「无轨迹」。
    FP16 帧 alpha 通道恒为 1.0（0x3C00）非零，判黑只查 RGB 分量；SDR 查 BGR。
    全帧均匀抽样 ~1k 点，所有采样点 RGB 分量字节全零才算黑。"""
    bpp = 8 if fmt == 10 else 4          # FP16 = RGBA16F；SDR = BGRA8
    rgbb = 6 if fmt == 10 else 3
    total = stride * h
    if total <= rgbb:
        return True
    # ~1k 个采样点，按像素步进对齐（stride 可能 > w*bpp，只采样有效列）
    step_px = max(1, (w * h) // 1024)
    off_px = 0
    while off_px * bpp < min(total - rgbb, w * bpp * h):
        row, col = divmod(off_px, w)
        off = row * stride + col * bpp
        if off + rgbb <= total and any(raw[off + k] for k in range(rgbb)):
            return False
        off_px += step_px
    return True


def _log_frame(fmt, white_nits):
    """每次抓帧落一行诊断日志（10s 节流）：格式 + 生效白电平。"""
    if time.time() - _LOG_TS[0] < 10.0:
        return
    _LOG_TS[0] = time.time()
    nits, src = (white_nits, "-") if white_nits else _sdr_white_nits()
    fmt_name = {10: "FP16(HDR)", 87: "B8G8R8A8(SDR)"}.get(fmt, str(fmt))
    _log("grab fmt=%s sdr_white=%.0fnit(%s)" % (fmt_name, nits, src))


def _tonemap_fp16(np, Image, raw, stride, w, h, crop, fmt=10):
    """scRGB 线性 FP16 → SDR 8bit。线性值 1.0 = 80 nit；
    按 SDR 白电平归一化后 gamma 2.2 编码，色相全程保持。"""
    x0 = y0 = 0
    x1, y1 = w, h
    if crop:
        l, t, rr, b = crop
        x0 = max(0, min(w - 1, l)); x1 = max(x0 + 1, min(w, rr))
        y0 = max(0, min(h - 1, t)); y1 = max(y0 + 1, min(h, b))
    ncol = stride // 2                      # 每行 short 数
    arr = np.frombuffer(raw, np.uint16, count=ncol * h).reshape(h, ncol)
    # 每像素 4 个 short：R G B A；只取裁剪区的 RGB
    rgb = arr[y0:y1, x0 * 4:x1 * 4].reshape(y1 - y0, x1 - x0, 4)[:, :, :3]
    bits = rgb.astype(np.uint32)
    sign = (bits & 0x8000) << 16
    expo = (bits >> 10) & 0x1F
    mant = (bits & 0x3FF) << 13
    f32 = (sign | ((expo - 15 + 127).astype(np.uint32) << 23) | mant)
    f32[expo == 0] = 0                      # 非规格化数忽略（近 0）
    vals = f32.view(np.float32)
    vals[~np.isfinite(vals)] = 0.0
    nits, src = _sdr_white_nits()
    _log_frame(fmt, nits)
    white_n = nits / 80.0
    norm = np.clip(vals / white_n, 0.0, 1.0) ** (1.0 / 2.2)
    out = (norm * 255.0 + 0.5).astype(np.uint8)
    return Image.frombuffer('RGB', (x1 - x0, y1 - y0),
                            out.tobytes(), 'raw', 'RGB', 0, 1)
