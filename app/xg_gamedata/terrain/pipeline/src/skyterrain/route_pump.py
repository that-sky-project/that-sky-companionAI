"""Opt-in OFFLINE route orchestration; never installs a live game integration.

Commands only replace an in-memory intent and revoke its lease. One designated
controller thread calls tick(); expensive geometry preparation runs exclusively
in RouteScheduler. The existing controller still owns actual Actor serialization.
"""
from dataclasses import dataclass
import math
import threading
import time

from .controller_bridge import ControllerLifecycle, ControllerTerrainBridge
from .locomotion_gate import OfflineGroundOnlyGate
from .route_scheduler import RouteScheduler
from .session import TerrainSession
from .tracker_lifecycle import TrackerLifecycleProvider

_CONTROLLER_OWNERS = threading.Lock()


@dataclass(frozen=True)
class PumpStatus:
    status: str
    reason: str
    request_id: int = 0
    offline_only: bool = True


@dataclass(frozen=True)
class _Intent:
    goal: tuple
    speed: float
    reason: str


class GateBoundLifecycleProvider:
    """Rechecks permission at ALL bridge boundaries, including sample replies."""
    def __init__(self, provider, gate, *, clock=time.monotonic):
        if not isinstance(provider, TrackerLifecycleProvider) or not callable(clock):
            raise TypeError('tracker_provider_and_clock_required')
        if not isinstance(gate, OfflineGroundOnlyGate):
            raise TypeError('offline_ground_gate_required')
        if provider.expected_engine is not gate._engine:
            raise ValueError('gate_tracker_engine_mismatch')
        self.provider, self.gate, self.clock = provider, gate, clock
        self._engine = provider.expected_engine

    def __call__(self):
        try:
            if self.provider.expected_engine is not self._engine or self.gate._engine is not self._engine:
                return None
            value = self.provider()
            if not isinstance(value, ControllerLifecycle):
                return None
            permission = self.gate.permit(value, self.clock())
            if (permission.allowed is True and permission.live_permitted is False
                    and permission.mode == 'offline'
                    and self.provider.expected_engine is self._engine
                    and self.gate._engine is self._engine):
                return value
        except Exception:
            pass
        return None


