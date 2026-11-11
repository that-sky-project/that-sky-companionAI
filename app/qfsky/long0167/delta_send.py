"""Offline current-native snapshot writer; no sockets or game operations.

Current build421629 independently checked equivalents: update33cba94,
frame33cc1d4, XOR/RLE33cafbc, acknowledgment33cc438. The older addresses
below are retained provenance, not the current APK addresses. See the
managed CURRENT_WRAPPER_REVIEW_421629.md for the instruction evidence.

Recovered original functions: init32f97d4, update32f9bc0, frame32fa300,
XOR/RLE32f90e8, acknowledgment32fa564. This module treats a caller's already
verified serialized state as opaque RAM-only bytes. It neither constructs a
PlayerState nor asserts that an arbitrary byte string is a valid game state.
"""
from .delta_sync import MAX_BASELINES, MAX_SNAPSHOT_BYTES, _fnv8


def _encode_delta(current, baseline):
    # The native XOR output length is the current length, never the larger
    # baseline length. New trailing bytes are copied unchanged.
    difference = bytearray(current)
    for index in range(min(len(difference), len(baseline))):
        difference[index] ^= baseline[index]
    wire = bytearray()
    offset = 0
    while offset < len(difference):
        byte = difference[offset]
        if byte:
            wire.append(byte)
            offset += 1
        else:
            count = 1
            while (count < 255 and offset + count < len(difference)
                   and difference[offset + count] == 0):
                count += 1
            wire.extend((0, count))
            offset += count
    return bytes(wire)


def _next_saved_sequence(previous):
    # Native32f9db8..9dc8: skip zero, retain 1 after 255 wraps.
    sequence = (previous + 1) & 255
    return sequence or 1


