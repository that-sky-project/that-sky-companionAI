# -*- coding: utf-8 -*-
"""光遇封号统计：机位报「禁止登录至…」等封禁类错误时自动记录。
完整视图（含报错原文）仅管理员可查（/api/admin/bans）；公开视图（机位归属+期限，
无角色ID/报错原文）经 list_public() 暴露给全体登录用户（/api/bans）。

data/bans.json（0600）结构：{"items": [...]}，每条：
  {account_id, alias, channel, platform, user_uuid, owner_id,
   message, until, first_seen, last_seen, count}
until 为「禁止登录至」后的日期文本（解析不出留空串）。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime
from datetime import timedelta, timezone
try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

_DATA_DIR = os.path.abspath(os.environ.get("QFLY_DATA_DIR") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"))
FILE = os.path.join(_DATA_DIR, "bans.json")
MAX_ITEMS = 500
try:
    _TZ = ZoneInfo("Asia/Shanghai")
except Exception:       # Windows 无 tzdata：固定 +08:00（国服无夏令时）
    _TZ = timezone(timedelta(hours=8), "Asia/Shanghai")
# until 解析不出日期时的兜底：按「最后一次探测 + 7 天」视为仍在封禁
_STALE_BAN = 7 * 86400

_lock = threading.RLock()

_HINT = re.compile(
    # 线上实测原文：「业务错误：禁止登录e您的角色ID:xxx因使用外挂、脚本等辅助软件被禁止登录至2026-10-07 11:48:26」
    r"禁止登录|封禁|封号|账号被?冻结|限制登录|使用外挂|辅助软件|脚本等|"
    r"banned|suspended|violat",
    re.IGNORECASE)
# 「禁止登录至2026年09月15日 08:00」/「禁止登录到 2026-09-15」等
_UNTIL = re.compile(r"禁止登录[至到]?\s*([0-9]{4}[^ ，,。;；]*)")


def _ensure_dir() -> None:
    os.makedirs(_DATA_DIR, exist_ok=True)


def _load() -> dict:
    if not os.path.exists(FILE):
        return {"items": []}
    try:
        with open(FILE, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {"items": []}
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return {"items": []}
    return data


def _save(data: dict) -> None:
    _ensure_dir()
    tmp = FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, FILE)
    try:
        os.chmod(FILE, 0o600)
    except Exception:
        pass


def parse_ban(message: str) -> str | None:
    """命中封禁特征返回「至日期」文本（可能为空串），未命中返回 None。"""
    msg = " ".join(str(message or "").split())
    if not msg:
        return None
    m = _UNTIL.search(msg)
    if m:
        return m.group(1)[:60]
    if _HINT.search(msg):
        return ""
    return None


def _ctx(acc: dict) -> dict:
    """封禁现场的出口与设备：没有这两个字段，就永远无法证明「批量封」是按什么聚类的。"""
    out = {"device": "", "egress": "direct"}
    try:
        from . import fp
        out["device"] = "%s %s" % (fp.device(acc.get("user_uuid") or "")["brand"],
                                   fp.device(acc.get("user_uuid") or "")["model"])
    except Exception:
        pass
    try:
        from . import netcfg
        u = netcfg.proxy_url(acc.get("user_uuid") or "")
        if u:
            from urllib.parse import urlparse
            p = urlparse(u)
            out["egress"] = "%s://%s%s" % (p.scheme, p.hostname or "", (":%d" % p.port) if p.port else "")
    except Exception:
        pass
    return out


def maybe_record(acc: dict, message: str) -> bool:
    """命中封禁特征时记录/更新（同机位合并，count 累加）。返回是否记录。"""
    if not isinstance(acc, dict) or not acc.get("id"):
        return False
    until = parse_ban(message)
    if until is None:
        return False
    msg = " ".join(str(message or "").split())[:200]
    now = int(time.time())
    ctx = _ctx(acc)
    with _lock:
        data = _load()
        for it in data["items"]:
            if it.get("account_id") == acc["id"]:
                it["message"] = msg
                it["until"] = until
                it["last_seen"] = now
                it["count"] = int(it.get("count") or 1) + 1
                it.setdefault("device", ctx["device"])
                it.setdefault("egress", ctx["egress"])
                _save(data)
                return True
        data["items"].append({
            "account_id": acc["id"],
            "alias": acc.get("alias") or "",
            "channel": acc.get("channel") or "",
            "platform": acc.get("platform") or "",
            "user_uuid": acc.get("user_uuid") or "",
            "owner_id": acc.get("owner") or "",
            "device": ctx["device"],
            "egress": ctx["egress"],
            "message": msg,
            "until": until,
            "first_seen": now,
            "last_seen": now,
            "count": 1,
        })
        if len(data["items"]) > MAX_ITEMS:
            data["items"].sort(key=lambda x: int(x.get("last_seen") or 0), reverse=True)
            data["items"] = data["items"][:MAX_ITEMS]
        _save(data)
        return True


def clusters(window_secs: int = 120, items: list[dict] | None = None) -> list[dict]:
    """把「首次命中时间彼此接近」的封禁聚成一簇 —— 批量封的直接证据。

    2026-09-10 那批：12:24:46 / 12:24:51 / 12:24:57 / 12:25:07（**四位不同机主**，
    21 秒内）。单账号行为检测不会产生这种时间结构，只有服务端按共同特征
    （出口 IP / 设备指纹）批量处置才会。这个函数就是把它从数据里挖出来。
    """
    src = sorted((items if items is not None else list_bans()),
                 key=lambda x: int(x.get("first_seen") or 0))
    out: list[dict] = []
    cur: list[dict] = []
    for it in src:
        if cur and int(it.get("first_seen") or 0) - int(cur[0].get("first_seen") or 0) > window_secs:
            if len(cur) > 1:
                out.append(_cluster_view(cur))
            cur = []
        cur.append(it)
    if len(cur) > 1:
        out.append(_cluster_view(cur))
    return sorted(out, key=lambda c: -c["size"])


def _cluster_view(group: list[dict]) -> dict:
    owners = {str(x.get("owner_id") or "") for x in group}
    devs = {str(x.get("device") or "") for x in group}
    eg = {str(x.get("egress") or "") for x in group}
    return {
        "size": len(group),
        "owners": len(owners),
        "first_seen": int(group[0].get("first_seen") or 0),
        "span_secs": int(group[-1].get("first_seen") or 0) - int(group[0].get("first_seen") or 0),
        "items": [{"alias": x.get("alias"), "owner_id": x.get("owner_id"),
                   "until": x.get("until"), "device": x.get("device"),
                   "egress": x.get("egress"), "first_seen": x.get("first_seen")} for x in group],
        "unique_devices": len(devs - {""}),
        "unique_egress": len(eg - {""}),
    }


def _drop_expired(data: dict, now: float) -> int:
    """把 data 里已解封的条目剔掉；**只有真的删了才落盘**。返回剔除条数。

    调用方必须已持 _lock（本函数做读改写）。
    """
    items = data.get("items") or []
    keep = [it for it in items if _still_banned(it, now)]
    gone = len(items) - len(keep)
    if gone:
        data["items"] = keep
        _save(data)
        print("[bans] 封神榜清理 %d 条已解封记录（%d → %d）" % (gone, len(items), len(keep)),
              flush=True)
    return gone


def prune(now: float | None = None) -> int:
    """清理封神榜：把「封号已结束、可以正常玩」的机位摘掉。返回摘掉几条。

    ★ 2026-09-26 用户要求：「有些账号封号结束了可以正常玩的那些都自动从封神榜去掉」。
      在这之前只有调度器（banned_ids）会按解封放过这些机位，**列表却永久保留** ——
      于是榜上挂着一堆早就能正常玩的号。
    """
    with _lock:
        return _drop_expired(_load(), time.time() if now is None else now)


def list_bans(prune_expired: bool = True) -> list[dict]:
    """全部记录（管理员视图用）。

    ★ 默认每次读取都顺手清理已解封的条目 —— 榜单是「**当前**还在被封的人」，
      不是历史档案；没人读的时候留在盘上无害（有上限），一读就自动落榜。
      prune_expired=False 仅供需要看原始全量的排查场景。
    """
    with _lock:
        data = _load()
        if prune_expired:
            _drop_expired(data, time.time())
        items = data.get("items") or []
        return sorted(items, key=lambda x: int(x.get("last_seen") or 0), reverse=True)


def list_public() -> dict:
    """公开视图（全体登录用户可见）：机位归属（别名/账号id）+ 渠道/平台/封禁期限/时间/次数；
    不含 user_uuid 角色ID 与原始报错信息（归属邮箱由 main.py 按 owner_id 解析补充）。"""
    items = []
    for it in list_bans():
        items.append({
            "alias": str(it.get("alias") or ""),
            "account_id": str(it.get("account_id") or ""),
            "owner_id": str(it.get("owner_id") or ""),
            "channel": str(it.get("channel") or ""),
            "platform": str(it.get("platform") or ""),
            "until": str(it.get("until") or ""),
            "first_seen": int(it.get("first_seen") or 0),
            "last_seen": int(it.get("last_seen") or 0),
            "count": int(it.get("count") or 1),
        })
    return {"total": len(items), "items": items}


def clear() -> int:
    with _lock:
        n = len(_load()["items"])
        _save({"items": []})
        return n


def _until_epoch(until: str) -> float:
    """「禁止登录至」的日期文本 → epoch 秒。解析不出返回 0（= 期限未知）。

    线上实测到的形态：`2026-10-07 11:48:26`（带时刻）、`2026-10-09`（只到日）、
    `2026年09月15日 08:00`（中文分隔）。全部按北京时间解释。
    """
    t = " ".join(str(until or "").split())
    if not t:
        return 0.0
    m = re.search(r"(\d{4})\D(\d{1,2})\D(\d{1,2})"
                  r"(?:\D+(\d{1,2})\D(\d{1,2})(?:\D(\d{1,2}))?)?", t)
    if not m:
        return 0.0
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                        int(m.group(4) or 0), int(m.group(5) or 0),
                        int(m.group(6) or 0), tzinfo=_TZ).timestamp()
    except Exception:
        return 0.0


def _still_banned(it: dict, now: float) -> bool:
    """这条记录现在还算「在榜」吗 —— **唯一判据**，banned_ids() 与 prune() 共用。

    ★ 必须是同一个函数。调度器（banned_ids）和封神榜列表（prune）对「解封没解封」的
      判断一旦分叉，就会出现自相矛盾的界面：榜上还挂着某台机位，而它已经在正常跑图
      （调度器按解封放过了它）。2026-09-26 用户报的就是这个 ——
      「有些账号封号结束了可以正常玩的那些都自动从封神榜去掉」。

    三种情况：
      · `until` 能解析出日期 → 到期即解封（ue > now 才在榜）；
      · `until` 解析不出（空串/异形日期，见 parse_ban 返回 "" 的那条路径）
        → 用「最后探测 + _STALE_BAN(7天)」兜底，因为确实有拿不到日期的封禁类报错；
      · 兜底期也过了 → 视为已解封，落榜。
    """
    ue = _until_epoch(it.get("until"))
    if ue:
        return ue > now
    return int(it.get("last_seen") or 0) + _STALE_BAN > now


def banned_ids(now: float | None = None) -> set[str]:
    """**仍在封禁期内**的机位 id 集合（解封的自动落榜）。

    存在的理由：调度器据此跳过给已封机位派发任务。不跳过的代价实测有两份 ——
    ①每个已封机位仍按自己的定时计划反复打 9393，纯浪费请求；
    ②bans.json 的 count 被重复探测刷高，把「封号越来越严重」变成假象
    （实测：32 行记录累计 count=68，其中 petalmail04 单机位贡献 12 次，
    x2486244871 贡献 8 次 —— 全是同一条封禁被反复撞见，不是 12 次封禁）。

    ★ 刻意**只读不清理**（不走 list_bans）：
      · 这是调度器的热路径，不该在读的时候写盘；
      · 也不该受 list_bans 的「按当前时间清理」影响 —— 传 now 进来时结果必须只由 now 决定。
    """
    t = time.time() if now is None else now
    with _lock:
        items = _load().get("items") or []
    out: set[str] = set()
    for it in items:
        aid = str(it.get("account_id") or "")
        if aid and _still_banned(it, t):
            out.add(aid)
    return out
