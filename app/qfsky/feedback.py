# -*- coding: utf-8 -*-
"""问题反馈：任意登录用户提交，管理员在管理员面板查看 / 回复 / 归档。

data/feedback.json（0600）结构：
  {"items": [ {...}, ... ]}          # 新的在前，最多 MAX_ITEMS 条

条目字段：
  id          10 位 hex
  user_id     提交者用户 id（用户被删也保留，便于管理员追溯）
  email       提交时快照的邮箱（用户改邮箱后仍能看到当时是谁提的）
  kind        bug | suggest | account | other
  text        正文（≤ TEXT_MAX 字）
  contact     可选联系方式（≤ CONTACT_MAX 字）
  page        提交时所在页面（前端传，便于定位）
  ua / ip     现场信息（ip 取 XFF 第一段 = 真实客户端）
  status      new | doing | done     管理员侧处理状态
  reply       管理员回复，用户可见
  created_at / updated_at / handled_by / handled_at

限流：同一用户 RATE_WINDOW 秒内最多 RATE_MAX 条、24h 内最多 DAY_MAX 条 ——
反馈是给管理员看的，被刷屏就失去意义。
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time

_DATA_DIR = os.path.abspath(os.environ.get("QFLY_DATA_DIR") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"))
FILE = os.path.join(_DATA_DIR, "feedback.json")

MAX_ITEMS = 500          # 总量上限，超出丢最旧的
TEXT_MAX = 2000
CONTACT_MAX = 120
PAGE_MAX = 200
UA_MAX = 300

KINDS = ("bug", "suggest", "account", "other")
KIND_ZH = {"bug": "功能异常", "suggest": "功能建议", "account": "账号问题", "other": "其他"}
STATUSES = ("new", "doing", "done")
STATUS_ZH = {"new": "待处理", "doing": "处理中", "done": "已处理"}

RATE_WINDOW = 60         # 秒
RATE_MAX = 3             # 窗口内最多条数
DAY_MAX = 20             # 24h 内最多条数

_lock = threading.RLock()


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
        # 与 store.py 一致：不压分隔符，保持可读
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, FILE)
    try:
        os.chmod(FILE, 0o600)
    except Exception:
        pass


def _clean(it: dict) -> dict:
    """统一字段与类型，任何一条老数据都能安全渲染。"""
    it = it or {}
    kind = str(it.get("kind") or "other")
    status = str(it.get("status") or "new")
    return {
        "id": str(it.get("id") or ""),
        "user_id": str(it.get("user_id") or ""),
        "email": str(it.get("email") or ""),
        "kind": kind if kind in KINDS else "other",
        "text": str(it.get("text") or ""),
        "contact": str(it.get("contact") or ""),
        "page": str(it.get("page") or ""),
        "ua": str(it.get("ua") or ""),
        "ip": str(it.get("ip") or ""),
        "status": status if status in STATUSES else "new",
        "reply": str(it.get("reply") or ""),
        "created_at": int(it.get("created_at") or 0),
        "updated_at": int(it.get("updated_at") or 0),
        "handled_by": str(it.get("handled_by") or ""),
        "handled_at": int(it.get("handled_at") or 0),
    }


def _day_start(now: float) -> float:
    """本地（服务器 TZ=Asia/Shanghai）当天 0 点的时间戳。"""
    lt = time.localtime(now)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


def _mine(items: list[dict], user_id: str) -> list[dict]:
    return [i for i in items if i.get("user_id") == user_id]


def _check_rate(items: list[dict], user_id: str, now: float) -> None:
    mine = _mine(items, user_id)
    recent = [i for i in mine if now - int(i.get("created_at") or 0) <= RATE_WINDOW]
    if len(recent) >= RATE_MAX:
        raise ValueError("提交太频繁，请等一分钟后再试")
    day = [i for i in mine if int(i.get("created_at") or 0) >= _day_start(now)]
    if len(day) >= DAY_MAX:
        raise ValueError("今天提交的反馈已达上限（%d 条），请明天再来" % DAY_MAX)


def add(user_id: str, email: str, text: str, kind: str = "",
        contact: str = "", page: str = "", ua: str = "", ip: str = "") -> dict:
    """新增一条反馈，返回条目。校验/限流不通过抛 ValueError。"""
    if not user_id:
        raise ValueError("未登录")
    text = (text or "").strip()
    if len(text) < 4:
        raise ValueError("反馈内容太短，请至少写 4 个字")
    if len(text) > TEXT_MAX:
        raise ValueError("反馈内容请控制在 %d 字以内" % TEXT_MAX)
    contact = (contact or "").strip()[:CONTACT_MAX]
    kind = str(kind or "")
    if kind not in KINDS:
        kind = "other"
    now = int(time.time())
    with _lock:
        data = _load()
        items = data.setdefault("items", [])
        _check_rate(items, user_id, now)
        item = {
            "id": secrets.token_hex(5),
            "user_id": user_id,
            "email": str(email or ""),
            "kind": kind,
            "text": text,
            "contact": contact,
            "page": (page or "")[:PAGE_MAX],
            "ua": (ua or "")[:UA_MAX],
            "ip": str(ip or ""),
            "status": "new",
            "reply": "",
            "created_at": now,
            "updated_at": now,
            "handled_by": "",
            "handled_at": 0,
        }
        items.insert(0, item)              # 新的在前，前端不用再排
        if len(items) > MAX_ITEMS:
            del items[MAX_ITEMS:]
        _save(data)
        return _clean(item)


def list_mine(user_id: str, limit: int = 50) -> list[dict]:
    """当前用户自己的反馈（含管理员回复），新的在前。"""
    with _lock:
        items = [_clean(i) for i in _load().get("items") or []
                 if (i or {}).get("user_id") == user_id]
    return items[:max(1, int(limit or 50))]


def mine_stats(user_id: str) -> dict:
    mine = list_mine(user_id, limit=MAX_ITEMS)
    return {
        "total": len(mine),
        "replied": sum(1 for i in mine if i["reply"]),
        "pending": sum(1 for i in mine if i["status"] != "done"),
    }


def list_all(status: str = "", q: str = "", limit: int = 300) -> list[dict]:
    """管理员视图：按状态过滤 + 关键词搜索（正文 / 邮箱 / 联系方式）。"""
    status = str(status or "")
    q = (q or "").strip().lower()
    with _lock:
        items = [_clean(i) for i in _load().get("items") or []]
    if status in STATUSES:
        items = [i for i in items if i["status"] == status]
    if q:
        items = [i for i in items
                 if q in i["text"].lower() or q in i["email"].lower()
                 or q in i["contact"].lower() or q in i["user_id"].lower()]
    return items[:max(1, int(limit or 300))]


def get(fid: str) -> dict | None:
    with _lock:
        for i in _load().get("items") or []:
            if str((i or {}).get("id")) == str(fid):
                return _clean(i)
    return None


def set_status(fid: str, status: str | None = None, reply: str | None = None,
               by: str = "") -> dict:
    """管理员更新处理状态 / 回复。只改显式传进来的字段。"""
    if status is not None and status not in STATUSES:
        raise ValueError("状态不合法")
    if reply is not None and len(str(reply)) > TEXT_MAX:
        raise ValueError("回复请控制在 %d 字以内" % TEXT_MAX)
    now = int(time.time())
    with _lock:
        data = _load()
        for i in data.get("items") or []:
            if str((i or {}).get("id")) != str(fid):
                continue
            if status is not None:
                i["status"] = status
            if reply is not None:
                i["reply"] = str(reply).strip()
            # 有待处理 → 已处理 时记一次处理人与时间；回退到 new 则清掉
            if i.get("status") == "done":
                i["handled_by"] = str(by or i.get("handled_by") or "")
                i["handled_at"] = int(i.get("handled_at") or now)
            else:
                i["handled_by"] = ""
                i["handled_at"] = 0
            i["updated_at"] = now
            _save(data)
            return _clean(i)
    raise KeyError(fid)


def delete(fid: str) -> bool:
    with _lock:
        data = _load()
        items = data.get("items") or []
        keep = [i for i in items if str((i or {}).get("id")) != str(fid)]
        if len(keep) == len(items):
            return False
        data["items"] = keep
        _save(data)
        return True


def delete_done() -> int:
    """批量清理已处理条目，返回删除条数。"""
    with _lock:
        data = _load()
        items = data.get("items") or []
        keep = [i for i in items if _clean(i)["status"] != "done"]
        n = len(items) - len(keep)
        if n:
            data["items"] = keep
            _save(data)
        return n


def stats() -> dict:
    with _lock:
        items = [_clean(i) for i in _load().get("items") or []]
    now = time.time()
    day0 = _day_start(now)
    return {
        "total": len(items),
        "new": sum(1 for i in items if i["status"] == "new"),
        "doing": sum(1 for i in items if i["status"] == "doing"),
        "done": sum(1 for i in items if i["status"] == "done"),
        "replied": sum(1 for i in items if i["reply"]),
        "today": sum(1 for i in items if i["created_at"] >= day0),
        "week": sum(1 for i in items if now - i["created_at"] <= 7 * 86400),
    }


def new_count() -> int:
    """待处理条数（管理员按钮红点用）。"""
    try:
        return int(stats().get("new") or 0)
    except Exception:
        return 0