class SnapshotSendState:
    """One RAM-only writer for one room/context, with explicit received ACKs.

    The original normal-period parameter is10 for own PlayerState. Normal
    mode saves one nonzero-sequence snapshot every10 updates; intervening
    updates use transient sequence0. Native init seeds saved sequence1 with
    an empty baseline. An unacknowledged16-row window switches to raw mode;
    raw mode produces one saved frame every5 updates until a matching ACK.

    update() takes bytes from a complete, verified serializer. build_frame()
    returns snapshot bytes only, without encryption, app framing or sending.
    An ACK must come from the authenticated current room's Game14 message;
    do not accept unbound user input as an acknowledgment.
    """
    def __init__(self, keyframe_period=10, *, require_ack=False):
        # Native zero disables periodic saves in differential mode. The
        # game's observed10 is supported; reject1 rather than emulate ARM's
        # divide-by-zero raw-mode corner case with misleading cadence.
        if (type(keyframe_period) is not int or not 0 <= keyframe_period <= 0xffffffff
                or keyframe_period == 1):
            raise ValueError('invalid_snapshot_keyframe_period')
        if type(require_ack) is not bool:
            raise ValueError('invalid_snapshot_ack_policy')
        self._period = keyframe_period
        self._ticks = 0
        self._baselines = [(1, b'')]
        self._raw_mode = False
        self._pending = None
        # Optional transport policy, separate from the recovered native cadence.
        # In this mode even the implicit empty baseline is not used until an
        # emitted saved frame is acknowledged by this authenticated lifecycle.
        self._require_ack = require_ack
        self._emitted = []
        self._confirmed = None

    def __repr__(self):
        return '<SnapshotSendState RAM-only>'

    def metadata(self):
        return {'snapshot_writer': True, 'pending_frame': self._pending is not None,
                'raw_mode': self._raw_mode, 'retained_baselines': len(self._baselines),
                'ack_required': self._require_ack,
                'acknowledged_baseline': self._confirmed is not None}

    def fork(self):
        """Independent RAM-only staging copy; reject packet build atomically."""
        state = SnapshotSendState(self._period, require_ack=self._require_ack)
        state._ticks = self._ticks
        state._baselines = list(self._baselines)
        state._raw_mode = self._raw_mode
        state._pending = self._pending
        state._emitted = list(self._emitted)
        state._confirmed = self._confirmed
        return state

    def update(self, serialized_state):
        """Stage one native update, preserving atomicity on invalid input."""
        if type(serialized_state) is not bytes or len(serialized_state) > MAX_SNAPSHOT_BYTES:
            raise ValueError('invalid_serialized_snapshot')
        baselines = list(self._baselines)
        raw_mode = self._raw_mode
        ticks = self._ticks
        pending = self._pending
        if len(baselines) >= MAX_BASELINES:
            if raw_mode:
                # Exact current build, not an assumed last-eight ring:
                # 32f9ca8..9cb8 copies7 records (0xfd18 bytes) from row8
                # over rows0..6, leaves old row7, then sets count8. This
                # can repeat saved sequence IDs; acknowledge() deliberately
                # resolves the last exact match as the native writer does.
                baselines = baselines[8:15] + [baselines[7]]
            else:
                baselines = []
                ticks = 0
                raw_mode = True
        if raw_mode:
            save = bool(self._period and ticks % (self._period // 2) == 0)
            if save:
                sequence = _next_saved_sequence(baselines[-1][0]) if baselines else 1
                pending = (sequence, serialized_state)
                baselines.append(pending)
            # Otherwise the native writer does not replace a pending update.
        else:
            save = bool(self._period and ticks % self._period == 0)
            if save:
                sequence = _next_saved_sequence(baselines[-1][0]) if baselines else 1
                pending = (sequence, serialized_state)
                baselines.append(pending)
            else:
                pending = (0, serialized_state)
        if self._require_ack:
            # A wrapped saved sequence starts a fresh raw synchronization window.
            # Do not let pre-wrap retained rows satisfy the new window's ACK.
            if (pending is not None and pending is not self._pending
                    and pending[0] == 1 and baselines
                    and self._baselines and self._baselines[-1][0] == 255):
                baselines = [pending]
                raw_mode = True
            self._emitted = [row for row in self._emitted
                             if any(row is retained for retained in baselines)]
            if not any(self._confirmed is row for row in baselines):
                self._confirmed = None
        self._baselines = baselines
        self._raw_mode = raw_mode
        self._ticks = (ticks + 1) & 0xffffffff
        self._pending = pending

    def build_frame(self, maximum_frame_bytes=MAX_SNAPSHOT_BYTES):
        """Consume a pending native sequence/base/checksum/data frame offline.

        Reject insufficient output capacity without consuming pending data.
        This is deliberately stricter than the native encoder's zero-byte
        overflow return; a truncated snapshot must never be transmitted.
        Empty native snapshots can be represented by their3-byte header.
        """
        if (type(maximum_frame_bytes) is not int
                or not 3 <= maximum_frame_bytes <= MAX_SNAPSHOT_BYTES + 3):
            raise ValueError('invalid_snapshot_frame_capacity')
        if self._pending is None:
            return None
        sequence, current = self._pending
        if self._raw_mode or (self._require_ack and self._confirmed is None):
            base = 0
            wire = current
        else:
            if not self._baselines:
                raise ValueError('snapshot_writer_baseline_missing')
            base, baseline = self._baselines[0]
            wire = _encode_delta(current, baseline)
        frame = bytes((sequence, base, _fnv8(wire))) + wire
        if len(frame) > maximum_frame_bytes:
            raise ValueError('snapshot_frame_too_large')
        if self._require_ack and sequence:
            row = next((row for row in reversed(self._baselines)
                        if row is self._pending), None)
            if row is not None and not any(row is sent for sent in self._emitted):
                self._emitted.append(row)
        self._pending = None
        return frame

    def acknowledge(self, sequence):
        """Retain the exact acknowledged saved baseline and newer snapshots.

        Unknown/expired/transient sequence numbers have no effect, matching
        native32fa564. A valid ACK exits raw mode. The last match wins after
        sequence wrap. Caller supplies the received private sequence in RAM.
        """
        if type(sequence) is not int or not 0 <= sequence <= 255:
            raise ValueError('invalid_snapshot_acknowledgment')
        matches = [index for index, row in enumerate(self._baselines) if row[0] == sequence]
        if not matches:
            return False
        row = self._baselines[matches[-1]]
        if self._require_ack and not any(row is sent for sent in self._emitted):
            return False
        self._baselines = self._baselines[matches[-1]:]
        self._raw_mode = False
        self._confirmed = row
        self._emitted = [sent for sent in self._emitted
                         if any(sent is retained for retained in self._baselines)]
        return True
