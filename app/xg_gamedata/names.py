# -*- coding: utf-8 -*-
"""names：从用户自己的 APK 生成小光运行时名称表。

五种表（小光代码读的路径见 w4a/patches）：
  emotes.json / outfit_names_zh.json / gift_names.json / catalog.json /
  destination-records-v25.json

口径
====
* 能由 APK 推出的：表情素材名/等级、装扮内部名与 FNV id、礼包 commerce_item_name_*、
  场景名与 map_id（FNV-1a32）、NetVersion。
* APK 里没有的：官方/人工整理的中文名、社区地名别名、动作时长与说明。这些文本放在
  ``name_data.py``（人工整理文本，不是游戏二进制数据），生成时仍逐项用 APK 校验 ID。
* 因此 emotes 与 outfit/gift 可做到逐字段一致；catalog/destination 的结构与
  identity 字段一致，人工说明/来源路径按本机实际情况写。
"""
from __future__ import annotations

import json
import re
import time
import zipfile
from pathlib import Path

from . import name_data
from .common import (APK_BUILD, GameDataError, Progress, atomic_write, final,
                     game_dir, resolve_data)
from .apk import NATIVE_MEMBER, inspect_apk, sha256_file_from_zip

FNV_OFFSET = 0x811C9DC5
FNV_PRIME = 0x01000193

# 与 base/release-v20-hop-trial/bot/build_emotes.py 一致（人工整理）
EMOTE_CN = {
    "Acknowledge": ["致意"], "AirplaneArms": ["飞机手"], "Anxious": ["焦虑"], "Approve": ["赞同"],
    "Awww": ["感动"], "BackFlip": ["后空翻"], "Beckon": ["招手"], "BlowKiss": ["飞吻"], "Bow": ["鞠躬"],
    "Bubbles": ["吹泡泡"], "Cartwheel": ["侧手翻"], "Celebrate": ["庆祝"], "Cheer": ["欢呼"],
    "ChestPound": ["捶胸"], "Chuckle": ["偷笑"], "Cough": ["咳嗽"], "Cry": ["哭泣", "哭"], "Dance": ["跳舞"],
    "DeepBreath": ["深呼吸"], "DontGo": ["别走"], "DustOff": ["拍灰"], "Evillaugh": ["坏笑"], "Eww": ["嫌弃"],
    "FacePalm": ["捂脸"], "Flag": ["摇旗"], "FloatDance": ["漂浮舞"], "Frustration": ["抓狂"], "Gloat": ["得意"],
    "Gratitude": ["感谢"], "Grumpy": ["生气"], "Hackysack": ["踢毽子"], "HeartHands": ["比心"],
    "Hesitate": ["犹豫"], "Joy": ["开心"], "Juggle": ["杂耍"], "KungFu": ["功夫"], "LaughWith": ["大笑"],
    "LookAround": ["张望"], "Mope": ["沮丧"], "Muscle": ["秀肌肉"], "NoThanks": ["不用了"], "Nod": ["点头"],
    "Playfight": ["比划"], "Plead": ["恳求"], "Pout": ["撅嘴"], "Rally": ["加油"], "Respect": ["致敬"],
    "Roll": ["翻滚"], "SakuraCelebrate": ["樱花庆祝"], "Salutation": ["行礼"], "Sarcastic": ["嘲讽"],
    "Scare": ["吓唬"], "Scheme": ["密谋"], "Shh": ["嘘"], "Shocked": ["震惊"], "ShowDance": ["表演舞"],
    "ShowOff": ["炫耀"], "Shrug": ["耸肩"], "Shy": ["害羞"], "SilentClap": ["鼓掌"], "Skip": ["蹦跳"],
    "Sneeze": ["打喷嚏"], "Stretch": ["伸懒腰"], "Think": ["思考"], "ThumbsUp": ["点赞"], "TiskTisk": ["啧啧"],
    "TripleAxel": ["三周跳"], "Twirl": ["转圈"], "Wallow": ["躺平"], "Wave": ["挥手"], "Whistle": ["吹口哨"],
    "WipeBrow": ["擦汗"], "Yawn": ["打哈欠"],
}
EMOTE_SECONDS = {"Dance": 6, "FloatDance": 6, "ShowDance": 6, "Juggle": 6, "KungFu": 5, "Hackysack": 6,
                 "Bubbles": 5, "Cartwheel": 5, "BackFlip": 5, "TripleAxel": 5, "Roll": 5, "Wallow": 6,
                 "SakuraCelebrate": 6, "Celebrate": 5, "Flag": 5, "Stretch": 5}

SCENE_MEMBER = re.compile(r"assets/Data/Levels/([A-Za-z0-9_]{1,96})/Objects\.level\.bin\Z")
OUTFIT_SLOT = re.compile(r"CharSkyKid_(Body|Wing|Hair|Hat|Horn|Mask|Neck|Feet|Face|Prop)_")


