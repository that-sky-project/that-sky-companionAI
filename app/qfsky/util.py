# -*- coding: utf-8 -*-
"""通用协议工具：RC4 / FNV-1a / varint / hex —— 移植自 :core Kotlin（SkyApi.rc4、EnetPacket）。"""
from __future__ import annotations

import hashlib
import uuid
import zlib


def rc4(key: bytes, data: bytes) -> bytes:
    """与 Kotlin rc4 完全一致（KSA+PRGA，加解密对称）。"""
    if not key:
        raise ValueError("RC4 key 不能为空")
    s = list(range(256))
    j = 0
    for i in range(256):
        j = (j + s[i] + key[i % len(key)]) & 0xFF
        s[i], s[j] = s[j], s[i]
    out = bytearray(len(data))
    i = j = 0
    for k, b in enumerate(data):
        i = (i + 1) & 0xFF
        j = (j + s[i]) & 0xFF
        s[i], s[j] = s[j], s[i]
        out[k] = b ^ s[(s[i] + s[j]) & 0xFF]
    return bytes(out)


def fnv1a(data: bytes) -> int:
    """peer_key 派生（FNV-1a 32bit），测试向量 0x90fb39ac / 0x066d9204 已对拍。"""
    h = 0x811C9DC5
    for b in data:
        h ^= b
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h


def derive_peer_key(uid: str) -> int:
    """uid(UUID 字符串) → 16B → FNV-1a u32。对齐 RandomJoinTemplate.derivePeerKey。"""
    return fnv1a(uuid.UUID(uid).bytes)


def read_varint(data: bytes, start: int) -> tuple[int, int]:
    value = 0
    shift = 0
    pos = start
    while pos < len(data):
        b = data[pos]
        pos += 1
        value |= (b & 0x7F) << shift
        if not (b & 0x80):
            return value, pos
        shift += 7
        if shift > 35:
            break
    raise ValueError("bad varint")


def write_varint(value: int) -> bytes:
    out = bytearray()
    v = value & 0xFFFFFFFF
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def md5_hex(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def crc32(*parts: bytes) -> int:
    c = 0
    for p in parts:
        c = zlib.crc32(p, c)
    return c & 0xFFFFFFFF


def random_trace() -> str:
    import random
    import string
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=7))
