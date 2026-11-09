# -*- coding: utf-8 -*-
"""维护页小游戏「接烛火」的排行榜（data/game_scores.json，0600 + 进程内存票据）。

三个设计理由：
  1. **放 data/ 不放 assets/**：部署会把 assets/** 上传覆盖，排行榜是运行期数据，
     放 assets 会被仓库旧值盖掉（同 mapcfg 的理由）。
  2. **只在维护模式开放提交**：维护页是唯一入口；把可提交窗口压到维护期间，
     等于把能刷分的时段缩到最小。读榜不限时间（就一组游戏分数，无所谓）。
  3. **开赛票据 + 时间校验**：客户端先领 ticket、结束时带 ticket 交分，
     服务端按「用时 × 每秒合理上限」卡一个分数天花板。拦不住铁了心伪造的人，
     但把「开局 1 秒提交 99999」这种最廉价的刷法直接堵死 —— 对一个维护页小游戏，
     这个强度的性价比最高。

另外：同名字只保留最高分（防重复刷榜），名字做长度与字符清洗，按 IP 限频。
"""
from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time

_DATA_DIR = os.path.abspath(os.environ.get("QFLY_DATA_DIR") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"))
SCORE_FILE = os.path.join(_DATA_DIR, "game_scores.json")

SCORE_CAP = 30000            # 单局分数硬上限（超过一律拒）
MAX_KEEP = 500               # 榜单最多保留多少个名字
RATE_MAX = 12                # 每 IP 每小时最多提交次数
RATE_WINDOW = 3600
TICKET_TTL = 900             # 票据有效期（秒）
MAX_TICKETS = 5000
NAME_MAX = 12
DEFAULT_NAME = "无名光之子"
# 分数天花板：起步给 30 分容错，之后按每秒 20 分封顶（正常局够用，秒交大分必被拒）
PLAUSIBLE_BASE = 30.0
PLAUSIBLE_PER_SEC = 20.0

_lock = threading.Lock()
_tickets: dict[str, tuple] = {}      # ticket -> (issued_at, ip)
_submits: dict[str, list] = {}       # ip -> [提交时间]
_name_re = re.compile(r"[\x00-\x1f\x7f<>\"'`\\]")


def _load() -> dict:
    try:
        with open(SCORE_FILE, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return {"scores": {}}
    if not isinstance(d, dict) or not isinstance(d.get("scores"), dict):
        return {"scores": {}}
    return d


def _save(d: dict) -> None:
    os.makedirs(_DATA_DIR, exist_ok=True)
    tmp = SCORE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SCORE_FILE)
    try:
        os.chmod(SCORE_FILE, 0o600)
    except OSError:
        pass


def clean_name(raw) -> str:
    s = _name_re.sub("", str(raw if raw is not None else "")).strip()
    s = " ".join(s.split())
    return (s[:NAME_MAX] or DEFAULT_NAME)


def _prune(d: dict) -> dict:
    scores = d.get("scores") or {}
    if len(scores) > MAX_KEEP:
        keep = sorted(scores.items(), key=lambda kv: -int(kv[1].get("score") or 0))[:MAX_KEEP]
        d["scores"] = dict(keep)
    return d


# ── 开赛票据 ──
def issue_ticket(ip: str) -> str:
    now = time.time()
    with _lock:
        for k in [k for k, v in _tickets.items() if now - v[0] > TICKET_TTL]:
            _tickets.pop(k, None)
        if len(_tickets) > MAX_TICKETS:
            _tickets.clear()
        t = secrets.token_urlsafe(12)
        _tickets[t] = (now, ip)
    return t


def _peek_ticket(ticket: str, ip: str):
    """校验票据但**不消费**，返回已用时（秒）；无效返回 None。

    为什么不在这里消费：交分可能因为「分数和用时对不上」以外的原因失败（限频等），
    这时票据得留着让玩家重试 —— 重试仍按最初的开局时间算天花板，才是真实用时。
    消费放在落库成功之后（_consume_ticket）。
    """
    with _lock:
        rec = _tickets.get(ticket)
    if not rec:
        return None
    issued, tip = rec
    if tip != ip:
        return None
    elapsed = time.time() - issued
    if elapsed > TICKET_TTL:
        return None
    return elapsed


def _consume_ticket(ticket: str) -> None:
    with _lock:
        _tickets.pop(ticket, None)


def _rate_ok(ip: str) -> bool:
    now = time.time()
    with _lock:
        arr = [t for t in _submits.get(ip, []) if now - t < RATE_WINDOW]
        if len(arr) >= RATE_MAX:
            _submits[ip] = arr
            return False
        arr.append(now)
        _submits[ip] = arr
    return True


def board(limit: int = 20) -> dict:
    with _lock:
        d = _load()
    rows = [
        {"name": k, "score": int(v.get("score") or 0), "at": float(v.get("at") or 0)}
        for k, v in (d.get("scores") or {}).items()
    ]
    rows.sort(key=lambda x: (-x["score"], x["at"]))
    return {"rows": rows[:limit], "total": len(rows),
            "cap": SCORE_CAP, "name_max": NAME_MAX}


def submit(ticket: str, name, score, ip: str) -> dict:
    """交分。返回 {"ok":bool, "reason":str, "rank":int|None, "best":int}。"""
    try:
        sc = int(score)
    except (TypeError, ValueError):
        return {"ok": False, "reason": "分数不是数字"}
    if sc < 1:
        return {"ok": False, "reason": "分数要大于 0 才有资格上榜"}
    if sc > SCORE_CAP:
        return {"ok": False, "reason": "分数超出上限"}
    elapsed = _peek_ticket(ticket, ip)
    if elapsed is None:
        return {"ok": False, "reason": "票据无效或已过期，请重开一局再看看"}
    ceiling = PLAUSIBLE_BASE + PLAUSIBLE_PER_SEC * max(elapsed, 0.0)
    if sc > ceiling:
        return {"ok": False, "reason": "这局分数和用时对不上（%d 分 / %.0f 秒），没能上榜" % (sc, elapsed)}
    if not _rate_ok(ip):
        return {"ok": False, "reason": "提交太频繁了，歇一会儿再来"}

    nm = clean_name(name)
    with _lock:
        d = _load()
        scores = d.setdefault("scores", {})
        cur = int((scores.get(nm) or {}).get("score") or 0)
        best = max(cur, sc)
        scores[nm] = {"score": best, "at": time.time(), "ip": _ip_tag(ip)}
        d = _prune(d)
        _save(d)
    _consume_ticket(ticket)
    rows = board(200)["rows"]
    rank = next((i + 1 for i, r in enumerate(rows) if r["name"] == nm), None)
    return {"ok": True, "reason": "上榜了" if sc >= cur else "没超过你自己的最高分（保留 %d）" % best,
            "rank": rank, "best": best, "name": nm, "improved": sc >= cur}


def _ip_tag(ip: str) -> str:
    import hashlib
    return hashlib.sha256(("qfly-minigame:" + str(ip)).encode("utf-8")).hexdigest()[:12]


def reset() -> None:
    with _lock:
        _save({"scores": {}})
