# -*- coding: utf-8 -*-
"""装扮图标目录：移植 OutfitCatalog.kt（国服）。

装扮数字 id（= get_outfit/set_outfit 里的 id = FNV-1a32(CharSkyKid 名)）→ CDN png 数字 + 中文名。
⚠️ CDN png 数字 ≠ FNV id（两套体系），必须经在线 game_auto_config.js 的 display_content 转换；
FNV id 直接拼 URL 会 404。

三级数据源（优先级递减）：内存表 → data/outfit_icons_cache.json 磁盘缓存 → 在线拉
https://cbg-sky.res.netease.com/js/game_auto_config.js。拉取失败不抛（调用方降级为只显示 id）。
"""
from __future__ import annotations

import json
import os
import threading
import time

import requests

CONFIG_URL = "https://cbg-sky.res.netease.com/js/game_auto_config.js"
CDN_BASE = "https://cbg-sky.res.netease.com/game_res/"
CACHE_FILE = os.path.join(os.path.abspath(os.environ.get("QFLY_DATA_DIR") or os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")), "outfit_icons_cache.json")

_FNV_OFFSET = 0x811C9DC5
_FNV_PRIME = 0x01000193
_U32 = 0xFFFFFFFF
_TTL = 7 * 86400  # 磁盘缓存超过 7 天才尝试在线刷新（网易更新不频繁）

_lock = threading.Lock()
_table: dict[int, dict] | None = None   # {fnv_id: {"png": "123", "cn": "中文名"}}
_loaded_at = 0.0


def sky_outfit_id(name: str) -> int:
    """name → FNV-1a32（UTF-8 字节），= outfit 接口里的装扮 id。"""
    v = _FNV_OFFSET
    for b in name.encode("utf-8"):
        v = ((v ^ b) * _FNV_PRIME) & _U32
    return v


def _parse_config(text: str) -> dict[int, dict]:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("game_auto_config.js 无 JSON 主体")
    cfg = json.loads(text[start:end + 1])
    disp = cfg.get("display_content")
    if not isinstance(disp, dict) or not disp:
        raise ValueError("缺 display_content")
    ofc = cfg.get("outfit_from_cbg") if isinstance(cfg.get("outfit_from_cbg"), dict) else {}
    out: dict[int, dict] = {}
    for name, val in disp.items():
        png = str(val or "").strip()
        if png.endswith(".png"):
            png = png[: -len(".png")]
        if not png:
            continue
        cn = name
        o = ofc.get(name)
        if isinstance(o, dict):
            names = o.get("names")
            if isinstance(names, list) and names and str(names[0] or "").strip():
                cn = str(names[0])
        out[sky_outfit_id(name)] = {"png": png, "cn": cn}
    return out


def _load_disk() -> dict[int, dict] | None:
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        out = {}
        for k, o in raw.items():
            fid = int(k)
            if isinstance(o, dict) and o.get("p"):
                out[fid] = {"png": str(o["p"]), "cn": str(o.get("n") or "")}
        return out or None
    except Exception:
        return None


def _save_disk(table: dict[int, dict]) -> None:
    try:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        raw = {str(fid): {"p": e["png"], "n": e["cn"]} for fid, e in table.items()}
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False)
        os.replace(tmp, CACHE_FILE)
        try:
            os.chmod(CACHE_FILE, 0o600)
        except OSError:
            pass
    except Exception:
        pass


def _fetch_online() -> dict[int, dict]:
    resp = requests.get(CONFIG_URL, timeout=30, headers={"User-Agent": "qfly/1.0"})
    resp.raise_for_status()
    table = _parse_config(resp.text)
    if not table:
        raise ValueError("在线表为空")
    _save_disk(table)
    return table


def ensure(force: bool = False, max_age: float = _TTL) -> bool:
    """懒加载（幂等）。返回表是否非空；失败不抛，调用方降级。"""
    global _table, _loaded_at
    with _lock:
        if _table is not None and not force and time.time() - _loaded_at < max_age:
            return bool(_table)
        if _table is None:
            _table = _load_disk()
            if _table:
                try:
                    _loaded_at = os.path.getmtime(CACHE_FILE)
                except OSError:
                    _loaded_at = time.time()
        if _table is None or force or time.time() - _loaded_at >= max_age:
            try:
                _table = _fetch_online()
                _loaded_at = time.time()
            except Exception:
                if _table is None:
                    _table = {}
                    _loaded_at = time.time()
                # 拉取失败但有旧表/磁盘表 → 继续用旧的
        return bool(_table)


def lookup(fid: int) -> dict | None:
    """数字 id → {"name": 中文名, "icon": CDN URL}；未收录/未加载返回 None。"""
    ensure()
    e = (_table or {}).get(int(fid))
    if not e:
        return None
    return {"name": e["cn"], "icon": CDN_BASE + e["png"] + ".png"}


def enrich_parts(parts: list) -> list:
    """为 parse_outfit_parts 输出挂 name/icon（查不到保留原样）。"""
    if not isinstance(parts, list):
        return parts
    for p in parts:
        if not isinstance(p, dict):
            continue
        try:
            info = lookup(int(p.get("id") or 0))
        except (TypeError, ValueError):
            info = None
        if info:
            p["name"] = info["name"]
            p["icon"] = info["icon"]
    return parts


def enrich_summary(summary: dict) -> dict:
    """summarize_outfit 结果补充中文名/CDN 图标（就地修改并返回）。"""
    if isinstance(summary, dict) and isinstance(summary.get("parts"), list):
        try:
            enrich_parts(summary["parts"])
        except Exception:
            pass
    return summary