def fnv32(text: str) -> int:
    h = FNV_OFFSET
    for b in text.encode("utf-8"):
        h = ((h ^ b) * FNV_PRIME) & 0xFFFFFFFF
    return h


def _native_names(raw: bytes) -> dict[int, str]:
    names: dict[int, str] = {}
    for m in re.finditer(rb"[A-Za-z_][A-Za-z0-9_./-]{2,80}", raw):
        text = m.group().decode("ascii", "replace")
        names.setdefault(fnv32(text), text)
    return names


def _scene_names(zf: zipfile.ZipFile) -> list[str]:
    names = []
    for member in zf.namelist():
        m = SCENE_MEMBER.fullmatch(member)
        if m:
            names.append(m.group(1))
    return sorted(set(names))


def _write_json(path: Path, value, indent=None, newline=False):
    text = json.dumps(value, ensure_ascii=False, indent=indent)
    if newline:
        text += "\n"
    atomic_write(path, text.encode("utf-8"))


def build_emotes(native: dict[int, str], out_dir: Path) -> dict:
    levels = {}
    for name in native.values():
        m = re.fullmatch(r"([A-Z][A-Za-z]+)Emote_(\d)", name)
        if m:
            levels[m.group(1)] = max(levels.get(m.group(1), 0), int(m.group(2)))
    table = {}
    missing = []
    for base, top in sorted(levels.items()):
        if base not in EMOTE_CN:
            missing.append(base)
            continue
        for cn in EMOTE_CN[base]:
            table[cn] = {"anim": base + "Emote", "levels": top,
                         "seconds": EMOTE_SECONDS.get(base, 4)}
    path = out_dir / "emotes.json"
    _write_json(path, table, indent=1)
    return {"file": "emotes.json", "entries": len(table),
            "actions": len({v["anim"] for v in table.values()}),
            "excluded_native_actions": len(missing), "missing_names": missing}


def build_outfits(native: dict[int, str], names: dict[int, str], out_dir: Path) -> dict:
    table = {}
    verified = unverified = absent = 0
    for fid, (name, kind, key) in name_data.DATA["OUTFIT"].items():
        if key:
            calc = fnv32(key)
            if str(calc) != fid:
                raise GameDataError("装扮名表 ID 与内部名不一致：%s" % key, "outfit_id_mismatch")
            if names.get(calc) != key:
                absent += 1
            else:
                verified += 1
        else:
            unverified += 1
        table[str(fid)] = {"name": name, "kind": kind, "key": key}
    path = out_dir / "outfit_names_zh.json"
    _write_json(path, table, indent=0)
    return {"file": "outfit_names_zh.json", "entries": len(table),
            "ids_verified_against_apk": verified,
            "key_not_in_native_strings": absent,
            "official_bundles_without_internal_key": unverified}


def build_gifts(arsc: dict, out_dir: Path) -> dict:
    keys = {k: v for k, v in name_data.DATA["GIFT"]["keys"].items()
            if k.startswith("commerce_item_name_")}
    table = {"source": "resources.arsc 0.16.7 commerce_item_name_* + astrbot_plugin_sky gift_catalog.json",
             "keys": keys, "skus": dict(name_data.DATA["GIFT"]["skus"])}
    found = sum(1 for k in keys if k in arsc)
    path = out_dir / "gift_names.json"
    _write_json(path, table)
    return {"file": "gift_names.json", "keys": len(keys), "skus": len(table["skus"]),
            "keys_found_in_arsc": found}


def build_catalog(native: dict[int, str], out_dir: Path) -> dict:
    meta = name_data.DATA["CATALOG_EMOTES"]
    table = {"emotes": {}, "stances": dict(name_data.DATA["CATALOG_STANCES"])}
    missing = []
    for cn, row in meta.items():
        base = row["en"]
        nums = sorted({int(m.group(2)) for name in native.values()
                       if (m := re.fullmatch("(%s)Emote_(\\d)" % re.escape(base), name))})
        levels = {str(n): ["%sEmote_%d" % (base, n)] for n in nums}
        # APK 的 libBootloader 里没有的动作名/菜单分段：用人工整理的等级表（已钉住）。
        if row.get("levels"):
            levels = {k: list(v) for k, v in row["levels"].items()}
        if not levels:
            missing.append(cn)
            continue
        table["emotes"][cn] = {
            "en": base,
            "levels": levels,
            "default": row["default"] or "1",
            "seconds": dict(row["seconds"]),
            "aliases": row["aliases"],
            "confidence": row["confidence"],
            "note": row["note"],
        }
    path = out_dir / "catalog.json"
    _write_json(path, table, indent=2, newline=True)
    return {"file": "catalog.json", "emotes": len(table["emotes"]),
            "stances": len(table["stances"]), "missing_in_apk": missing}


