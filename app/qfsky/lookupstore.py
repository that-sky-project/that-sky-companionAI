# -*- coding: utf-8 -*-
"""查身高 / 装扮结果的**落盘缓存**。

为什么必须落盘：好友码是**一次性**的 —— 查询背号拿它 `accept_invite` 之后令牌
立刻**全局**作废（换另一台背号用同一个码也是 `not_available`），软删不恢复，
重新 accept 也不行。也就是说「同一个码查第二次」在服务端已经不可能成功，
只能靠缓存回答。内存缓存（main.py 的 `_LOOKUP_TTL`，5 分钟）扛不住
「隔天再查」和「服务重启」，所以把成功的查询结果落盘。

data/lookup_results.json（0600）结构：
    {"items": {"<key>": {"t": <unix ts>, "v": <lookup 结果>}},
     "links": {"<key>": {"t": <unix ts>, "uuid": "<玩家uuid>", "backer": "<背号机位id>"}}}
key 有两种，同一份结果同时挂两个键：
    · "code:xxxx-xxxx-xxxx"  —— 用户手里的 12 位好友码（归一化后）
    · "uuid:<玩家uuid>"       —— 查出来的玩家 uuid（用户下次直接贴光遇 ID 时命中）

只存**成功**的结果：失败（码错 / 已用过 / 没快照）不落盘 —— 「码已死」虽然是
终态，但把一次临时故障（限流、机位掉线）钉成 7 天结论，比不缓存更糟。

★ links 是「查过一次，下次还能查」的关键（2026-10-06）：
  查询背号读完数据后**保留**好友关系，所以只要知道「这个码/这个 ID 对应哪个玩家、
  当初是哪台背号加的他」，下次就能直接 `read_friend()` —— 纯星盘读、实时数据、
  不消费任何令牌。links 就是这份身份映射，**长期有效**（不像结果缓存会过期）。
"""
from __future__ import annotations

import json
import os
import threading
import time

_DATA_DIR = os.path.abspath(os.environ.get("QFLY_DATA_DIR") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"))
FILE = os.path.join(_DATA_DIR, "lookup_results.json")

MAX_ITEMS = 2000              # 条目上限（超出淘汰最旧，防文件无界增长）
PERSIST_TTL = 7 * 86400.0     # 落盘结果的有效期：7 天

_lock = threading.RLock()


def _ensure_dir() -> None:
    os.makedirs(_DATA_DIR, exist_ok=True)


def _load() -> dict:
    if not os.path.exists(FILE):
        return {"items": {}, "links": {}}
    try:
        with open(FILE, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:                                       # noqa: BLE001
        return {"items": {}, "links": {}}
    if not isinstance(data, dict):
        return {"items": {}, "links": {}}
    if not isinstance(data.get("items"), dict):
        data["items"] = {}
    if not isinstance(data.get("links"), dict):
        data["links"] = {}
    return data


def _save(data: dict) -> None:
    _ensure_dir()
    tmp = FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, FILE)
    try:
        os.chmod(FILE, 0o600)
    except Exception:                                       # noqa: BLE001
        pass


def get(key: str, ttl: float = PERSIST_TTL) -> tuple | None:
    """取缓存 → (unix ts, 结果)；没有 / 过期回 None。"""
    k = str(key or "")
    if not k:
        return None
    with _lock:
        rec = _load().get("items", {}).get(k)
    if not isinstance(rec, dict):
        return None
    try:
        t = float(rec.get("t") or 0)
    except (TypeError, ValueError):
        return None
    if not t or (time.time() - t) > float(ttl):
        return None
    v = rec.get("v")
    return (t, v) if isinstance(v, dict) else None


def put_many(keys, value: dict, now: float | None = None) -> None:
    """把同一份结果挂到多个键上（code + uuid 双键），并淘汰最旧。"""
    ks = [str(k) for k in (keys or []) if str(k).strip()]
    if not ks or not isinstance(value, dict):
        return
    t = float(now if now is not None else time.time())
    with _lock:
        data = _load()
        items = data.setdefault("items", {})
        for k in ks:
            items[k] = {"t": t, "v": value}
        if len(items) > MAX_ITEMS:
            for k, _v in sorted(items.items(), key=lambda kv: float((kv[1] or {}).get("t") or 0)
                                 )[:len(items) - MAX_ITEMS]:
                items.pop(k, None)
        _save(data)


def stats() -> dict:
    """条数与最旧/最新时间（给管理员排查用）。"""
    with _lock:
        data = _load()
    items, links = data.get("items", {}), data.get("links", {})
    ts = [float((r or {}).get("t") or 0) for r in items.values() if isinstance(r, dict)]
    return {"count": len(items), "links": len(links),
            "oldest": min(ts) if ts else 0,
            "newest": max(ts) if ts else 0,
            "file": FILE}


# ── 身份映射（links）：码 / 玩家 ID → 玩家 uuid + 当初加他的背号 ──────────────

def link_get(key: str) -> dict | None:
    """取身份映射 → {"uuid","backer","t"}；没有回 None。

    ★ 与结果缓存不同，这里**不设过期** —— 好友关系是长期保留的，
      只要映射还在，`read_friend()` 就能一直读到最新数据。
      映射失效（好友被删）由调用方的直读失败来发现并落回完整链路。
    """
    k = str(key or "")
    if not k:
        return None
    with _lock:
        rec = _load().get("links", {}).get(k)
    if not isinstance(rec, dict):
        return None
    uid = str(rec.get("uuid") or "")
    if not uid:
        return None
    return {"uuid": uid, "backer": str(rec.get("backer") or ""),
            "t": float(rec.get("t") or 0)}


def link_put_many(keys, uuid: str, backer: str = "", now: float | None = None) -> None:
    """把「这些键都指向这个玩家」写进身份映射（码键 + 玩家 ID 键各一份）。"""
    ks = [str(k) for k in (keys or []) if str(k).strip()]
    uid = str(uuid or "").strip()
    if not ks or not uid:
        return
    t = float(now if now is not None else time.time())
    rec = {"t": t, "uuid": uid, "backer": str(backer or "")}
    with _lock:
        data = _load()
        links = data.setdefault("links", {})
        for k in ks:
            links[k] = rec
        if len(links) > MAX_ITEMS * 4:                       # 映射比结果更值钱，给更大上限
            for k, _v in sorted(links.items(),
                                key=lambda kv: float((kv[1] or {}).get("t") or 0)
                                )[:len(links) - MAX_ITEMS * 4]:
                links.pop(k, None)
        _save(data)
