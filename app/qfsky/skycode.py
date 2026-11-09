# -*- coding: utf-8 -*-
"""Friend-code encoding helper; no real-account examples are distributed."""
from __future__ import annotations

import struct

from .skyapi import SkyApiError

P_CODE_VERIFY = "/account/code/verify"

# Crockford 变体：去掉了 I / L / O / U（防手抄混淆）
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_LEN = 12          # 去横线后的字符数
CODE_GROUP = 4         # 显示时按 4 位分组的宽度 → XXXX-XXXX-XXXX
_UUID_HEX_LEN = 32


def _strip(code: str) -> str:
    """去掉分隔符与大小写差异，只留字母表内的字符。"""
    s = str(code or "").strip().upper()
    return "".join(ch for ch in s if ch.isalnum())


def normalize_friend_code(code: str) -> str:
    """把用户输入归一成规范形态：短码 → XXXX-XXXX-XXXX；32 位 uuid → 原样小写。

    手抄常见的坑一起吃掉：小写、空格、全角横线、少写/多写横线、
    把字母表里的 O/I/L 误写成 0/1（Crockford 变体本来就没有 O/I/L，
    玩家照着抄经常会抄成数字）。
    """
    raw = str(code or "").strip()
    if not raw:
        return ""
    hexonly = raw.replace("-", "").replace(" ", "").lower()
    if len(hexonly) == _UUID_HEX_LEN and all(c in "0123456789abcdef" for c in hexonly):
        return "%s-%s-%s-%s-%s" % (
            hexonly[0:8], hexonly[8:12], hexonly[12:16], hexonly[16:20], hexonly[20:32])
    s = _strip(raw).replace("O", "0").replace("I", "1").replace("L", "1")
    if len(s) == CODE_LEN:
        return "-".join(s[i:i + CODE_GROUP] for i in range(0, CODE_LEN, CODE_GROUP))
    return raw


def is_short_code(code: str) -> bool:
    """看起来是不是一个短码（12 位、字母表内）。"""
    s = _strip(code)
    if len(s) != CODE_LEN:
        return False
    return all(ch in ALPHABET for ch in s.replace("O", "0").replace("I", "1").replace("L", "1"))


def uuid_to_short_code(uuid_str: str) -> str:
    """Friend-code encoding helper; no real-account examples are distributed."""
    h = str(uuid_str or "").replace("-", "").strip().lower()
    if len(h) != _UUID_HEX_LEN:
        raise ValueError("uuid 须为 32 位 hex，实得 %d 位" % len(h))
    try:
        b = bytes.fromhex(h)
    except ValueError:
        raise ValueError("uuid 含非法 hex 字符：%s" % uuid_str)

    val = (b[8] & 0x07) << 56
    val |= b[9] << 48
    val |= b[10] << 40
    val |= b[11] << 32
    u32 = struct.unpack_from("<I", b, 12)[0]
    u32 = ((u32 >> 16) | (u32 << 16)) & 0xFFFFFFFF
    u32 = ((u32 & 0xFF00FF00) >> 8) | ((u32 & 0x00FF00FF) << 8)
    val |= u32

    chars = []
    for _ in range(CODE_LEN):
        chars.append(ALPHABET[val & 0x1F])
        val >>= 5
    chars.reverse()          # ← 这一步是关键：整体反转才是玩家看到的顺序
    s = "".join(chars)
    return "-".join(s[i:i + CODE_GROUP] for i in range(0, CODE_LEN, CODE_GROUP))


def try_uuid_to_short_code(uuid_str: str) -> str:
    """短码化的容错版：算不出来就回空串（用于列表批量渲染，绝不因一行炸整页）。"""
    try:
        return uuid_to_short_code(uuid_str)
    except Exception:
        return ""


def code_to_uuid(client, code: str) -> dict:
    """短码 → **邀请令牌 uuid**（必须查服务端，本地算不出来）。

    返回 {"found", "uuid", "type", "code", "result", "zh"}。
    ★ 这里回来的 `uuid` 是**邀请令牌**（`invite_token_id`），不是玩家 uuid ——
      要拿玩家 uuid 得再 `accept_invite`，回包里的 `invite_friend_id` 才是。
      （`type` 实测为 "i" = invite。）
    打不通服务端（限流/掉线）时抛 RuntimeError，由路由翻成中文。
    """
    import re
    norm = normalize_friend_code(code)
    if not norm:
        raise RuntimeError("请先填写好友码")
    if not (is_short_code(norm) or re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", norm)):
        raise RuntimeError("这不像一个好友码：要么是 12 位短码（XXXX-XXXX-XXXX），要么是 32 位光遇 ID")
    try:
        resp = client.post(P_CODE_VERIFY, {"code": norm}) or {}
    except SkyApiError as e:
        st = getattr(e, "status", None)
        if st in (422, 429):
            raise RuntimeError("网易出口限流中（HTTP %s），等几秒再试" % st) from e
        if st == 401:
            raise RuntimeError("这个机位的登录已经失效（HTTP 401），去机位列表重新登录再试") from e
        raise RuntimeError("查好友码失败（sky api HTTP %s）" % st) from e

    result = str(resp.get("result") or "")
    uuid = str(resp.get("code") or "").strip()
    if result == "ok" and uuid:
        return {
            "found": True, "uuid": uuid, "type": str(resp.get("type") or ""),
            "code": norm, "result": result, "zh": "码有效",
        }
    if result == "throttle":
        return {
            "found": False, "uuid": "", "type": "",
            "code": norm, "result": result,
            "cooldown": int(resp.get("cooldown") or 0),
            "zh": "查得太快了，等 %d 秒再试" % int(resp.get("cooldown") or 0),
        }
    _ZH = {
        "not_found": "查不到这个码。多半是抄错了（差一位、多一位都会 not_found），"
                     "或者这个码不是「创建好友码」那种邀请码。",
        "expired": "这个码已经过期了",
        "not_available": "这个码已经被用过了 —— 好友码是一次性的，"
                         "回游戏里重新创建一个再来查",
    }
    return {
        "found": False, "uuid": "", "type": str(resp.get("type") or ""),
        "code": norm, "result": result,
        "zh": _ZH.get(result, result or "服务端没有给出结果"),
    }
