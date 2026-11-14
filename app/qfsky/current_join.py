"""Explicit account/build/revision/scene-bound current JoinGame preparation.

No network calls. Missing current captures never fall back to historical packets.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import socket

from . import join0167_profile as profiles

BUILD = 421629
REGISTRATION_CHAIN_VERIFIED = False


class CaptureRequired(ValueError):
    pass


def mode():
    value = os.environ.get("QFLY_JOIN_MODE", "current").strip().lower()
    if value not in {"current", "legacy"}:
        raise CaptureRequired("QFLY_JOIN_MODE must be current or explicit legacy")
    return value


def config():
    name = os.environ.get("QFLY_JOIN_CONFIG", "").strip()
    revision = os.environ.get("QFLY_RESOURCE_REVISION", "").strip()
    if not name:
        raise CaptureRequired("缺少 0.16.8 实测缓存配置；不会回退到历史模板")
    if not revision:
        raise CaptureRequired("尚未显式选择当前实际资源版本 QFLY_RESOURCE_REVISION")
    p = Path(name).resolve(strict=True)
    if p.stat().st_size > 131072:
        raise CaptureRequired("JoinGame configuration exceeds size bound")
    doc = json.loads(p.read_text("utf-8-sig"))
    if doc.get("apk_build") != BUILD or doc.get("resource_revision") != revision:
        raise CaptureRequired("缓存配置与当前 build/资源版本不匹配")
    root = Path(doc["private_root"])
    if not root.is_absolute():
        root = p.parent / root
    root = root.resolve(strict=True)
    entries = doc.get("maps")
    if not isinstance(entries, dict) or len(entries) > 256:
        raise CaptureRequired("JoinGame maps must be an explicit bounded mapping")
    if any(not isinstance(value, dict) for value in entries.values()):
        raise CaptureRequired("JoinGame map entries must be objects")
    return doc, root, revision


def load_profile(map_id: int, account_uuid: str, build: int):
    if build != BUILD:
        raise CaptureRequired("当前 JoinGame codec 仅对应 APK build 421629")
    doc, root, revision = config()
    entry = doc["maps"].get(str(map_id))
    if not isinstance(entry, dict):
        raise CaptureRequired("该地图缺少当前版本实测缓存；未知地图不使用通用模板")
    profile = profiles.load_cache_profile(root, entry["record"], scene=entry["scene"],
        resource_revision=revision, account_uuid=account_uuid, apk_build=build)
    if profile.record["level_id"] != map_id:
        raise CaptureRequired("缓存场景与请求的地图 ID 不一致")
    return profile, entry, root, revision


def prepare(map_id: int, account_uuid: str, build: int, *, require_registration=False):
    profile, entry, root, revision = load_profile(map_id, account_uuid, build)
    if require_registration and not REGISTRATION_CHAIN_VERIFIED:
        raise CaptureRequired("已绑定当前实测缓存，但新版注册首组包/保活链尚未验证；不发送旧版 burst")
    if not entry.get("fields"):
        raise CaptureRequired("缺少本图当前版本显式 ticket/tail 字段记录，不能推测填零")
    f = profiles.inside(root, entry["fields"])
    if f.stat().st_size > 65536:
        raise CaptureRequired("JoinGame field record exceeds size bound")
    record = json.loads(f.read_text("utf-8-sig"))
    if (record.get("apk_build") != build or record.get("resource_revision") != revision
            or record.get("scene") != entry["scene"] or record.get("account_binding") != profile.binding):
        raise CaptureRequired("显式字段记录的账号/build/资源/场景绑定不一致")
    fields = dict(record["fields"])
    required = ("counter", "ticket_flag", "others", "ticket_extra_u32_a", "ticket_extra_u8",
        "ticket_extra_u32_b", "tail_bool_a", "tail_bool_b", "previous_address_tag",
        "previous_address", "previous_port", "previous_level_id", "server_version", "client_version")
    absent = [k for k in required if k not in fields]
    if absent:
        raise CaptureRequired("显式字段记录缺少: " + ", ".join(absent))
    fields["others"] = [profiles.uuid_raw(x) for x in fields["others"]]
    if not isinstance(fields["previous_address"], str):
        raise CaptureRequired("previous_address must be explicit hex (empty when tag=0)")
    fields["previous_address"] = bytes.fromhex(fields["previous_address"])
    if fields["server_version"] != 442 or fields["client_version"] != 5127:
        raise CaptureRequired("当前 native wire version 必须明确为 442/5127")
    from .join0167_codec import uint, octets
    if len(fields["others"]) > 7:
        raise CaptureRequired("当前 native othersCount 最大为 7")
    for item in fields["others"]:
        octets(item, 16, "other uid")
    for key, bits in (("counter",8), ("ticket_flag",8), ("ticket_extra_u32_a",32),
        ("ticket_extra_u8",8), ("ticket_extra_u32_b",32), ("tail_bool_a",1),
        ("tail_bool_b",1), ("previous_address_tag",2), ("previous_port",16), ("previous_level_id",32)):
        uint(fields[key], bits, key)
    octets(fields["previous_address"], {0:0,1:4,2:16,3:0}[fields["previous_address_tag"]], "previous_address")
    return {"profile": profile, "fields": fields, "level_hash": profile.record["netversion_u16"][0]}


def build_payload(prepared, *, uid, session_hex, ticket, peer_key):
    fields = dict(prepared["fields"])
    fields.update(uid=profiles.uuid_raw(uid), session=bytes.fromhex(session_hex),
        signature=bytes.fromhex(ticket["sig"]), ticket_ipv4=socket.inet_pton(socket.AF_INET, ticket["ip"]),
        ticket_port=int(ticket["port"]), move_ts=int(ticket["move_ts"]), peer_key=int(peer_key))
    return profiles.build_from_cache(prepared["profile"], fields, frame=True)


def _public_error(exc):
    if isinstance(exc, OSError):
        return "私有缓存配置或文件不可读取；请在本机检查配置"
    return str(exc)


def status_for_account(account_uuid: str, build=BUILD):
    selected = mode()
    result = {"mode": selected, "maps": {}, "registration_chain_verified": False}
    if selected == "legacy":
        result["message"] = "显式历史模式：不是 0.16.8 联机验证结果"
        return result
    try:
        doc, _, revision = config()
        result["resource_revision"] = revision
    except (ValueError, OSError, KeyError, TypeError) as exc:
        result["message"] = _public_error(exc)
        return result
    for key, entry in doc["maps"].items():
        row = {"cache_ready": False, "can_enter": False, "scene": entry.get("scene", "")}
        try:
            load_profile(int(key), account_uuid, build)
            row["cache_ready"] = True
            row["message"] = "缓存绑定通过；新版注册首组包/保活链尚未验证"
        except (ValueError, OSError, KeyError, TypeError) as exc:
            row["message"] = _public_error(exc)
        result["maps"][key] = row
    result["message"] = "逐图匹配私有缓存；未知图不回退，旧 burst 不发送"
    return result
