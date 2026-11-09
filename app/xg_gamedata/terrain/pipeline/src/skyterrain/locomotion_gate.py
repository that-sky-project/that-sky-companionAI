"""Explicit offline ground-only test gate; no native/live permission producer.

This consumes two memory-only, subject-bound observations. Native diagnostic
bytes can justify a stop, never a ground permit: !InAir is not ground contact.
Only explicitly synthetic offline support may exercise the positive path.
Neither a successful decision nor a synthetic flag attests game physics,
contact, calibration, network delivery, or a production-ready movement mode.
"""
from __future__ import annotations
from dataclasses import dataclass, field
import math
import threading
from .controller_bridge import ControllerLifecycle
from .session import SceneScope

NATIVE_DIAGNOSTIC = 'native_diagnostic'
SYNTHETIC_OFFLINE_SUPPORT = 'synthetic_offline_support'


def _finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _key(value):
    return (type(value) is str and 0 < len(value) <= 128
            and not any(ord(c) < 32 for c in value))


@dataclass(frozen=True)
class LocomotionObservation:
    """Immutable input envelope; data still needs the strict gate validation.

    engine is an opaque identity reference, never a stored pointer/address.
    capture_start/end use the SAME host monotonic clock as permit(now).
    Ground/air/water meanings must be supplied by a separately established
    producer. No flag in this envelope turns native diagnostics into proof.
    """
    role: str
    subject_key: str
    incarnation: int
    scene: SceneScope
    engine: object
    capture_start: float
    capture_end: float
    source: str
    ground_contact: bool | None
    in_air: bool | None
    in_water: bool | None
    touch_water: bool | None
    unsupported_surface: bool | None


@dataclass(frozen=True)
class LocomotionPair:
    own: LocomotionObservation
    target: LocomotionObservation


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    status: str
    reason: str
    synthetic_only: bool = False
    mode: str = field(default='offline', init=False)
    live_permitted: bool = field(default=False, init=False)


class OfflineGroundOnlyGate:
    """No I/O, no worker operation, no automatic activation or live permit.

    observations must be a bounded memory-only supplier returning LocomotionPair.
    Native positive support is deliberately UNIMPLEMENTED. All native inputs
    remain unavailable/blocked even when a caller sets ground_contact=True.
    Call permit again at submit, poll, start, and publication boundaries; the
    returned value is a snapshot, not a reusable permission token.
    """
    offline_only = True
    live_permitted = False

    def __init__(self, observations, *, mode, expected_engine, own_key,
                 own_incarnation, ttl=.5):
        if type(mode) is not str or mode != 'offline':
            raise ValueError('explicit_offline_only')
        if not callable(observations):
            raise TypeError('memory_observation_supplier_required')
        if expected_engine is None or not _key(own_key):
            raise ValueError('own_identity_required')
        if type(own_incarnation) is not int or own_incarnation < 0:
            raise ValueError('own_incarnation_required')
        if not _finite(ttl) or not 0 < ttl <= .5:
            raise ValueError('ttl_out_of_range')
        self._observations, self._engine = observations, expected_engine
        self._own_key, self._own_incarnation, self._ttl = own_key, own_incarnation, float(ttl)
        self._lock = threading.Lock()
        self._generation, self._closed, self._last_now = 0, False, None

    @staticmethod
    def _deny(reason, *, blocked=False):
        return GateDecision(False, 'blocked' if blocked else 'unavailable', reason)

    def close(self):
        # The supplier is never called under this lock, so closure is memory-only.
        with self._lock:
            self._closed = True
            self._generation += 1

    def _check(self, pair, lifecycle, now):
        if (type(lifecycle) is not ControllerLifecycle
                or lifecycle.connected is not True or lifecycle.target_fresh is not True):
            return self._deny('lifecycle_unavailable')
        if type(pair) is not LocomotionPair:
            return self._deny('observations_missing')
        if self._own_key == lifecycle.target_key:
            return self._deny('own_target_not_distinct')
        for role, observation, key, incarnation in (
                ('own', pair.own, self._own_key, self._own_incarnation),
                ('target', pair.target, lifecycle.target_key, lifecycle.target_incarnation)):
            if type(observation) is not LocomotionObservation:
                return self._deny(role + '_observation_missing')
            if (observation.role != role or type(observation.role) is not str
                    or not _key(observation.subject_key) or observation.subject_key != key
                    or type(observation.incarnation) is not int or observation.incarnation != incarnation
                    or observation.engine is not self._engine
                    or type(observation.scene) is not SceneScope or observation.scene != lifecycle.scene):
                return self._deny(role + '_binding_mismatch')
            start, end = observation.capture_start, observation.capture_end
            if (not _finite(start) or not _finite(end) or not 0 <= start <= end <= now
                    or now - start > self._ttl):
                return self._deny(role + '_observation_stale_or_future')
            if type(observation.source) is not str or observation.source not in (NATIVE_DIAGNOSTIC, SYNTHETIC_OFFLINE_SUPPORT):
                return self._deny(role + '_producer_unknown')
            values = {name: getattr(observation, name) for name in (
                'ground_contact', 'in_air', 'in_water', 'touch_water', 'unsupported_surface')}
            if any(value is not None and type(value) is not bool for value in values.values()):
                return self._deny(role + '_state_malformed')
            for name in ('in_air', 'in_water', 'touch_water', 'unsupported_surface'):
                if values[name] is True:
                    return self._deny(role + '_' + name, blocked=True)
            if any(values[name] is None for name in ('in_air', 'in_water', 'touch_water', 'unsupported_surface')):
                return self._deny(role + '_state_unknown')
            if values['ground_contact'] is False:
                return self._deny(role + '_no_support', blocked=True)
            if values['ground_contact'] is None:
                return self._deny(role + '_support_unknown')
            if observation.source != SYNTHETIC_OFFLINE_SUPPORT:
                return self._deny(role + '_native_support_unverified')
        return GateDecision(True, 'ready', 'synthetic_offline_ground_only', synthetic_only=True)

    def permit(self, lifecycle, now):
        """Evaluate fresh own AND target evidence without implicit state inference."""
        if not _finite(now) or now < 0:
            return self._deny('invalid_monotonic_time')
        if not self._lock.acquire(False):
            return self._deny('gate_busy')
        try:
            if self._closed:
                return self._deny('gate_closed')
            if self._last_now is not None and now < self._last_now:
                return self._deny('clock_regression')
            self._last_now = now
            self._generation += 1  # A later evaluation revokes an older in-flight read.
            ticket = self._generation
        finally:
            self._lock.release()
        try:
            result = self._check(self._observations(), lifecycle, now)
        except Exception:
            result = self._deny('observation_supplier_failed')
        if not self._lock.acquire(False):
            return self._deny('gate_busy')
        try:
            if self._closed or ticket != self._generation:
                return self._deny('gate_revoked_during_read')
            return result
        finally:
            self._lock.release()