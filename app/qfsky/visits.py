# -*- coding: utf-8 -*-
"""本站访问计数。

不蒜子（cdn.busuanzi.cc / count.php）现在稳定返回 502，首页再去抓它就一直是「—」。
计数改由本站自己记：进站 POST 一次加今日访问，之后 GET 只读，刷新数字不会再加一。

落档 data/visits.json（0600）：
  {"v": 1, "days": {"2026-10-09": 12}, "uv": ["<sha256 前 16 位>", ...]}
今日访问 = 北京时间当天的 days。访客 = 去重后的 uv 长度（同一浏览器只算一个人）。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone

_DATA_DIR = os.path.abspath(os.environ.get("QFLY_DATA_DIR") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"))
FILE = os.environ.get("QFLY_VISITS_FILE") or os.path.join(_DATA_DIR, "visits.json")

_CN = timezone(timedelta(hours=8))
_KEEP_DAYS = 21
_HIT_GAP = 2.0          # 同一访客 2 秒内的重复 POST 不加访问量（脚本重入、双发）
_IP_MAX = 40            # 同一 IP 每分钟最多记这么多次，挡住刷接口
_lock = threading.Lock()
_state = None           # {"days": {}, "uv": set, "dirty": bool}
_recent = {}            # hash -> last hit ts
_ip_hits = {}           # ip -> [ts, ...]


def _today() -> str:
    return datetime.now(_CN).strftime("%Y-%m-%d")


def _load():
    global _state
    if _state is not None:
        return _state
    days, uv = {}, set()
    try:
        with open(FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            d = raw.get("days")
            if isinstance(d, dict):
                days = {str(k): int(v) for k, v in d.items() if str(k)[:4].isdigit() and isinstance(v, int) and v >= 0}
            ids = raw.get("uv")
            if isinstance(ids, list):
                uv = {str(x) for x in ids if isinstance(x, str) and len(x) == 16}
    except Exception:
        days, uv = {}, set()
    _state = {"days": days, "uv": uv}
    return _state


def _save(st) -> None:
    os.makedirs(os.path.dirname(FILE), exist_ok=True)
    day = _today()
    cutoff = (datetime.now(_CN) - timedelta(days=_KEEP_DAYS)).strftime("%Y-%m-%d")
    days = {k: v for k, v in st["days"].items() if k >= cutoff or k == day}
    st["days"] = days
    payload = {"v": 1, "days": days, "uv": sorted(st["uv"])}
    tmp = FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, FILE)
    try:
        os.chmod(FILE, 0o600)
    except OSError:
        pass


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _visitor(vid: str, ip: str) -> str:
    raw = re.sub(r"[^A-Za-z0-9_-]", "", str(vid or ""))[:64]
    if len(raw) < 8:
        raw = "ip:" + str(ip or "")
    return _hash("qfly-visit:" + raw)


def snapshot() -> dict:
    with _lock:
        st = _load()
        return {"today_pv": int(st["days"].get(_today(), 0)), "site_uv": len(st["uv"])}


def hit(vid: str = "", ip: str = "") -> dict:
    """记一次页面打开。太密的重复请求只回当前数字，不加量。"""
    now = time.time()
    who = _visitor(vid, ip)
    ipk = str(ip or "")[:64]
    with _lock:
        st = _load()
        last = _recent.get(who, 0.0)
        bucket = [t for t in _ip_hits.get(ipk, []) if now - t < 60]
        counted = False
        if now - last >= _HIT_GAP and len(bucket) < _IP_MAX:
            day = _today()
            st["days"][day] = int(st["days"].get(day, 0)) + 1
            st["uv"].add(who)
            _recent[who] = now
            bucket.append(now)
            counted = True
            _save(st)
        _ip_hits[ipk] = bucket
        # 顺手清掉过期的节流表，避免进程长跑越积越大
        if len(_recent) > 4000:
            keep = now - 120
            for k in [k for k, t in _recent.items() if t < keep]:
                _recent.pop(k, None)
        data = {"today_pv": int(st["days"].get(_today(), 0)), "site_uv": len(st["uv"])}
    data["counted"] = counted
    return data
