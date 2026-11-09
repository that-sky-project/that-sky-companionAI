# -*- coding: utf-8 -*-
"""哥哥标注落点包（spawn_pack.json）读取与 FNV-1a32。"""
from __future__ import annotations

import json
from pathlib import Path

from .common import GameDataError

PACK = Path(__file__).with_name("spawn_pack.json")
FNV_OFFSET = 0x811C9DC5
FNV_PRIME = 0x01000193
_CACHE = None


def fnv32(text: str) -> int:
    h = FNV_OFFSET
    for b in text.encode("utf-8"):
        h = ((h ^ b) * FNV_PRIME) & 0xFFFFFFFF
    return h


def load(path: Path | None = None) -> dict:
    global _CACHE
    if _CACHE is not None and path is None:
        return _CACHE
    target = Path(path) if path is not None else PACK
    try:
        doc = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GameDataError("落点包读不出来：%s（%s）" % (target, exc), "spawn_pack")
    if (not isinstance(doc, dict) or doc.get("schema") != "xg-spawn-pack-v1"
            or not isinstance(doc.get("placements"), dict) or not doc["placements"]):
        raise GameDataError("落点包格式不对：%s" % target, "spawn_pack_shape")
    for scene, row in doc["placements"].items():
        if not isinstance(scene, str) or not isinstance(row, dict):
            raise GameDataError("落点包 scene 行不对：%r" % scene, "spawn_pack_row")
        if row.get("map_id") != fnv32(scene):
            raise GameDataError("落点包 map_id 与场景名不一致：%s" % scene, "spawn_pack_map")
    if path is None:
        _CACHE = doc
    return doc
