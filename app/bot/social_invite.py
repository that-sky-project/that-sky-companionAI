"""Pure outgoing Actor invitation patch + strict incoming social RPC readers.

No engine, transport, account, file access, timers or sleeps. This module NEVER
constructs/sends SocialAcceptOfferRpc on behalf of the intended recipient.
Runtime owns the single pending ticket and verifies engine/room generation,
nonce, both UUIDs/slot incarnations, epoch/source, deadline and offerer role.
A catalogue entry/bit patch does not prove display, acceptance or completion.
"""
import bisect
import re
import struct
from types import MappingProxyType

import anim_pose as _pose
from social_catalog import ACTION_CODES, catalog, resolve_action
from qfsky.long0167.actor_codec.actor_wire_0167 import inspect_actor_wire
from qfsky.long0167.actor_codec.actor_wire_schema_0167 import MAIN, PUBLIC_ELF_SHA256

PROFILE_SHA256 = "0b81f1da54d9c6f28c08746b2ec19519d8eb78c4f1b3c5e4e420ff726dc25321"
TYPE_LABEL, TARGET_LABEL = _pose.OFFER_TYPE, _pose.OFFER_TARGET
SOCIAL_ACCEPT_KEY = 0x603E65DF
RELEASE_KEYS = MappingProxyType({0x5BCC196F: "hold", 0x32D2F05A: "carry"})
MAX_GAME_BYTES, MAX_RPC_BODY, MAX_RPC_FRAMES, MAX_ANIMATION_BYTES = 20000, 0x1500, 512, 64
_ANIMATION = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}\Z")


def _slot(value):
    return type(value) is int and 1 <= value <= 255


def _observation(body):
    if type(body) is not bytes:
        raise ValueError("offer_body_must_be_bytes")
    if PUBLIC_ELF_SHA256 != PROFILE_SHA256 or _pose.SHA != PROFILE_SHA256:
        raise ValueError("offer_profile_mismatch")
    if (TYPE_LABEL, "range", 0, 255) not in MAIN or (TARGET_LABEL, "entity") not in MAIN:
        raise ValueError("offer_schema_mismatch")
    try:
        return inspect_actor_wire(body, verified_profile_sha256=PROFILE_SHA256, check_zero_padding=False)
    except (ValueError, TypeError, IndexError, OverflowError):
        raise ValueError("offer_body_invalid") from None


def _locations(observation):
    starts = _pose._starts(observation)
    result = {}
    for label, kind, extent in ((TYPE_LABEL, "bits", 8), (TARGET_LABEL, "raw", 1)):
        positions = [p for name, p in observation._boundaries if name == label]
        if len(positions) != 1:
            raise ValueError("offer_label_missing_or_ambiguous")
        index = bisect.bisect_left(starts, positions[0])
        if index >= len(observation._tokens):
            raise ValueError("offer_token_missing")
        token = observation._tokens[index]
        if token.kind != kind or token.extent != extent:
            raise ValueError("offer_token_shape_mismatch")
        result[label] = (index, starts[index], 8)
    if result[TYPE_LABEL][0] == result[TARGET_LABEL][0]:
        raise ValueError("offer_field_alias")
    return result


def _values(observation, locations):
    return {label: (int.from_bytes(observation._tokens[i].value, "little")
                    if observation._tokens[i].kind == "raw" else observation._tokens[i].value)
            for label, (i, _, _) in locations.items()}


def inspect_offer(body):
    observation = _observation(body)
    values = _values(observation, _locations(observation))
    return {"type_code": values[TYPE_LABEL], "target_slot": values[TARGET_LABEL]}


def field_info(body):
    observation = _observation(body)
    return {"profile": "netease-android-0.16.7-421589", "counts": dict(observation._counts),
            "fields": {label: {"token_index": index, "bit_offset": bit, "bit_width": width}
                       for label, (index, bit, width) in _locations(observation).items()}}


