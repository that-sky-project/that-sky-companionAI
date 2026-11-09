"""Pure native ChangeLevelWithFadeRpc codec and local commit bookkeeping.

No I/O, hooks, queue mutation or network activity occurs at import. The caller
must supply *current* room/PlayerId bindings and independently verify a fresh,
accepted handhold/carrier relationship. A role is not encoded in this RPC.

libBootloader.so SHA256 0b81f1da54d9c6f28c08746b2ec19519d8eb78c4f1b3c5e4e420ff726dc25321:
serializer 0x1808c2c writes PlayerId, length-prefixed scene, two u32 values.
PlayerId serializer 0x1f7c218 selects byte/mapped-byte/u32 by stream mode.
Behavior constructor 0x1809900 initializes the two opaque values to zero;
0x180803c copies them into the RPC. Zero is the ordinary constructor default,
not an established parameter for every specially configured transition.

This is a generic ChangeLevel RPC, not restricted to GoHome. Receiver-side
relationship, local map access and actual follower loading remain independent
requirements. Local transport commit is NOT an application acknowledgment.
"""
from dataclasses import dataclass
import re
import struct


CHANGE_LEVEL_WITH_FADE_KEY = 0x4D84A062
NATIVE_ELF_SHA256 = "0b81f1da54d9c6f28c08746b2ec19519d8eb78c4f1b3c5e4e420ff726dc25321"
_SCENE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}\Z")


def _uint(value, bits, label):
    if type(value) is not int or not 0 <= value < (1 << bits):
        raise ValueError("invalid_" + label)
    return value


def encode_player_id(player_id, *, stream_mode, mapped_slot=None):
    """Encode an actual current PlayerId, requiring explicit stream semantics.

    Mode 1: raw u8. Mode 2: supplied current room mapping of native PlayerId
    to u8 (this pure function does not discover/guess it). Mode 3: raw u32.
    Mapping provenance/freshness must be checked by the caller each enqueue.
    """
    _uint(player_id, 32, "player_id")
    if type(stream_mode) is not int or stream_mode not in (1, 2, 3):
        raise ValueError("explicit_stream_mode_required")
    if stream_mode == 2:
        return bytes((_uint(mapped_slot, 8, "mapped_slot"),))
    if mapped_slot is not None:
        raise ValueError("mapping_only_valid_for_stream_mode_2")
    if stream_mode == 1:
        return bytes((_uint(player_id, 8, "player_id_for_stream_mode_1"),))
    return struct.pack("<I", player_id)


def build_change_level_body(*, leader_player_id, stream_mode, scene,
                            mapped_slot=None, tail0=0, tail1=0):
    if type(scene) is not str or not _SCENE.fullmatch(scene):
        raise ValueError("invalid_scene_identifier")
    raw = scene.encode("ascii")
    player = encode_player_id(leader_player_id, stream_mode=stream_mode,
                              mapped_slot=mapped_slot)
    return (player + struct.pack("<I", len(raw)) + raw +
            struct.pack("<II", _uint(tail0, 32, "opaque_tail0"),
                        _uint(tail1, 32, "opaque_tail1")))


def build_change_level_game(*, epoch, own_slot, leader_player_id, stream_mode,
                            scene, attachment_kind, mapped_slot=None,
                            tail0=0, tail1=0):
    """Return existing social_accept._game-compatible GameMsg bytes.

    Body PlayerId identifies the leader (the sender), never the carried peer.
    The caller must not select a stream mode merely to obtain a desired byte
    layout. In mode 2 mapped_slot must be the leader's current network slot.
    """
    _uint(epoch, 8, "epoch")
    _uint(own_slot, 8, "own_slot")
    if own_slot == 0:
        raise ValueError("own_slot_must_be_bound")
    if attachment_kind not in ("handhold", "carrier"):
        raise ValueError("unsupported_leader_attachment_kind")
    if stream_mode == 1 and leader_player_id != own_slot:
        raise ValueError("raw_byte_player_id_is_not_current_leader_slot")
    if stream_mode == 2 and mapped_slot != own_slot:
        raise ValueError("mapping_is_not_current_leader_slot")
    body = build_change_level_body(leader_player_id=leader_player_id,
        stream_mode=stream_mode, mapped_slot=mapped_slot, scene=scene,
        tail0=tail0, tail1=tail1)
    return (bytes((2, epoch, 0, own_slot)) +
            struct.pack("<IBH", CHANGE_LEVEL_WITH_FADE_KEY, 0, len(body)) + body)


def build_change_level_frame(frame_factory, **kwargs):
    """Construct qfsky.long0167.engine.Frame(14, payload, True, channel=0)."""
    if not callable(frame_factory):
        raise TypeError("frame_factory_required")
    return frame_factory(14, build_change_level_game(**kwargs), True, channel=0)


def build_current_slot_game(*, epoch, own_slot, scene, attachment_kind, wire_layout):
    """Encode the existing game's observed current-room byte-slot layout.

    This is NOT a claim about the native stream's internal mode. Its mode 1
    byte and mode 2 mapped-byte results are indistinguishable once the current
    room's resolved slot is known. No native PlayerId is invented. This API is
    for the existing _game protocol/receiver, not a generic mode-3 stream.
    The two opaque parameters use only the verified native default (0, 0).
    """
    if wire_layout != "room_slot_u8":
        raise ValueError("explicit_current_room_slot_wire_layout_required")
    # Byte layout equals mode-1 serialization, but no native mode is inferred.
    return build_change_level_game(epoch=epoch, own_slot=own_slot,
        leader_player_id=own_slot, stream_mode=1, scene=scene,
        attachment_kind=attachment_kind)


def build_current_slot_frame(frame_factory, **kwargs):
    if not callable(frame_factory):
        raise TypeError("frame_factory_required")
    return frame_factory(14, build_current_slot_game(**kwargs), True, channel=0)


@dataclass
class LocalCommitWatch:
    """Observe one exact Frame on one exact engine, after original commit.

    Owner-loop integration calls on_commit_returned ONLY after the real
    engine.commit succeeds. The caller rechecks request revision, current
    room/epoch/own_slot, deadline and attachment and supplies current=True.
    This watch neither queues nor sends, and deliberately offers no retry.
    Only 'committed' permits scheduling the leader's own transition; it is
    never evidence of a follower's arrival or an ENet/application ACK.
    """
    engine: object
    frame: object
    state: str = "pending"

    def _contains(self, group):
        return sum(item is self.frame for item in getattr(group, "frames", ()))

    def on_commit_returned(self, engine, group, sent_count, *, current):
        if self.state != "pending":
            return False
        if engine is not self.engine or not self._contains(group):
            return False
        if current is not True:
            self.state = "revoked_after_send"
            return False
        if (type(sent_count) is not int or sent_count != len(group.frames)
                or self._contains(group) != 1):
            self.state = "delivery_unknown"
            return False
        self.state = "committed"
        return True

    def on_commit_failed(self, engine, group):
        if self.state == "pending" and engine is self.engine and self._contains(group):
            self.state = "delivery_unknown"

    def revoke(self):
        if self.state == "pending":
            self.state = "revoked"

