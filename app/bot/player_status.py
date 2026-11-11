"""Read-only CN 0.16.8 mode2 Actor status; no transport, logging or mutation.

Outfit indices are from the pinned CN ELF table at 0x39aaa38, resolved through
R_AARCH64_RELATIVE and matched to the native serializers' shared object layout.
Item IDs are opaque IDs, not human garment names. Received label content is
never exported. Social codes are observations, not consent or action commands.
Optional world_state is a fixed state array, NOT a daily-quest completion list.
The profile declaration identifies the decoder, not runtime/server provenance.
"""
import bisect
import math
import struct

import anim_pose as _pose
from qfsky.long0167.actor_codec.actor_wire_0167 import inspect_actor_wire
from qfsky.long0167.actor_codec.actor_wire_schema_0167 import (
    PUBLIC_ELF_SHA256, FIXED_HELPERS,
)

SCHEMA_VERSION = "cn0167-player-status-v1"
PROFILE = "netease-android-0.16.8-421629"
OUTFIT_SLOTS = ("body", "wing", "hair", "mask", "neck", "feet", "horn", "face", "prop", "hat")


class _View:
    def __init__(self, observation):
        self.tokens = observation._tokens
        self.starts = _pose._starts(observation)
        self.boundaries = {}
        for label, pos in observation._boundaries:
            self.boundaries.setdefault(label, []).append(pos)

    def index(self, label):
        positions = self.boundaries.get(label, ())
        if len(positions) != 1:
            raise ValueError("player_status_label_shape")
        index = bisect.bisect_left(self.starts, positions[0])
        if index >= len(self.tokens):
            raise ValueError("player_status_token_shape")
        return index

    def token(self, index, kind, extent):
        token = self.tokens[index]
        if token.kind != kind or token.extent != extent:
            raise ValueError("player_status_token_shape")
        return token.value

    def integer(self, label, kind, extent, maximum=None):
        value = self.token(self.index(label), kind, extent)
        value = int.from_bytes(value, "little") if kind == "raw" else value
        limit = ((1 << (extent * 8 if kind == "raw" else extent)) - 1
                 if maximum is None else maximum)
        if type(value) is not int or not 0 <= value <= limit:
            raise ValueError("player_status_integer_range")
        return value


class _Cursor:
    def __init__(self, view, start, end):
        self.view = view
        self.index = view.index(start)
        self.end = view.index(end)

    def take(self, kind, extent):
        if self.index >= self.end:
            raise ValueError("player_status_section_shape")
        value = self.view.token(self.index, kind, extent)
        self.index += 1
        return value

    def uint(self, size):
        return int.from_bytes(self.take("raw", size), "little")

    def bits(self, width, maximum=None):
        value = self.take("bits", width)
        limit = (1 << width) - 1 if maximum is None else maximum
        if type(value) is not int or not 0 <= value <= limit:
            raise ValueError("player_status_integer_range")
        return value

    def finish(self):
        if self.index != self.end:
            raise ValueError("player_status_section_shape")


def _outfit(view):
    cursor = _Cursor(view, "0x21df444", "0x21df454")
    slots = []
    for index, slot in enumerate(OUTFIT_SLOTS):
        field_id = cursor.uint(4)
        tints = [cursor.bits(2, 3) for _ in range(3)]
        size = cursor.uint(4)
        label = cursor.take("raw", size)
        canonical = (1 <= size <= 64 and label[-1:] == b"\0"
                     and b"\0" not in label[:-1])
        slots.append({"slot": slot, "index": index, "field_id": field_id,
                      "tint_codes": tints,
                      "label_meta": {"wire_length": size, "canonical": canonical,
                                     "untrusted": True, "content_omitted": True}})
    # Validate the remaining outfit scalars without interpreting or exporting them.
    cursor.bits(8)
    cursor.bits(8)
    cursor.bits(4, 15)
    cursor.bits(4, 14)
    cursor.uint(4)
    cursor.uint(2)
    cursor.finish()
    return {"slots": slots}


def _social(view):
    # Same native labels/meaning as anim_pose.social_state, but reuse the single
    # parsed observation and keep raw seat_id even while seated is false.
    seated = bool(view.integer(_pose.SEATED, "bits", 1))
    seat = view.integer(_pose.SEAT_ID, "bits", 13, 6657)
    kind = view.integer(_pose.OFFER_TYPE, "bits", 8)
    target = view.integer(_pose.OFFER_TARGET, "raw", 1)
    return {"seated": seated, "seat_id": seat,
            "animation_mode": view.integer(_pose.MODE, "raw", 1),
            "offer": None if kind == 0 else {"type_code": kind, "target_slot": target},
            "candle_target_slot": view.integer(_pose.CANDLE_TARGET, "raw", 1)}


def _energy(view):
    fields = {}
    for label, kind, *args in FIXED_HELPERS["0x21dd8c4"]:
        if kind == "float":
            value = struct.unpack("<f", view.token(view.index(label), "raw", 4))[0]
            if not math.isfinite(value):
                raise ValueError("player_status_nonfinite_energy")
            fields[label] = {"value": value, "encoding": "float32_le"}
        else:
            width = ((args[1] - args[0]).bit_length() if kind == "range"
                     else args[0] if kind == "quant" else 1)
            maximum = args[1] if kind == "range" else (1 << width) - 1
            code = view.integer(label, "bits", width, maximum)
            fields[label] = {"code": code, "bit_width": width}
    return {"tier_code": fields["0x21dd918"]["code"], "fields": fields,
            "semantic_scope": "native_energy_group_raw_codes_not_ui_percentages"}


def _world(view):
    cursor = _Cursor(view, "0x21df648", "0x21df66c")
    bst_id, level_id, level_hash = cursor.uint(2), cursor.uint(4), cursor.uint(4)
    join_only = bool(cursor.bits(1))
    count = cursor.bits(9, 256)
    flags = [bool(cursor.bits(1)) for _ in range(256)]
    cursor.finish()
    return {"bst_id": bst_id, "level_id": level_id, "level_hash": level_hash,
            "join_only": join_only, "active_count": count, "flags": flags,
            "canonical_inactive_tail": not any(flags[count:]),
            "semantic_scope": "native_fixed_states_not_daily_quests"}


def read(body, *, include_extended=False):
    """Return JSON-safe observed outfit/social status or raise ValueError.

    Exactly one bounded Actor parse is used. Raw strings, identifiers derived
    from account data, human clothing names and action decisions are not added.
    Extended energy codes/world states are opt-in for offline inspection only.
    No freshness, UUID/slot binding or authentication is asserted by this helper;
    the caller must enforce its existing room/session binding and expiry checks.
    """
    if type(include_extended) is not bool:
        raise ValueError("player_status_invalid_option")
    try:
        observation = inspect_actor_wire(body, verified_profile_sha256=PUBLIC_ELF_SHA256,
                                         check_zero_padding=False)
        view = _View(observation)
        result = {"schema_version": SCHEMA_VERSION, "profile": PROFILE,
                  "outfit": _outfit(view), "social": _social(view)}
        if include_extended:
            result["energy_codes"] = _energy(view)
            result["world_state"] = _world(view)
        return result
    except (ValueError, IndexError, KeyError, TypeError, struct.error, OverflowError):
        raise ValueError("player_status_invalid_actor") from None
