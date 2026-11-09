"""Read-only current-room social/outfit projection; no account/API or action code."""
import json
import math
import re
import time
from pathlib import Path

ROOM_SCHEMA = "cn0167-room-status-v1"
PLAYER_SCHEMA = "cn0167-player-status-v1"
PROFILE = "netease-android-0.16.7-421589"
MAX_AGE = 5.0
MAX_STATE_BYTES = 256 * 1024
SLOTS = ("body", "wing", "hair", "mask", "neck", "feet", "horn", "face", "prop", "hat")
# Reuse the existing API slot labels, without copying its different ordering.
LABELS = {"body": "裤子", "wing": "斗篷", "hair": "发型", "mask": "面具",
          "neck": "颈部", "feet": "鞋子", "horn": "头饰", "face": "face槽",
          "prop": "背饰", "hat": "发饰"}
SOURCE = "当前房间实时观测"
NOT_RELATION = "邀请和目标标记不代表对方已接受、已牵手或已点亮。"

def _number(value):
    return type(value) in (int, float) and math.isfinite(value)

def _uint(value, maximum):
    return type(value) is int and 0 <= value <= maximum

def _unknown(reason="unavailable"):
    messages = {"stale": "实时观测已过期，请等新数据，不能沿用旧状态。",
                "disconnected": "当前没有已确认的房间连接，状态未知。",
                "map_mismatch": "正在换图或地图不一致，当前状态未知。",
                "not_observed": "当前房间还没读到你本人的实时状态。",
                "invalid": "实时状态格式或身份校验未通过，暂时未知。",
                "unavailable": "暂时没读到实时状态，不能凭旧记录判断。"}
    return {"ok": False, "source": SOURCE, "freshness": "unknown",
            "reason": reason, "error": messages.get(reason, messages["unavailable"])}

def _fresh_time(stamp, now, limit):
    return _number(stamp) and -1.0 <= now - stamp <= limit

def _load(path, map_id, now):
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(MAX_STATE_BYTES + 1)
        if len(raw) > MAX_STATE_BYTES:
            return None, "invalid"
        envelope = json.loads(raw)
    except (OSError, ValueError, TypeError, UnicodeError):
        return None, "unavailable"
    if not isinstance(envelope, dict) or not isinstance(envelope.get("room"), dict):
        return None, "invalid"
    room = envelope["room"]
    if room.get("status_schema_version") != ROOM_SCHEMA:
        return None, "invalid"
    if room.get("connected") is not True:
        return None, "disconnected"
    if (not _uint(map_id, 0xffffffff) or not map_id or room.get("map_id") != map_id
            or room.get("pending_map_id") not in (None, map_id)):
        return None, "map_mismatch"
    if not _uint(room.get("room_generation"), 0x7fffffff) or room["room_generation"] == 0:
        return None, "invalid"
    limit = room.get("status_max_age_seconds")
    if not _number(limit) or limit <= 0:
        return None, "invalid"
    limit = min(MAX_AGE, limit)
    if not all(_fresh_time(value, now, limit) for value in
               (envelope.get("generated_at"), room.get("generated_at"))):
        return None, "stale"
    if not isinstance(room.get("slots"), dict) or not isinstance(room.get("players"), dict):
        return None, "invalid"
    if len(room["players"]) > 256 or len(room["slots"]) > 256:
        return None, "invalid"
    return (room, limit), None

def _target(slot, room, subject_uuid, own_uuid):
    if not _uint(slot, 255):
        raise ValueError("target")
    if slot == 0:
        return {"kind": "unset", "label": "无目标标记"}
    target = room["slots"].get(hex(slot))
    if isinstance(own_uuid, str) and own_uuid and target == own_uuid:
        return {"kind": "companion", "label": "小光"}
    if target == subject_uuid:
        return {"kind": "self", "label": "本人"}
    if isinstance(target, str) and target:
        return {"kind": "room_player", "label": "同房其他玩家"}
    return {"kind": "unknown", "label": "未识别目标（不能确定是谁）"}

