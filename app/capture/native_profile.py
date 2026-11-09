# -*- coding: utf-8 -*-
"""APK 0.16.7 / 421589 的 native 库身份与账号绑定算法（只读）。"""
from __future__ import annotations

import hashlib
import re

BUILD = 421589
NATIVE_SHA256 = "0b81f1da54d9c6f28c08746b2ec19519d8eb78c4f1b3c5e4e420ff726dc25321"
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
