# -*- coding: utf-8 -*-
"""地图与活动代币的**本地覆盖层**（data/map_overrides.json，0600）。

**为什么改动不直接写 assets/**：`scratch/deploy.py` 的 backend 清单会把整棵
`assets/**` 上传到生产 —— 在面板上改资产文件，下次部署就被仓库里的旧值覆盖回去。
所以运行期的修改一律落在 `data/`（部署会备份 data/ 但不会上传覆盖），
资产文件永远是"出厂值"，两者在加载时合并。

合并规则：
  · 地图：按 **原始 scene 名**做键（管理员改了 scene 也能对上），在该行上覆盖
    scene / scene_zh / map_id / join_level_id；标了 deleted 的行整条剔除。
  · 代币：整份替换（活动轮换本来就是整份换），覆盖字段逐个盖在资产配置上。

读失败一律当"没有覆盖"——一个坏文件不能让跑图用不了。
"""
from __future__ import annotations

import json
import os
import threading
import time

_DATA_DIR = os.path.abspath(os.environ.get("QFLY_DATA_DIR") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"))
OVERRIDE_FILE = os.path.join(_DATA_DIR, "map_overrides.json")

_lock = threading.Lock()

_MAP_FIELDS = ("scene", "scene_zh", "map_id", "join_level_id")
_TOKEN_STR_FIELDS = ("event", "label", "scene", "scene_zh", "currency_key")
# level_id：collect_pickup_batch 的 level_id（= 进图用的 map_id），与 map_id 分开写是为了
# 兼容「活动图 map_id 与 collect level_id 不同」的活动。缺省时 eventtoken 回落到 map_id。
_TOKEN_INT_FIELDS = ("map_id", "level_id", "level_hash", "join_level_id")
_EMITTER_KEYS = ("emitter_name", "event_name", "chunk_seq", "pickup_seq")
_MAX_EMITTERS = 512           # emitters 条目上限（实测一次活动最多几十条）
_MAX_ID = 4294967295          # 游戏侧是 u32
_MAX_IDS = 4000               # 代币 guid 数量上限（实测 127，留足余量）


def _load() -> dict:
    try:
        with open(OVERRIDE_FILE, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return {}
    if not isinstance(d, dict):
        return {}
    out = {}
    if isinstance(d.get("maps"), dict):
        out["maps"] = {k: v for k, v in d["maps"].items() if isinstance(v, dict)}
    if isinstance(d.get("token"), dict):
        out["token"] = d["token"]
    return out


def _save(d: dict) -> None:
    os.makedirs(_DATA_DIR, exist_ok=True)
    tmp = OVERRIDE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, OVERRIDE_FILE)
    try:
        os.chmod(OVERRIDE_FILE, 0o600)
    except OSError:
        pass


def _pos_int(v, field: str, allow_zero: bool = False) -> int:
    if isinstance(v, bool):
        raise ValueError("%s 不接受布尔值" % field)
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise ValueError("%s 必须是整数：%r" % (field, v))
    if n < 0 or (n == 0 and not allow_zero):
        raise ValueError("%s 必须大于 0" % field)
    if n > _MAX_ID:
        raise ValueError("%s 超出范围（最大 %d）" % (field, _MAX_ID))
    return n


def _text(v, field: str, maxlen: int = 64) -> str:
    s = str(v if v is not None else "").strip()
    if not s:
        raise ValueError("%s 不能为空" % field)
    if len(s) > maxlen:
        raise ValueError("%s 太长（最多 %d 字）" % (field, maxlen))
    return s


# ─────────────────────────── 地图 ───────────────────────────
def apply_routes(routes: list) -> list:
    """把本地覆盖合并进资产路线表。资产表本身不动。

    每条都带上 `_key`（= 资产表里的**原始 scene 名**）：管理员可能把 scene 改名，
    覆盖层与面板都必须用这个稳定键去对，不能用改名后的值。
    """
    ov = (_load().get("maps") or {})
    out = []
    for r in routes:
        key = str(r.get("scene") or "")
        o = ov.get(key) or {}
        if o.get("deleted"):
            continue
        m = dict(r)
        m["_key"] = key
        for f in _MAP_FIELDS:
            if o.get(f) not in (None, ""):
                m[f] = o[f]
        out.append(m)
    return out


def map_overrides() -> dict:
    """面板用：原始 scene 名 → 覆盖内容。"""
    return dict(_load().get("maps") or {})


def set_map(key: str, patch: dict, by: str = "") -> dict:
    """改一张图。key 是**原始 scene 名**（资产表里的那个），改 scene 本身也不会错位。"""
    key = _text(key, "scene", 64)
    unknown = [k for k in patch if k not in _MAP_FIELDS]
    if unknown:
        raise ValueError("不认识的字段：%s" % "、".join(unknown))
    cur: dict = {}
    with _lock:
        data = _load()
        cur = dict((data.get("maps") or {}).get(key) or {})
        if "scene" in patch:
            cur["scene"] = _text(patch["scene"], "scene", 64)
        if "scene_zh" in patch:
            cur["scene_zh"] = _text(patch["scene_zh"], "scene_zh", 64)
        if "map_id" in patch:
            cur["map_id"] = _pos_int(patch["map_id"], "map_id")
        if "join_level_id" in patch:
            cur["join_level_id"] = _pos_int(patch["join_level_id"], "join_level_id", allow_zero=True)
        cur["at"] = time.time()
        cur["by"] = str(by or "")[:64]
        data.setdefault("maps", {})[key] = cur
        _save(data)
    return {"key": key, "override": cur}


def reset_map(key: str) -> bool:
    with _lock:
        data = _load()
        maps = data.get("maps") or {}
        hit = maps.pop(str(key), None) is not None
        data["maps"] = maps
        _save(data)
    return hit


# ─────────────────────────── 活动代币 ───────────────────────────
def token_override() -> dict:
    return dict(_load().get("token") or {})


def apply_token(cfg: dict) -> dict:
    """把覆盖合并到资产代币配置上。覆盖为空则原样返回。"""
    ov = token_override()
    if not ov:
        return cfg
    m = dict(cfg or {})
    for k, v in ov.items():
        if k in ("at", "by"):
            continue
        m[k] = v
    return m


def set_token(patch: dict, by: str = "") -> dict:
    """改代币配置。pickup_ids 传列表（面板里是多行文本拆出来的整数）。"""
    allowed = set(_TOKEN_STR_FIELDS) | set(_TOKEN_INT_FIELDS) | {
        "pickup_ids", "currency_types", "source_types", "emitters"}
    unknown = [k for k in patch if k not in allowed]
    if unknown:
        raise ValueError("不认识的字段：%s" % "、".join(unknown))
    with _lock:
        data = _load()
        cur = dict(data.get("token") or {})
        for f in _TOKEN_STR_FIELDS:
            if f in patch:
                cur[f] = _text(patch[f], f, 120)
        for f in _TOKEN_INT_FIELDS:
            if f in patch:
                cur[f] = _pos_int(patch[f], f, allow_zero=(f == "level_hash"))
        if "pickup_ids" in patch:
            ids = patch["pickup_ids"]
            if not isinstance(ids, list) or not ids:
                raise ValueError("pickup_ids 不能为空")
            if len(ids) > _MAX_IDS:
                raise ValueError("pickup_ids 太多了（最多 %d 个）" % _MAX_IDS)
            clean = []
            for x in ids:
                clean.append(_pos_int(x, "pickup_ids 里的值"))
            cur["pickup_ids"] = clean
        if isinstance(patch.get("currency_types"), list):
            cur["currency_types"] = [str(x)[:60] for x in patch["currency_types"][:20]]
        if isinstance(patch.get("source_types"), list):
            cur["source_types"] = [str(x)[:60] for x in patch["source_types"][:20]]
        if "emitters" in patch:
            raw = patch["emitters"]
            if not isinstance(raw, list):
                raise ValueError("emitters 必须是数组")
            if len(raw) > _MAX_EMITTERS:
                raise ValueError("emitters 太多了（最多 %d 条）" % _MAX_EMITTERS)
            clean = []
            for e in raw:
                if not isinstance(e, dict):
                    raise ValueError("emitters 每条必须是对象")
                row = {}
                for k in _EMITTER_KEYS:
                    if k not in e:
                        continue
                    row[k] = _text(e[k], k, 80) if k.endswith("_name") \
                        else _pos_int(e[k], k, allow_zero=True)
                if not row.get("emitter_name"):
                    raise ValueError("emitters 每条必须有 emitter_name")
                clean.append(row)
            cur["emitters"] = clean
        cur["at"] = time.time()
        cur["by"] = str(by or "")[:64]
        data["token"] = cur
        _save(data)
    return cur


def reset_token() -> bool:
    with _lock:
        data = _load()
        hit = "token" in data
        data.pop("token", None)
        _save(data)
    return hit
