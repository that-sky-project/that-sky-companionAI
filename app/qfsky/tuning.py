# -*- coding: utf-8 -*-
"""跑图速率与就绪探针的可调参数（data/tuning.json，0600）。

**为什么要单独一层**：这些值原来是 `pickup.py` 里的模块常量 —— 每图冷却、批间隔、
就绪探针超时、单批条数、进图重试次数。它们恰好就是「跑快一点 / 稳一点 / 少触发风控」
的旋钮，却只能改代码再部署。做成可调之后，管理员在面板上就能实时拧。

三个硬约束：
  1. **读失败一律回默认值** —— 一个坏掉的 JSON 不能让所有人的跑图跑不起来。
  2. **写入做区间钳制** —— 夹到 [min, max]，非法类型抛 ValueError（由接口转 400）。
  3. **调用方每次现读**（带 2 秒进程内缓存）—— 所以调完即时生效，不用重启服务。

与「并发池」的分工：并发池（`/api/admin/concurrency`）控制**同时开几个任务**；
这里控制**单个跑图任务内部每一步多快**。两个都要，管的是不同的东西。
"""
from __future__ import annotations

import json
import os
import threading
import time

_DATA_DIR = os.path.abspath(os.environ.get("QFLY_DATA_DIR") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"))
TUNING_FILE = os.path.join(_DATA_DIR, "tuning.json")

_lock = threading.Lock()
_flock = threading.Lock()
_cache: dict | None = None
_cache_at = 0.0
_TTL = 2.0

# 字段定义： 键 -> (默认值, 最小, 最大, 中文名, 单位, 说明)
FIELDS: dict[str, tuple] = {
    "between_maps": (0.35, 0.0, 10.0, "每张图之间停多久", "秒", "跑完一张图歇一下再进下一张，调大更慢更稳"),
    "batch_gap": (0.35, 0.0, 10.0, "两批收集之间停多久", "秒", "同一张图内两批 collect 的间隔"),
    "ready_timeout": (22.0, 3.0, 120.0, "进图就绪等待上限", "秒", "进图后反复试探；超过这个时间还没探到就判本图失败，调大能给慢图更多机会"),
    "ready_interval": (0.4, 0.1, 5.0, "就绪探针间隔", "秒", "两次试探之间等多久"),
    "probe_ids": (4, 1.0, 32, "每轮试探带几个收集点", "个", "试探请求里放的 pickup id 条数，越少越轻"),
    "live_batch": (32, 1.0, 64, "单批收集条数（烛火）", "个", "一次 collect 请求最多带多少个点，官方上限 64"),
    "dye_batch": (32, 1.0, 64, "单批收集条数（染料）", "个", "染料收集的批大小"),
    "enter_attempts": (2, 1.0, 5, "进图重试次数", "次", "进图失败后重试几轮，调大更耐抖"),
}

_ORDER = list(FIELDS.keys())

PRESETS: dict[str, dict] = {
    "gentle": {
        "label": "保守（最慢最稳）",
        "note": "适合整站多人同时跑图、或风控敏感期。速度约标准档的一半。",
        "v": {"between_maps": 1.2, "batch_gap": 0.9, "ready_timeout": 30.0, "ready_interval": 0.6,
              "probe_ids": 2, "live_batch": 16, "dye_batch": 16, "enter_attempts": 3},
    },
    "standard": {
        "label": "标准（默认）",
        "note": "出厂的取值，实测单人多开都不容易出问题。",
        "v": {k: FIELDS[k][0] for k in _ORDER},
    },
    "fast": {
        "label": "快速（激进）",
        "note": "追求速度，请求更密、单批更大。人少时用；风控敏感期别用。",
        "v": {"between_maps": 0.12, "batch_gap": 0.15, "ready_timeout": 16.0, "ready_interval": 0.25,
              "probe_ids": 8, "live_batch": 64, "dye_batch": 64, "enter_attempts": 1},
    },
}


def _defaults() -> dict:
    return {k: FIELDS[k][0] for k in _ORDER}


def _clamp(key: str, v) -> float:
    _d, lo, hi, _zh, _u, _n = FIELDS[key]
    if isinstance(v, bool):
        raise ValueError("%s 不接受布尔值" % key)
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise ValueError("%s 不是数字：%r" % (key, v))
    if x != x or x in (float("inf"), float("-inf")):   # NaN / inf
        raise ValueError("%s 不是有效数字" % key)
    return max(lo, min(hi, x))


def _norm(raw) -> dict:
    out = _defaults()
    preset = "standard"
    if isinstance(raw, dict):
        vals = raw.get("v")
        if isinstance(vals, dict):
            for k in _ORDER:
                if k in vals:
                    try:
                        out[k] = _clamp(k, vals[k])
                    except ValueError:
                        pass          # 单项非法就保留默认，不让整份配置失效
        p = raw.get("preset")
        if isinstance(p, str) and p in PRESETS:
            preset = p
    # 整型字段收成整数，面板显示才不会是 4.0
    for k in ("probe_ids", "live_batch", "dye_batch", "enter_attempts"):
        out[k] = int(out[k])
    return {"preset": preset, "v": out}


def load() -> dict:
    """读全量配置（含 preset）。任何异常都回默认值。"""
    try:
        with open(TUNING_FILE, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        raw = None
    return _norm(raw)


def _save(data: dict) -> None:
    os.makedirs(_DATA_DIR, exist_ok=True)
    tmp = TUNING_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, TUNING_FILE)
    try:
        os.chmod(TUNING_FILE, 0o600)
    except OSError:
        pass


def pickup() -> dict:
    """跑图路径按这个取值。2 秒进程内缓存 —— 跑一张图会读几十次，不能每次都打盘。"""
    global _cache, _cache_at
    now = time.time()
    with _lock:
        if _cache is None or now - _cache_at > _TTL:
            _cache = load()
            _cache_at = now
        return _cache["v"]


def snapshot() -> dict:
    """给管理面板：当前值 + 字段说明 + 预设档 + 是否偏离预设。"""
    global _cache, _cache_at
    with _lock:
        data = load()
        _cache = data                      # 面板读一次就顺便刷新缓存
        _cache_at = time.time()
    cur = data["v"]
    diff = {k: cur[k] for k in _ORDER if abs(float(cur[k]) - float(PRESETS[data["preset"]]["v"][k])) > 1e-9}
    return {
        "preset": data["preset"],
        "v": cur,
        "custom": bool(diff) or data["preset"] not in PRESETS,
        "presets": {k: {"label": v["label"], "note": v["note"], "v": v["v"]} for k, v in PRESETS.items()},
        "fields": {k: {"zh": FIELDS[k][3], "unit": FIELDS[k][4], "note": FIELDS[k][5],
                       "min": FIELDS[k][1], "max": FIELDS[k][2], "default": FIELDS[k][0]}
                   for k in _ORDER},
    }


def apply(preset: str | None = None, values: dict | None = None) -> dict:
    """套用预设 / 改单项。返回最新快照。非法输入抛 ValueError。"""
    global _cache, _cache_at
    with _flock:
        data = load()
        if preset:
            if preset not in PRESETS:
                raise ValueError("没有这个档位：%s" % preset)
            data = {"preset": preset, "v": dict(PRESETS[preset]["v"])}
        if values:
            cur = dict(data["v"])
            for k, v in values.items():
                if k not in FIELDS:
                    raise ValueError("没有这个参数：%s" % k)
                cur[k] = _clamp(k, v)
            data = {"preset": data["preset"], "v": cur}
        data = _norm(data)
        _save(data)
        with _lock:
            _cache = data
            _cache_at = time.time()
    return snapshot()
