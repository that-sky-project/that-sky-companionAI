"""Game2 RPC 发送口（点蜡烛/点火盆）。

控制文件：<数据目录>/control/light_candle.json
  {"auto": true}                        进图后周期性地把全图火焰+蜡烛全点一遍（每 25 秒一轮）
  {"state": "pending", "key": "0x5428fb22", "ordinals": [...]}   手动指定（优先）

图数据：app/config/candle-tables.json（可被 control/ 同名文件覆盖）
  {"maps": {"<map_id>": {"flames": N, "candles": M}}}

帧格式（与真实客户端一致，已实测有效）：
  [02][epoch][00][自己槽位] + {key u32, hdr u8, len u16, body}
  LightRpc     0x5428fb22  body [u16 序号][u8 1]        火焰序号从 1
  CandleCreate 0xc4f750fc  body [u16 序号][u8 0][u16 0]
  点蜡烛       0x7d43c1ec  body [u16 序号][ff ff]        蜡烛序号从 0
  SpawnShout   0x9b4aeaed  body [slot][1a1a1a][强度][3 随机]
"""
from __future__ import annotations

import json
import os
import struct
import time

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

CONTROL = _xg("control", "light_candle.json")
TABLES = _xg_cfg("candle-tables.json")
KEY_CANDLE = 0x7D43C1EC
KEY_FLAME = 0x5428FB22
KEY_CREATE = 0xC4F750FC
KEY_SHOUT = 0x9B4AEAED
KEY_CHANGELEVEL = 0x4D84A062  # ChangeLevelWithFadeRpc
KEY_OPENGATE = 0x5E9411BD  # OpenGateRpc
KEY_REVOKE = 0xFFFFFFFF  # 特殊：放弃关卡所有者（不是 RPC，是 kind 9 帧）
AUTO_DELAY = 12.0
AUTO_PERIOD = 25.0

_cache = {"control_mtime": None, "control": None, "tables_mtime": None, "tables": None}
_auto = {"key": None, "since": None, "next": 0.0, "queue": [], "sent": 0}


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


def _save(cmd):
    try:
        with open(CONTROL, "w", encoding="utf-8") as handle:
            json.dump(cmd, handle, ensure_ascii=False)
        _cache["control_mtime"] = os.path.getmtime(CONTROL)
        _cache["control"] = cmd
    except Exception:
        pass


def build(key, body, epoch, slot):
    from qfsky.long0167.engine import Frame
    payload = (bytes((2, epoch & 0xFF, 0, slot & 0xFF))
               + struct.pack("<I", key & 0xFFFFFFFF)
               + bytes((0,))
               + struct.pack("<H", len(body))
               + body)
    return Frame(14, payload, True)


def _body(key, ordinal):
    if key == KEY_FLAME:
        return struct.pack("<H", int(ordinal)) + bytes((1,))
    if key == KEY_CREATE:
        return struct.pack("<H", int(ordinal)) + bytes((0,)) + struct.pack("<H", 0)
    return struct.pack("<H", int(ordinal)) + b"\xff\xff"


def _log(tag, slot, frames):
    if tag == "auto":
        return          # 自动轮次每 25 秒一轮，不写日志免得无限增长
    try:
        if not os.environ.get("XG_RESEARCH_LOG"): return
        with open(_xg("logs", "candle-rpc-sent.log"), "a", encoding="utf-8") as handle:
            stamp = time.strftime("%H:%M:%S")
            for frame in frames:
                handle.write("%s %s slot=%s %s\n" % (stamp, tag, slot, frame.payload.hex()))
    except Exception:
        pass


def _raw_frames(epoch, slot, limit):
    """批量模式：cmd["raw_frames"] = [{"key": "0x..", "scene": "...", "tail": [a,b], "ordinal": n}, ...]"""
    cmd = _load(CONTROL, "control")
    if not cmd or cmd.get("state") != "pending":
        return None
    frames_spec = cmd.get("raw_frames") or []
    if not frames_spec:
        return None
    batch = frames_spec[:limit]
    frames = []
    for spec in batch:
        key = int(str(spec.get("key") or "0x0"), 16)
        if key == KEY_CHANGELEVEL:
            scene = str(spec.get("scene") or "").encode("utf-8")
            tails = spec.get("tail") or [0, 0]
            body = (struct.pack("<B", slot & 0xFF) + struct.pack("<I", len(scene)) + scene
                    + struct.pack("<II", int(tails[0]) & 0xFFFFFFFF, int(tails[1]) & 0xFFFFFFFF))
        elif key == KEY_OPENGATE:
            body = struct.pack("<H", int(spec.get("ordinal") or 0)) + bytes((1,))
        else:
            body = bytes.fromhex(str(spec.get("body") or "")) or struct.pack("<H", int(spec.get("ordinal") or 0)) + b"\xff\xff"
        frames.append(build(key, body, epoch, slot))
    cmd["raw_frames"] = frames_spec[limit:]
    cmd["sent"] = int(cmd.get("sent", 0)) + len(batch)
    if not cmd["raw_frames"]:
        cmd["state"] = "done"
    _save(cmd)
    _log("raw", slot, frames)
    return frames


