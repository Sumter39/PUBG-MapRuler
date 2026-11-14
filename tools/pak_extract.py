"""从 PUBG pak 提取资源（自研索引解析 + oo2core_9 Oodle 解压，只读）。

索引布局（PUBG 自定义 pak，索引 AES-ECB 加密）：
  mount FString → count i32 → 每 entry:
    name FString（len 含结尾 \0）
    元数据:
      +0  u64 A = 数据区起始（= 上一条 entry 的数据区结束）
      +8  u64 B = 压缩总大小（单块时 = blockEnd-blockStart）
      +16 u64 C = 解压后大小
      +24 u32 = 16（常量）
      +28 hash 20B
      +48 u32 comp（1=单块 Oodle，2/4=多块 Oodle，0=原样存储）
      +52 块列表：连续 (start u64, end u64)，首尾相接（comp!=0 时存在）
      块后：u8(0)，若随后 u32 == C 或 65536 再吞 4B（判别法：跳过后能
      解析出合法下一个名字；名字首字符 ASCII≥32 保证不会误判）
  数据区 = [A 起 meta_size 字节的元数据副本][各块压缩数据，块 i 解出
           min(65536, 剩余) 字节（多块时）或 C（单块时）]
key = TslGame.exe 内嵌 hex 字符串的 ASCII 本身（AES-256）。
"""
import os, struct, ctypes, sys
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

KEY = b'45DD15D6DD2DA50AEB71CE7A5284CF8E'
MAGIC = 0x5A6F12E1
OODLE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     '..', 'vendor', 'ooz', 'oo2core_9_win64.dll')

_oo = ctypes.CDLL(OODLE)
_oo.OodleLZ_Decompress.restype = ctypes.c_ssize_t
# 经典 14 参签名: (src, srcLen, dst, dstLen, fuzzSafe, checkCRC, verbosity,
#                 decThreadBase, decThreadSize, decThreadMem,
#                 cbFree, cbProg, cbProgUserData, verboseSeconds)
_oo.OodleLZ_Decompress.argtypes = [ctypes.c_char_p, ctypes.c_ssize_t,
                                   ctypes.c_char_p, ctypes.c_ssize_t] + \
                                  [ctypes.c_int] * 3 + [ctypes.c_void_p] * 6 + [ctypes.c_int]

def oodle_decompress(src, expected):
    dst = ctypes.create_string_buffer(expected)
    n = _oo.OodleLZ_Decompress(src, len(src), dst, expected,
                               1, 1, 0, None, None, None, None, None, None, 3)
    return dst.raw[:n] if 0 < n <= expected else None

def footer(path):
    data = open(path, 'rb').read()
    p = data.rfind(struct.pack('<I', MAGIC))
    assert len(data) - p == 44
    off, isz = struct.unpack_from('<QQ', data, p + 8)
    return data, off, isz

def parse_index(data, off, isz):
    idx = data[off:off + isz]
    c = Cipher(algorithms.AES(KEY), modes.ECB()).decryptor()
    dec = c.update(idx) + c.finalize()
    def rs(p):
        if p + 4 > len(dec):
            return None, p
        n, = struct.unpack_from('<i', dec, p)
        if not (0 < n < 1024):
            return None, p
        s = dec[p + 4:p + n]
        if not all(32 <= b < 127 or b == 0 for b in s) or not s.rstrip(b'\x00'):
            return None, p
        return s.rstrip(b'\x00'), p + 4 + n
    mount, pos = rs(0)
    mount = mount.decode()
    n, = struct.unpack_from('<i', dec, pos); pos += 4
    entries = []
    for _ in range(n):
        name, p2 = rs(pos)
        if name is None:
            raise ValueError(f'bad name at {pos}')
        A, B, C = struct.unpack_from('<QQQ', dec, p2)
        comp, = struct.unpack_from('<I', dec, p2 + 48)
        pos = p2 + 52
        blocks = []
        if comp != 0:  # 1=单块，2/4=多块（实测 comp=4 与 2 布局一致）
            prev_end = None
            while pos + 16 <= len(dec):
                s, e = struct.unpack_from('<QQ', dec, pos)
                if e <= s or (prev_end is not None and s != prev_end):
                    break
                blocks.append((s, e))
                prev_end = e
                pos += 16
            block_hint = None
            if pos < len(dec) and dec[pos] == 0:
                pos += 1
        else:  # comp == 0 原样存储
            if pos < len(dec) and dec[pos] == 0:
                pos += 1
            block_hint = None
        # pad 后可能跟一个 u32（单块=C，多块=块解压尺寸 65536）。
        # 判别：若跳过它后能解析出合法下一个名字，则它属于本条目。
        if comp != 0 and pos + 8 <= len(dec):
            v, = struct.unpack_from('<I', dec, pos)
            ok_skip, _ = rs(pos + 4)
            if ok_skip is not None:
                block_hint = v
                pos += 4
        entries.append(dict(name=name.decode(), comp=comp, csize=B, usize=C,
                            blocks=blocks, region=A, block_hint=block_hint))
    return mount, entries

def extract(pak_path, out_dir, keyword=None):
    data, off, isz = footer(pak_path)
    mount, entries = parse_index(data, off, isz)
    ok = fail = 0
    for e in entries:
        if keyword and keyword not in e['name'].lower():
            continue
        rel = e['name'][len(mount):].lstrip('/') if e['name'].startswith(mount) else e['name']
        dst = os.path.join(out_dir, rel)
        os.makedirs(os.path.dirname(dst) or out_dir, exist_ok=True)
        try:
            if e['comp'] == 0:
                a = e['region']
                out = data[a + 53:a + 53 + e['usize']]
                out = out if len(out) == e['usize'] else None
            else:
                hint = e['block_hint'] or 0
                out = bytearray()
                remaining = e['usize']
                for s, t in e['blocks']:
                    want = min(hint, remaining) if hint and len(e['blocks']) > 1 \
                        else remaining
                    chunk = oodle_decompress(data[s:t], want)
                    if chunk is None:
                        out = None
                        break
                    out += chunk
                    remaining -= len(chunk)
                out = bytes(out) if out is not None and remaining == 0 else None
        except Exception:
            out = None
        if out is None:
            fail += 1
            continue
        open(dst, 'wb').write(out)
        ok += 1
    return mount, len(entries), ok, fail

if __name__ == '__main__':
    pak, out = sys.argv[1], sys.argv[2]
    kw = sys.argv[3].lower() if len(sys.argv) > 3 else None
    mount, total, ok, fail = extract(pak, out, kw)
    print(f'mount={mount} total={total} extracted={ok} failed={fail}')
