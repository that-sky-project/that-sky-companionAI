"""Network-free observations of current-native LevelData role messages.

Native Game8 Elect first decodes a fresh snapshot (1ef170c..17a8), then
reads LevelDataHeader (1ed3ea4) and its opaque record (1ed3de0). Game9
Revoke has the fixed three-byte owner/version body (1ed3f30). Game11 uses
the room's persistent LevelData receive context (1ef18ec..1990).

This module never elects a local authority, changes a native controller,
validates opaque record semantics, builds gameplay packets, or sends. All
raw records, network IDs, versions and room-generation objects stay in RAM.
Only metadata() is suitable for diagnostic output.
"""
from dataclasses import dataclass, field
import struct

from .bit_stream import BitReader
from .delta_sync import MAX_SNAPSHOT_BYTES, SnapshotReceiveState
from .game_binding import filter_current_peer_game
from .room_join import SessionMatch

MAX_LEVEL_RECORD_BYTES = 0x2000


def _uint(value, width, error):
    if type(value) is not int or not 0 <= value < (1 << width):
        raise ValueError(error)
    return value


@dataclass(frozen=True, repr=False)
class LevelRecord:
    """Opaque native slots; their business semantics are not inferred."""
    slot_0: int = field(repr=False)
    slot_2: int = field(repr=False)
    slot_4: int = field(repr=False)
    slot_8: int = field(repr=False)
    slot_a: int = field(repr=False)
    slot_c: int = field(repr=False)
    data: bytes = field(repr=False)

    def metadata(self):
        return {'record_bytes': len(self.data),
                'record_semantics_verified': False}


@dataclass(frozen=True, repr=False)
class LevelRoleObservation:
    """A parsed signal, not a grant of local authority or gameplay readiness."""
    kind: str
    sender_network_id: int = field(repr=False)
    owner_network_id: int = field(repr=False)
    level_id: int = field(repr=False)
    version: int = field(repr=False)
    include_record: bool
    record: LevelRecord | None = field(repr=False)
    snapshot_saved: bool

    def metadata(self):
        return {'kind': self.kind, 'room_bound': True, 'level_match': True,
                'sender_bound': True,
                'owner_binding_present': self.owner_network_id != 0,
                'include_record': self.include_record,
                'record_bytes': 0 if self.record is None else len(self.record.data),
                'record_semantics_verified': False,
                'snapshot_saved': self.snapshot_saved}


def _parse_level_header(data):
    """Exact header plus record; native raw fields realign after one bool.

    Header fixed part: owner u8, map u32LE, version u16LE, bool LSB.
    Record fixed part: u16/u8/u32/u8/u16/u16/u16 then length bytes. The
    enum's inclusive0..255 wrapper consumes exactly8 bits (1edc110).
    Native ignores the seven alignment padding bits; this parser does too.
    """
    reader = BitReader(data, max_bytes=MAX_SNAPSHOT_BYTES)
    owner = reader.read_raw(1)[0]
    level_id = struct.unpack('<I', reader.read_raw(4))[0]
    version = struct.unpack('<H', reader.read_raw(2))[0]
    include_record = reader.read_bool()
    slot_0 = struct.unpack('<H', reader.read_raw(2))[0]
    slot_2 = reader.read_raw(1)[0]
    slot_4 = struct.unpack('<I', reader.read_raw(4))[0]
    slot_8 = reader.read_u8()
    slot_a = struct.unpack('<H', reader.read_raw(2))[0]
    slot_c = struct.unpack('<H', reader.read_raw(2))[0]
    length = struct.unpack('<H', reader.read_raw(2))[0]
    if length > MAX_LEVEL_RECORD_BYTES:
        raise ValueError('level_record_too_large')
    record = LevelRecord(slot_0, slot_2, slot_4, slot_8, slot_a, slot_c,
                         reader.read_raw(length))
    reader.require_end()
    return owner, level_id, version, include_record, record


class LevelRoleReceiver:
    """Bind parsing to one verified PlayerSync, peer epoch and room lifetime.

    The caller must authenticate/decrypt the packet using the exact transport
    peer first. room_generation is an opaque unique object created when that
    peer/session starts; calls must pass the identical object. It is neither
    an account ID nor an authentication substitute. Recreate this receiver
    on any room/epoch/map/roster change, and never persist it.

    Local validation is deliberately stricter than native dispatch: a
    nonzero sender or owner must be in the verified same-map roster. ID0 is
    the native unassigned/server sentinel, not a fabricated player binding.
    Bad headers, unknown players and wrong maps do not advance the retained
    LevelData snapshot/ACK. Native updates the decoder before those semantic
    checks; this staging is an explicit local safety difference.
    """
    def __init__(self, session_match, *, room_generation, peer_epoch):
        if not isinstance(session_match, SessionMatch) or not session_match.ready:
            raise ValueError('unverified_role_session')
        _uint(peer_epoch, 8, 'invalid_role_peer_epoch')
        level_id = _uint(session_match.self_level_id, 32, 'invalid_role_level')
        own_id = _uint(session_match.self_net_player_id, 8, 'invalid_role_roster')
        roster = session_match.level_roster_ids
        if not level_id or not own_id or not roster or own_id not in roster:
            raise ValueError('unverified_role_roster')
        if any(type(net_id) is not int or not 1 <= net_id <= 255 for net_id in roster):
            raise ValueError('invalid_role_roster')
        if room_generation is None:
            raise ValueError('invalid_role_room_generation')
        self._room_generation = room_generation
        self._peer_epoch = peer_epoch
        self._level_id = level_id
        self._roster = roster
        self._level_receive_state = SnapshotReceiveState()

    def __repr__(self):
        return '<LevelRoleReceiver RAM-only>'

    @property
    def level_snapshot_state(self):
        """RAM-only ACK context; valid only for this bound room lifetime."""
        return self._level_receive_state

    def observe(self, game_payload, *, room_generation, peer_epoch):
        if room_generation is not self._room_generation:
            raise ValueError('role_room_generation_mismatch')
        _uint(peer_epoch, 8, 'invalid_role_peer_epoch')
        if peer_epoch != self._peer_epoch:
            raise ValueError('role_peer_epoch_changed')
        game_payload = filter_current_peer_game(game_payload, peer_epoch=peer_epoch)
        if game_payload is None:
            return None
        kind, _, sender = game_payload[:3]
        if kind not in (8, 9, 11):
            raise ValueError('unsupported_level_role_message')
        if sender and sender not in self._roster:
            raise ValueError('unverified_role_sender')
        body = game_payload[3:]
        if kind == 9:
            if len(body) != 3:
                raise ValueError('invalid_level_revoke')
            owner, version = struct.unpack('<BH', body)
            if owner and owner not in self._roster:
                raise ValueError('unverified_role_owner')
            return LevelRoleObservation('LevelData_Revoke', sender, owner,
                                        self._level_id, version, False, None, False)
        state = (SnapshotReceiveState() if kind == 8
                 else self._level_receive_state.fork())
        accepted = state.accept(body)
        if not accepted.data:
            return None
        owner, level_id, version, include_record, record = _parse_level_header(accepted.data)
        if level_id != self._level_id:
            return None
        if owner and owner not in self._roster:
            raise ValueError('unverified_role_owner')
        observation = LevelRoleObservation(
            'LevelData_Elect' if kind == 8 else 'LevelData_Snapshot',
            sender, owner, level_id, version, include_record, record,
            accepted.saved)
        if kind == 11:
            self._level_receive_state = state
        return observation