def _manual(epoch, slot, limit):
    cmd = _load(CONTROL, "control")
    if not cmd or cmd.get("state") != "pending":
        return None
    ordinals = cmd.get("ordinals") or []
    if not ordinals:
        cmd["state"] = "done"
        _save(cmd)
        return []
    key = int(str(cmd.get("key") or "0x7d43c1ec"), 16)
    if key == KEY_REVOKE:
        from qfsky.long0167.engine import Frame as _Frame
        payload = bytes((9, epoch & 0xFF, 0)) + struct.pack("<BH", 0, 0)
        frames = [_Frame(14, payload, True)]
        cmd["state"] = "done"
        _save(cmd)
        _log("revoke", slot, frames)
        return frames
    if key == KEY_SHOUT:
        import random as _random
        tail = _random.randrange(0, 1 << 16) | (_random.randrange(5, 9) << 16)
        body = bytes((slot & 0xFF, 0x1A, 0x1A, 0x1A, 255)) + tail.to_bytes(3, "little")
        frames = [build(key, body, epoch, slot)]
        cmd["state"] = "done"
        _save(cmd)
        _log("shout", slot, frames)
        return frames
    if key == KEY_CHANGELEVEL:
        scene = str(cmd.get("scene") or "")
        tails = cmd.get("tail") or [0, 0]
        t0 = int(tails[0]) if len(tails) > 0 else 0
        t1 = int(tails[1]) if len(tails) > 1 else 0
        name = scene.encode("utf-8")
        body = struct.pack("<B", slot & 0xFF) + struct.pack("<I", len(name)) + name + struct.pack("<II", t0 & 0xFFFFFFFF, t1 & 0xFFFFFFFF)
        frames = [build(key, body, epoch, slot)]
        cmd["state"] = "done"
        _save(cmd)
        _log("changelevel", slot, frames)
        return frames
    if key == KEY_OPENGATE:
        batch = ordinals[:limit]
        frames = [build(key, struct.pack("<H", int(n)) + bytes((1,)), epoch, slot) for n in batch]
        cmd["ordinals"] = ordinals[limit:]
        cmd["sent"] = int(cmd.get("sent", 0)) + len(batch)
        if not cmd["ordinals"]:
            cmd["state"] = "done"
        _save(cmd)
        _log("opengate", slot, frames)
        return frames
    batch = ordinals[:limit]
    frames = [build(key, _body(key, n), epoch, slot) for n in batch]
    cmd["ordinals"] = ordinals[limit:]
    cmd["sent"] = int(cmd.get("sent", 0)) + len(batch)
    if not cmd["ordinals"]:
        cmd["state"] = "done"
    _save(cmd)
    _log("manual", slot, frames)
    return frames


def _auto_plan(map_id):
    tables = _load(TABLES, "tables") or {}
    return (tables.get("maps") or {}).get(str(map_id))


def drain(epoch, slot, map_id=None, limit=8):
    """返回本 tick 要发的 Frame 列表。"""
    raw = _raw_frames(epoch, slot, limit)
    if raw is not None:
        return raw
    manual = _manual(epoch, slot, limit)
    if manual is not None:
        return manual

    conf = _load(CONTROL, "control") or {}
    if not conf.get("auto"):
        return []
    row = _auto_plan(map_id)
    if not row:
        return []

    now = time.time()
    key = (map_id, epoch)
    if _auto["key"] != key:
        _auto.update(key=key, since=now, next=now + AUTO_DELAY, queue=[], sent=0)

    if not _auto["queue"] and now >= _auto["next"]:
        flames = [(KEY_FLAME, n) for n in range(1, int(row.get("flames") or 0) + 1)]
        candles = [(KEY_CANDLE, n) for n in range(0, int(row.get("candles") or 0))]
        _auto["queue"] = flames + candles
        _auto["next"] = now + AUTO_PERIOD

    if not _auto["queue"]:
        return []
    batch = _auto["queue"][:limit]
    _auto["queue"] = _auto["queue"][limit:]
    frames = [build(k, _body(k, n), epoch, slot) for k, n in batch]
    _auto["sent"] += len(batch)
    _log("auto", slot, frames)
    return frames