def arm_actor(body, action_code, target_slot, *, known_slots=None, own_slot=None):
    """Patch only type/target bits; preserve padding and all other Actor tokens.

    A nonzero target is only a dictionary slot. The caller must bind the fresh
    current room/UUID/incarnation; optional known_slots/own_slot reject obvious
    stale/self requests but are not authentication or permission evidence.
    """
    if type(action_code) is not int or type(target_slot) is not int:
        raise ValueError("offer_codes_must_be_strict_integers")
    if action_code == 0:
        if target_slot != 0:
            raise ValueError("offer_clear_requires_zero_pair")
    else:
        entry = resolve_action(action_code)
        if not entry["supported"]:
            raise ValueError("offer_action_unsupported")
        if not _slot(target_slot):
            raise ValueError("offer_active_target_range")
        if own_slot is not None:
            if not _slot(own_slot):
                raise ValueError("offer_own_slot_invalid")
            if own_slot == target_slot:
                raise ValueError("offer_target_is_self")
        if known_slots is not None:
            if (type(known_slots) not in (set, frozenset, tuple, list)
                    or not 1 <= len(known_slots) <= 255 or any(not _slot(s) for s in known_slots)):
                raise ValueError("offer_roster_invalid")
            if target_slot not in known_slots or (own_slot is not None and own_slot not in known_slots):
                raise ValueError("offer_target_not_in_roster")
    observation = _observation(body)
    locations = _locations(observation)
    patched, masks = bytearray(body), bytearray(len(body))
    for label, value in ((TYPE_LABEL, action_code), (TARGET_LABEL, target_slot)):
        _, start, width = locations[label]
        for shift in range(width):
            byte, bit = divmod(start + shift, 8)
            mask = 1 << bit
            masks[byte] |= mask
            patched[byte] = (patched[byte] & ~mask) | (((value >> shift) & 1) << bit)
    result = bytes(patched)
    after = _observation(result)
    if (len(result) != len(body) or len(after._tokens) != len(observation._tokens)
            or after._counts != observation._counts or after._boundaries != observation._boundaries
            or _locations(after) != locations):
        raise ValueError("offer_structure_changed")
    allowed = {i for i, _, _ in locations.values()}
    if any(i not in allowed and a != b for i, (a, b) in enumerate(zip(observation._tokens, after._tokens))):
        raise ValueError("offer_unrelated_token_changed")
    if any((a ^ b) & (~mask & 255) for a, b, mask in zip(body, result, masks)):
        raise ValueError("offer_unrelated_bit_or_padding_changed")
    actual = _values(after, locations)
    if actual[TYPE_LABEL] != action_code or actual[TARGET_LABEL] != target_slot:
        raise ValueError("offer_roundtrip_mismatch")
    return result


# Compatibility with the previous isolated proof's pure helper name.
patch_offer = arm_actor


def clear_offer(body):
    """Clear only the outgoing pair; this does not send a release RPC."""
    return arm_actor(body, 0, 0)


def _animation(value):
    return type(value) is str and len(value) <= MAX_ANIMATION_BYTES and (value == "" or _ANIMATION.fullmatch(value) is not None)


def parse_social_accept_body(body):
    """Read bounded two-slot/two-name/u8/u8/u4 body, with exact consumption.

    Animation_a/b are wire order ONLY; no role is inferred by this parser.
    Empty names are valid (handhold/carry). NUL/control/unicode text is rejected.
    """
    if type(body) is not bytes or not 13 <= len(body) <= 13 + 2 * MAX_ANIMATION_BYTES:
        raise ValueError("social_accept_invalid_length")
    offerer, acceptor = body[:2]
    if not _slot(offerer) or not _slot(acceptor) or offerer == acceptor:
        raise ValueError("social_accept_invalid_roles")
    offset, names = 2, []
    for _ in range(2):
        if offset + 4 > len(body):
            raise ValueError("social_accept_truncated_string")
        size = struct.unpack_from("<I", body, offset)[0]
        offset += 4
        if size > MAX_ANIMATION_BYTES or offset + size > len(body):
            raise ValueError("social_accept_invalid_string_length")
        try:
            name = body[offset:offset + size].decode("ascii")
        except UnicodeDecodeError:
            raise ValueError("social_accept_invalid_animation") from None
        if not _animation(name):
            raise ValueError("social_accept_invalid_animation")
        names.append(name)
        offset += size
    # CN serializer: 0x230a384/390 are raw u8; 0x230a3ac is 0..8,
    # occupying four bits. High four bits are ignored alignment padding.
    if len(body) - offset != 3 or (body[offset + 2] & 15) > 8:
        raise ValueError("social_accept_invalid_tail")
    tail = [body[offset], body[offset + 1], body[offset + 2] & 15]
    return {"offerer_slot": offerer, "acceptor_slot": acceptor,
            "animation_a": names[0], "animation_b": names[1], "tail": tail}


