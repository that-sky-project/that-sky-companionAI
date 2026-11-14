# -*- coding: utf-8 -*-
"""status：看本机已经生成了哪些游戏数据。"""
from __future__ import annotations

from pathlib import Path

from . import spawn_loader

from .common import (GAME_VERSION, GameDataError, Progress, final, game_dir,
                     read_json, resolve_data, sha256_file, xg_paths)


def props_status(root: Path, profile=None) -> dict:
    """Use the runtime's full generated-catalog validator, never a saved checkbox."""
    import sys
    from .common import APP_ROOT
    root = Path(root)
    result = {"ready": False, "state": "missing", "code": "props_missing", "count": 0,
              "known": 0, "message": "道具形状尚未生成，请单独补生成；当前暂停道具地形与坐座位，行走可用。"}
    if not (root / "generation.json").is_file() or not (root / "catalog.json").is_file():
        return result
    try:
        source = str(APP_ROOT / "src")
        if source not in sys.path:
            sys.path.insert(0, source)
        from skyterrain.props import load_generated_catalog
        load_generated_catalog(root, expected_build=(profile or {}).get("build"))
        proof = read_json(root / "generation.json", {}) or {}
        counts = proof.get("counts") or {}
        ready = counts.get("ready", proof.get("ready", 0))
        known = counts.get("known", proof.get("known", ready))
        if type(ready) is not int or ready <= 0 or type(known) is not int or known < ready:
            raise ValueError("props_counts_invalid")
        result.update(ready=True, state="ready", code="", count=ready, known=known,
                      message="道具形状已生成：%d 种；运行时按实际校验结果使用。" % ready)
        return result
    except Exception:
        result.update(state="invalid", code="props_invalid",
                      message="道具形状缺失或校验未通过，请单独补生成；当前暂停道具地形与坐座位，行走可用。")
        return result


