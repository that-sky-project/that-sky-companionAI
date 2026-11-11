# -*- coding: utf-8 -*-
"""actor 投影：把源录制 actor 的 map_id / 起点 / 速度改成目标图，并修好友传送头。

与 T9 ``tools/friend_join.py``、``tools/v32c/build_bundles_v32c.py`` 同一套
token 边界算法；不重新实现 codec。
"""
from __future__ import annotations

import dataclasses
import struct
from pathlib import Path

from .common import GameDataError

BOUNDARY_MAIN_MAP = "0x21dd0e8"
BOUNDARY_ORIGIN = "0x21dd054"
BOUNDARY_HISTORY = "0x21defc0"
BOUNDARY_VELOCITY = "0x21dd298"
BOUNDARY_LEVEL_STATE = "0x21df648"


def _codec():
    try:
        from qfsky.long0167.actor_codec import actor_wire_0167 as aw
        return aw
    except Exception as exc:  # pragma: no cover - 安装不完整
        raise GameDataError("加载 actor codec 失败：%s（程序目录不完整）" % exc,
                            "actor_codec_missing")


def _starts(obs) -> list[int]:
    starts = []
    bit = 0
    for token in obs._tokens:
        if token.kind == "raw":
            bit = (bit + 7) & ~7
        starts.append(bit)
        bit += token.extent * 8 if token.kind == "raw" else token.extent
    return starts


def _index_for(obs, label: str, kind: str, extent: int) -> int:
    offsets = [offset for name, offset in obs._boundaries if name == label]
    if len(offsets) != 1:
        raise GameDataError("actor 缺少边界 %s（源录制格式不对）" % label, "actor_boundary")
    starts = _starts(obs)
    index = next((i for i, start in enumerate(starts) if start >= offsets[0]), None)
    if index is None or (obs._tokens[index].kind, obs._tokens[index].extent) != (kind, extent):
        raise GameDataError("actor 边界 %s 的形状不是 %s/%d" % (label, kind, extent),
                            "actor_field_shape")
    return index


def level_state(raw: bytes, native_sha: str) -> dict:
    aw = _codec()
    obs = aw.inspect_actor_wire(raw, verified_profile_sha256=native_sha,
                                check_zero_padding=True, require_canonical_writer=True)
    i = _index_for(obs, BOUNDARY_LEVEL_STATE, "raw", 2)
    tokens = obs._tokens[i:i + 3]
    if [t.extent for t in tokens] != [2, 4, 4]:
        raise GameDataError("0x21df648 头形状不是 raw2/raw4/raw4", "actor_header_shape")
    return {"raw2": tokens[0].value, "level_id": struct.unpack("<I", tokens[1].value)[0],
            "resource_tag": struct.unpack("<I", tokens[2].value)[0],
            "bit_offset": _starts(obs)[i]}


def project(source: bytes, map_id: int, position, tag: int, native_sha: str) -> tuple[bytes, dict]:
    """源 actor -> 目标图 actor；只改 map_id/起点/速度/0x21df648 头。"""
    if type(map_id) is not int or not 0 < map_id < 2 ** 32:
        raise GameDataError("目标 map_id 不合法", "actor_map_id")
    if not isinstance(position, (list, tuple)) or len(position) != 3:
        raise GameDataError("目标落点不是三个数", "actor_position")
    if type(tag) is not int or not 0 <= tag <= 0xFFFF:
        raise GameDataError("目标 resource tag 不合法", "actor_tag")
    aw = _codec()
    try:
        obs = aw.inspect_actor_wire(source, verified_profile_sha256=native_sha,
                                    check_zero_padding=True, require_canonical_writer=True)
    except Exception as exc:
        raise GameDataError("源 actor 不是 canonical 0.16.8 actor：%s" % exc, "actor_source")
    T = list(obs._tokens)
    R = type(T[0])
    main = _index_for(obs, BOUNDARY_MAIN_MAP, "raw", 4)
    origin = _index_for(obs, BOUNDARY_ORIGIN, "raw", 4)
    history = _index_for(obs, BOUNDARY_HISTORY, "raw", 1)
    velocity = _index_for(obs, BOUNDARY_VELOCITY, "bits", 8)
    state = _index_for(obs, BOUNDARY_LEVEL_STATE, "raw", 2)
    T[main] = R("raw", 4, struct.pack("<I", map_id))
    T[history + 1] = R("raw", 4, struct.pack("<I", map_id))
    for k, value in enumerate(position):
        T[origin + k] = R("raw", 4, struct.pack("<f", float(value)))
        T[history + 2 + k] = R("raw", 4, struct.pack("<f", float(value)))
    for k in range(3):
        T[velocity + k] = R("bits", 8, 127)
    T[state] = R("raw", 2, b"\0\0")
    T[state + 1] = R("raw", 4, struct.pack("<I", map_id))
    T[state + 2] = R("raw", 4, struct.pack("<I", tag))
    try:
        out = aw.reencode_actor_wire(dataclasses.replace(obs, _tokens=tuple(T)),
                                     verified_profile_sha256=native_sha)
    except Exception as exc:
        raise GameDataError("actor 重新编码失败：%s" % exc, "actor_reencode")
    checked = aw.inspect_actor_wire(out, verified_profile_sha256=native_sha,
                                    check_zero_padding=True, require_canonical_writer=True)
    if aw.reencode_actor_wire(checked, verified_profile_sha256=native_sha) != out:
        raise GameDataError("actor 重新编码后不是 canonical", "actor_canonical")
    if len(out) != len(source):
        raise GameDataError("actor 长度变化：%d -> %d" % (len(source), len(out)), "actor_extent")
    if not checked.matches_prefix_u32(map_id):
        raise GameDataError("actor 的 map_id 前缀没改对", "actor_prefix")
    after = level_state(out, native_sha)
    if (after["raw2"], after["level_id"], after["resource_tag"]) != (b"\0\0", map_id, tag):
        raise GameDataError("actor 0x21df648 头没改对", "actor_header")
    return out, after


def inspect(path: Path, native_sha: str) -> dict:
    return inspect_bytes(Path(path).read_bytes(), native_sha)


def inspect_bytes(raw: bytes, native_sha: str) -> dict:
    aw = _codec()
    obs = aw.inspect_actor_wire(raw, verified_profile_sha256=native_sha,
                                check_zero_padding=True, require_canonical_writer=True)
    state = level_state(raw, native_sha)
    return {"bytes": len(raw), "counts": dict(obs._counts),
            "raw2": state["raw2"].hex(), "level_id": state["level_id"],
            "resource_tag": state["resource_tag"]}
