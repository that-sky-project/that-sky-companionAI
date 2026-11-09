# -*- coding: utf-8 -*-
"""装扮（衣柜）/身高/无翼：移植 SkyGameInfoApi 国服分支（get_outfit / set_outfit）。

⚠️ set_outfit 为**整套覆盖语义**（06-26 真机实证：只提交 wing 一项 → 其余 8 部位全清），
任何修改必须先拉完整装扮、深拷贝、只改目标字段、整套回灌。
身高三值公式（同源国服后端重构 extension_page.js）：
  max = 7.6 - 8.3*scale - 6；min = 7.6 - 8.3*scale + 6；current = 7.6 - 8.3*scale - 3*height
"""
from __future__ import annotations

P_OUTFIT = "/account/get_outfit"
P_SET_OUTFIT = "/account/set_outfit"

# 槽位顺序与中文名，对齐游戏官方 9 格（3×3）：发型/斗篷/裤子 头饰/颈部/面具 发饰/背饰/鞋子
OUTFIT_SLOTS = [
    ("hair", "发型"), ("wing", "斗篷"), ("body", "裤子"),
    ("horn", "头饰"), ("neck", "颈部"), ("mask", "面具"),
    ("hat", "发饰"), ("prop", "背饰"), ("feet", "鞋子"),
]

# 新号初始化默认装扮（知识库 12 真机 set_outfit 实录：10 部位 + height/scale）
DEFAULT_OUTFIT_INIT = {
    "body": {"id": 3691343236, "tex": 0, "pat": 0, "mask": 0, "dye": "(none,none)"},
    "wing": {"id": 2245279351, "tex": 0, "pat": 0, "mask": 0, "dye": "(none,none)"},
    "hair": {"id": 1229053584, "tex": 0, "pat": 0, "mask": 0, "dye": "(none,none)"},
    "mask": {"id": 3663390882, "tex": 0, "pat": 0, "mask": 0, "dye": "(none,none)"},
    "neck": {"id": 3800884691, "tex": 0, "pat": 0, "mask": 0, "dye": "(none,none)"},
    "feet": {"id": 1532093990, "tex": 0, "pat": 0, "mask": 0, "dye": "(none,none)"},
    "horn": {"id": 3680499229, "tex": 0, "pat": 0, "mask": 0, "dye": "(none,none)"},
    "face": {"id": 3599986885, "tex": 0, "pat": 0, "mask": 0, "dye": "(none,none)"},
    "prop": {"id": 2035109393, "tex": 0, "pat": 0, "mask": 0, "dye": "(none,none)"},
    "hat": {"id": 1470462579, "tex": 0, "pat": 0, "mask": 0, "dye": "(none,none)"},
    "height": 0.00024390705,
    "scale": 0.026282763,
    "voice": 0, "attitude": 0, "seed": 0, "refreshversion": 0,
}


def _data(resp: dict) -> dict:
    if isinstance(resp, dict) and isinstance(resp.get("data"), dict):
        return resp["data"]
    return resp if isinstance(resp, dict) else {}


def outfit_set(outfit: dict, key: str = "set_outfit") -> dict | None:
    """装扮响应里的装扮对象（数组取首项 / 直接对象）。

    key：看自己是 `set_outfit`，看别人是 `get_remote_outfit`（见 qfsky/lookup.py）。
    两者内部字段完全同构（部位 + scale/height/voice/seed），所以共用一套解析。
    """
    data = _data(outfit)
    v = data.get(key)
    if isinstance(v, list) and v and isinstance(v[0], dict):
        return v[0]
    if isinstance(v, dict):
        return v
    return None


def is_should_sync(resp: dict) -> bool:
    """账号尚无装扮数据：{"result":"should_sync"}。"""
    if not isinstance(resp, dict):
        return False
    if resp.get("result") == "should_sync":
        return True
    d = resp.get("data")
    return isinstance(d, dict) and d.get("result") == "should_sync"


