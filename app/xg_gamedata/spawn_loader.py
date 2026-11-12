# -*- coding: utf-8 -*-
"""哥哥标注落点包（spawn_pack*.json）读取与 FNV-1a32。

0.16.7 / 421589 与 0.16.8 / 421629 各有一份落点包：坐标与标注相同，
版本/源证据字段（native_sha256、APK 目录 sha、场景成员 sha、对象索引）不同。
调用方必须按当前版本档案显式选包，不能默认拿旧包去生成新版本目录。
"""
from __future__ import annotations

import json
from pathlib import Path

from qfsky.version_profile import CURRENT_BUILD, LEGACY_BUILD, PROFILES
from .common import GameDataError

PACKS = {
    LEGACY_BUILD: Path(__file__).with_name("spawn_pack.json"),
    CURRENT_BUILD: Path(__file__).with_name("spawn_pack_0168.json"),
}
# 兼容旧引用：PACK 仍指向原 0.16.7 包；按版本请用 pack_path()/load(build=)。
PACK = PACKS[LEGACY_BUILD]
PACK_CURRENT = PACKS[CURRENT_BUILD]
DEFAULT_BUILD = CURRENT_BUILD
FNV_OFFSET = 0x811C9DC5
FNV_PRIME = 0x01000193
_CACHE: dict[object, dict] = {}


def fnv32(text: str) -> int:
    h = FNV_OFFSET
    for b in text.encode("utf-8"):
        h = ((h ^ b) * FNV_PRIME) & 0xFFFFFFFF
    return h


def pack_path(build=None) -> Path:
    target_build = DEFAULT_BUILD if build is None else build
    if type(target_build) is not int or target_build not in PACKS:
        raise GameDataError(
            "没有 %r 版本的落点包；只支持 0.16.7/421589 与 0.16.8/421629。"
            % (build,), "spawn_pack_version")
    return PACKS[target_build]


def load(path: Path | None = None, build: int | None = None) -> dict:
    """读落点包；build 显式给定时按版本选包并核对包内版本字段。

    path 用于测试/排查，按路径单独缓存，不影响按版本缓存。
    """
    target = Path(path) if path is not None else pack_path(build)
    if path is None:
        key = DEFAULT_BUILD if build is None else build
        if key in _CACHE:
            return _CACHE[key]
    else:
        key = ("path", str(target.resolve()))
        if key in _CACHE:
            return _CACHE[key]
    try:
        doc = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GameDataError("落点包读不出来：%s（%s）" % (target, exc), "spawn_pack")
    if (not isinstance(doc, dict) or doc.get("schema") != "xg-spawn-pack-v1"
            or not isinstance(doc.get("placements"), dict) or not doc["placements"]):
        raise GameDataError("落点包格式不对：%s" % target, "spawn_pack_shape")
    declared = doc.get("apk_build")
    if declared is not None and type(declared) is not int:
        raise GameDataError("落点包的 apk_build 字段形状不对：%s" % target, "spawn_pack_version")
    if build is not None:
        if declared is None:
            # 原 0.16.7 spawn_pack 没有顶层版本字段；只允许它继续服务旧档案。
            if build != LEGACY_BUILD:
                raise GameDataError(
                    "落点包没有版本字段，不能用于按版本选包：%s" % target,
                    "spawn_pack_version")
        elif declared != build:
            raise GameDataError(
                "落点包版本 %r 与当前版本档案 %r 不一致：%s" % (declared, build, target),
                "spawn_pack_version")
    for scene, row in doc["placements"].items():
        if not isinstance(scene, str) or not isinstance(row, dict):
            raise GameDataError("落点包 scene 行不对：%r" % scene, "spawn_pack_row")
        if row.get("map_id") != fnv32(scene):
            raise GameDataError("落点包 map_id 与场景名不一致：%s" % scene, "spawn_pack_map")
    for scene, rows in (doc.get("variants") or {}).items():
        if not isinstance(scene, str) or scene not in doc["placements"] or not isinstance(rows, dict):
            raise GameDataError("落点包 variants 行不对：%r" % scene, "spawn_pack_variant")
        for name, row in rows.items():
            if (not isinstance(name, str) or not isinstance(row, dict)
                    or row.get("scene") != scene
                    or row.get("map_id") != doc["placements"][scene]["map_id"]):
                raise GameDataError("落点包变体不对：%s/%s" % (scene, name), "spawn_pack_variant")
    _CACHE[key] = doc
    return doc


def require_apk_evidence(pack: dict, *, native_sha256: str, apk_archive_directory_sha256: str,
                         build: int) -> None:
    """核对落点包声明的版本证据与本次 APK 实算值；不一致时中文 fail-closed。"""
    if type(build) is not int or build not in PACKS:
        raise GameDataError("不支持的目录版本：%r" % (build,), "spawn_pack_version")
    declared_build = pack.get("apk_build")
    declared_native = pack.get("native_sha256")
    declared_directory = pack.get("apk_archive_directory_sha256")
    label = "%s / %s" % (PROFILES[build]["version"], build)
    if build == CURRENT_BUILD and (declared_build != build or declared_native is None
                                   or declared_directory is None):
        raise GameDataError(
            "0.16.8 落点包缺少版本证据（apk_build/native_sha256/APK 目录 sha）："
            "不能用来生成当前版本目录。", "spawn_pack_version")
    if declared_build is not None and declared_build != build:
        raise GameDataError(
            "落点包版本 %r 与当前版本档案 %r 不一致：" % (declared_build, build),
            "spawn_pack_version")
    if declared_native is not None and declared_native != native_sha256:
        raise GameDataError(
            "落点包的 native 证据与本次 APK 不一致（包=%s…，APK=%s…）。"
            "这个安装包不是 %s 档案，请重新拉取完整 base.apk。"
            % (str(declared_native)[:12], str(native_sha256)[:12], label),
            "spawn_pack_apk_mismatch")
    if declared_directory is not None and declared_directory != apk_archive_directory_sha256:
        raise GameDataError(
            "落点包的 APK 目录证据与本次 APK 不一致（包=%s…，APK=%s…）。"
            "这个安装包不是 %s 档案，请重新拉取完整 base.apk。"
            % (str(declared_directory)[:12], str(apk_archive_directory_sha256)[:12], label),
            "spawn_pack_apk_mismatch")
