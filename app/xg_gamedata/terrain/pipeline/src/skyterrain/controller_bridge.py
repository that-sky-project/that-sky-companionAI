"""Explicit OFFLINE controller-scope bridge; no loading, transport, or activation.

PoseController owns an opaque token, whereas BoundWorkerBackend requires the
exact prepared SceneScope. This bridge translates identity, never coordinates.
The owner must supply a fast, memory-only lifecycle provider and must perform
cold session prepare/close separately. No environment variable enables this.
"""
from __future__ import annotations
from dataclasses import dataclass
import threading

from .integration import CompatibleMotion, Decision
from .session import SceneScope, TerrainSession


@dataclass(frozen=True)
class ControllerLifecycle:
    scene: SceneScope
    target_key: str
    target_incarnation: int
    connected: bool
    target_fresh: bool

    def __post_init__(self):
        if not isinstance(self.scene, SceneScope):
            raise ValueError('scene_scope_required')
        if (type(self.target_key) is not str or not 0 < len(self.target_key) <= 128
                or any(ord(c) < 32 for c in self.target_key)):
            raise ValueError('target_key_required')
        if type(self.target_incarnation) is not int or self.target_incarnation < 0:
            raise ValueError('target_incarnation_required')
        if type(self.connected) is not bool or type(self.target_fresh) is not bool:
            raise ValueError('lifecycle_boolean_required')