def build_destinations(scenes: list[str], apk: Path, out_dir: Path) -> dict:
    meta = name_data.DATA["DEST_SCENES"]
    records = []
    missing = []
    scene_names = {}
    for scene in scenes:
        row = meta.get(scene)
        if row is None:
            missing.append(scene)
            row = {"displayName": scene, "aliases": [scene, "scene:" + scene],
                   "verification": "scene_identity_verified", "arrival": {"kind": "scene_spawn", "position": None}}
        scene_names[scene] = row["displayName"]
        aliases = list(row["aliases"]) or list(dict.fromkeys([scene, "scene:" + scene]))
        records.append({
            "id": "scene:" + scene,
            "kind": "scene",
            "sceneName": scene,
            "sceneHash": fnv32(scene),
            "displayName": row["displayName"],
            "aliases": aliases,
            "verification": row["verification"],
            "arrival": row["arrival"],
            "provenance": [{"kind": "local_apk_scene_list", "path": str(apk),
                            "sha256": None, "claim": "场景名与 map_id 由本机 APK 推得；中文名为人工整理。"}],
            "sourceMember": row.get("sourceMember") or ("assets/Data/Levels/%s/BstBaked.meshes" % scene),
            "runtimeEligibilityNotEstablished": row.get("runtimeEligibilityNotEstablished", True),
            "labelBasis": row.get("labelBasis"),
        })
    for row in name_data.DATA["DEST_LANDMARKS"]:
        row = dict(row)
        row["provenance"] = [{"kind": "user_annotation",
                              "claim": "哥哥标注的命名落点；不是该场景的原生出生点。"}]
        records.append(row)
    table = {
        "schema": "sky-destination-registry-v25",
        "records": records,
        "notes": list(name_data.DATA["DEST_NOTES"]),
    }
    path = out_dir / "destination-records-v25.json"
    _write_json(path, table, indent=1, newline=True)
    # 录图 worker / 登记表用的“场景 → 中文名”小表（仍来自同一份 APK 场景 + 人工名）。
    _write_json(out_dir / "scene_names.json", scene_names, indent=1, newline=True)
    return {"file": "destination-records-v25.json", "records": len(records),
            "scenes_from_apk": len(scenes), "landmarks": len(name_data.DATA["DEST_LANDMARKS"]),
            "scenes_without_curated_name": missing,
            "scene_names": len(scene_names)}


def generate(apk: Path, data: Path, progress: Progress) -> dict:
    info = inspect_apk(apk, progress)
    out_dir = game_dir(data) / "names"
    out_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(apk) as zf:
        native_raw = zf.read(NATIVE_MEMBER)
        arsc_raw = zf.read("resources.arsc")
        scenes = _scene_names(zf)
    progress.emit("解析 resources.arsc…", done=1)
    arsc = None
    try:
        from . import arsc_strings
        parsed = arsc_strings.parse(arsc_raw)
        arsc = parsed.get("", {})
    except Exception:
        arsc = {}
    progress.emit("解析 libBootloader.so 名称表…", done=1)
    native = _native_names(native_raw)
    progress.emit("生成 5 张名称表…", done=2)
    rows = []
    rows.append(build_emotes(native, out_dir))
    rows.append(build_outfits(native, native, out_dir))
    rows.append(build_gifts(arsc, out_dir))
    rows.append(build_catalog(native, out_dir))
    rows.append(build_destinations(scenes, apk, out_dir))
    from . import dictionary
    dict_row = dictionary.ensure_for_data(apk, data, progress)
    if not isinstance(dict_row, dict) or dict_row.get("ok") is not True:
        code = dict_row.get("code") if isinstance(dict_row, dict) else None
        # Keep diagnostic codes, not raw exception messages or filesystem paths.
        if code not in ("apk_missing", "dictionary_size", "dictionary_magic",
                        "dictionary_id", "dictionary_sha256", "dictionary_missing",
                        "dictionary_bad_zip", "dictionary_apk_read", "dictionary_apk_missing",
                        "dictionary_write_verify", "dictionary_io"):
            code = "dictionary_unavailable"
        raise GameDataError("压缩字典生成或校验失败（%s）。名称表已保留，请检查安装包后重试或续跑。" % code,
                            code)
    progress.finish("名称表已生成：%s" % out_dir)
    return {"ok": True, "apk": info, "names_dir": str(out_dir), "tables": rows,
            "dictionary": dict_row,
            "assets": {"native_strings": len(native), "arsc_entries": len(arsc),
                       "scenes": len(scenes)}}


def cmd_names(args) -> int:
    progress = Progress("names", 4)
    try:
        apk = Path(args.apk).expanduser().resolve()
        data = resolve_data(args.data)
        result = generate(apk, data, progress)
        final(True, **{k: v for k, v in result.items() if k != "ok"})
        return 0
    except GameDataError as exc:
        final(False, error=exc.message, code=exc.code)
        return 1
    except (OSError, zipfile.BadZipFile) as exc:
        final(False, error="生成名称表失败：%s" % exc, code="names_io")
        return 1