def _name(field_id, resolver):
    if resolver is None:
        return None
    try:
        value = resolver(field_id)
    except Exception:
        return None
    # Only an exact ID-table hit is accepted, never a raw native name or guess.
    if not isinstance(value, str) or not value.strip() or len(value) > 30:
        return None
    value = re.sub(r"[\r\n\t\x00-\x1f]", " ", value).strip()
    if any(x in value for x in ("[动作", "[站姿", "【", "】", "http:", "https:")):
        return None
    return value

def query(path, subject_uuid, map_id, *, own_uuid=None, topic="all", now=None, name_resolver=None):
    """Project only the exact current speaker's fresh fields. No state is cached."""
    if topic not in ("all", "outfit", "social") or not isinstance(subject_uuid, str) or not subject_uuid:
        return _unknown("invalid")
    now = time.time() if now is None else now
    if not _number(now):
        return _unknown("invalid")
    loaded, error = _load(path, map_id, now)
    if error:
        return _unknown(error)
    room, limit = loaded
    matches = [(slot, row) for slot, row in room["players"].items()
               if isinstance(row, dict) and row.get("uuid") == subject_uuid]
    if len(matches) != 1:
        return _unknown("not_observed" if not matches else "invalid")
    slot, row = matches[0]
    if not isinstance(slot, str) or not re.fullmatch(r"0x[0-9a-f]{1,2}", slot):
        return _unknown("invalid")
    if room["slots"].get(slot) != subject_uuid:
        return _unknown("invalid")
    state = row.get("status")
    if not isinstance(state, dict) or state.get("freshness") != "fresh":
        return _unknown("stale" if isinstance(state, dict) and state.get("freshness") == "stale" else "not_observed")
    if not _fresh_time(state.get("observed_at"), now, limit):
        return _unknown("stale")
    age = state.get("age_seconds")
    if not _number(age) or not 0 <= age <= limit:
        return _unknown("invalid")
    if state["observed_at"] > room["generated_at"] + 1:
        return _unknown("invalid")
    value = state.get("value")
    if (not isinstance(value, dict) or value.get("schema_version") != PLAYER_SCHEMA
            or value.get("profile") != PROFILE):
        return _unknown("invalid")
    result = {"ok": True, "source": SOURCE, "freshness": "fresh"}
    try:
        if topic in ("all", "outfit"):
            outfit = value.get("outfit")
            items = outfit.get("slots") if isinstance(outfit, dict) else None
            if not isinstance(items, list) or len(items) != len(SLOTS):
                raise ValueError("outfit")
            parts = []
            for index, expected in enumerate(SLOTS):
                item = items[index]
                if not isinstance(item, dict) or item.get("slot") != expected or item.get("index") != index:
                    raise ValueError("outfit_order")
                field_id, tints = item.get("field_id"), item.get("tint_codes")
                if not _uint(field_id, 0xffffffff) or not isinstance(tints, list) or len(tints) != 3:
                    raise ValueError("outfit_value")
                if not all(_uint(code, 3) for code in tints):
                    raise ValueError("outfit_tints")
                parts.append({"slot": expected, "label": LABELS[expected], "field_id": field_id,
                              "name": _name(field_id, name_resolver), "tint_codes": list(tints)})
            result["outfit"] = parts
        if topic in ("all", "social"):
            social = value.get("social")
            if not isinstance(social, dict) or type(social.get("seated")) is not bool:
                raise ValueError("social")
            offer = social.get("offer")
            invitation = None
            if offer is not None:
                if not isinstance(offer, dict) or not _uint(offer.get("type_code"), 255):
                    raise ValueError("offer")
                invitation = {"type_code": offer["type_code"],
                              "target": _target(offer.get("target_slot"), room, subject_uuid, own_uuid)}
            result["social"] = {"seated": social["seated"], "invitation": invitation,
                                "candle_target": _target(social.get("candle_target_slot"), room, subject_uuid, own_uuid)}
            result["note"] = NOT_RELATION
    except (ValueError, TypeError, KeyError):
        return _unknown("invalid")
    return result

