# -*- coding: utf-8 -*-
"""auth/login 设备身份三件套（device_key / sig_ts / sig）。

移植 SkyDeviceKey.generateEphemeral：P-256 压缩公钥 b64 + ECDSA-SHA256 签名。
"""
from __future__ import annotations

import base64
import time

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec


def generate_ephemeral() -> tuple[str, int, str]:
    """返回 (device_key, sig_ts, sig)。"""
    priv = ec.generate_private_key(ec.SECP256R1())
    nums = priv.public_key().public_numbers()
    prefix = 0x03 if (nums.y & 1) else 0x02
    device_key = base64.b64encode(bytes([prefix]) + nums.x.to_bytes(32, "big")).decode("ascii").strip()
    sig_ts = int(time.time())
    sig = priv.sign((device_key + str(sig_ts)).encode("ascii"), ec.ECDSA(hashes.SHA256()))
    return device_key, sig_ts, base64.b64encode(sig).decode("ascii").strip()
