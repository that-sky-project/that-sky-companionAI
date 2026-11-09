"""地图清单（2026-10-31）：录到哪张就有哪张，不再写死四张图。

清单就是 runtime/private/connection0167_profiles.local.json 的 maps（自动录图登记新图时往里加条目）；
中文名优先用条目里的 name，没有就查客户端字符串 name_level_<Scene>（ref/client-0167/strings.json，107 个场景）。
同名的（比如禁阁好几层都叫“禁阁”）在后面带上场景名区分。
"""
import json
from pathlib import Path

import xg_paths

ROOT = xg_paths.DATA
CFG = xg_paths.PROFILES_JSON
STRINGS = xg_paths.DATA / "ref/client-0167/strings.json"
OVERRIDE = {"CandleSpace": "遇境", "Sunset": "霞谷", "Prairie_ButterflyFields": "云野大厅", "DuskStart": "墓土大厅"}
_strings = {}


def scene_names():
    """{场景名: 中文名}：客户端里所有 name_level_*。"""
    if not _strings:
        try:
            z = json.loads(STRINGS.read_text(encoding="utf-8"))[""]
            _strings.update({k[11:]: v for k, v in z.items() if k.startswith("name_level_")})
        except (OSError, ValueError, KeyError):
            pass
        _strings.update(OVERRIDE)
    return _strings


def zh_name(scene):
    return scene_names().get(scene) or scene


def _config():
    try:
        return json.loads(CFG.read_text()).get("maps", {})
    except (OSError, ValueError):
        return {}


def all_maps():
    """{map_id: 中文名}，按 遇境/霞谷/云野大厅/墓土大厅 在前、其余按名字排。"""
    rows = [(int(k), e.get("name") or zh_name(e.get("scene", "")), e.get("scene", "")) for k, e in _config().items()]
    count = {}
    for _, n, _ in rows:
        count[n] = count.get(n, 0) + 1
    first = list(OVERRIDE)
    rows.sort(key=lambda r: (first.index(r[2]) if r[2] in first else len(first), r[1], r[2]))
    return {mid: (n if count[n] == 1 else f"{n}（{scene}）") for mid, n, scene in rows}


def recorded_by(uuid):
    """{map_id: 这个号录过没}（地图录制绑定账号，没录过的图进不去）。"""
    out = {}
    for k, e in _config().items():
        cap = e.get("current_capture") or {}
        out[int(k)] = bool(uuid) and (uuid in (cap.get("by_account") or {}) or cap.get("account") == uuid)
    return out