def validate_accept(action_code, parsed, *, offerer_slot=None, acceptor_slot=None):
    """Match the pending action's exact known pattern; not proof of delivery.

    Many ability codes share a pattern; RPC has no action code or nonce. The
    runtime MUST additionally enforce its single current pending ticket/roles,
    engine/room/UUID/incarnation binding, receive timestamp and timeout.
    """
    try:
        entry = resolve_action(action_code)
    except ValueError:
        return False
    if type(parsed) is not dict or not entry["supported"]:
        return False
    a, b = parsed.get("offerer_slot"), parsed.get("acceptor_slot")
    if not _slot(a) or not _slot(b) or a == b:
        return False
    if offerer_slot is not None and (not _slot(offerer_slot) or offerer_slot != a):
        return False
    if acceptor_slot is not None and (not _slot(acceptor_slot) or acceptor_slot != b):
        return False
    first, second, tail = parsed.get("animation_a"), parsed.get("animation_b"), parsed.get("tail")
    if not _animation(first) or not _animation(second) or type(tail) not in (tuple, list) or len(tail) != 3:
        return False
    if any(type(value) is not int or not 0 <= value <= limit for value, limit in zip(tail, (255, 255, 8))):
        return False
    return any(first == pair["animation_a"] and second == pair["animation_b"] and list(tail) == pair["tail"]
               for pair in entry["accept_pairs"])


def parse_release_body(key, body):
    """Strict CN mode2 release layouts; unknown tail meaning stays unlabelled.

    DisconnectCarried: two byte-sized PlayerIds plus two packed bool bits.
    AvatarDetachFollower: two PlayerIds plus u32 byte length and string bytes.
    Direction/permission still needs the runtime's accepted-pair binding. The
    string is opaque and never returned; 256 is a conservative local length cap.
    """
    if type(key) is not int or key not in RELEASE_KEYS:
        raise ValueError("social_release_unknown_key")
    if type(body) is not bytes or not 3 <= len(body) <= 262:
        raise ValueError("social_release_invalid_length")
    actor, target = body[:2]
    if not _slot(actor) or not _slot(target) or actor == target:
        raise ValueError("social_release_invalid_roles")
    result = {"kind": RELEASE_KEYS[key], "actor_slot": actor, "target_slot": target,
              "tail_semantics_known": False, "prefix_only": False}
    if key == 0x32D2F05A:
        if len(body) != 3:
            raise ValueError("social_release_invalid_carry_length")
        # 0x14e401c/0x14e4030 call the one-bit serializer. High six bits are padding.
        result["tail_codes"] = [body[2] & 1, (body[2] >> 1) & 1]
        result["layout"] = "two_slots_two_bool_bits"
    else:
        if len(body) < 6:
            raise ValueError("social_release_invalid_hold_length")
        size = struct.unpack_from("<I", body, 2)[0]
        if size > 256 or size != len(body) - 6:
            raise ValueError("social_release_invalid_string_length")
        result["layout"] = "two_slots_length_prefixed_string"
        result["string_meta"] = {"wire_length": size, "untrusted": True, "content_omitted": True}
    return result


def _rpc_frames(game):
    if type(game) is not bytes or not 4 <= len(game) <= MAX_GAME_BYTES or game[0] != 2:
        raise ValueError("social_game_invalid")
    offset, frames = 4, []
    while offset < len(game):
        if len(frames) >= MAX_RPC_FRAMES or len(game) - offset < 7:
            raise ValueError("social_rpc_invalid_stream")
        key, header, size = struct.unpack_from("<IBH", game, offset)
        end = offset + 7 + size
        if header != 0 or size & 0xe000 or size > MAX_RPC_BODY or end > len(game):
            raise ValueError("social_rpc_invalid_frame")
        frames.append((key, game[offset + 7:end]))
        offset = end
    return frames


def parse_social_game(game):
    """Fully validate framing before returning bounded accept/release events."""
    frames = _rpc_frames(game)
    events = []
    for key, body in frames:
        if key == SOCIAL_ACCEPT_KEY:
            parsed = parse_social_accept_body(body)
            event = {"event": "accept", **parsed}
        elif key in RELEASE_KEYS:
            event = {"event": "release", **parse_release_body(key, body)}
        else:
            continue
        events.append({"epoch": game[1], "source_slot": game[2], "context_slot": game[3], "rpc_key": key, **event})
    return events


def parse_accept_game(game):
    return [event for event in parse_social_game(game) if event["event"] == "accept"]


def parse_release_game(game):
    return [event for event in parse_social_game(game) if event["event"] == "release"]