def _fmt(x: float) -> str:
    r = round(x * 100) / 100.0
    if r == int(r):
        return str(int(r))
    return ("%.2f" % r).rstrip("0").rstrip(".")


def parse_height(outfit: dict, key: str = "set_outfit") -> dict | None:
    """身高三值；缺 scale → None。"""
    s = outfit_set(outfit, key)
    if not s:
        return None
    scale = s.get("scale")
    if not isinstance(scale, (int, float)) or isinstance(scale, bool):
        return None
    height = s.get("height") if isinstance(s.get("height"), (int, float)) else None
    base = 7.6 - 8.3 * float(scale)
    return {
        "max": _fmt(base - 6),
        "min": _fmt(base + 6),
        "current": _fmt(base - 3 * float(height)) if height is not None else "--",
    }


def parse_outfit_parts(outfit: dict, key: str = "set_outfit") -> list[dict]:
    """各部位 id（>0 才收）。"""
    s = outfit_set(outfit, key) or {}
    parts = []
    for key, label in OUTFIT_SLOTS:
        o = s.get(key)
        if isinstance(o, dict):
            try:
                fid = int(o.get("id") or 0)
            except (TypeError, ValueError):
                fid = 0
            if fid > 0:
                parts.append({"slot": key, "label": label, "id": fid})
    return parts


def get_or_init_outfit(client) -> dict:
    """取装扮；should_sync（新号）先 set_outfit 初始化再重新 get。"""
    first = client.post(P_OUTFIT, {})
    if not is_should_sync(first):
        return first
    client.post(P_SET_OUTFIT, {"outfit": DEFAULT_OUTFIT_INIT})
    return client.post(P_OUTFIT, {})


def summarize_outfit(resp: dict, key: str = "set_outfit") -> dict:
    """刷新任务/端点展示用的压缩摘要：衣柜部位 / 无翼 / 身高三值。"""
    s = outfit_set(resp, key) or {}
    parts = parse_outfit_parts(resp, key)
    wing = s.get("wing") if isinstance(s.get("wing"), dict) else None
    try:
        wing_id = int(wing.get("id") or 0) if wing else 0
    except (TypeError, ValueError):
        wing_id = 0
    return {
        "parts": parts,
        "part_count": len(parts),
        "wingless": wing_id <= 0,
        "height": parse_height(resp, key),
    }


def set_wingless(client) -> dict:
    """无翼：拉完整装扮 → 仅 wing.id=0（其余/身高原封不动）→ 整套回灌。
    游戏内再换装扮会触发整套重新同步 → 无翼失效。"""
    current = get_or_init_outfit(client)
    s = outfit_set(current)
    if not s:
        raise RuntimeError("未取到当前装扮数据")
    import copy
    outfit = copy.deepcopy(s)
    if isinstance(outfit.get("wing"), dict):
        outfit["wing"]["id"] = 0
    else:
        outfit["wing"] = {"id": 0}
    resp = client.post(P_SET_OUTFIT, {"outfit": outfit})
    if outfit_set(resp) is not None:
        return resp
    return client.post(P_OUTFIT, {})


def apply_outfit(client, slot_to_id: dict) -> dict:
    """衣柜换装：只覆盖指定部位 id，其余保持原样（整套覆盖语义下的安全改法）。"""
    current = get_or_init_outfit(client)
    s = outfit_set(current)
    if not s:
        raise RuntimeError("未取到当前装扮数据")
    import copy
    outfit = copy.deepcopy(s)
    for slot, fid in (slot_to_id or {}).items():
        fid = int(fid or 0)
        if fid <= 0:
            continue
        part = outfit.get(slot)
        if isinstance(part, dict):
            part["id"] = fid
        else:
            outfit[slot] = {"id": fid, "tex": 0, "pat": 0, "mask": 0, "dye": "(none,none)"}
    resp = client.post(P_SET_OUTFIT, {"outfit": outfit})
    if outfit_set(resp) is not None:
        return resp
    return client.post(P_OUTFIT, {})
