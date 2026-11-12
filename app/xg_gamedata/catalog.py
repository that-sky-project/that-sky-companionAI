# -*- coding: utf-8 -*-
"""catalog：用这个号自己的遇境真录当目录源，生成 115 张按号目录。

链路（对齐 T9 rebuild_all）：
  APK 场景输入（scene/cache 由 scene_inputs_build 现场模拟 APK 里的 native
  构造器/序列化函数生成，缓存缺失时自动生成；见 scene_inputs_build.py）
  + 哥哥标注落点包（spawn_pack，允许随包）
  + 本号遇境录制的 actor
  → DATA/runtime/.../asset-catalog-acct/<账号ID>/
  → 写 catalog_sources.json 与按号 pin 注册表
  → 逐张（含变体）load_catalog_trial 自检；失败整体回滚。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import struct
import sys
import time
from pathlib import Path

from . import actor, spawn_loader
from .common import (APK_BUILD, GameDataError, Progress, atomic_write, canonical_json,
                     final, game_dir, long_path, read_json, resolve_data, rmtree, sha256_bytes,
                     sha256_file, xg_paths)
from .apk import NATIVE_SHA256

HOME = 3526133726
HOME_SCENE = "CandleSpace"
ACCOUNT_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
SCENE = re.compile(r"[A-Za-z0-9_]{1,96}")
CACHE_HEADER = bytes.fromhex("c063000000000000000005007d00")
CACHE_SIZE = 8208
NATIVE_RECORD_SIZE = 25536


def _install_paths(xg):
    paths = [str(xg.OVERLAY), str(xg.QFSKY_CODE.parent), str(xg.BOT_CODE)]
    for path in paths:
        while path in sys.path:
            sys.path.remove(path)
    sys.path[:0] = paths


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _bind(data: Path):
    xg = xg_paths()
    _install_paths(xg)
    return xg


def _account(xg, account_id: str) -> dict:
    if ACCOUNT_ID.fullmatch(account_id) is None:
        raise GameDataError("账号 ID 形状不对：%r" % account_id, "account_id_shape")
    doc = read_json(Path(xg.ACCOUNTS_JSON), {}) or {}
    for row in doc.get("accounts") or []:
        if str(row.get("id")) == account_id:
            uuid = row.get("user_uuid")
            if not isinstance(uuid, str):
                raise GameDataError("账号 %s 没有 user_uuid" % account_id, "account_uuid")
            from qfsky.join0167_profile import account_binding
            return {"id": account_id, "uuid": uuid, "binding": account_binding(uuid),
                    "alias": row.get("alias") or ""}
    raise GameDataError("账号表里没有 %s；先在启动器里登录这个号。" % account_id,
                        "account_not_found")


def _donor(xg, profiles: dict, uuid: str) -> dict:
    """找这个号的遇境真录（优先 CandleSpace），返回目录/record/actor。

    catalog_sources 可能还是旧版本登记的 36851 条目；当前 APK 版本对不上时
    不拿它当源，回退到 profiles 里本号的遇境录制。
    """
    sources = read_json(Path(xg.RUNTIME_PRIVATE) / "catalog_sources.json", {}) or {}
    rows = []
    for map_id, entry in (profiles.get("maps") or {}).items():
        cap = (entry or {}).get("current_capture") or {}
        own = (cap.get("by_account") or {}).get(uuid)
        rows.append((map_id, entry, cap, own))
    source_row = sources.get(uuid)
    if isinstance(source_row, dict) and source_row.get("record_dir"):
        name = source_row["record_dir"]
        try:
            return _read_donor(xg, name, expected_uuid=uuid, prefer_home=True)
        except GameDataError as exc:
            if exc.code not in ("donor_version", "donor_not_home", "donor_missing"):
                raise
    home_rows = [r for r in rows if str(r[0]) == str(HOME)]
    for map_id, entry, cap, own in home_rows:
        if isinstance(own, dict) and own.get("record_dir"):
            return _read_donor(xg, own["record_dir"], expected_uuid=uuid, prefer_home=True)
        if cap.get("account") == uuid and cap.get("record_dir"):
            return _read_donor(xg, cap["record_dir"], expected_uuid=uuid, prefer_home=True)
    # 没有遇境就明确让用户录一次
    raise GameDataError(
        "这个号还没有当前版本（0.16.8）的遇境（CandleSpace）真录。先用 MuMu 录一次遇境，"
        "再跑 capture-register 登记。", "home_capture_missing")


def _read_donor(xg, name: str, expected_uuid: str, prefer_home: bool = True) -> dict:
    if not isinstance(name, str) or re.fullmatch(r"[A-Za-z0-9_-]+", name) is None:
        raise GameDataError("录制目录名不合法：%r" % name, "donor_name")
    folder = Path(xg.CURRENT_PROFILES) / name
    record = read_json(folder / "record.json", None)
    if not isinstance(record, dict):
        raise GameDataError("源录制不存在：%s" % folder, "donor_missing")
    from qfsky.version_profile import profile_for_build
    profile = profile_for_build(record.get("apk_build"))
    if (profile is None or profile.get("legacy")
            or record.get("apk_build") != APK_BUILD
            or record.get("native_sha256") != profile.get("native_sha256")):
        raise GameDataError(
            "目录源不是当前 0.16.8/421629 的录制（apk_build=%r）。"
            % record.get("apk_build"), "donor_version")
    if record.get("resource_revision") != profile.get("resource_revision"):
        raise GameDataError(
            "目录源的 resource_revision 与当前版本档案不一致：%r。"
            % record.get("resource_revision"), "donor_version")
    if record.get("level_id") != HOME or record.get("scene") != HOME_SCENE:
        raise GameDataError(
            "目录源必须是这个号的遇境真录（CandleSpace）；当前源是 %s/%s。"
            % (record.get("scene"), record.get("level_id")), "donor_not_home")
    nv = record.get("netversion_u16")
    if (not isinstance(nv, list) or len(nv) != 6
            or any(type(v) is not int or not 0 <= v <= 65535 for v in nv)):
        raise GameDataError("目录源的 netversion_u16 不合法。", "donor_version")
    if nv[0] != profile.get("home_resource_tag"):
        raise GameDataError(
            "目录源的遇境 resource tag=%r 与当前版本档案 %r 不一致。"
            % (nv[0], profile.get("home_resource_tag")), "donor_version")
    from qfsky.join0167_profile import account_binding
    if (record.get("account_binding") or {}).get("value") != account_binding(expected_uuid):
        raise GameDataError("源录制不是这个号的（账号绑定不一致）。", "donor_account")
    actor_raw = (folder / "actor.bin").read_bytes()
    actor.inspect_bytes(actor_raw, NATIVE_SHA256)
    return {"record_dir": name, "folder": folder, "record": record,
            "record_sha256": sha256_bytes((folder / "record.json").read_bytes()),
            "actor": actor_raw, "actor_sha256": sha256_bytes(actor_raw),
            "netversion_u16": list(nv)}


def _scene_inputs(cache_dir: Path, pack: dict, apk, progress: Progress | None = None) -> dict:
    if not cache_dir.is_dir():
        raise GameDataError(
            "缺少场景输入目录：%s\nscene/cache 需要从 APK 现场生成"
            "（unicorn 2.1.4 + capstone >=5.0）；catalog 会自动生成，"
            "如果这里报错说明生成步骤失败，按上面的中文错误处理。" % cache_dir,
            "scene_inputs_missing")
    import zipfile
    out = {}
    scenes = sorted(pack["placements"])
    zf = zipfile.ZipFile(apk)
    try:
        for idx, scene in enumerate(scenes):
            proof_path = cache_dir / (scene + ".json")
            wire_path = cache_dir / (scene + ".scene.bin")
            cache_path = cache_dir / (scene + ".cache.bin")
            native_path = cache_dir / (scene + ".native.bin")
            if not (proof_path.is_file() and wire_path.is_file() and cache_path.is_file()
                    and native_path.is_file()):
                raise GameDataError("场景输入缺 %s 的四件套。" % scene, "scene_inputs_incomplete")
            proof = json.loads(proof_path.read_text(encoding="utf-8"))
            if (proof.get("schema") != "sky-catalog-partial-scene-inputs-v32"
                    or proof.get("scene") != scene
                    or proof.get("map_id") != spawn_loader.fnv32(scene)
                    or proof.get("native_sha256") != NATIVE_SHA256
                    or proof.get("canonical_roundtrip") is not True):
                raise GameDataError("场景输入证明不对：%s" % scene, "scene_inputs_proof")
            nv = proof.get("netversion_u16")
            if (not isinstance(nv, list) or len(nv) != 6
                    or any(type(v) is not int or not 0 <= v <= 65535 for v in nv)
                    or nv[2:] != [23536, 0, 0, NATIVE_RECORD_SIZE]):
                raise GameDataError("场景输入 netversion 不对：%s" % scene, "scene_inputs_netversion")
            if scene == HOME_SCENE:
                from qfsky.version_profile import profile_for_native_sha256
                home = profile_for_native_sha256(NATIVE_SHA256)
                if home is None or nv[0] != home["home_resource_tag"]:
                    raise GameDataError(
                        "遇境场景输入的 resource tag=%r 与 0.16.8 档案 %r 不一致。"
                        % (nv[0], home and home.get("home_resource_tag")),
                        "scene_inputs_netversion")
            wire = wire_path.read_bytes()
            cache = cache_path.read_bytes()
            native = native_path.read_bytes()
            member = "assets/Data/Levels/%s/Objects.level.bin" % scene
            try:
                member_raw = zf.read(member)
            except KeyError:
                raise GameDataError("APK 里没有场景 %s 的 Objects.level.bin" % scene, "scene_member")
            if _sha(member_raw) != (proof.get("provenance") or {}).get("scene_member_sha256"):
                raise GameDataError("场景输入缓存与 APK 成员不一致：%s" % scene, "scene_member_sha")
            for kind, value in (("native", native), ("cache", cache), ("wire", wire)):
                item = (proof.get("artifacts") or {}).get(kind) or {}
                if _sha(value) != item.get("sha256") or len(value) != item.get("bytes"):
                    raise GameDataError("场景输入 %s 的 %s 与证明不一致" % (scene, kind),
                                        "scene_artifact_sha")
            rebuilt = _cache_from_wire(wire)
            if rebuilt != cache:
                raise GameDataError("cache.bin 不是由 scene.bin 规范生成的：%s" % scene,
                                    "scene_cache_shape")
            out[scene] = {"proof": proof, "wire": wire, "cache": rebuilt}
            if progress:
                progress.emit("校验场景 %s" % scene, done=idx + 1, total=len(scenes))
    finally:
        zf.close()
    return out


def _cache_from_wire(wire: bytes) -> bytes:
    if not 0 < len(wire) < 0x2000:
        raise GameDataError("scene.bin 长度不合法：%d" % len(wire), "scene_wire_extent")
    cache = bytearray(CACHE_SIZE)
    cache[:14] = CACHE_HEADER
    cache[14:14 + len(wire)] = wire
    struct.pack_into("<H", cache, 0x200E, len(wire))
    return bytes(cache)


def _placement(pack: dict, scene: str, map_id: int, variant: str | None = None) -> tuple[dict, bytes]:
    if variant is None:
        row = (pack.get("placements") or {}).get(scene)
    else:
        row = ((pack.get("variants") or {}).get(scene) or {}).get(variant)
    if not isinstance(row, dict):
        raise GameDataError("落点包里没有 %s%s" % (scene, ("/" + variant) if variant else ""),
                            "placement_missing")
    row = json.loads(json.dumps(row))
    if row.get("scene") != scene or row.get("map_id") != map_id:
        raise GameDataError("落点包的 scene/map_id 对不上：%s" % scene, "placement_scope")
    pos = row.get("wire_position")
    if not (isinstance(pos, list) and len(pos) == 3
            and all(isinstance(v, (int, float)) for v in pos)):
        raise GameDataError("落点包的 wire_position 不合法：%s" % scene, "placement_position")
    return row, canonical_json(row)


def _placement_evidence(proof: dict, placement: dict, profile: dict) -> None:
    """落点包的版本/源证据必须与本机 APK、场景证明三方一致；不一致就报中文错。

    旧实现会把落点包的 source_member_sha256 反写进 scene-provenance，等于让
    0.16.7 的落点包静默覆盖本次 0.16.8 APK 的现场证据；这里改成 fail-closed。
    """
    pprov = placement.get("provenance") or {}
    prov = proof.get("provenance") or {}
    scene = proof.get("scene")
    build = profile["build"]
    label = "%s/%s" % (profile["version"], build)
    expected_member = "assets/Data/Levels/%s/Objects.level.bin" % scene
    declared_build = pprov.get("apk_build")
    if declared_build is not None and declared_build != build:
        raise GameDataError(
            "落点包 %s 的 placement 版本是 %r，不是当前 %s。" % (scene, declared_build, label),
            "placement_version")
    if (pprov.get("native_sha256") != profile["native_sha256"]
            or pprov.get("apk_archive_directory_sha256") != profile["apk_directory_sha256"]):
        raise GameDataError(
            "落点包 %s 的 placement 源证据不是当前 %s（native=%s…，APK 目录=%s…）。"
            "这份落点包与本次 APK 版本不一致，不能生成目录。"
            % (scene, label, str(pprov.get("native_sha256"))[:12],
               str(pprov.get("apk_archive_directory_sha256"))[:12]),
            "placement_version")
    if pprov.get("source_member") != expected_member:
        raise GameDataError(
            "落点包 %s 的 source_member 不是本场景成员：%r。"
            % (scene, pprov.get("source_member")), "placement_source_member")
    actual = prov.get("scene_member_sha256")
    if actual != pprov.get("source_member_sha256"):
        raise GameDataError(
            "落点包 %s 的场景成员 SHA 与本机 APK 不一致（落点包=%s…，APK=%s…）。"
            "这个 APK 不是 %s 档案，或场景文件被改过；请用完整国服安装包重新拉取。"
            % (scene, str(pprov.get("source_member_sha256"))[:12], str(actual)[:12], label),
            "placement_apk_mismatch")


def _source_meta(donor: dict, actor_counts: dict) -> tuple[dict, bytes]:
    meta = {
        "account_ids_exported": False,
        "actorSHA256": donor["actor_sha256"],
        "bytes": len(donor["actor"]),
        "canonical_roundtrip": True,
        "map_id": HOME,
        "nativeSHA256": NATIVE_SHA256,
        "same_active_bot_account_verified_on_server": True,
        "scene": HOME_SCENE,
        "source_kind": "existing_native_capture_not_live_current",
        "variable_group_counts": dict(actor_counts),
    }
    raw = canonical_json(meta)
    return meta, raw


def _install_verify_env(xg, manifest_path: Path, revision: str):
    saved = {k: os.environ.get(k) for k in (
        "QFSKY_ASSET_CATALOG_TRIAL", "QFSKY_ASSET_CATALOG_MANIFEST",
        "QFSKY_ASSET_CATALOG_MANIFEST_SHA256", "QFLY_JOIN_CONFIG",
        "QFLY_RESOURCE_REVISION", "QFLY_DATA_DIR", "QFLY_JOIN_MODE")}
    os.environ.update({
        "QFSKY_ASSET_CATALOG_TRIAL": "1",
        "QFSKY_ASSET_CATALOG_MANIFEST": str(manifest_path.resolve()),
        "QFSKY_ASSET_CATALOG_MANIFEST_SHA256": sha256_file(manifest_path),
        "QFLY_JOIN_CONFIG": str(Path(xg.PROFILES_JSON)),
        "QFLY_RESOURCE_REVISION": revision,
        "QFLY_DATA_DIR": str(Path(xg.RUNTIME_DATA)),
        "QFLY_JOIN_MODE": "current",
    })
    return saved


def _verify_all(xg, uuid: str, binding: str, manifest: dict, manifest_path: Path,
                revision: str, progress: Progress) -> dict:
    from qfsky.asset_catalog_runtime_v32c import load_catalog_trial
    saved = _install_verify_env(xg, manifest_path, revision)
    checked = 0
    variants = 0
    failures = []
    try:
        for map_id in sorted(manifest["entries"], key=lambda x: int(x)):
            entry = manifest["entries"][map_id]
            try:
                result = load_catalog_trial(uuid, int(map_id))
                inputs, profile, row = result
                if profile.binding != binding:
                    raise ValueError("profile_binding")
                if profile.record["actor_sha256"] != entry["actor_sha256"]:
                    raise ValueError("profile_actor_pin")
                if _sha(profile.cache) != entry["cache_sha256"]:
                    raise ValueError("profile_cache_pin")
                if _sha(profile.blob) != entry["scene_sha256"]:
                    raise ValueError("profile_scene_pin")
                checked += 1
            except Exception as exc:
                failures.append({"map_id": map_id, "variant": None, "error": str(exc)[:200]})
                continue
            for name in sorted((manifest.get("variants") or {}).get(map_id) or {}):
                try:
                    inputs, profile, row = load_catalog_trial(uuid, int(map_id), variant=name)
                    if profile.binding != binding:
                        raise ValueError("profile_binding")
                    if profile.record["actor_sha256"] != manifest["variants"][map_id][name]["actor_sha256"]:
                        raise ValueError("profile_variant_actor_pin")
                    variants += 1
                except Exception as exc:
                    failures.append({"map_id": map_id, "variant": name, "error": str(exc)[:200]})
            if progress and checked % 10 == 0:
                progress.emit("自检 %d/%d" % (checked, len(manifest["entries"])),
                              done=checked, total=len(manifest["entries"]))
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    if failures:
        raise GameDataError("按号目录自检失败 %d 张（已回滚）：%s"
                            % (len(failures), json.dumps(failures[:6], ensure_ascii=False)),
                            "catalog_self_check")
    return {"maps": checked, "variants": variants}


def build(apk: Path, account_id: str, data: Path, scene_inputs: str | None,
          donor_name: str | None, progress: Progress) -> dict:
    xg = _bind(data)
    account = _account(xg, account_id)
    uuid, binding = account["uuid"], account["binding"]
    from .apk import inspect_apk
    apk_info = inspect_apk(apk, progress=None)
    profiles = read_json(Path(xg.PROFILES_JSON), {}) or {}
    if not isinstance(profiles.get("maps"), dict):
        raise GameDataError("本机连接配置缺失/损坏：%s" % xg.PROFILES_JSON, "profiles_config")
    from qfsky.version_profile import profile_for_build
    profile = profile_for_build(profiles.get("apk_build"))
    if (profile is None or profile.get("legacy")
            or profiles.get("apk_build") != APK_BUILD):
        raise GameDataError(
            "本机连接配置还是旧版本（apk_build=%r）；先在向导里重新登记 0.16.8 遇境录制。"
            % profiles.get("apk_build"), "profiles_stale")
    if profiles.get("resource_revision") != profile["resource_revision"]:
        raise GameDataError(
            "本机连接配置的 resource_revision=%r 与 0.16.8 档案不一致。"
            % profiles.get("resource_revision"), "profiles_stale")
    revision = profile["resource_revision"]
    progress.emit("找这个号的遇境真录…", done=1)
    if donor_name:
        donor = _read_donor(xg, donor_name, expected_uuid=uuid)
    else:
        donor = _donor(xg, profiles, uuid)
    pack = spawn_loader.load(build=profile["build"])
    spawn_loader.require_apk_evidence(
        pack, native_sha256=apk_info["native_sha256"],
        apk_archive_directory_sha256=apk_info["apk_archive_directory_sha256"],
        build=profile["build"])
    cache_dir = Path(scene_inputs) if scene_inputs else game_dir(data) / "catalog-inputs"
    progress.emit("准备场景输入（%d 张，缺的从 APK 现场生成）…" % len(pack["placements"]),
                  done=0, total=len(pack["placements"]) + 10)
    from .scene_inputs_build import ensure_scene_inputs
    built = ensure_scene_inputs(apk, cache_dir, progress=None)
    scenes = _scene_inputs(cache_dir, pack, apk, progress)
    if len(scenes) != len(pack["placements"]):
        raise GameDataError("场景输入与落点包数量不一致", "scene_inputs_count")

    release = Path(xg.ASSET_RELEASE)
    acct_root = release / "asset-catalog-acct"
    os.makedirs(long_path(acct_root), exist_ok=True)
    # W5：临时/staging 名尽量短（原来是 .<账号>.tmp-<pid>-<纳秒>，在 MAX_PATH
    # 附近会把 scene-provenance.json.pending 顶到 260 字符）。
    token = os.urandom(4).hex()
    staging = acct_root / (".t" + token)
    final_dir = acct_root / account_id
    backup = acct_root / (".b" + token)
    sources_path = Path(xg.RUNTIME_PRIVATE) / "catalog_sources.json"
    from qfsky.map_long0167 import catalog_account_pins as pins_mod
    registry_path = pins_mod.registry_path()
    before = {
        "sources": sources_path.read_bytes() if sources_path.is_file() else None,
        "registry": registry_path.read_bytes() if registry_path.is_file() else None,
    }
    moved_old = False
    published = False
    try:
        source_meta, source_meta_raw = _source_meta(donor, _source_actor_counts(donor))
        entries = {}
        variants = {}
        os.makedirs(long_path(staging), exist_ok=False)
        total = len(scenes)
        for idx, scene in enumerate(sorted(scenes)):
            item = scenes[scene]
            proof = item["proof"]
            map_id = int(proof["map_id"])
            placement, placement_raw = _placement(pack, scene, map_id)
            # 落点包必须与本次 APK 的版本证据/场景成员一一对上；不再把落点包的
            # 旧 source_member_sha256 反写进 scene-provenance。
            _placement_evidence(proof, placement, profile)
            wire = item["wire"]
            cache = item["cache"]
            # 本号录过这张图：把 scene 的 switches 段起换成自己的录制，补 MPVar。
            if scene == donor["record"].get("scene"):
                cap_wire = (donor["folder"] / "scene.bin").read_bytes()
                if cap_wire != wire:
                    from .scene_splice import splice, apply_to_proof
                    wire, obs = splice(wire, cap_wire)
                    cache = _cache_from_wire(wire)
                    proof, _raw, cache = apply_to_proof(
                        proof, wire, cache, obs, donor["record_dir"], _sha(cap_wire))
            position = placement["wire_position"]
            tag = int(proof["netversion_u16"][0])
            new_actor, _state = actor.project(donor["actor"], map_id, position, tag, NATIVE_SHA256)
            folder = staging / scene
            os.makedirs(long_path(folder), exist_ok=True)
            atomic_write(folder / "scene.bin", wire)
            atomic_write(folder / "cache.bin", cache)
            atomic_write(folder / "actor.bin", new_actor)
            atomic_write(folder / "placement.json", placement_raw)
            prov_bytes = canonical_json(proof)
            atomic_write(folder / "scene-provenance.json", prov_bytes)
            entries[str(map_id)] = {
                "actor_sha256": _sha(new_actor),
                "cache_sha256": _sha(cache),
                "label": proof["label"],
                "level_id": map_id,
                "netversion_u16": proof["netversion_u16"],
                "placement_sha256": _sha(placement_raw),
                "resource_revision": proof["resource_revision"],
                "scene": scene,
                "scene_provenance_sha256": _sha(prov_bytes),
                "scene_sha256": _sha(wire),
            }
            for name in sorted((pack.get("variants") or {}).get(scene) or {}):
                vplacement, vplacement_raw = _placement(pack, scene, map_id, name)
                _placement_evidence(proof, vplacement, profile)
                vtag = tag
                vactor, _ = actor.project(donor["actor"], map_id, vplacement["wire_position"],
                                          vtag, NATIVE_SHA256)
                vdir = folder / "variants" / name
                os.makedirs(long_path(vdir), exist_ok=True)
                atomic_write(vdir / "actor.bin", vactor)
                atomic_write(vdir / "placement.json", vplacement_raw)
                annotation = (vplacement.get("provenance") or {}).get("annotation") or {}
                variants.setdefault(str(map_id), {})[name] = {
                    "scene": scene,
                    "label": annotation.get("note") or annotation.get("name_zh") or name,
                    "actor_sha256": _sha(vactor),
                    "placement_sha256": _sha(vplacement_raw),
                    "annotation_sha256": _sha(canonical_json(annotation)),
                }
            progress.emit("生成 %s" % scene, done=idx + 1, total=total + 4)
        manifest = {
            "apk_build": APK_BUILD,
            "capture_generated": False,
            "classification": "asset-bootstrap-experimental",
            "complete_scene_initialization": False,
            "deadline_utc": "2027-06-30T00:00:00+00:00",
            "enabled": True,
            "full_connection_ready": False,
            "kind": "asset-bootstrap-experimental",
            "loaded_scene_snapshot": False,
            "native_sha256": NATIVE_SHA256,
            "schema": "sky-asset-catalog-trial-v32c",
            "source_account_binding_hash": binding,
            "source_actor_sha256": donor["actor_sha256"],
            "source_capture_record_sha256": donor["record_sha256"],
            "source_metadata_sha256": _sha(source_meta_raw),
            "trial_validated": False,
            "entries": entries,
            "variants": variants,
        }
        manifest_raw = canonical_json(manifest)
        atomic_write(staging / "source-metadata.json", source_meta_raw)
        atomic_write(staging / "manifest.json", manifest_raw)
        # 落点包里的 variant 名称与 scene 来源
        sources_doc = read_json(sources_path, {}) or {}
        sources_doc[uuid] = {"record_dir": donor["record_dir"],
                             "netversion_u16": donor["netversion_u16"]}
        registry_doc = pins_mod.load_registry() or {}
        progress.emit("发布按号目录…", done=total + 1, total=total + 4)
        if os.path.exists(long_path(final_dir)):
            os.rename(long_path(final_dir), long_path(backup))
            moved_old = True
        os.rename(long_path(staging), long_path(final_dir))
        published = True
        atomic_write(sources_path, canonical_json(sources_doc))
        registry_doc[binding] = {
            "source_actor_sha256": donor["actor_sha256"],
            "source_metadata_sha256": _sha(source_meta_raw),
            "entries_sha256": _sha(canonical_json(entries)),
            "variants": variants,
        }
        pins_mod.validate_registry(registry_doc)
        atomic_write(registry_path, canonical_json(registry_doc))
        progress.emit("逐张 load_catalog_trial 自检…", done=total + 2, total=total + 4)
        checks = _verify_all(xg, uuid, binding, manifest, final_dir / "manifest.json",
                             revision, progress)
        if moved_old and os.path.exists(long_path(backup)):
            rmtree(backup)
        progress.finish("按号目录完成：%d 张 + %d 变体" % (checks["maps"], checks["variants"]))
        return {
            "ok": True, "account": account_id, "uuid": uuid, "binding": binding,
            "source_record_dir": donor["record_dir"],
            "source_actor_sha256": donor["actor_sha256"],
            "source_metadata_sha256": _sha(source_meta_raw),
            "source_capture_record_sha256": donor["record_sha256"],
            "catalog_dir": str(final_dir), "manifest": str(final_dir / "manifest.json"),
            "manifest_sha256": _sha(manifest_raw),
            "entries_sha256": _sha(canonical_json(entries)),
            "maps": checks["maps"], "variants": checks["variants"],
            "apk_sha256": apk_info["sha256"], "scene_inputs": str(cache_dir),
            "scene_inputs_generated": built.get("generated"),
            "scene_inputs_reused": built.get("reused"),
            "scene_inputs_seconds": built.get("total_seconds"),
            "scene_inputs_peak_memory_mb": built.get("peak_memory_mb"),
            "registry": str(registry_path), "catalog_sources": str(sources_path),
            "seconds": None,
        }
    except Exception:
        try:
            if published and os.path.exists(long_path(final_dir)):
                rmtree(final_dir)
            if moved_old and os.path.exists(long_path(backup)):
                os.rename(long_path(backup), long_path(final_dir))
        except OSError:
            pass
        try:
            if os.path.exists(long_path(staging)):
                rmtree(staging)
        except OSError:
            pass
        for key, path in (("sources", sources_path), ("registry", registry_path)):
            data = before[key]
            try:
                if data is None:
                    if path.exists():
                        path.unlink()
                else:
                    atomic_write(path, data)
            except OSError:
                pass
        raise


def _source_actor_counts(donor: dict) -> dict:
    aw = actor._codec()
    obs = aw.inspect_actor_wire(donor["actor"], verified_profile_sha256=NATIVE_SHA256,
                                check_zero_padding=True, require_canonical_writer=True)
    return dict(obs._counts)


def cmd_catalog(args) -> int:
    started = time.monotonic()
    progress = Progress("catalog", 120)
    try:
        data = resolve_data(args.data)
        result = build(Path(args.apk).expanduser().resolve(), args.account, data,
                       args.scene_inputs, args.donor, progress)
        result["seconds"] = round(time.monotonic() - started, 1)
        final(True, **{k: v for k, v in result.items() if k != "ok"})
        return 0
    except GameDataError as exc:
        final(False, error=exc.message, code=exc.code)
        return 1
    except (OSError, ValueError, KeyError) as exc:
        final(False, error="生成按号目录失败：%s" % exc, code="catalog_error")
        return 1
