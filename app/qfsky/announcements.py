# -*- coding: utf-8 -*-
"""站内公告：管理员发布/下架；可选弹窗展示；可选「需用户确认收到」并统计确认情况。

data/announcements.json（0600）结构：
  {"current": {...} | None, "history": [...]}
current 字段：id/title/content/popup/require_ack/enabled/created_at/updated_at/acks
acks: {user_id: {"email": ..., "at": ...}}（用户确认收到的记录，供管理员统计）
发布新公告时旧公告整体归档进 history（保留最终 acks），确认计数清零。
"""
from __future__ import annotations

import base64
import json
import os
import re
import secrets
import threading
import time

_DATA_DIR = os.path.abspath(os.environ.get("QFLY_DATA_DIR") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"))
FILE = os.path.join(_DATA_DIR, "announcements.json")
HISTORY_MAX = 30

# 公告配图：存 data/ann_images/（部署 dist 不清空 data/），经 /api/ann/img/<name> 免登录读取。
IMG_DIR = os.path.join(_DATA_DIR, "ann_images")
IMG_MAX = 5 * 1024 * 1024
_IMG_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
)
_IMG_NAME_RE = re.compile(r"^[0-9a-f]{12}\.(png|jpg|webp|gif)$")
# 行内图片标记（渲染端只认这个语法）：![alt](url)
_IMG_MARK_RE = re.compile(r"!\[[^\]\n]*\]\([^)\n]*\)")

_lock = threading.RLock()


def _ensure_dir() -> None:
    os.makedirs(_DATA_DIR, exist_ok=True)


def _load() -> dict:
    if not os.path.exists(FILE):
        return {"current": None, "history": []}
    try:
        with open(FILE, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {"current": None, "history": []}
    if not isinstance(data, dict):
        data = {}
    if not isinstance(data.get("history"), list):
        data["history"] = []
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


def _clean_current(c: dict) -> dict:
    return {
        "id": c.get("id") or "",
        "title": c.get("title") or "",
        "content": c.get("content") or "",
        "popup": bool(c.get("popup", True)),
        "require_ack": bool(c.get("require_ack", False)),
        "enabled": bool(c.get("enabled", True)),
        "created_at": int(c.get("created_at") or 0),
        "updated_at": int(c.get("updated_at") or 0),
        "acks": dict(c.get("acks") or {}),
    }


def get_active() -> dict | None:
    """当前上架公告（不含确认明细）；未上架返回 None。"""
    with _lock:
        cur = _load().get("current")
        if not cur:
            return None
        c = _clean_current(cur)
        return c if c["enabled"] else None


def acked(user_id: str) -> bool:
    if not user_id:
        return False
    with _lock:
        cur = _load().get("current")
        return bool(cur and user_id in (cur.get("acks") or {}))


def ack(user_id: str, email: str) -> bool:
    """用户确认收到当前公告（幂等）。返回是否首次记录。"""
    if not user_id:
        return False
    with _lock:
        data = _load()
        cur = data.get("current")
        if not cur:
            return False
        acks = cur.setdefault("acks", {})
        if user_id in acks:
            return False
        acks[user_id] = {"email": email, "at": int(time.time())}
        _save(data)
        return True


def publish(title: str, content: str, popup: bool, require_ack: bool) -> dict:
    """发布新公告（旧公告归档进 history，确认统计重新开始）。"""
    title = (title or "").strip()
    content = (content or "").strip()
    if not title:
        raise ValueError("公告标题不能为空")
    if len(title) > 60:
        raise ValueError("公告标题请控制在 60 字以内")
    if len(content) > 8000:
        raise ValueError("公告内容过长")
    visible = _IMG_MARK_RE.sub("", content)
    if len(visible) > 2000:
        raise ValueError("公告正文（不含图片）请控制在 2000 字以内")
    now = int(time.time())
    with _lock:
        data = _load()
        cur = data.get("current")
        if cur:
            data.setdefault("history", []).append(_clean_current(cur))
            data["history"] = data["history"][-HISTORY_MAX:]
        data["current"] = {
            "id": secrets.token_hex(5),
            "title": title,
            "content": content,
            "popup": bool(popup),
            "require_ack": bool(require_ack),
            "enabled": True,
            "created_at": now,
            "updated_at": now,
            "acks": {},
        }
        _save(data)
        return _clean_current(data["current"])


def set_enabled(enabled: bool) -> dict | None:
    """上架/下架当前公告（不改确认统计）。"""
    with _lock:
        data = _load()
        cur = data.get("current")
        if not cur:
            return None
        cur["enabled"] = bool(enabled)
        cur["updated_at"] = int(time.time())
        _save(data)
        return _clean_current(cur)


def admin_view() -> dict:
    """管理员视图：current（含确认明细 acks 列表）+ history（仅 ack_count）。"""
    with _lock:
        data = _load()
        out = {"current": None, "history": []}
        cur = data.get("current")
        if cur:
            c = _clean_current(cur)
            acks = cur.get("acks") or {}
            c["acks"] = [
                {"user_id": uid, "email": (v or {}).get("email", ""), "at": int((v or {}).get("at") or 0)}
                for uid, v in sorted(acks.items(), key=lambda kv: int((kv[1] or {}).get("at") or 0))
            ]
            out["current"] = c
        for h in data.get("history") or []:
            hh = _clean_current(h)
            hh["ack_count"] = len(h.get("acks") or {})
            hh.pop("acks", None)
            out["history"].append(hh)
        out["history"].reverse()
        return out


def _sniff_ext(raw: bytes) -> str:
    for magic, ext in _IMG_MAGIC:
        if raw.startswith(magic):
            return ext
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return ".webp"
    return ""


def save_image(data_b64: str, filename: str = "") -> str:
    """保存公告配图（base64 数据，可为 dataURL）→ 返回文件名，经 /api/ann/img/<name> 访问。

    按文件魔数识别格式（不信任扩展名），仅 PNG/JPG/WebP/GIF，≤5MB。
    """
    data_b64 = (data_b64 or "").strip()
    if data_b64.startswith("data:"):
        i = data_b64.find(",")
        if i >= 0:
            data_b64 = data_b64[i + 1:]
    if not data_b64:
        raise ValueError("图片数据为空")
    try:
        raw = base64.b64decode(data_b64)
    except Exception:
        raise ValueError("图片数据格式错误")
    if not raw:
        raise ValueError("图片数据为空")
    if len(raw) > IMG_MAX:
        raise ValueError("图片不能超过 5MB")
    ext = _sniff_ext(raw)
    if not ext:
        raise ValueError("仅支持 PNG / JPG / WebP / GIF 图片")
    os.makedirs(IMG_DIR, exist_ok=True)
    name = secrets.token_hex(6) + ext
    with open(os.path.join(IMG_DIR, name), "wb") as f:
        f.write(raw)
    return name


def image_path(name: str) -> str | None:
    """按文件名取图片磁盘路径；名字必须匹配内部生成格式（防路径穿越）。"""
    name = str(name or "")
    if not _IMG_NAME_RE.match(name):
        return None
    p = os.path.join(IMG_DIR, name)
    return p if os.path.isfile(p) else None
