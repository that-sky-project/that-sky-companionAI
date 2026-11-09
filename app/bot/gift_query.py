"""小光查好友的礼包（2026-10-31）：用小光自己的号读 POST /account/commerce/friend_info（只读，礼包服务同款接口）。

条件：对方是小光好友，且好友树解锁到“送礼”那一层（否则接口不给 / allowed 为假）。
返回里：update_friend_commerce.purchased_non_consumables = 对方已拥有的礼包 SKU；
       netease_gift_iap_state.gift_iap_list = 当前可赠送的礼包目录（name 是本地化键、price 是 base64 的“¥98.00”）。
中文名：bot/gift_names.json（客户端 resources.arsc 的 commerce_item_name_* + QQ 插件礼包目录的 SKU→名字）。
早期 NC* 等已下架礼包客户端里没有名字，如实标“早期礼包（编号 …）”。
"""
import base64
import json
import re
import threading
import time
from pathlib import Path

import xg_paths

NAMES = xg_paths.load_name_table("gift_names.json", {})   # 用户机上生成；没有就是空表
CACHE_SECONDS = 300
_cache = {}
_lock = threading.Lock()


def _price(raw):
    """“wqU5OC4wMA==” → (“¥98.00”, 98.0)；解不出返回 (None, None)。"""
    try:
        text = base64.b64decode(raw).decode("utf-8").strip()
        m = re.search(r"\d+(?:\.\d+)?", text)
        return text, float(m.group()) if m else None
    except Exception:
        return None, None


def _name(sku, entry):
    key = (entry or {}).get("name")
    return NAMES["keys"].get(key) or NAMES["skus"].get(sku)


def query(client, friend_uuid, map_id=817373972):
    """→ {"ok": True, "owned": [...], "catalog": [...]} 或 {"ok": False, "why": "not_allowed"/"failed", ...}。
    owned/catalog 每项 {"sku", "name"（可能 None）, "price"（“¥98.00”或 None）, "yuan"（数字或 None）, "owned"}。"""
    now = time.monotonic()
    with _lock:
        hit = _cache.get(friend_uuid)
        if hit and now - hit[0] < CACHE_SECONDS:
            return hit[1]
    try:
        r = client.post("/account/commerce/friend_info", {"platform": "", "friend_id": friend_uuid},
                        include_level_id=True, level_id=str(map_id), timeout=15, attempts=2)
    except Exception as exc:
        text = str(exc)
        # 只有明确「不允许」才算没解锁送礼；HTTP 400/路径里带 commerce 都是接口/会话问题，
        # 之前把它们判成 not_allowed，用户明明解锁了也被说成没解锁。
        why = "not_allowed" if re.search(r"(?i)not.?allowed|forbidden|HTTP 403", text) else "failed"
        return {"ok": False, "why": why, "error": text[:160]}
    commerce = r.get("update_friend_commerce") or {}
    state = r.get("netease_gift_iap_state") or {}
    if commerce.get("allowed") is not True:
        return {"ok": False, "why": "not_allowed"}
    owned_ids = [x.get("product_id") for x in commerce.get("purchased_non_consumables") or [] if x.get("product_id")]
    owned_set = set(owned_ids)
    catalog = {}
    for e in state.get("gift_iap_list") or []:
        sku = e.get("id")
        if not sku:
            continue
        price, yuan = _price(e.get("price") or "")
        catalog[sku] = {"sku": sku, "name": _name(sku, e), "price": price, "yuan": yuan, "owned": sku in owned_set}
    owned = []
    for sku in owned_ids:
        item = dict(catalog.get(sku) or {"sku": sku, "name": NAMES["skus"].get(sku), "price": None, "yuan": None})
        item["owned"] = True
        owned.append(item)
    out = {"ok": True, "owned": owned, "catalog": list(catalog.values()),
           "enable_gift": state.get("enable_gift"), "month_limit": state.get("is_gift_month_limit")}
    with _lock:
        _cache[friend_uuid] = (now, out)
    return out


def label(item):
    return item["name"] or f"早期礼包（编号 {item['sku']}）"


def summary(res):
    """一句话统计：总数、有价格的合计、没名字的早期礼包数。"""
    owned = res["owned"]
    priced = [i["yuan"] for i in owned if i["yuan"] is not None]
    early = sum(1 for i in owned if not i["name"])
    text = f"你一共拥有 {len(owned)} 个礼包"
    if priced:
        text += f"，其中 {len(priced)} 个能查到价格，合计约 ¥{sum(priced):.0f}"
    if early:
        text += f"；另有 {early} 个早期礼包（已下架，没有中文目录名）"
    return text + "。"


def list_lines(res, width=60, max_lines=6):
    """已拥有的礼包名逐行列出（带价格），超出就截断并注明还有多少。"""
    lines, cur, shown = [], "", 0
    named = [i for i in res["owned"] if i["name"]]
    for i in named:
        piece = i["name"] + (f"({i['price']})" if i["price"] else "")
        if cur and len(cur) + 1 + len(piece) > width:
            lines.append(cur)
            cur = ""
            if len(lines) >= max_lines:
                break
        cur = piece if not cur else cur + "、" + piece
        shown += 1
    if cur and len(lines) < max_lines:
        lines.append(cur)
    if shown < len(named):
        lines.append(f"……还有 {len(named) - shown} 个没列出来，想问哪个直接说名字～")
    return lines


def find(res, text):
    """消息里提到的礼包（按中文名子串匹配，2 个字以上），已拥有和目录里的都算。"""
    pool = {i["sku"]: i for i in res["catalog"]}
    pool.update({i["sku"]: i for i in res["owned"]})
    hits = []
    for i in pool.values():
        n = i["name"] or ""
        core = re.sub(r"礼包$", "", n)
        if len(core) >= 2 and (core in text or (len(n) >= 2 and n in text)):
            hits.append(i)
    return hits[:6]