def reply_lines(result):
    """Human-readable facts; native labels, UUIDs and net-slot numbers never escape."""
    if not result.get("ok"):
        return [result.get("error") or _unknown()["error"]]
    lines = []
    if "outfit" in result:
        lines.append("房间实时装扮，染色暂只显示代码：")
        pending = ""
        for part in result["outfit"]:
            name = part["name"] or "名称未知(ID" + str(part["field_id"]) + ")"
            codes = "/".join(str(x) for x in part["tint_codes"])
            item = part["label"] + "：" + name + "，染色码" + codes
            if pending and len(pending) + len(item) + 1 > 56:
                lines.append(pending)
                pending = ""
            pending += ("；" if pending else "") + item
        if pending:
            lines.append(pending)
    if "social" in result:
        social = result["social"]
        lines.append("实时座位标记：" + ("坐着" if social["seated"] else "未坐下"))
        offer = social["invitation"]
        if offer is None:
            lines.append("未观察到本人发起中的互动邀请")
        else:
            lines.append("互动邀请代码" + str(offer["type_code"]) + "，目标：" + offer["target"]["label"])
        lines.append("举烛目标标记：" + social["candle_target"]["label"])
        lines.append(NOT_RELATION)
    return lines

def system_note(path, own_uuid, map_id, *, now=None):
    """A short social-only self observation, never energy/world state or an outfit dump."""
    result = query(path, own_uuid, map_id, own_uuid=own_uuid, topic="social", now=now)
    if not result["ok"]:
        return "\n自身实时社交观测暂不可用；不要凭旧记录断言已牵手或已点亮。"
    social = result["social"]
    seat = "有坐下标记" if social["seated"] else "无坐下标记"
    offer = "有发起中的邀请" if social["invitation"] else "未观察到发起邀请"
    candle = "无举烛目标标记" if social["candle_target"]["kind"] == "unset" else "有举烛目标标记"
    return "\n自身实时观测：" + "；".join((seat, offer, candle)) + "。邀请不等于已接受关系。"

def query_intent(text):
    """Only complete, unambiguous personal observation questions bypass the model.

    No substring classifier or growing action blacklist: action requests, compound
    clauses, other-person questions and unsupported wording use the existing path.
    """
    text = str(text or "").strip().lstrip("/／").strip()
    if not text or len(text) > 200:
        return None
    compact = re.sub(r"\s+", "", text).rstrip("？?！!。～~")
    if compact in ("状态", "实时状态", "我的状态", "看看我的状态", "查我的状态", "看下我的状态"):
        return "all"
    if compact in ("社交状态", "我的社交状态", "实时社交状态", "查看我的社交状态"):
        return "social"
    prefix = r"(?:看看|看下|查看|查一下|查下|查询|查)?"
    current = r"(?:现在|当前|目前|此刻|实时)?"
    personal = prefix + r"我(?:的)?" + current + r"(?:的)?"
    outfit = (
        personal + r"(?:装扮|穿搭|穿着|服装|衣服|斗篷|面具|发型|鞋子)",
        prefix + r"我" + current + r"(?:穿(?:的|着|了)?|戴(?:的|着|了)?)(?:是什么|是啥|什么|啥)(?:装扮|衣服|斗篷|面具|帽子|发型)?",
        prefix + r"我" + current + r"(?:是)?(?:什么|啥)(?:装扮|穿搭|服装|衣服)",
    )
    if any(re.fullmatch(pattern, compact) for pattern in outfit):
        return "outfit"
    social = (
        personal + r"(?:社交状态|互动邀请|座位状态|举烛目标|举蜡烛目标)",
        r"我" + current + r"(?:正|还)?(?:在)?(?:坐着|举烛|举蜡烛|牵手|背背|肩背)(?:了)?(?:吗|么|没)",
        r"我" + current + r"(?:是不是|是否|有没有)(?:正|还)?(?:在)?(?:坐着|坐下了|举烛|举蜡烛|牵手|背背|肩背)(?:了)?",
        r"我" + current + r"(?:正|还)?(?:在)?(?:向谁|对谁)(?:举烛|举蜡烛|发起邀请)(?:呢)?",
    )
    if any(re.fullmatch(pattern, compact) for pattern in social):
        return "social"
    return None
