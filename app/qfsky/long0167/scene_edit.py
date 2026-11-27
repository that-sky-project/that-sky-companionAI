"""暗植清零 v3：按"场景名"查槽位，新登记的图自动覆盖。

数据：
  control/darkstone-slots.json
    {"maps": {"4133595729": 38, ...}, "scenes": {"Rain_Cave": 38, "Dusk": 86, ...}}
  runtime/private/connection0167_profiles.local.json  → maps[id].scene 用来把 map_id 换成场景名
控制：
  control/zero_darkstones.json
    {"enabled": true}                                     全自动
    {"enabled": true, "maps": [..], "from": 0, "to": 5}    精细覆盖
"""
from __future__ import annotations

import json
import os

def _xg(*parts, config=False):
    """客户端：用户数据目录（xg_paths.DATA）或随包 app/config 下的文件。"""
    try:
        import xg_paths as _xp
        base = _xp.APP / "config" if config else _xp.DATA
    except Exception:
        base = os.environ.get("XG_DATA_DIR") or "."
    return os.path.join(str(base), *parts)


def _xg_cfg(name):
    """先找用户 control/ 覆盖，再退回随包 app/config/。"""
    p = _xg("control", name)
    return p if os.path.isfile(p) else _xg(name, config=True)

CONTROL = _xg("control", "zero_darkstones.json")
SLOTS = _xg_cfg("darkstone-slots.json")
CONFIG = _xg("runtime", "private", "connection0167_profiles.local.json")
_cache = {}


def _load(path, key):
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    if _cache.get(key + "_mtime") != mtime:
        try:
            _cache[key] = json.load(open(path, encoding="utf-8"))
            _cache[key + "_mtime"] = mtime
        except Exception:
            _cache[key] = None
    return _cache.get(key)


def _scene_of(map_id):
    cfg = _load(CONFIG, "config") or {}
    entry = (cfg.get("maps") or {}).get(str(map_id)) or {}
    return entry.get("scene")


def _slots_of(map_id, scene):
    table = _load(SLOTS, "slots") or {}
    scenes = table.get("scenes") or {}
    if scene and scene in scenes:
        return scenes[scene]
    return (table.get("maps") or {}).get(str(map_id))


def _set_bits(data, position, width, value):
    shift = 0
    while width:
        index, offset = divmod(position, 8)
        take = min(8 - offset, width)
        mask = ((1 << take) - 1) << offset
        chunk = (value >> shift) & ((1 << take) - 1)
        data[index] = (data[index] & ~mask) | (chunk << offset)
        position += take
        shift += take
        width -= take


# T107（11-22）：只在真有可烧花（giveWax=1 的 SoulTrap）的 26 张图清暗石；遇境等其它图一律不动
# （遇境表里有 15 个暗石槽，清零后主人那边牵手按钮变灰）。SKY_T107_FLOWER_ONLY=0 回退。
FLOWER_MAPS = frozenset((128844448, 164626931, 170656205, 295816905, 507487826, 864432821, 1104763408,
    1147491976, 1563891948, 1597085778, 1612139937, 1649439303, 1694829989, 1705189686, 2159642775,
    2350532176, 2650921869, 2720691892, 2839585646, 2899790755, 3057325709, 3244931597, 3429122502,
    3516048122, 4070111637, 4133595729))


def _range_for(map_id):
    if os.environ.get("SKY_T107_FLOWER_ONLY", "1") != "0" and int(map_id) not in FLOWER_MAPS:
        return None
    conf = _load(CONTROL, "control")
    if not conf or not conf.get("enabled"):
        return None
    maps = conf.get("maps") or []
    if maps and int(map_id) not in [int(x) for x in maps]:
        return None
    if "to" in conf:
        return int(conf.get("from", 0)), int(conf.get("to", 0))
    slots = _slots_of(map_id, _scene_of(map_id))
    if not slots:
        return None
    return 0, int(slots) - 1



GATE_CONTROL = _xg("control", "gate_flip.json")


def _gate_conf():
    try:
        import json
        return json.load(open(GATE_CONTROL, encoding="utf-8"))
    except Exception:
        return None


def _walk_positions(obs):
    """按 walker 的对齐规则算出每个 token 的起始 bit（raw 前对齐到字节）。"""
    cursor = 0
    out = []
    for token in obs._tokens:
        if token.kind == "raw":
            cursor = (cursor + 7) & ~7
        out.append(cursor)
        cursor += token.extent * 8 if token.kind == "raw" else token.extent
    return out


def apply_gates(body, *, map_id, profile_sha):
    """只对 gate_flip.json 里 maps 指定的地图生效。stage: 1=门布尔 2=+子布尔 3=+槽位。"""
    try:
        conf = _gate_conf()
        if not conf or not conf.get("enabled"):
            return body
        maps = [int(x) for x in (conf.get("maps") or [])]
        if maps and int(map_id) not in maps:
            return body
        stage = int(conf.get("stage") or 1)
        from qfsky.long0167.codec import scene_wire as wire
        obs = wire.inspect_scene_wire(body, verified_profile_sha256=profile_sha)
        pos = {}
        for label, bitpos in obs._boundaries:
            pos.setdefault(label, bitpos)
        start = pos.get("candle_gates")
        end = pos.get("rain")
        if start is None or end is None:
            return body
        positions = _walk_positions(obs)
        i = next((k for k, p in enumerate(positions) if p >= start), None)
        if i is None or positions[i] != start:
            return body
        data = bytearray(body)
        idx = i
        for gate in range(16):
            _set_bits(data, positions[idx], 1, 1)          # 门布尔
            idx += 1
            for sub in range(6):
                ent_pos = positions[idx]; idx += 1          # entity 字节不动
                if stage >= 2:
                    _set_bits(data, positions[idx], 1, 1)   # 子布尔1
                idx += 1
                if stage >= 3:
                    _set_bits(data, positions[idx], 16, (gate % 8) + 1)  # 槽位 u16
                idx += 1
                if stage >= 2:
                    _set_bits(data, positions[idx], 1, 1)   # 子布尔2
                    _set_bits(data, positions[idx + 1], 1, 1)
                idx += 2
        out = bytes(data)
        try:
            wire.inspect_scene_wire(out, verified_profile_sha256=profile_sha, check_zero_padding=True)
        except Exception:
            return body
        return out
    except Exception:
        return body


def apply_zero(body, *, map_id, profile_sha):
    """返回清零后的新 body；未启用/该图无暗植/出错时原样返回。"""
    try:
        span = _range_for(map_id)
        if not span or span[1] < span[0]:
            return body
        lo, hi = span
        from qfsky.long0167.codec import scene_wire as wire
        obs = wire.inspect_scene_wire(body, verified_profile_sha256=profile_sha)
        pos = {}
        for label, bitpos in obs._boundaries:
            pos.setdefault(label, bitpos)
        start, end = pos["darkstones"], pos["darkvines"]
        data = bytearray(body)
        for slot in range(lo, hi + 1):
            bit = start + slot * 9
            if bit + 9 > end:
                break
            _set_bits(data, bit, 8, 0)
            _set_bits(data, bit + 8, 1, 0)
        return bytes(data)
    except Exception:
        return body
