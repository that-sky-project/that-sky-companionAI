# -*- coding: utf-8 -*-
"""status：看本机已经生成了哪些游戏数据。"""
from __future__ import annotations

from pathlib import Path

from . import spawn_loader

from .common import (GAME_VERSION, GameDataError, Progress, final, game_dir,
                     read_json, resolve_data, sha256_file, xg_paths)


def collect(data: Path) -> dict:
    game = game_dir(data)
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
            capture_rows.append({"map_id": map_id, "scene": (entry or {}).get("scene"),
                                 "record_dir": cap.get("record_dir"),
                                 "accounts": sorted(own)})

    catalogs = []
    if account_dir.is_dir():
        for folder in sorted(account_dir.iterdir()):
            manifest = folder / "manifest.json"
            if folder.is_dir() and manifest.is_file():
                doc = read_json(manifest, {}) or {}
                catalogs.append({"account": folder.name, "manifest": str(manifest),
                                 "sha256": sha256_file(manifest),
                                 "maps": len(doc.get("entries") or {}),
                                 "variants": sum(len(v) for v in (doc.get("variants") or {}).values())})

    return {
        "ok": True,
        "version": GAME_VERSION,
        "data": str(data),
        "apk": apk_row,
        "names": name_rows,
        "names_ready": all(v is not None for v in name_rows.values()),
        "terrain": terrain_row,
        "scene_inputs": scene_inputs_row,
        "profiles": profiles_row,
        "dictionary": dict_row,
        "captures": capture_rows,
        "catalog_sources": sources,
        "catalogs": catalogs,
        "done": {
            "apk": apk_row is not None,
            "names": all(v is not None for v in name_rows.values()),
            "terrain": terrain_row is not None,
            "scene_inputs": scene_inputs_row is not None,
            "capture": bool(capture_rows),
            "dictionary": bool(dict_row and dict_row.get("exists")),
            "catalog": bool(catalogs),
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
