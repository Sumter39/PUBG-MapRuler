"""最小 UAsset 读取器：解析包名表 / 导入表 / 导出表，并按名字取属性值。

PUBG 的资产是拆开的：`.uas` 存包头 + 表，`.u` 存 export 数据。
本文件只依赖标准库，用于离线看 Blueprint 变量的默认值（比如缩放档位）。

    python tools/uasset_dump.py <x.uas> [--names 关键字] [--props 关键字]
"""
import struct
import sys
from pathlib import Path

MAGIC = 0x9E2A83C1
DEBUG = True


class R:
    def __init__(self, buf, pos=0):
        self.b = buf
        self.p = pos

    def i32(self):
        v = struct.unpack_from("<i", self.b, self.p)[0]; self.p += 4; return v

    def u32(self):
        v = struct.unpack_from("<I", self.b, self.p)[0]; self.p += 4; return v

    def i64(self):
        v = struct.unpack_from("<q", self.b, self.p)[0]; self.p += 8; return v

    def f32(self):
        v = struct.unpack_from("<f", self.b, self.p)[0]; self.p += 4; return v

    def fstr(self):
        n = self.i32()
        if n == 0:
            return ""
        if n < 0:                                  # UTF-16
            s = self.b[self.p:self.p - n * 2].decode("utf-16-le"); self.p += -n * 2
            return s.rstrip("\x00")
        s = self.b[self.p:self.p + n - 1].decode("latin-1"); self.p += n
        return s.rstrip("\x00")

    def guid(self):
        self.p += 16

    def fname(self):
        """FName 序列化 = (名字索引, 实例号)，共 8 字节，不是字符串。"""
        i = self.i32(); n = self.i32()
        return i, n


class Asset:
    def __init__(self, uas_path, u_path=None):
        self.buf = Path(uas_path).read_bytes()
        self.payload = Path(u_path).read_bytes() if u_path and Path(u_path).exists() else b""
        r = R(self.buf)
        assert r.u32() == MAGIC, "不是 uasset"
        self.legacy = r.i32()
        r.i32(); r.i32(); r.i32()                  # UE3 / UE4 / licensee
        n = r.u32()                                 # 自定义版本数量
        for _ in range(n):
            r.guid(); r.i32()
        self.total_header = r.i32()
        self.folder = r.fstr()
        self.flags = r.u32()
        self.name_count = r.i32()
        self.name_off = r.i32()
        r.i32(); r.i32()                            # gatherable
        self.export_count = r.i32()
        self.export_off = r.i32()
        self.import_count = r.i32()
        self.import_off = r.i32()
        # 名表是否带 hash 由 UE4 版本位决定，这里版本被擦成 0，靠扫描探明
        self.hashed_names = self._probe_hash()
        self.names = self._names()
        self.imports = self._imports()
        self.exports = self._exports()

    def _probe_hash(self):
        """试「不带 hash」与「带 hash」两种读法，哪条能读完 747 个合法名字就用哪条。

        判据：结束时恰好停在下一页边界的那个不现实，所以只要求全程可读、
        且名字字符集干净（FString 首字节不会出现 0 或 >126 的怪值）。
        """
        for hashed in (True, False):
            r = R(self.buf, self.name_off)
            ok = True
            try:
                for _ in range(self.name_count):
                    s = r.fstr()
                    if hashed:
                        r.u32()
                    if not s or any(not (32 <= ord(c) < 127) for c in s[:64]):
                        ok = False
                        break
            except Exception:                       # noqa: BLE001
                ok = False
            if ok:
                return hashed
        return True

    def _names(self):
        r = R(self.buf, self.name_off)
        out = []
        for _ in range(self.name_count):
            s = r.fstr()
            if self.hashed_names:
                r.u32()                             # name hash
            out.append(s)
        return out

    def _imports(self):
        r = R(self.buf, self.import_off)
        out = []
        for _ in range(self.import_count):
            r.fname(); r.fname()                    # class package, class name
            r.i32()                                 # outer
            name = r.fname(); r.fname()             # object name, package name
            out.append((None, self.names[name[0]] if 0 <= name[0] < len(self.names) else "?"))
        return out

    def _exports(self):
        """导出表布局随 UE 版本变化，这里按「每条的字节数」试探，选名字合法的那个。"""
        best = None
        for has_asset in (False, True):
            r = R(self.buf, self.export_off)
            rows = []
            bad = 0
            try:
                for _ in range(self.export_count):
                    cls = r.i32(); r.i32(); r.i32(); outer = r.i32()
                    ni, _ = r.fname()               # object name
                    r.i32()                         # object flags
                    size = r.i64(); off = r.i64()
                    r.i32(); r.i32(); r.i32()       # forced / notForClient / notForServer
                    r.guid()                        # package guid
                    r.u32()                         # package flags
                    if has_asset:
                        r.i32()                     # bIsAsset
                    r.i32()                         # notAlwaysLoadedForEditorGame
                    r.i32()                         # first dependency
                    r.i32(); r.i32(); r.i32(); r.i32()
                    if not (0 <= ni < len(self.names)):
                        bad += 1
                    rows.append(dict(cls=cls, name=self.names[ni] if 0 <= ni < len(self.names) else "?",
                                     size=size, off=off, outer=outer))
            except Exception:                       # noqa: BLE001
                continue
            if best is None or bad < best[0]:
                best = (bad, rows)
        return best[1] if best else []

    def export_name(self, i):
        if 0 <= i < len(self.exports):
            return self.exports[i]["name"]
        return None

    def obj_name(self, idx):
        """FPackageIndex → 名字（>0 是 export，<0 是 import）。"""
        if idx > 0:
            return self.export_name(idx - 1)
        if idx < 0:
            j = -idx - 1
            if 0 <= j < len(self.imports):
                return self.imports[j][1]
        return None