def collect(data: Path, account=None, account_uuid=None, profile=None) -> dict:
    from qfsky.version_profile import CURRENT_BUILD, profile_for_build
    from .catalog_state import certify
    profile = profile or profile_for_build(CURRENT_BUILD)
    if account is None:
        try:
            account = (Path(data) / "control/active_account").read_text(encoding="utf-8").strip()
        except OSError:
            account = ""
    if account_uuid is None and account:
        accounts = read_json(Path(data) / "runtime/runtime-data/accounts.json", {}) or {}
        rows = accounts.get("accounts", []) if isinstance(accounts, dict) else []
        account_uuid = next((row.get("user_uuid") for row in rows if isinstance(row, dict)
                             and str(row.get("id")) == str(account)), None)
    game = game_dir(data)
    props = props_status(game / "props", profile)
    names_dir = game / "names"
    terrain_dir = game / "terrain"
    apk_dir = Path(data) / "game" / "apk"
    runtime = Path(data) / "runtime"
    private = runtime / "private"
    profiles = read_json(private / "connection0167_profiles.local.json", {}) or {}
    sources = read_json(private / "catalog_sources.json", {}) or {}
    account_dir = runtime / "terrain-experimental-20261104" / "release-v20-hop-trial" / "asset-catalog-acct"

    apk = None
    for name in ("merged.apk", "base.apk"):
        cand = apk_dir / name
        if cand.is_file():
            apk = cand
            break
    apk_row = None
    if apk is not None:
        apk_row = {"path": str(apk), "bytes": apk.stat().st_size}

    name_rows = {}
    for name in ("emotes.json", "outfit_names_zh.json", "gift_names.json",
                 "catalog.json", "destination-records-v25.json"):
        path = names_dir / name
        if path.is_file():
            doc = read_json(path, None)
            name_rows[name] = {"path": str(path), "bytes": path.stat().st_size,
                               "entries": len(doc) if isinstance(doc, (dict, list)) else None}
        else:
            name_rows[name] = None

    terrain_row = None
    catalog = terrain_dir / "multimap-catalog.json"
    if catalog.is_file():
        doc = read_json(catalog, {}) or {}
        entries = doc.get("entries") or []
        scenes = {row.get("scene_name") for row in entries if isinstance(row, dict)}
        enabled = set(spawn_loader.load()["placements"])
        terrain_row = {"path": str(terrain_dir), "maps": len(scenes & enabled),
                       "resource_maps": len(entries), "excluded_maps": len(scenes - enabled),
                       "catalog_sha256": sha256_file(catalog),
                       "matches_w3_v13": (doc.get("profile") == "experimental_multimap_v13")}

    scene_inputs = game / "catalog-inputs"
    scene_inputs_row = None
    index = read_json(scene_inputs / "index.json", None)
    if isinstance(index, dict):
        scene_inputs_row = {
            "path": str(scene_inputs),
            "scene_count": index.get("scene_count"),
            "generated": index.get("generated"),
            "reused": index.get("reused"),
            "total_seconds": index.get("total_seconds"),
            "peak_memory_mb": index.get("peak_memory_mb"),
        }

    maps = profiles.get("maps") or {}
    dict_rel = profiles.get("dictionary_path") if isinstance(profiles, dict) else None
    dict_path = None
    if isinstance(dict_rel, str) and dict_rel:
        cand = Path(dict_rel)
        if not cand.is_absolute():
            cand = private / cand
        dict_path = cand
    dict_row = None
    if dict_path is not None:
        dict_row = {"path": str(dict_path), "exists": dict_path.is_file(),
                    "bytes": dict_path.stat().st_size if dict_path.is_file() else None}
    profiles_row = None
    if isinstance(profiles, dict) and profiles:
        profiles_row = {"path": str(private / "connection0167_profiles.local.json"),
                        "apk_build": profiles.get("apk_build"),
                        "resource_revision": profiles.get("resource_revision"),
                        "maps": len(maps),
                        "home_long_templates": bool(profiles.get("home_long_templates")),
                        "dictionary_path": dict_rel}
    capture_rows = []
    for map_id, entry in sorted(maps.items()):
        cap = (entry or {}).get("current_capture") or {}
        own = cap.get("by_account") or {}
        if own or cap.get("record_dir"):
            selected = own.get(account_uuid) if account_uuid else None
            selected = selected if isinstance(selected, dict) else cap if cap.get("account") == account_uuid else {}
            record_name = selected.get("record_dir")
            record = {}
            if isinstance(record_name, str) and record_name and Path(record_name).name == record_name:
                record = read_json(private / "current_profiles" / record_name / "record.json", {}) or {}
            nv = record.get("netversion_u16") if isinstance(record, dict) else None
            current_capture = (isinstance(record, dict)
                and record.get("apk_build") == profile.get("build")
                and record.get("native_sha256") == profile.get("native_sha256")
                and record.get("resource_revision") == profile.get("resource_revision")
                and isinstance(nv, list) and len(nv) == 6
                and (str(map_id) != str(profile.get("home_level_id")) or nv[0] == profile.get("home_resource_tag")))
            capture_rows.append({"map_id": map_id, "scene": (entry or {}).get("scene"),
                                 "record_dir": cap.get("record_dir"),
                                 "accounts": sorted(own), "current": bool(current_capture)})

    catalogs = []
    if account_dir.is_dir():
        for folder in sorted(account_dir.iterdir()):
            manifest = folder / "manifest.json"
            if folder.is_dir() and manifest.is_file():
                doc = read_json(manifest, {}) or {}
                checked = certify(data, str(account), account_uuid, profile) if folder.name == account else {
                    "certified": False, "state": "not_current_account", "code": "not_current_account",
                    "message": "此目录不是当前账号，未作可用性认证"}
                catalogs.append({"account": folder.name, "manifest": str(manifest),
                                 "sha256": sha256_file(manifest),
                                 "maps": len(doc.get("entries") or {}),
                                 "variants": sum(len(v) for v in (doc.get("variants") or {}).values()), **checked})

    return {
        "ok": True,
        "version": GAME_VERSION,
        "data": str(data),
        "apk": apk_row,
        "names": name_rows,
        "props": props,
        "names_ready": all(v is not None for v in name_rows.values()),
        "terrain": terrain_row,
        "scene_inputs": scene_inputs_row,
        "profiles": profiles_row,
        "dictionary": dict_row,
        "captures": capture_rows,
        "catalog_sources": sources,
        "catalogs": catalogs,
        "account": str(account or ""),
        "done": {
            "apk": apk_row is not None,
            "names": all(v is not None for v in name_rows.values()),
            "terrain": terrain_row is not None,
            "props": props["ready"],
            "scene_inputs": scene_inputs_row is not None,
            "capture": any(r["current"] for r in capture_rows),
            "dictionary": bool(dict_row and dict_row.get("exists")),
            "catalog": any(r.get("account") == account and r.get("certified") is True for r in catalogs),
        },
    }


def cmd_status(args) -> int:
    progress = Progress("status", 1)
    try:
        data = resolve_data(args.data)
        progress.emit("读取 %s" % data, done=0)
        row = collect(data)
        progress.finish("完成")
        ok = row.pop("ok", True)
        final(ok, **row)
        return 0
    except GameDataError as exc:
        final(False, error=exc.message, code=exc.code)
        return 1
    except OSError as exc:
        final(False, error="读数据目录失败：%s" % exc, code="status_io")
        return 1
