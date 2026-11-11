# -*- coding: utf-8 -*-
"""APK 0.16.8 / 421629 的 native 库身份与账号绑定算法（只读）。

账号绑定域故意保持 421589：STAGING-NOTES 第 2 条要求所有历史录制/目录的
account_binding 继续匹配；域字符串不是 APK 版本号，不能跟着 build 改。
"""
from __future__ import annotations

import hashlib
import re

BUILD = 421629
NATIVE_SHA256 = "0b18ecc44e873f6caa181e6bc49f14b7e07058888523e47db52a14b32077a7ed"
# 兼容旧录制：域固定为 421589，不随 BUILD 变。
ACCOUNT_DOMAIN = b"sky-cn-421589-level-cache-account-v1\x00"
BINDING_ALGORITHM = "sha256-domain-sky-cn-421589-level-cache-account-v1"
UUID_RE = re.compile(r"(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})")


def uuid_raw(value):
    if isinstance(value, (bytes, bytearray)):
        if len(value) != 16:
            raise ValueError("账号 uuid 必须是 16 字节")
        return bytes(value)
    if not isinstance(value, str) or not UUID_RE.fullmatch(value):
        raise ValueError("账号 uuid 形状不对")
    return bytes.fromhex(value.replace("-", ""))


def account_binding(value):
    return hashlib.sha256(ACCOUNT_DOMAIN + uuid_raw(value)).hexdigest()