def dump_props(a, e):
    """按 UE4 属性 tag 流解析一个 export 的默认值。"""
    base = e["off"] - a.total_header
    buf = a.payload
    r = R(buf, base)
    end = min(base + e["size"], len(buf))
    out = []
    while r.p + 8 <= end:
        ni, _ = r.fname()
        if not (0 <= ni < len(a.names)):
            if DEBUG:
                print("  [断] p=%d ni=%d 前 32 字节 %s"
                      % (r.p - 8, ni, buf[max(0, r.p - 40):r.p + 8].hex(" ")))
            break
        name = a.names[ni]
        if name == "None":
            break
        ti, _ = r.fname()
        typ = a.names[ti] if 0 <= ti < len(a.names) else "?"
        size = r.i32()
        r.i32()                                     # array index
        # 新版属性 tag 在 array index 后带 1 字节标志（旧版是 BoolProperty 的取值，
        # 新版改成 tag flags）；这里只取值、不解释，非 0 时才有额外 guid。
        tag_flag = r.b[r.p]; r.p += 1
        if tag_flag == 2:                           # 带属性 guid
            r.guid()
        struct = ""
        inline = None
        if typ == "StructProperty":
            sn, _ = r.fname()
            struct = a.names[sn] if 0 <= sn < len(a.names) else "?"
            r.guid()
        elif typ == "BoolProperty":
            inline = r.b[r.p]; r.p += 1          # BoolProperty 的值单独占 1 字节
        elif typ in ("ByteProperty", "EnumProperty"):
            en, _ = r.fname()
            struct = a.names[en] if 0 <= en < len(a.names) else "?"
        elif typ == "ArrayProperty":
            it, _ = r.fname()
            struct = a.names[it] if 0 <= it < len(a.names) else "?"
        elif typ == "SetProperty":
            it, _ = r.fname()
            struct = a.names[it] if 0 <= it < len(a.names) else "?"
        elif typ == "MapProperty":
            k, _ = r.fname(); v, _ = r.fname()
            struct = "%s->%s" % (a.names[k], a.names[v])
        val = buf[r.p:r.p + size]
        r.p += size
        out.append((name, typ, struct, size, inline, val))
    return out


def show(val, typ, size):
    if typ == "FloatProperty" and len(val) == 4:
        return "%.6f" % struct.unpack("<f", val)[0]
    if typ == "DoubleProperty" and len(val) == 8:
        return "%.6f" % struct.unpack("<d", val)[0]
    if typ in ("IntProperty", "UInt32Property") and len(val) == 4:
        return str(struct.unpack("<i", val)[0])
    if typ == "Int64Property" and len(val) == 8:
        return str(struct.unpack("<q", val)[0])
    if typ == "NameProperty" and len(val) == 8:
        i = struct.unpack_from("<i", val, 0)[0]
        return "?" if not (0 <= i < 10 ** 6) else "name#%d" % i
    if typ == "ArrayProperty" and len(val) >= 4:
        n = struct.unpack_from("<i", val, 0)[0]
        body = val[4:]
        if typ == "ArrayProperty" and struct in ("FloatProperty", "IntProperty") and n == len(body) // 4:
            fmt = "<%d%s" % (n, "f" if struct == "FloatProperty" else "i")
            return "count=%d %s" % (n, struct.unpack(fmt, body[:n * 4]))
        return "count=%d raw=%s" % (n, body[:32].hex())
    return val[:24].hex()


def main(argv):
    if not argv:
        print(__doc__); return 2
    uas = Path(argv[0])
    up = uas.with_suffix(".u")
    a = Asset(uas, up if up.exists() else None)
    print("legacy=%d  header=%d  flags=0x%08X" % (a.legacy, a.total_header, a.flags))
    print("names=%d  imports=%d  exports=%d  payload=%d"
          % (len(a.names), len(a.imports), len(a.exports), len(a.payload)))
    print("folder=%r" % a.folder)
    kws = []
    props = None
    args = argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--names":
            kws.append(args[i + 1].lower()); i += 2
        elif args[i] == "--props":
            props = args[i + 1]; i += 2
        else:
            i += 1
    if props:
        target = None
        for e in a.exports:
            if props.lower() in e["name"].lower():
                target = e
                break
        if not target:
            print("找不到 export: %s" % props); return 1
        print("\n=== %s 的属性（%d 字节）===" % (target["name"], target["size"]))
        for name, typ, stype, size, inline, val in dump_props(a, target):
            if typ == "BoolProperty":
                print("  %-42s %-16s size=%-5d = %s" % (name, typ, size, inline))
            else:
                print("  %-42s %-16s size=%-5d%s = %s"
                      % (name, typ, size, ("<" + stype + ">") if stype else "",
                         show(val, typ, size)))
        return 0
    if kws:
        print("\n=== 名字命中 ===")
        for k, s in enumerate(a.names):
            if any(x in s.lower() for x in kws):
                print("  [%3d] %s" % (k, s))
    print("\n=== 前 20 个 export ===")
    for e in a.exports[:20]:
        print("  %-40s cls=%-30s size=%d off=%d"
              % (e["name"][:40], a.obj_name(e["cls"]) or "?", e["size"], e["off"]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
