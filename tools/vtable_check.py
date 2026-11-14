# -*- coding: utf-8 -*-
"""解析 SDK 头文件 C 风格 Vtbl（DECLSPEC_XFGVIRT 完整有序列表）核 vtable 序号。"""
import re, os

KIT = r"D:/Windows Kits/10/Include/10.0.26100.0"
FILES = ["shared/dxgi.h", "shared/dxgi1_2.h", "um/d3d11.h"]
CACHE = {h: open(os.path.join(KIT, h), encoding='utf-8', errors='ignore').read() for h in FILES}

def vtbl_order(iface):
    for h, src in CACHE.items():
        m = re.search(r'typedef struct %sVtbl\s*\{(.*?)\n\s*\}\s*%sVtbl;' % (iface, iface), src, re.S)
        if m:
            body = m.group(1)
            names = re.findall(r'DECLSPEC_XFGVIRT\(\s*\w+\s*,\s*(\w+)\s*\)', body)
            if not names:
                names = re.findall(r'\w+\s*\(\s*STDMETHODCALLTYPE\s*\*\s*(\w+)\s*\)', body)
            return h, names
    return None, None

def slot_of(iface, method):
    h, names = vtbl_order(iface)
    if names is None:
        return "%sVtbl 未找到" % iface
    if method in names:
        return "%s::%s = %d   (%s)" % (iface, method, names.index(method), h)
    return "%s 无 %s；末尾方法: %s" % (iface, method, names[-3:])

for it, m in (("IDXGIOutput1", "DuplicateOutput"),
              ("IDXGIOutputDuplication", "AcquireNextFrame"),
              ("IDXGIOutputDuplication", "ReleaseFrame"),
              ("IDXGIFactory1", "EnumAdapters"),
              ("IDXGIAdapter", "EnumOutputs"),
              ("IDXGIOutput", "GetDesc"),
              ("ID3D11Device", "CreateTexture2D"),
              ("ID3D11Device", "GetImmediateContext"),
              ("ID3D11Texture2D", "GetDesc"),
              ("ID3D11DeviceContext", "CopyResource"),
              ("ID3D11DeviceContext", "Map"),
              ("ID3D11DeviceContext", "Unmap")):
    print(slot_of(it, m))