class OfflineRoutePump:
    """Latest-intent route pump for an already prepared offline session/bridge.

    This does not attach/enable a controller, load assets, choose a foot offset,
    infer native contact, or start a game. submit/cancel/close can be called by a
    command thread; tick must stay on one controller thread. New commands revoke
    the old motion lease immediately; controller cleanup occurs on its next tick
    or guarded Actor sample, not by touching controller state on command threads.
    """
    def __init__(self, session, backend, controller, bridge, lifecycle, *,
                 enabled=False, preparation_ttl=2.):
        if not isinstance(session, TerrainSession) or session.mode != 'offline':
            raise ValueError('offline_session_required')
        if not isinstance(bridge, ControllerTerrainBridge):
            raise TypeError('controller_bridge_required')
        if bridge._session is not session:
            raise ValueError('bridge_session_mismatch')
        if not isinstance(lifecycle, GateBoundLifecycleProvider) or bridge._provider is not lifecycle:
            raise ValueError('gate_bound_bridge_required')
        if type(enabled) is not bool:
            raise ValueError('enabled_must_be_bool')
        self.session, self.backend = session, backend
        self.controller, self.bridge, self.lifecycle = controller, bridge, lifecycle
        self._owner_token = None
        if enabled:
            with _CONTROLLER_OWNERS:
                if getattr(controller, '_offline_route_pump_owner', None) is not None:
                    raise ValueError('controller_already_owned_by_route_pump')
                self._owner_token = object()
                controller._offline_route_pump_owner = self._owner_token
        try:
            # No injected/shared scheduler: cancellation ownership is exclusive.
            self._scheduler = RouteScheduler(backend, enabled=enabled, ttl=preparation_ttl)
        except Exception:
            self._release_controller()
            raise
        self._enabled, self._closed = enabled, False
        self._lock = threading.Lock()
        self._request = 0
        self._intent = None
        self._applied = None
        self._binding = None
        self._motion = None
        self._tick_thread = None
        self._tick_busy = threading.Lock()
        self._status = PumpStatus('idle' if enabled else 'disabled', 'not_requested')

    @property
    def scheduler(self):
        return self._scheduler

    @property
    def status(self):
        with self._lock:
            return self._status

    def _publish(self, request_id, status, reason):
        with self._lock:
            if request_id == self._request:
                self._status = PumpStatus(status, reason, request_id)
            return self._status

    def _current(self, request_id):
        with self._lock:
            return not self._closed and self._enabled and request_id == self._request

    def submit(self, goal, *, speed=3.6, reason='offline_route', force_new=False):
        if (not isinstance(goal, (tuple, list)) or len(goal) != 3
                or any(type(v) not in (int, float) or not math.isfinite(v) for v in goal)):
            raise ValueError('finite_goal_required')
        if type(speed) not in (int, float) or not math.isfinite(speed) or not 0 < speed <= 10:
            raise ValueError('speed_out_of_range')
        if type(reason) is not str or len(reason) > 128 or any(ord(c) < 32 for c in reason):
            raise ValueError('reason_invalid')
        if type(force_new) is not bool:
            raise ValueError('force_new_must_be_bool')
        intent = _Intent(tuple(float(v) for v in goal), float(speed), reason)
        with self._lock:
            if self._closed or not self._enabled:
                return self._status
            if not force_new and intent == self._intent:
                return self._status
            self._request += 1
            request = self._request
            self._intent = intent
            self._status = PumpStatus('pending', 'awaiting_controller_tick', request)
            # Event revocation only; serialize it with intent publication so a
            # late command-thread cancel cannot revoke a newer tick's prepare.
            self.scheduler.cancel()
        return self.status

    def cancel(self):
        with self._lock:
            if self._closed or not self._enabled:
                return self._status
            self._request += 1
            self._intent = None
            self._status = PumpStatus('stopped', 'cancelled', self._request)
            self.scheduler.cancel()
        return self.status

    def close(self):
        with self._lock:
            self._closed = True
            self._request += 1
            self._intent = None
            self._status = PumpStatus('closed', 'closed', self._request)
            self.scheduler.close()
        self._release_controller()

    def _release_controller(self):
        with _CONTROLLER_OWNERS:
            if (self._owner_token is not None
                    and getattr(self.controller, '_offline_route_pump_owner', None) is self._owner_token):
                self.controller._offline_route_pump_owner = None
            self._owner_token = None

    def wait_closed(self, timeout=2.5):
        """Cold shutdown only. Never call from an Actor callback."""
        return self.scheduler.wait_closed(timeout)

    def _stop_owned(self, reason):
        motion = self._motion
        self._motion = None
        if motion is not None:
            self.controller._terrain_failed(reason, expected=motion)
        self._binding = None

    def _refuse(self, request_id, reason):
        self.scheduler.cancel()
        self._stop_owned(reason)
        self._applied = request_id
        return self._publish(request_id, 'unavailable', reason)

    def tick(self):
        if not self._tick_busy.acquire(False):
            return PumpStatus('unavailable', 'controller_tick_busy')
        try:
            return self._tick()
        finally:
            self._tick_busy.release()

    def _tick(self):
        thread = threading.get_ident()
        with self._lock:
            if self._tick_thread is None:
                self._tick_thread = thread
            if self._tick_thread != thread:
                raise RuntimeError('single_controller_thread_required')
            request, intent = self._request, self._intent
            active = self._enabled and not self._closed
        if not active or intent is None:
            self._stop_owned('route_cancelled')
            return self.status
        life = self.lifecycle()
        if self.session.mode != 'offline' or life is None:
            return self._refuse(request, 'ground_permission_or_lifecycle_unavailable')
        c = self.controller
        if (getattr(c, '_terrain_adapter', None) is not self.bridge
                or not self.bridge.enabled or not getattr(c, '_terrain_prepared', False)):
            return self._refuse(request, 'terrain_attachment_unavailable')
        if c._terrain_social_owns():
            return self._refuse(request, 'social_owns_movement')
        canonical = self.session.prepared_scope(life.scene)
        if canonical is None:
            return self._refuse(request, 'scene_not_prepared')
        adapter = self.session.prepared_adapter(canonical)
        if adapter is None or adapter._backend is not self.backend:
            return self._refuse(request, 'prepared_backend_mismatch')
        if request != self._applied:
            self._stop_owned('route_replaced')
            if c.motion is not None:
                return self._refuse(request, 'other_motion_owns_controller')
            c._terrain_invalidate(clear_prepared=False)
            c._terrain_sync()
            if not self._current(request) or self.lifecycle() != life:
                return self._refuse(request, 'request_changed_before_preparation')
            start = tuple(c.position)
            try:
                generation = self.scheduler.submit(canonical,
                    (life.target_key, life.target_incarnation), start, intent.goal,
                    speed=intent.speed, force_new=True)
            except Exception:
                return self._refuse(request, 'preparation_request_rejected')
            if not self._current(request):
                return self._refuse(request, 'request_changed_during_submission')
            if generation is None:
                return self._refuse(request, 'preparation_not_enabled')
            self._binding = (life, canonical, start, generation, c._terrain_request_seq)
            self._applied = request
            return self._publish(request, 'pending', 'cold_preparation')
        binding = self._binding
        if binding is None:
            return self.status  # Finished/refused requests need an explicit new submit.
        old_life, old_scope, start, generation, operation = binding
        if old_life != life or canonical is not old_scope:
            return self._refuse(request, 'bound_identity_changed')
        if self._motion is not None:
            if c.motion is self._motion:
                return self._publish(request, 'moving', 'controller_owns_motion')
            # Do not cancel a replacement movement or claim successful arrival.
            self.scheduler.cancel()
            self._motion = self._binding = None
            return self._publish(request, 'stopped', 'controller_motion_ended_or_replaced')
        if c.motion is not None or tuple(c.position) != start or c._terrain_request_seq != operation:
            return self._refuse(request, 'controller_changed_during_preparation')
        try:
            prepared = self.scheduler.poll(generation)
        except Exception:
            return self._refuse(request, 'preparation_poll_failed')
        if prepared is None:
            return self.status
        if prepared.status != 'ready':
            return self._refuse(request, prepared.reason or 'preparation_failed')
        if not self._current(request) or self.lifecycle() != life:
            return self._refuse(request, 'request_changed_before_start')
        try:
            started = c.walk_to(intent.goal, intent.reason, speed=intent.speed)
        except Exception:
            return self._refuse(request, 'controller_start_failed')
        self._motion = c.motion if started else None
        if not started:
            return self._refuse(request, 'controller_rejected_prepared_route')
        if not self._current(request) or self.lifecycle() != life:
            return self._refuse(request, 'request_changed_during_start')
        return self._publish(request, 'moving', 'prepared_route_started')