class ControllerTerrainBridge:
    """Opt-in bridge for staged/offline controllers, not a live deployment gate.

    Usage after an explicit offline cold prepare:
        controller.attach_terrain(bridge, bridge.scope_key, prepared=True)

    Default disabled preserves the controller's legacy motion factory exactly.
    Enabled but unknown/stale/unprepared blocks without a legacy fallback.
    target_key must bind the full intended identity, and target_incarnation must
    change on slot reuse/ABA. Provider returns None (or false freshness/connection)
    for any unknown state. Neither a name nor a cached coordinate proves freshness.

    invalidate/detach revoke sample publication using local generations only;
    worker cleanup remains the cold TerrainSession owner's responsibility.
    """
    offline_only = True

    def __init__(self, session, lifecycle_provider, *, enabled=False, arrival_tolerance=.4):
        if not isinstance(session, TerrainSession) or session.mode != 'offline':
            raise ValueError('offline_session_required')
        if not callable(lifecycle_provider):
            raise TypeError('lifecycle_provider_required')
        if type(enabled) is not bool:
            raise ValueError('enabled_must_be_bool')
        self._session, self._provider = session, lifecycle_provider
        self._enabled, self._closed, self._social_owned = enabled, False, False
        self._lock = threading.RLock()
        self._generation = 0
        self._lifecycle = self._canonical = self._inner = self._opaque = None
        self._last_decision = None
        self._arrival_tolerance = arrival_tolerance

    @property
    def enabled(self):
        with self._lock:
            return self._enabled and not self._closed

    @property
    def last_decision(self):
        with self._lock:
            return self._last_decision

    def _retire_locked(self, reason):
        self._generation += 1
        inner = self._inner
        self._opaque = self._lifecycle = self._canonical = self._inner = None
        self._last_decision = Decision('unavailable', reason=reason)
        if inner is not None:
            inner.invalidate()  # TerrainIntegration: memory-only, never a worker RPC.

    def _refresh(self):
        with self._lock:
            if not self._enabled or self._closed:
                return None
        lifecycle = canonical = inner = None
        try:
            if self._session.mode != 'offline':
                raise ValueError('offline_mode_changed')
            value = self._provider()
            if isinstance(value, ControllerLifecycle) and value.connected and value.target_fresh:
                canonical = self._session.prepared_scope(value.scene)
                inner = self._session.prepared_adapter(value.scene)
                if canonical is not None and inner is not None and inner.enabled:
                    lifecycle = value
        except Exception:
            lifecycle = None
        with self._lock:
            if not self._enabled or self._closed:
                return None
            if lifecycle is None:
                if self._inner is not None or self._opaque is not None:
                    self._retire_locked('lifecycle_unavailable')
                return None
            if (self._lifecycle != lifecycle or self._canonical is not canonical or self._inner is not inner):
                self._retire_locked('lifecycle_replaced')
                self._lifecycle, self._canonical, self._inner = lifecycle, canonical, inner
            return self._lifecycle

    def scope_key(self):
        """Fast controller key; equal refreshed immutable values keep one token."""
        return self._refresh()

    def bind_scope(self, opaque):
        if opaque is None:
            raise ValueError('opaque_scope_required')
        current = self._refresh()
        with self._lock:
            if current is None or self._lifecycle != current or self._inner is None:
                self._last_decision = Decision('unavailable', reason='lifecycle_unavailable')
                return False
            # Every bind, even a reused opaque object, revokes the preceding plan.
            self._generation += 1
            self._opaque = opaque
            self._inner.bind_scope(self._canonical)
            self._inner.set_social_owned(self._social_owned)
            self._last_decision = None
            return True

    def invalidate(self):
        with self._lock:
            self._retire_locked('invalidated')

    def set_enabled(self, enabled):
        if type(enabled) is not bool:
            raise ValueError('enabled_must_be_bool')
        with self._lock:
            if self._closed and enabled:
                raise ValueError('bridge_detached')
            if self._enabled != enabled:
                self._retire_locked('activation_changed')
                self._enabled = enabled

    def set_social_owned(self, owned):
        if type(owned) is not bool:
            raise ValueError('social_owned_must_be_bool')
        with self._lock:
            if self._social_owned != owned:
                self._generation += 1
                self._social_owned = owned
                if self._inner is not None:
                    self._inner.set_social_owned(owned)
                self._last_decision = Decision('blocked', reason='social_owned') if owned else None

    def _current(self, ticket, opaque, lifecycle, inner, canonical):
        if self._refresh() != lifecycle:
            return False
        with self._lock:
            return (self._enabled and not self._closed and not self._social_owned
                    and ticket == self._generation and opaque is self._opaque
                    and inner is self._inner and canonical is self._canonical)

    def choose_motion(self, opaque, start, goal, reason, limits, legacy_factory, *, social_owns_movement=False):
        with self._lock:
            if self._closed:
                self._last_decision = Decision('unavailable', reason='bridge_detached')
                return None
            if social_owns_movement or self._social_owned:
                self._generation += 1
                if self._inner is not None:
                    self._inner.bind_scope(self._canonical)
                self._last_decision = Decision('blocked', reason='social_owned')
                return None
            disabled = not self._enabled
        if disabled:
            return legacy_factory()
        lifecycle = self._refresh()
        with self._lock:
            if lifecycle is None or opaque is None or opaque is not self._opaque or self._inner is None:
                self._last_decision = Decision('unavailable', reason='controller_scope_inactive')
                return None
            self._generation += 1
            ticket, inner, canonical = self._generation, self._inner, self._canonical
        try:
            # Never pass the controller's opaque token into BoundWorkerBackend.
            worker = inner.choose_motion(canonical, start, goal, reason, limits, lambda: None)
            if not self._current(ticket, opaque, lifecycle, inner, canonical):
                return None
            decision = inner.last_decision
            if worker is None or decision is None or decision.status != 'ready' or decision.motion is not worker:
                result = Decision(getattr(decision, 'status', 'unavailable'), reason=getattr(decision, 'reason', 'backend_unavailable'))
            else:
                motion = CompatibleMotion(worker,
                    lambda: self._current(ticket, opaque, lifecycle, inner, canonical),
                    arrival_tolerance=self._arrival_tolerance)
                result = Decision('ready', motion)
        except Exception:
            result = Decision('unavailable', reason='bridge_plan_failed')
        with self._lock:
            if (ticket != self._generation or self._closed or self._opaque is not opaque
                    or self._inner is not inner or self._social_owned):
                return None
            self._last_decision = result
            return result.motion if result.status == 'ready' else None

    def detach(self):
        """Permanently revoke this bridge; does not close somebody else's session."""
        with self._lock:
            self._closed, self._enabled = True, False
            self._retire_locked('bridge_detached')

    close = detach
