"""只读 pak 索引：列出资源名，不解内容、不落盘。

    python tools/pak_index.py <pak 或目录> [--grep 关键字 ...]

只 seek 读文件末尾 44 字节 footer + 索引区，几十 MB 的 pak 也是秒级。
"""
import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "src"))

import pak_extract as P                # noqa: E402


def read_index(path):
    with open(path, "rb") as f:
        f.seek(-44, 2)
        ft = f.read(44)
        if ft[:4] != struct.pack("<I", P.MAGIC):
            raise ValueError("footer magic 不对")
        off, isz = struct.unpack_from("<QQ", ft, 8)
        f.seek(off)
        return f.read(isz)


def names_of(path):
    idx = read_index(path)
    mount, entries = P.parse_index(idx, 0, len(idx))
    return mount, [e["name"] for e in entries]


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    target = Path(argv[0])
    kws = []
    args = argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--grep":
            kws.append(args[i + 1].lower()); i += 2
        else:
            i += 1

    paks = [target] if target.is_file() else sorted(target.glob("*.pak"))
    for pak in paks:
        try:
            mount, names = names_of(pak)
        except Exception as e:                        # noqa: BLE001
            print("%-52s 失败: %s" % (pak.name[:52], e))
            continue
        hit = [n for n in names
               if not kws or all(k in n.lower() for k in kws)]
        print("%-52s 条目 %6d  命中 %5d  mount=%s"
              % (pak.name[:52], len(names), len(hit), mount))
        if kws:
            for n in hit[:400]:
                print("    " + n)
            if len(hit) > 400:
                print("    …还有 %d 条" % (len(hit) - 400))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
