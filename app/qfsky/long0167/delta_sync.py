"""Offline native snapshot acceptance and acknowledgment, without game actions.

Native 32faa94 reads a three-byte sequence/base/FNV8 header. 32f9374
expands zero runs and XORs a saved baseline; 32fb318 returns the last saved
sequence. Game14 acknowledgment (1ef3854) reports two receive contexts;
an incoming Game14 is instead an acknowledgment of *outgoing* contexts.
All buffers are RAM-only. This module has no sockets, logging or persistence.
It does not interpret a snapshot as account progress or claim map authority.
"""
from dataclasses import dataclass
import struct


MAX_SNAPSHOT_BYTES = 0x2420
MAX_BASELINES = 16


def _fnv8(data):
    value = 0x811c9dc5
    for byte in data:
        value = ((value ^ byte) * 0x01000193) & 0xffffffff
    return value & 0xff


def _xor_zero_runs(wire, baseline):
    diff = bytearray()
    offset = 0
    while offset < len(wire):
        byte = wire[offset]
        offset += 1
        if byte:
            diff.append(byte)
        else:
            if offset == len(wire) or wire[offset] == 0:
                raise ValueError('invalid_snapshot_zero_run')
            count = wire[offset]
            offset += 1
            if len(diff) + count > MAX_SNAPSHOT_BYTES:
                raise ValueError('snapshot_too_large')
            diff.extend(bytes(count))
        if len(diff) > MAX_SNAPSHOT_BYTES:
            raise ValueError('snapshot_too_large')
    # Native 32f9374 sets the new length to the expanded difference length.
    for index in range(min(len(diff), len(baseline))):
        diff[index] ^= baseline[index]
    return bytes(diff)


@dataclass(frozen=True, repr=False)
class AcceptedSnapshot:
    # No snapshots, sequence or player identifiers are exposed by metadata.
    data: bytes
    saved: bool

    def metadata(self):
        return {'snapshot_accepted': True, 'snapshot_bytes': len(self.data),
                'baseline_saved': self.saved}


class SnapshotReceiveState:
    """One native receive context; create a fresh object for each fresh room.

    Native initializer 32fa6c8 seeds sequence1 with an empty baseline. Saved
    nonzero-sequence frames retain at most16 baselines. A zero sequence is a
    transient delta and never changes the acknowledged saved sequence.
    Malformed/missing-base frames are rejected without advancing the state.
    Caller must first authenticate/decrypt and verify the enclosing room.
    """
    def __init__(self):
        self._baselines = [(1, b'')]
        self._raw_mode = False

    def __repr__(self):
        return '<SnapshotReceiveState RAM-only>'

    @property
    def acknowledgment(self):
        return self._baselines[-1][0]

    def fork(self):
        """Copy a RAM-only receive context for staged, atomic validation.

        Useful when the enclosing LevelData map/roster still needs checking:
        accept into this copy, then replace the caller's context only after
        all checks pass. The immutable byte buffers contain no public output.
        """
        state = SnapshotReceiveState()
        state._baselines = list(self._baselines)
        state._raw_mode = self._raw_mode
        return state

    def accept(self, frame):
        if type(frame) is not bytes or not 3 <= len(frame) <= MAX_SNAPSHOT_BYTES + 3:
            raise ValueError('invalid_snapshot_frame')
        sequence, base, checksum = frame[:3]
        wire = frame[3:]
        if _fnv8(wire) != checksum:
            raise ValueError('snapshot_checksum_mismatch')
        baselines = list(self._baselines)
        raw_mode = self._raw_mode
        if base == 0:
            data = wire
            # A nonzero raw keyframe resets the window while already in
            # differential mode (32fab20..a4). Zero raw sequence is transient.
            if sequence and not raw_mode:
                baselines = []
                raw_mode = True
        else:
            matches = [index for index, row in enumerate(baselines) if row[0] == base]
            if not matches:
                raise ValueError('snapshot_baseline_missing')
            index = matches[-1]  # native search selects the last matching row
            data = _xor_zero_runs(wire, baselines[index][1])
            raw_mode = False
            if sequence:
                # Drop baselines older than the exact one used by this frame.
                baselines = baselines[index:]
        if sequence:
            baselines.append((sequence, data))
            baselines = baselines[-MAX_BASELINES:]
        # Invalid input never commits a decoder state or an acknowledgment.
        self._baselines = baselines
        self._raw_mode = raw_mode
        return AcceptedSnapshot(data, sequence != 0)


def accept_player_state_delta(game_payload, *, receive_state):
    """Accept Game3 only after caller verifies/decrypts the matched room.

    Call ONLY after game_binding.filter_current_peer_game has accepted the
    exact incoming peer's epoch. Native1ef142c does not repeat the comparison,
    because its parent1ef26d4 already drops mismatches at1ef28a4..28b0. Do not
    confuse the epoch with the own player's level-change counter. This
    subtype parser does not parse entities or apply player-state changes.
    """
    if not isinstance(receive_state, SnapshotReceiveState):
        raise ValueError('invalid_snapshot_receive_state')
    if type(game_payload) is not bytes or len(game_payload) < 6 or game_payload[0] != 3:
        raise ValueError('invalid_player_state_delta')
    return receive_state.accept(game_payload[3:])


