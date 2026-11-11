# -*- coding: utf-8 -*-
"""capture-register：把一次 MuMu 录制登记到本机（不 synth、不连服务器）。

识别账号：录制 record.json 里只有 account_binding（域分离 sha256），没有 uuid。
本机 accounts.json 里每个号都能算出同一个 binding；相等的那个就是这个录制的主人。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

from .common import (APK_BUILD, GameDataError, Progress, atomic_write, canonical_json,
                     final, read_json, resolve_data, sha256_bytes, sha256_file, xg_paths)

HOME = 3526133726
REQUIRED = ("record.json", "actor.bin", "scene.bin", "cache.bin", "capture_evidence.json")
BINDING_ALGORITHM = "sha256-domain-sky-cn-421589-level-cache-account-v1"
DEFAULT_REVISION = "V76318"
# 与服务器 runtime/private/connection0167_profiles.local.json 同形：
# 配置在 DATA/runtime/private/ 下，字典在 DATA/runtime/protocol_results/... 下。
DICTIONARY_REL = "../protocol_results/handshake/resources/CompressDictionary.zst"


def _revision(xg) -> str:
    cfg = xg.config() or {}
    cap = cfg.get("capture") if isinstance(cfg.get("capture"), dict) else {}
    for value in ((cap or {}).get("resource_revision"),
                  ((cfg.get("gamedata") or {}) if isinstance(cfg.get("gamedata"), dict) else {}).get("resource_revision")):
        if isinstance(value, str) and re.fullmatch(r"V[0-9]+", value):
            return value
    return DEFAULT_REVISION


def _names_maps(names_dir) -> tuple[dict, dict]:
    """从用户 APK 生成的 names/ 里取“map_id → {scene,name}”和 scene→中文名。"""
    out: dict[str, dict] = {}
    names: dict[str, str] = {}
    doc = read_json(Path(names_dir) / "destination-records-v25.json", None)
    if isinstance(doc, dict):
        for row in doc.get("records") or []:
            if not isinstance(row, dict) or row.get("kind") != "scene":
                continue
            scene = row.get("sceneName")
            map_id = row.get("sceneHash")
            try:
                map_id = int(map_id)
            except (TypeError, ValueError):
                continue
            if not isinstance(scene, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,96}", scene):
                continue
            if not 0 < map_id <= 0xFFFFFFFF:
                continue
            name = row.get("displayName") or scene
            out[str(map_id)] = {"scene": scene, "name": str(name)}
            names[scene] = str(name)
    extra = read_json(Path(names_dir) / "scene_names.json", {}) or {}
    if isinstance(extra, dict):
        for scene, name in extra.items():
            if isinstance(scene, str) and isinstance(name, str) and name:
                names.setdefault(scene, name)
    return out, names


def _scene_name(scene: str, names: dict, fallback: str = "") -> str:
    if isinstance(names, dict) and isinstance(names.get(scene), str) and names[scene]:
        return names[scene]
    return fallback or scene


def _new_profile_doc(record: dict, revision: str, names_dir) -> dict:
    maps, names = _names_maps(names_dir)
    scene = record["scene"]
    maps.setdefault(str(record["level_id"]),
                    {"scene": scene, "name": _scene_name(scene, names)})
    return {
        "apk_build": APK_BUILD,
        "resource_revision": revision,
        "private_root": "current_profiles",
        "dictionary_path": DICTIONARY_REL,
        "home_long_templates": {},
        "maps": maps,
    }


def _ensure_profile_doc(cfg: Path, record: dict, *, revision: str, names_dir) -> tuple[dict, bool]:
    """读登记表；不存在就从 names 的场景表 + 本次录制从零建一份。

    已存在的表只补缺字段/缺 map，不覆盖已有录制与账号记录。
    """
    doc = read_json(cfg, None)
    created = False
    if doc is None:
        if cfg.exists():
            raise GameDataError("本机连接配置读不出来（不是合法 JSON）：%s" % cfg,
                                "profiles_config")
        doc = _new_profile_doc(record, revision, names_dir)
        created = True
    elif not isinstance(doc, dict) or not isinstance(doc.get("maps"), dict):
        raise GameDataError("本机连接配置缺失/损坏：%s（可先备份后删除，让向导重建）" % cfg,
                            "profiles_config")
    else:
        if not isinstance(doc.get("apk_build"), int):
            doc["apk_build"] = APK_BUILD
        if not isinstance(doc.get("resource_revision"), str):
            doc["resource_revision"] = revision
        if not isinstance(doc.get("private_root"), str) or not doc["private_root"]:
            doc["private_root"] = "current_profiles"
        if not isinstance(doc.get("dictionary_path"), str) or not doc["dictionary_path"]:
            doc["dictionary_path"] = DICTIONARY_REL
        if not isinstance(doc.get("home_long_templates"), dict):
            doc["home_long_templates"] = {}
        if any(not isinstance(v, dict) for v in doc["maps"].values()):
            raise GameDataError("本机连接配置的 maps 条目损坏：%s" % cfg, "profiles_config")
        names_maps, names = _names_maps(names_dir)
        for map_id, row in names_maps.items():
            entry = doc["maps"].setdefault(map_id, {})
            entry.setdefault("scene", row["scene"])
            entry.setdefault("name", row["name"])
        entry = doc["maps"].setdefault(str(record["level_id"]), {})
        entry.setdefault("scene", record["scene"])
        entry.setdefault("name", _scene_name(record["scene"], names))
    return doc, created


def _upsert_capture(doc: dict, record: dict, record_dir: str, uuid: str) -> None:
    entry = doc["maps"].setdefault(str(record["level_id"]), {})
    entry.setdefault("scene", record["scene"])
    entry.setdefault("name", record["scene"])
    cap = entry.setdefault("current_capture", {})
    by_account = cap.setdefault("by_account", {})
    if not isinstance(by_account, dict):
        by_account = {}
        cap["by_account"] = by_account
    by_account[uuid] = {"record_dir": record_dir,
                        "netversion_u16": list(record["netversion_u16"])}
    if cap.get("account") in (None, uuid):
        cap["record_dir"] = record_dir
        cap["netversion_u16"] = list(record["netversion_u16"])
        cap["account"] = uuid


def _apk_hint(xg) -> str:
    cfg = xg.config() or {}
    gd = cfg.get("gamedata") if isinstance(cfg.get("gamedata"), dict) else {}
    wizard = cfg.get("wizard") if isinstance(cfg.get("wizard"), dict) else {}
    for value in (gd.get("apk"), (wizard.get("apk") or {}).get("path")):
        if value and Path(str(value)).is_file():
            return str(value)
    try:
        for p in sorted(Path(xg.APK_DIR).glob("*.apk")):
            if p.is_file():
                return str(p)
    except OSError:
        pass
    return ""


def _prepare_env(cfg: Path, runtime_data: Path, revision: str):
    os.environ.setdefault("QFLY_JOIN_MODE", "current")
    os.environ.setdefault("QFLY_JOIN_CONFIG", str(cfg))
    os.environ.setdefault("QFLY_DATA_DIR", str(runtime_data))
    os.environ.setdefault("QFLY_RESOURCE_REVISION", revision)


def _account_binding(uuid: str) -> str:
    from qfsky.join0167_profile import account_binding
    return account_binding(uuid)


def _read_accounts(path: Path) -> list[dict]:
    doc = read_json(path, None)
    if not isinstance(doc, dict) or not isinstance(doc.get("accounts"), list):
        raise GameDataError(
            "没有本机账号表（%s）。先在小光里登录/添加一个游戏号，再登记录制。" % path,
            "accounts_missing")
    rows = []
    for row in doc["accounts"]:
        if isinstance(row, dict) and isinstance(row.get("user_uuid"), str):
            rows.append(row)
    if not rows:
        raise GameDataError("账号表里没有带 user_uuid 的号，先在启动器里登录游戏号。",
                            "accounts_empty")
    return rows


def _validate_record(folder: Path) -> dict:
    for name in REQUIRED:
        if not (folder / name).is_file():
            raise GameDataError("录制目录不完整，缺 %s：%s" % (name, folder), "capture_incomplete")
    record = read_json(folder / "record.json", None)
    if not isinstance(record, dict):
        raise GameDataError("record.json 读不出来：%s" % (folder / "record.json"), "capture_record")
    from qfsky.version_profile import profile_for_build
    profile = profile_for_build(record.get("apk_build"))
    if profile is None:
        raise GameDataError("录制不是支持的 0.16.8/421629（apk_build=%r）。" % record.get("apk_build"),
                            "capture_build")
    if record.get("native_sha256") != profile["native_sha256"]:
        raise GameDataError("录制里的 native_sha256 不是国服 0.16.8/421629。", "capture_native")
    if not isinstance(record.get("level_id"), int) or not 0 < record["level_id"] < 2 ** 32:
        raise GameDataError("录制里的 level_id 不合法。", "capture_level")
    if not isinstance(record.get("scene"), str) or not re.fullmatch(r"[A-Za-z0-9_]{1,96}", record["scene"]):
        raise GameDataError("录制里的 scene 不合法。", "capture_scene")
    nv = record.get("netversion_u16")
    if (not isinstance(nv, list) or len(nv) != 6
            or any(type(v) is not int or not 0 <= v <= 65535 for v in nv)):
        raise GameDataError("录制里的 netversion_u16 不合法。", "capture_netversion")
    files = record.get("files") or {}
    for name in ("actor.bin", "scene.bin", "cache.bin"):
        row = files.get(name) or {}
        raw = (folder / name).read_bytes()
        if row.get("size") != len(raw) or row.get("sha256") != sha256_bytes(raw):
            raise GameDataError("录制文件 %s 与 record.json 里的尺寸/哈希不一致（录制被改过？）。" % name,
                                "capture_file_mismatch")
    binding = (record.get("account_binding") or {}).get("value")
    if not isinstance(binding, str) or not re.fullmatch(r"[0-9a-f]{64}", binding):
        raise GameDataError("录制里没有 account_binding，无法认号。", "capture_binding_missing")
    if (record.get("account_binding") or {}).get("algorithm") not in (None, BINDING_ALGORITHM):
        raise GameDataError("录制的账号绑定算法不是 421589 域约定。", "capture_binding_algorithm")
    return record


def _match_account(record: dict, accounts: list[dict]) -> dict:
    binding = record["account_binding"]["value"]
    hits = []
    for row in accounts:
        try:
            if _account_binding(row["user_uuid"]) == binding:
                hits.append(row)
        except (ValueError, TypeError):
            continue
    if not hits:
        raise GameDataError(
            "这个录制不属于本机账号表里的任何一个号。先在启动器里登录录制时用的游戏号，"
            "或换一份这个号的录制。", "capture_account_unknown")
    if len(hits) > 1:
        raise GameDataError("录制同时匹配到多个账号（账号表重复），先清理重复账号。",
                            "capture_account_ambiguous")
    return hits[0]


def _target_name(source: Path, record: dict) -> str:
    base = source.name if re.fullmatch(r"[A-Za-z0-9_-]+", source.name) else "%s_%s" % (
        record["scene"], time.strftime("%Y%m%d_%H%M%S"))
    return base


def _install_paths(xg) -> None:
    for path in (str(xg.OVERLAY), str(xg.QFSKY_CODE.parent), str(xg.BOT_CODE)):
        while path in sys.path:
            sys.path.remove(path)
        sys.path.insert(0, path)


def _load_capture(profiles_root: Path, name: str, *, uuid: str, record: dict,
                  revision: str):
    from qfsky.map_long0167.prepare import prepare_current_capture
    return prepare_current_capture(
        profiles_root, name,
        expected_account_uuid=uuid,
        expected_scene=record["scene"],
        expected_resource_revision=revision,
        expected_level_id=record["level_id"],
        expected_netversion=tuple(record["netversion_u16"]),
        apk_build=APK_BUILD)


# 测试可替换成“只校验表结构、不真解析录制字节”的加载器；生产始终是上面这个。
LOADER = _load_capture


def register(folder: Path, data: Path, progress: Progress) -> dict:
    xg = xg_paths()
    _install_paths(xg)
    folder = folder.expanduser().resolve()
    if not folder.is_dir():
        raise GameDataError("录制目录不存在：%s" % folder, "capture_missing")
    record = _validate_record(folder)
    accounts = _read_accounts(Path(xg.ACCOUNTS_JSON))
    progress.emit("按录制里的账号绑定认号…", done=1)
    account = _match_account(record, accounts)
    uuid = account["user_uuid"]

    profiles_root = Path(xg.CURRENT_PROFILES)
    cfg = Path(xg.PROFILES_JSON)
    profiles_root.mkdir(parents=True, exist_ok=True)
    revision = _revision(xg)

    progress.emit("复制录制到 current_profiles…", done=2)
    name = _target_name(folder, record)
    target = profiles_root / name
    if target.exists():
        old_record = read_json(target / "record.json", {}) or {}
        if (old_record.get("level_id") != record["level_id"]
                or sha256_file(target / "record.json") != sha256_file(folder / "record.json")):
            name = "%s__%s" % (name, sha256_file(folder / "record.json")[:8])
            target = profiles_root / name
    if not (target.exists() and target.resolve() == folder):
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(folder, target)
    for child in ("record.json", "actor.bin", "scene.bin", "cache.bin"):
        if sha256_file(target / child) != sha256_file(folder / child):
            raise GameDataError("复制后校验失败：%s" % child, "capture_copy_verify")

    before = cfg.read_bytes() if cfg.exists() else None
    try:
        doc, created = _ensure_profile_doc(cfg, record, revision=revision,
                                           names_dir=Path(xg.NAMES))
        revision = doc.get("resource_revision") or revision
        _upsert_capture(doc, record, name, uuid)
        atomic_write(cfg, canonical_json(doc))

        progress.emit("实际加载验证…", done=3)
        _prepare_env(cfg, Path(xg.RUNTIME_DATA), revision)
        from qfsky.map_long0167.prepare import PreparationError
        try:
            prepared = LOADER(profiles_root, name, uuid=uuid, record=record,
                              revision=revision)
        except PreparationError as exc:
            raise GameDataError("录制加载失败（%s）。这份录制不是当前账号/场景/版本的完整现场。"
                                % getattr(exc, "code", str(exc)), "capture_load_failed")
        except GameDataError:
            raise
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise GameDataError("录制加载失败（%s）。这份录制不是当前账号/场景/版本的完整现场。"
                                % type(exc).__name__, "capture_load_failed")
        source_checked = None
        if record["level_id"] == HOME and getattr(LOADER, "__name__", "") == "_load_capture":
            from qfsky.asset_bootstrap_runtime_v32 import _source
            try:
                inputs = _source(uuid)
                source_checked = {"ok": True, "account": uuid,
                                  "scene": getattr(inputs, "scene", record["scene"])}
            except Exception as exc:
                raise GameDataError("遇境录制登记后，运行时仍取不到这个号的源（%s）。"
                                    % str(exc)[:160], "capture_source_failed")
        from . import dictionary
        dict_row = dictionary.ensure_for_data(_apk_hint(xg), data)
        entry_name = str((doc["maps"].get(str(record["level_id"])) or {}).get("name")
                         or record["scene"])
        progress.finish("登记完成：%s" % name)
        return {
            "ok": True,
            "account": account.get("id"),
            "alias": account.get("alias") or account.get("id"),
            "map": entry_name,
            "recorded": {"scenes": [record["scene"]], "names": [entry_name]},
            "uuid": uuid,
            "binding": record["account_binding"]["value"],
            "scene": record["scene"],
            "level_id": record["level_id"],
            "netversion_u16": record["netversion_u16"],
            "record_dir": name,
            "profiles_json": str(cfg),
            "current_profiles": str(profiles_root),
            "profiles_created": created,
            "maps": len(doc.get("maps") or {}),
            "dictionary": dict_row,
            "loaded": True,
            "source_checked": source_checked,
            "prepared_metadata": prepared.metadata(),
        }
    except Exception:
        if before is None:
            try:
                cfg.unlink()
            except OSError:
                pass
        else:
            atomic_write(cfg, before)
        raise


def _registered_dirs(doc: dict) -> set[str]:
    out = set()
    for entry in (doc.get("maps") or {}).values():
        cap = (entry or {}).get("current_capture") or {}
        if isinstance(cap.get("record_dir"), str):
            out.add(cap["record_dir"])
        for row in (cap.get("by_account") or {}).values():
            if isinstance(row, dict) and isinstance(row.get("record_dir"), str):
                out.add(row["record_dir"])
    return out


def register_pending(data: Path, progress: Progress) -> dict:
    """把 current_profiles 里还没写进登记表的录制逐个补登记（W9）。"""
    xg = xg_paths()
    root = Path(xg.CURRENT_PROFILES)
    doc = read_json(Path(xg.PROFILES_JSON), {}) or {}
    known = _registered_dirs(doc if isinstance(doc, dict) else {})
    pending = []
    if root.is_dir():
        for folder in sorted(root.iterdir()):
            if folder.is_dir() and (folder / "record.json").is_file() and folder.name not in known:
                pending.append(folder)
    results = []
    for folder in pending:
        try:
            row = register(folder, data, progress)
            results.append({"dir": folder.name, "ok": True,
                            "record_dir": row.get("record_dir"),
                            "scene": row.get("scene"), "account": row.get("account")})
        except GameDataError as exc:
            results.append({"dir": folder.name, "ok": False,
                            "code": exc.code, "error": exc.message})
        except (OSError, ValueError) as exc:
            results.append({"dir": folder.name, "ok": False,
                            "code": "capture_io", "error": str(exc)})
    ok = all(row.get("ok") for row in results)
    return {"ok": ok, "pending": len(pending), "registered": results}


def cmd_capture_register(args) -> int:
    progress = Progress("capture-register", 4)
    try:
        data = resolve_data(args.data)
        if getattr(args, "pending", False):
            result = register_pending(data, progress)
            final(**result)
            return 0 if result.get("ok") else 1
        if not getattr(args, "dir", None):
            raise GameDataError("要么给 --dir 指定一份录制，要么用 --pending 补登记所有未登记录制。",
                                "capture_missing")
        result = register(Path(args.dir), data, progress)
        final(True, **{k: v for k, v in result.items() if k != "ok"})
        return 0
    except GameDataError as exc:
        final(False, error=exc.message, code=exc.code)
        return 1
    except (OSError, ValueError) as exc:
        final(False, error="登记录制失败：%s" % exc, code="capture_io")
        return 1
