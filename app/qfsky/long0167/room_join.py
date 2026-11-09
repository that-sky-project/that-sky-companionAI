from dataclasses import dataclass,field
import struct

def _uint(value, bits):
    if type(value) is not int or not 0 <= value < (1 << bits):
        raise ValueError('invalid_join_integer')
    return value

def _fixed(value, size):
    if type(value) is not bytes or len(value) != size:
        raise ValueError('invalid_join_bytes')
    return value

@dataclass(frozen=True, repr=False)
class SessionMatch:
    """Private binding values are RAM-only; only metadata() is safe to record."""
    player_count: int
    own_match: bool
    level_match: bool
    self_net_player_id: int | None = field(repr=False)
    self_level_id: int | None = field(repr=False)
    # Exact PlayerSync roster is private RAM-only protocol binding data. It is
    # deliberately absent from metadata and repr; not a list of account IDs.
    roster: tuple[tuple[int, int], ...] = field(default=(), repr=False)
    self_uuid: bytes | None = field(default=None, repr=False)

    @property
    def ready(self):
        return self.own_match and self.level_match

    @property
    def level_roster_ids(self):
        """Network IDs in the verified own map; no identifiers are logged."""
        if not self.ready:
            return frozenset()
        return frozenset(net_id for net_id, level_id in self.roster
                         if level_id == self.self_level_id)

    def metadata(self):
        return {'player_count': self.player_count, 'own_match': self.own_match,
                'level_match': self.level_match, 'ready': self.ready}

def parse_player_sync(payload, *, own_uuid, expected_level_id):
    """Strictly parse current NetMsg17, matching self UUID and verified map ID.

    A list without self, a malformed list or wrong map never declares ready.
    All tokens, UUIDs, source IDs and levels are excluded from public metadata.
    """
    _fixed(own_uuid, 16)
    if _uint(expected_level_id, 32) == 0:
        raise ValueError('unverified_expected_level')
    if type(payload) is not bytes or not payload:
        raise ValueError('invalid_player_sync')
    count = payload[0] & 15  # native fixed range 0..8 -> four LSB bits
    if count > 8 or len(payload) != 1 + count * 37:
        raise ValueError('invalid_player_sync')
    own = []
    roster = []
    seen_ids = set()
    for index in range(count):
        offset = 1 + index * 37
        net_id = payload[offset]
        if net_id in seen_ids:
            raise ValueError('duplicate_player_binding')
        seen_ids.add(net_id)
        level_id = struct.unpack_from('<I', payload, offset + 33)[0]
        roster.append((net_id, level_id))
        if payload[offset + 1:offset + 17] == own_uuid:
            own.append((net_id, level_id))
    if len(own) > 1:
        raise ValueError('duplicate_own_binding')
    if not own:
        return SessionMatch(count, False, False, None, None, tuple(roster))
    net_id, level_id = own[0]
    return SessionMatch(count, True, level_id == expected_level_id, net_id,
                        level_id, tuple(roster), own_uuid)
