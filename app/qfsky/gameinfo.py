# -*- coding: utf-8 -*-
"""游戏数据：货币解析（get_currency）与账号摘要。移植 SkyGameInfoApi 国服分支。

注：货币合成（/account/buy_candle_wax）已按用户要求下线，协议本身仍记录在
`docs/知识库/协议与逆向/18_国际服完整接口文档.md` 与 `10_游戏请求接口.md`，需要时可据此恢复。
"""
from __future__ import annotations

from .skyapi import SkyClient


def _long(v) -> int:
    if v is None:
        return 0
    if isinstance(v, bool):
        return 1 if v else 0
    if isinstance(v, (int, float)):
        return int(v)
    try:
        return int(str(v))
    except ValueError:
        return 0


def _text(v) -> str:
    if v is None:
        return "0"
    if isinstance(v, str):
        return v if v.strip() else "0"
    return str(v)


CURRENCY_FIELDS = [
    ("candles", "long"), ("heart", "long"), ("prestige", "long"),
    ("season_candle", "long"), ("season_heart", "long"), ("season_pass_token", "long"),
    ("wax", "text"), ("heart_wax", "text"), ("prestige_wax", "text"), ("season_wax", "text"),
    ("color_black", "long"), ("color_white", "long"), ("color_blue", "long"), ("color_cyan", "long"),
    ("color_green", "long"), ("color_magenta", "long"), ("color_red", "long"), ("color_yellow", "long"),
    ("color_black_wax", "long"), ("color_white_wax", "long"), ("color_blue_wax", "long"),
    ("color_cyan_wax", "long"), ("color_green_wax", "long"), ("color_magenta_wax", "long"),
    ("color_red_wax", "long"), ("color_yellow_wax", "long"),
]


def parse_currency(resp: dict) -> dict:
    c = resp.get("currency") if isinstance(resp.get("currency"), dict) else resp
    out = {}
    for key, kind in CURRENCY_FIELDS:
        v = c.get(key)
        out[key] = _text(v) if kind == "text" else _long(v)
    # 活动代币键动态透传（event_candle_*：蜡烛键 + _wax 键；活动轮换出现新键自动带上）
    for key, v in c.items():
        if isinstance(key, str) and key.startswith("event_candle_") and key not in out:
            out[key] = _long(v)
    return out


def get_currency(client: SkyClient) -> dict:
    return parse_currency(client.get_currency())


def get_full_summary(client: SkyClient) -> dict:
    """聚合：货币 + 好友数 + 收藏数 + 光翼。单个接口失败不拖垮整体。"""
    summary: dict = {}
    try:
        summary["currency"] = get_currency(client)
    except Exception as e:
        summary["currency_error"] = str(e)
    try:
        friends = client.get_friends()
        data = friends.get("data") if isinstance(friends.get("data"), dict) else friends
        arr = data.get("friends") or data.get("gist") or []
        summary["friends_count"] = len(arr) if isinstance(arr, list) else 0
    except Exception:
        summary["friends_count"] = None
    try:
        coll = client.get_collectibles()
        data = coll.get("data") if isinstance(coll.get("data"), dict) else coll
        summary["collectibles_count"] = len(data) if isinstance(data, (list, dict)) else 0
    except Exception:
        summary["collectibles_count"] = None
    try:
        wing = client.get_wing_buffs()
        data = wing.get("data") if isinstance(wing.get("data"), dict) else wing
        buffs = data.get("wing_buffs") or data.get("buffs") or []
        summary["wing_buffs_count"] = len(buffs) if isinstance(buffs, list) else 0
    except Exception:
        summary["wing_buffs_count"] = None
    return summary
