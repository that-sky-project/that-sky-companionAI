"""从光遇 APK 的 resources.arsc 读出全部字符串资源（2026-10-31，0.16.7 / 421589，MuMu 安装包里解出）。

只做最小解析：全局字符串池 + 每个包的类型名/键名池 + RES_TABLE_TYPE 条目（只取值类型为字符串的简单条目）。
按语言配置分组：{"": {键: 文本}, "en": {...}, ...}；国服包默认配置（无语言）就是简体中文。
用法：python arsc_strings.py <resources.arsc> [输出 json]
"""
import json
import struct
import sys

RES_STRING_POOL, RES_TABLE, RES_TABLE_PACKAGE = 0x0001, 0x0002, 0x0200
RES_TABLE_TYPE, RES_TABLE_TYPE_SPEC = 0x0201, 0x0202
TYPE_STRING = 0x03
FLAG_COMPLEX, FLAG_SPARSE, UTF8_FLAG = 0x0001, 0x01, 0x100


def _len8(b, o):
    n = b[o]
    if n & 0x80:
        return ((n & 0x7F) << 8) | b[o + 1], o + 2
    return n, o + 1


def _len16(b, o):
    n = struct.unpack_from("<H", b, o)[0]
    if n & 0x8000:
        return ((n & 0x7FFF) << 16) | struct.unpack_from("<H", b, o + 2)[0], o + 4
    return n, o + 2


def string_pool(b, start):
    _t, hsize, size, count, _styles, flags, strings_start, _styles_start = struct.unpack_from("<HHIIIIII", b, start)
    offs = struct.unpack_from("<%dI" % count, b, start + hsize)
    base = start + strings_start
    out = []
    for off in offs:
        o = base + off
        if flags & UTF8_FLAG:
            _u16len, o = _len8(b, o)
            n, o = _len8(b, o)
            out.append(b[o:o + n].decode("utf-8", errors="replace"))
        else:
            n, o = _len16(b, o)
            out.append(b[o:o + n * 2].decode("utf-16-le", errors="replace"))
    return out, start + size


def _locale(cfg):
    """ResTable_config 里的 language/country（各 2 字节）→ "zh-CN" 之类；默认配置返回 ""。"""
    if len(cfg) < 12:
        return ""
    lang, country = cfg[8:10], cfg[10:12]

    def txt(x):
        if x == b"\0\0":
            return ""
        if x[0] & 0x80:      # 3 字母打包编码，光遇用不到，保留原样
            return x.hex()
        return x.decode("ascii", errors="replace")
    l, c = txt(lang), txt(country)
    return l + ("-" + c if c else "")


def parse(data):
    t, hsize, _size, _pkgs = struct.unpack_from("<HHII", data, 0)
    assert t == RES_TABLE, "不是 resources.arsc"
    values, o = string_pool(data, hsize)
    out = {}
    while o < len(data):
        ct, chs, csize = struct.unpack_from("<HHI", data, o)
        if ct == RES_TABLE_PACKAGE:
            _parse_package(data, o, chs, csize, values, out)
        o += csize
    return out


def _parse_package(b, start, hsize, size, values, out):
    type_off, _last_type, key_off = struct.unpack_from("<III", b, start + 8 + 4 + 256)
    types, _ = string_pool(b, start + type_off)
    keys, _ = string_pool(b, start + key_off)
    o, end = start + hsize, start + size
    while o < end:
        ct, chs, csize = struct.unpack_from("<HHI", b, o)
        if ct == RES_TABLE_TYPE:
            type_id, flags = b[o + 8], b[o + 9]
            count, entries_start = struct.unpack_from("<II", b, o + 12)
            cfg_size = struct.unpack_from("<I", b, o + 20)[0]
            cfg = b[o + 20:o + 20 + cfg_size]
            if types[type_id - 1] == "string" and not flags & FLAG_SPARSE:
                table = out.setdefault(_locale(cfg), {})
                offs = struct.unpack_from("<%dI" % count, b, o + chs)
                for off in offs:
                    if off == 0xFFFFFFFF:
                        continue
                    e = o + entries_start + off
                    esize, eflags, key = struct.unpack_from("<HHI", b, e)
                    if eflags & FLAG_COMPLEX:
                        continue
                    _vsize, _res0, vtype, vdata = struct.unpack_from("<HBBI", b, e + esize)
                    if vtype == TYPE_STRING:
                        table[keys[key]] = values[vdata]
        o += csize


if __name__ == "__main__":
    res = parse(open(sys.argv[1], "rb").read())
    print({k: len(v) for k, v in res.items()})
    if len(sys.argv) > 2:
        json.dump(res, open(sys.argv[2], "w", encoding="utf-8"), ensure_ascii=False)
