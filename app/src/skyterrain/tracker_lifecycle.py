"""Memory-only RoomTracker identity/freshness binding for the offline bridge.

This binds a previously prepared scene to one exact room engine and one peer
incarnation. It neither prepares terrain nor infers ground/air/water state. A
fresh position is not a ground-contact or native-physics calibration claim.
"""
from __future__ import annotations

import math
import time
import uuid

from .controller_bridge import ControllerLifecycle
from .session import SceneScope


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _identity(value):
    if type(value) is not str or len(value) != 36:
        return None
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return None
    # The tracker stores canonical complete UUIDs, not display names/prefixes.
    return value if str(parsed) == value else None


class TrackerLifecycleProvider:
    """Fail-closed, nonblocking snapshot of a real staged RoomTracker.

    Construction is explicit and cold; do not automatically rebind expected_engine
    or scene when a room changes. Calls perform no I/O or worker operations. The
    target supplier must be a memory-only callback returning a full UUID or slot.
    A busy tracker, timestamp inconsistency, duplicate identity, stale observation,
    missing incarnation, or changed room makes the lifecycle unavailable.
    """

    def __init__(self, tracker, scene, target_supplier, *, expected_engine,
                 freshness_seconds=.5, monotonic=time.monotonic):
        if not isinstance(scene, SceneScope):
            raise ValueError('scene_scope_required')
        if expected_engine is None:
            raise ValueError('expected_engine_required')
        if not callable(target_supplier) or not callable(monotonic):
            raise TypeError('memory_callbacks_required')
        if not _finite(freshness_seconds) or not 0 < freshness_seconds <= 2:
            raise ValueError('freshness_seconds_out_of_range')
        self.tracker, self.scene = tracker, scene
        self.expected_engine = expected_engine
        self.target_supplier, self.monotonic = target_supplier, monotonic
        self.freshness_seconds = freshness_seconds
        self.last_reason = 'not_observed'

    @classmethod
    def from_controller(cls, controller, scene, *, expected_engine, **kwargs):
        def target():
            follow = controller.follow
            return follow.get('target') if isinstance(follow, dict) else None
        return cls(controller.tracker, scene, target,
                   expected_engine=expected_engine, **kwargs)

    def _reject(self, reason):
        self.last_reason = reason
        return None

    def __call__(self):
        lock = None
        acquired = False
        try:
            selector = self.target_supplier()
            if type(selector) not in (str, int):
                return self._reject('target_missing')
            lock = self.tracker.lock
            acquired = lock.acquire(False)
            if not acquired:
                return self._reject('tracker_busy')
            value = self._read_locked(selector)
        except Exception:
            return self._reject('tracker_unavailable')
        finally:
            if acquired:
                lock.release()
        # Do not publish a snapshot made for a replaced follow request.
        try:
            after = self.target_supplier()
            if type(after) is not type(selector) or after != selector:
                return self._reject('target_changed_during_read')
        except Exception:
            return self._reject('target_unavailable')
        return value

    def _current_room_actor(self, slot, scene, engine):
        # Explicit candidate-follow capability. A fresh position alone is not
        # evidence: only the engine's current private snapshot receiver records
        # this tuple. This does not add the peer to level-authority/terrain lists.
        t = self.tracker
        if getattr(t, '_follow_room_presence_v20', False) is not True:
            return False
        proof = getattr(t, '_room_actor_presence_v20', {}).get(slot)
        if type(proof) is not tuple or len(proof) != 9:
            return False
        owner, receiver, epoch, room, map_id, peer, incarnation, wall, mono = proof
        origin, observed = t.origins.get(slot), t.position_observed.get(slot)
        return (owner is engine and receiver is not None and receiver is getattr(engine, '_player', None)
                and epoch == t.epoch == engine._epoch and room == t.room_generation == scene.room_generation
                and map_id == t.confirmed_map_id == t.pending_map_id == scene.scene_hash
                and peer == t.slots.get(slot) and incarnation == t._slot_versions.get(slot)
                and type(incarnation) is int and incarnation >= 0
                and isinstance(origin, (tuple, list)) and len(origin) == 4
                and isinstance(observed, (tuple, list)) and len(observed) == 2
                and _finite(wall) and _finite(mono) and origin[3] == wall == observed[0]
                and mono == observed[1])

    def _read_locked(self, selector):
        t, scene, engine = self.tracker, self.scene, self.expected_engine
        if (t.ready is not True or t._engine is not engine
                or type(t.confirmed_map_id) is not int
                or t.confirmed_map_id != scene.scene_hash
                or type(t.pending_map_id) is not int
                or t.pending_map_id != scene.scene_hash
                or type(t.room_generation) is not int
                or t.room_generation != scene.room_generation):
            return self._reject('room_unavailable')
        match = engine._match
        if (type(engine._map) is not int or engine._map != scene.scene_hash
                or getattr(engine, '_closed', None) is not False
                or getattr(match, 'ready', None) is not True
                or type(match.self_level_id) is not int
                or match.self_level_id != scene.scene_hash
                or type(t.epoch) is not int or not 0 <= t.epoch <= 255
                or type(engine._epoch) is not int or engine._epoch != t.epoch):
            return self._reject('engine_unavailable')
        requested_uuid = _identity(selector)
        if requested_uuid is not None:
            matches = [slot for slot, peer in t.slots.items() if peer == requested_uuid]
            if len(matches) != 1:
                return self._reject('target_missing_or_ambiguous')
            slot = matches[0]
        else:
            try:
                if type(selector) is int:
                    slot = selector
                elif selector and selector == selector.strip():
                    slot = int(selector, 16 if selector.lower().startswith('0x') else 10)
                else:
                    raise ValueError('invalid_slot')
            except (TypeError, ValueError):
                return self._reject('target_selector_invalid')
        if type(slot) is not int or not 0 <= slot <= 0xffffffff:
            return self._reject('target_slot_invalid')
        terrain_slots, level_slots = t._terrain_slots, match.level_roster_ids
        if (type(terrain_slots) not in (set, frozenset)
                or type(level_slots) not in (set, frozenset)
                or slot not in terrain_slots or slot not in level_slots):
            if not self._current_room_actor(slot, scene, engine):
                return self._reject('target_scene_unverified')
        peer = _identity(t.slots.get(slot))
        if peer is None or sum(value == peer for value in t.slots.values()) != 1:
            return self._reject('target_missing_or_ambiguous')
        incarnation = t._slot_versions.get(slot)
        if type(incarnation) is not int or incarnation < 0:
            return self._reject('target_incarnation_missing')
        origin, observed = t.origins.get(slot), t.position_observed.get(slot)
        if (not isinstance(origin, (tuple, list)) or len(origin) != 4
                or not isinstance(observed, (tuple, list)) or len(observed) != 2
                or not all(_finite(v) for v in (*origin, *observed))
                or origin[3] != observed[0]):
            return self._reject('position_observation_invalid')
        now = self.monotonic()
        if not _finite(now) or not 0 <= now - observed[1] <= self.freshness_seconds:
            return self._reject('position_stale_or_future')
        # Engine closure/match replacement is not owned by tracker.lock.
        if engine._closed is not False or engine._match is not match:
            return self._reject('engine_changed_during_read')
        self.last_reason = 'fresh'
        return ControllerLifecycle(scene, f'{peer}@slot={slot}', incarnation, True, True)
