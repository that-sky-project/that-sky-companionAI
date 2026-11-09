"""Prepared passive observer; importing this module installs nothing.

The caller may append an explicitly armed instance to existing SNAPSHOT_HOOKS.
Never start another game connection or re-decode deltas with a competing receiver.
finish() returns a whitelist report; export it outside the networking callback.
"""
import math
import threading
import time

from project_hop_actor_v20 import project_actor, target_body

# Local v20.1 correction. The deployed v20 freeze is deliberately unchanged.
OBSERVER_REVISION = 'accepted-deltas-v20.1-local'


def _binding(tracker, target):
    if not tracker.lock.acquire(blocking=False):
        raise ValueError('binding_busy')
    try:
        engine = tracker._engine
        if not tracker.ready or engine is None or getattr(engine, '_closed', False):
            raise ValueError('room_not_ready')
        if isinstance(target, str) and '-' in target:
            slots = [s for s, identity in tracker.slots.items() if identity == target]
        else:
            try:
                s = int(str(target), 16 if str(target).lower().startswith('0x') else 10)
                slots = [s] if s in tracker.slots else []
            except (ValueError, TypeError):
                slots = []
        if len(slots) != 1:
            raise ValueError('target_not_unique')
        slot = slots[0]
        identity = tracker.slots[slot]
        if identity == tracker.own_uuid:
            raise ValueError('target_is_self')
        version = tracker._slot_versions.get(slot)
        if version is None:
            raise ValueError('target_version_missing')
        return (engine, getattr(engine, '_player', None), tracker.epoch,
                tracker.room_generation, tracker.confirmed_map_id, slot, identity, version)
    finally:
        tracker.lock.release()


class PassiveHopSnapshot:
    def __init__(self, tracker, target, anim_pose, *, duration=20.0, maximum_hz=10.0,
                 clock=time.monotonic):
        if not (math.isfinite(duration) and 0 < duration <= 20):
            raise ValueError('duration_bound')
        if not (math.isfinite(maximum_hz) and 1 <= maximum_hz <= 20):
            raise ValueError('rate_bound')
        self.tracker, self.target, self.anim_pose = tracker, target, anim_pose
        self.duration, self.interval, self.clock = duration, 1.0 / maximum_hz, clock
        self.lock = threading.Lock()
        self.binding = None
        self.started = None
        self.rows = []
        self.stop_reason = None
        self.next_at = 0.0
        self.accepted_callbacks = self.saved_callbacks = self.transient_callbacks = 0
        self.target_missing_callbacks = 0
        self.errors = 0
        self.skipped_rate = self.skipped_sequence = 0
        self.max_decode_ms = 0.0

    def arm(self):
        with self.lock:
            if self.started is not None:
                raise ValueError('already_armed')
            self.binding = _binding(self.tracker, self.target)
            if self.binding[1] is None:
                raise ValueError('receiver_missing')
            self.started = self.clock()
            self.next_at = self.started

    def __call__(self, data, *, epoch=None, receive_state=None,
                 snapshot_sequence=None, snapshot_source=None):
        # Nonblocking: this passive observer never queues network callbacks.
        if not self.lock.acquire(blocking=False):
            return
        try:
            now = self.clock()
            if self.started is None or self.stop_reason is not None:
                return
            if now - self.started >= self.duration:
                self.stop_reason = 'duration_complete'
                return
            try:
                current = _binding(self.tracker, self.target)
            except ValueError as exc:
                if str(exc) == 'binding_busy':
                    return
                self.stop_reason = 'binding_changed'
                return
            if current != self.binding:
                self.stop_reason = 'binding_changed'
                return
            if receive_state is not self.binding[1] or epoch != self.binding[2]:
                return
            if (type(snapshot_sequence) is not int or not 0 <= snapshot_sequence <= 255
                    or type(snapshot_source) is not int or not 0 <= snapshot_source <= 255):
                self.skipped_sequence += 1
                return
            # SNAPSHOT_HOOKS supplies the existing receiver's accepted.data.
            # seq=0 is a valid transient frame, NOT a rejected/old snapshot.
            # Nonzero values name saved ACK baselines, not a universal clock;
            # they may repeat/reset. Detachment-evidence ordering must NOT be
            # reused to thin ordinary position/animation observations.
            self.accepted_callbacks += 1
            if snapshot_sequence:
                self.saved_callbacks += 1
            else:
                self.transient_callbacks += 1
            if now + 1e-9 < self.next_at:
                self.skipped_rate += 1
                return
            body = target_body(data, self.binding[5])
            if body is None:
                self.target_missing_callbacks += 1
                return
            start = self.clock()
            projected = project_actor(body, self.anim_pose)
            self.max_decode_ms = max(self.max_decode_ms, (self.clock() - start) * 1000)
            try:
                unchanged = _binding(self.tracker, self.target) == self.binding
            except ValueError as exc:
                if str(exc) == 'binding_busy':
                    return
                unchanged = False
            if not unchanged:
                self.stop_reason = 'binding_changed'
                return
            self.next_at = now + self.interval
            self.rows.append({'relative_seconds': round(now - self.started, 6),
                              'receive_ordinal': self.accepted_callbacks,
                              'snapshot_saved': snapshot_sequence != 0, **projected})
            if len(self.rows) >= 400:
                self.stop_reason = 'sample_bound'
            # Avoid repeatedly imposing heavy parsing on the network loop.
            if self.max_decode_ms > 50:
                self.stop_reason = 'decode_budget_exceeded'
        except Exception:
            self.errors += 1
            if self.errors >= 3:
                self.stop_reason = 'projection_errors'
        finally:
            self.lock.release()

    def finish(self):
        with self.lock:
            if self.started is None:
                raise ValueError('not_armed')
            elapsed = self.clock() - self.started
            if self.stop_reason is None:
                self.stop_reason = 'duration_complete' if elapsed >= self.duration else 'stopped_early'
            return {'schema': 'anonymous-single-target-hop-observation-v20',
                    'observer_revision': OBSERVER_REVISION,
                    'subject': 'test_player', 'map_id': self.binding[4],
                    'duration_requested_seconds': self.duration,
                    'elapsed_seconds': round(elapsed, 6), 'stop_reason': self.stop_reason,
                    'samples': list(self.rows), 'count': len(self.rows),
                    'projection_errors': self.errors, 'skipped_sequence': self.skipped_sequence,
                    'skipped_sequence_semantics': 'invalid_metadata_only',
                    'accepted_snapshot_callbacks': self.accepted_callbacks,
                    'saved_snapshot_callbacks': self.saved_callbacks,
                    'transient_snapshot_callbacks': self.transient_callbacks,
                    'target_missing_callbacks': self.target_missing_callbacks,
                    'skipped_rate': self.skipped_rate, 'max_decode_ms': self.max_decode_ms,
                    'identity_exported': False, 'raw_actor_exported': False,
                    'jump_codes_verified': False,
                    'interpretation': 'Accepted snapshot arrival order, not a native simulation clock. Numeric observations only; operator labels must identify takeoff, apex and landing.'}

    def detach(self, hooks):
        # Explicit caller operation; no monkeypatching or connection lifecycle change.
        while self in hooks:
            hooks.remove(self)
