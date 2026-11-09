"""Explicit bounded pilot motion adapter, not a renamed offline session/gate.

The backend is a pure offline JSONL geometry calculator. Permission is supplied
only by pilot_contract.Admission plus fresh, BOTH-subject observations. This
module contains no network/account/SSH/device code and never starts a runner.
"""
from concurrent.futures import ThreadPoolExecutor
import math
import threading
import time

from pilot_contract import check_seal, finite, immutable, require, vec
from skyterrain.backend import RouteLease, STAGED_PROFILE, validate_follow_intent, verified_motion_step_height
from skyterrain.controller_bridge import ControllerLifecycle
from skyterrain.integration import CompatibleMotion, Decision, MotionSample


class ObservationStore:
    """Memory-only store. Producer capture times MUST use the server clockDomain.

    A remote device monotonic timestamp is not accepted as server time. A future
    remote producer needs a reviewed clock-bound or challenge/roundtrip adapter.
    """
    def __init__(self):
        self._lock = threading.Lock()
        self._value = None
        self._sequence = -1

    def publish(self, bundle):
        require(type(bundle) is dict and type(bundle.get('sequence')) is int, 'observation_sequence')
        with self._lock:
            require(bundle['sequence'] > self._sequence, 'observation_replay')
            self._sequence = bundle['sequence']
            self._value = immutable(bundle)

    def snapshot(self):
        if not self._lock.acquire(False):
            return None
        try:
            return self._value
        finally:
            self._lock.release()

    def clear(self):
        with self._lock:
            self._value = None


def inside_patch(admission, position, role='bot'):
    p = vec(position)
    offset = admission.bot_offset if role == 'bot' else admission.target_offset
    feet = tuple(a-b for a, b in zip(p, offset))
    patch = admission.patch
    if not patch['minFeetY'] <= feet[1] <= patch['maxFeetY']:
        return False
    polygon, signs = patch['polygonXZ'], []
    for i, a in enumerate(polygon):
        b = polygon[(i+1) % len(polygon)]
        dx, dz = b[0]-a[0], b[1]-a[1]
        cross = dx*(feet[2]-a[1])-dz*(feet[0]-a[0])
        if abs(cross) / math.hypot(dx, dz) < patch['supportMarginMeters']:
            return False
        signs.append(cross >= 0)
    return all(s == signs[0] for s in signs)


class _BudgetedMotion:
    supports_deadline = True
    # Engineering core.mjs DEFAULTS.fixedDt, not a native game parameter.
    FIXED_DT = 1. / 60.
    INITIAL_CONTACT_WINDOW = 6. * FIXED_DT
    # Experimental startup only; existing KCC still decides actual contact.
    INITIAL_SETTLE_WINDOW = .25
    INITIAL_SETTLE_DROP = .35
    INITIAL_SETTLE_GRAVITY = 18.  # Pinned engineering core gravity, not native.
    def __init__(self, runtime, raw, operation):
        self.runtime, self.raw, self.operation = runtime, raw, operation
        for name in ('a', 'b', 't0', 'seconds', 'speed', 'heading', 'final_heading', 'desired_goal'):
            if hasattr(raw, name):
                setattr(self, name, getattr(raw, name))
        self.last, self.distance = tuple(raw.a), 0.
        self.horizontal_distance = 0.
        # Continuous speed credit is never reset by a same-motor retarget.
        self.speed_started, self.speed_distance = raw.t0, 0.
        self.ground_initialized = False
        self.request_id = runtime._request_id
        self.last_time = raw.t0
        self.halted = False

    def sample(self, now, *, deadline=None):
        r = self.runtime
        elapsed, dt = max(0., now-self.t0), max(0., now-self.last_time)
        proposed, velocity, grounded = self.last, (0., 0., 0.), None
        def stopped(reason):
            self.halted = True
            # One diagnostic per actual motion generation. Cancelled old motion
            # callbacks must not overwrite diagnostics for a newer request.
            if r._request_id == self.request_id:
                old = getattr(r, 'last_stop_diagnostic', None)
                if old is None or old['operation'] != self.operation:
                    r.last_stop_diagnostic = dict(operation=self.operation, reason=reason,
                        start=list(self.a), current=list(self.last), proposed=list(proposed),
                        grounded=grounded, dt=dt, elapsed=elapsed, velocity=list(velocity),
                        engineering_fixed_dt=self.FIXED_DT,
                        initial_contact_window=self.INITIAL_CONTACT_WINDOW)
            return MotionSample(self.last, blocked=True, reason=reason)
        if self.halted or not r._valid(self.operation):
            return stopped('pilot_permission_revoked')
        if getattr(self.raw, 'supports_deadline', False):
            result = self.raw.sample(now, deadline=deadline)
        else:
            result = self.raw.sample(now)
        if not r._valid(self.operation):
            return stopped('pilot_permission_changed_during_sample')
        proposed, velocity, grounded = vec(result.position), vec(result.velocity), result.grounded
        if result.blocked:
            return stopped('pilot_worker_reported_blocked')
        if result.arrived and (math.hypot(proposed[0]-self.b[0], proposed[2]-self.b[2]) > .12001
                or abs(proposed[1]-self.b[1]) >= .35001):
            return stopped('pilot_arrival_axis_mismatch')
        distance = math.dist(proposed, self.last)
        horizontal = math.hypot(proposed[0]-self.last[0], proposed[2]-self.last[2])
        if not inside_patch(r.admission, proposed):
            return stopped('pilot_patch_boundary')
        # Worker consumes accumulated time in 1/60 s steps. A callback can
        # consume one step carried from the previous callback, but cumulative
        # horizontal travel may never outrun elapsed time at the same .25 cap.
        if (self.distance+distance > r.admission.max_distance + 1e-9
                or self.speed_distance+horizontal > r.admission.max_speed*max(0., now-self.speed_started) + 1e-6
                or horizontal > r.admission.max_speed*(dt+self.FIXED_DT) + 1e-6
                or (grounded is not True and math.hypot(velocity[0], velocity[2]) > r.admission.max_speed + 1e-6)):
            return stopped('pilot_horizontal_motion_budget')
        # The experimental worker pins DEFAULTS: stepHeight=.3, slope=42deg,
        # skin=.015. At 3 m/s a 120 ms GROUNDED slope/step can legitimately
        # change Y by more than the old slow-pilot .1 m per-callback bound.
        # Keep strict pilots/airborne samples unchanged. This only accepts a
        # collision-checked grounded result, within the step OR actual-travel
        # slope envelope; it does not project Y or bypass the worker collision.
        # A v14 cold+hot receipt can seal a three-phase-preflighted .6 step/snap
        # for this one motion. Ordinary motions still use the default .3 limit.
        vertical_limit = .1
        if r.admission.mode == 'experimental_ground_trial' and grounded is True:
            vertical_limit = max(verified_motion_step_height(self.raw) + 2*.015,
                                 horizontal*math.tan(math.radians(42.)) + 2*.015)
        initial_trial = (r.admission.mode == 'experimental_ground_trial'
                         and not self.ground_initialized)
        downward = self.a[1] - proposed[1]
        settling = (initial_trial and grounded is not True and elapsed <= self.INITIAL_SETTLE_WINDOW + 1e-9
                    and 0. <= downward <= self.INITIAL_SETTLE_DROP
                    and -self.INITIAL_SETTLE_GRAVITY * (elapsed + self.FIXED_DT) - 1e-6 <= velocity[1] <= 0.)
        if initial_trial and grounded is not True and downward > self.INITIAL_SETTLE_DROP:
            return stopped('pilot_vertical_correction_budget')
        if initial_trial and grounded is not True and elapsed > self.INITIAL_SETTLE_WINDOW + 1e-9:
            return stopped('pilot_initial_ground_timeout')
        if not settling and (abs(proposed[1]-self.last[1]) > vertical_limit
                             or (grounded is not True and abs(velocity[1]) > 1.)):
            return stopped('pilot_vertical_correction_budget')
        if grounded is not True:
            if self.ground_initialized:
                return stopped('pilot_ground_contact_lost')
            contact_window = self.INITIAL_SETTLE_WINDOW if settling else self.INITIAL_CONTACT_WINDOW
            if elapsed >= contact_window:
                return stopped('pilot_initial_ground_timeout')
            # Hold the published Actor at its prior position with ZERO velocity
            # until initial contact is established. Only the internal worker
            # advances, within the already checked spatial/time budgets.
            return MotionSample(self.last, grounded=False, heading=result.heading)
        self.ground_initialized = True
        self.last, self.last_time = proposed, now
        self.distance += distance
        self.horizontal_distance += horizontal
        self.speed_distance += horizontal
        return result


class BoundedPilotRuntime:
    """Opt-in controller adapter and cold planner. Default is disabled.

    install/tick run on one designated controller thread; request/cancel may be
    command-thread calls. Caller must continue normal social/controller ticks.
    shutdown is cold. The existing runner is not modified/imported here.
    """
    hop_follow_seam_v20 = True

    def __init__(self, admission, backend, controller, lifecycle, observations, *,
                 simulation=False, enabled=False, clock=time.monotonic, wall=time.time):
        check_seal(admission, simulation)
        require(type(enabled) is bool and callable(lifecycle) and callable(clock), 'explicit_runtime_arguments')
        require(isinstance(observations, ObservationStore), 'observation_store_required')
        require(getattr(backend.transport, 'offline', False) is True
                and getattr(backend.transport, 'worker_profile', None) == STAGED_PROFILE,
                'pure_imported_nav_backend_required')
        require(tuple(backend.offset) == admission.bot_offset, 'bot_offset_backend_mismatch')
        self.admission, self.backend, self.controller = admission, backend, controller
        self.lifecycle, self.observations = lifecycle, observations
        self.simulation, self.enabled, self.clock, self.wall = simulation, enabled, clock, wall
        self._lock = threading.RLock()
        self._generation, self._scope, self._key = 0, None, None
        self._closed, self._social, self._attached = False, False, False
        self._owner, self._executor = object(), None
        self._future = self._lease = self._prepared = self._intent = self._motion = None
        self._follow_intent = None
        self._request_id = 0
        self._future_request = None
        self._motion_operation = None
        self._thread = None
        self.last_decision = Decision('unavailable', reason='pilot_not_attached')
        self.started = self.deadline = None
        self.last_permission_reason = 'not_observed'
        self._invalidation_reason = 'not_bound'
        self._permission_fault = None

    def _deny(self, reason):
        self.last_permission_reason = reason
        return None

    def _permission(self):
        try:
            if not self.enabled: return self._deny('disabled')
            if self._closed: return self._deny('closed')
            if self.wall() >= self.admission.expires_wall: return self._deny('admission_expired')
            now = self.clock()
            if self.deadline is not None and now >= self.deadline: return self._deny('deadline_expired')
            life = self.lifecycle()
            if not isinstance(life, ControllerLifecycle):
                return self._deny(getattr(self.lifecycle, 'last_reason', 'lifecycle_unavailable'))
            if not life.connected or not life.target_fresh: return self._deny('lifecycle_not_fresh')
            bundle = self.observations.snapshot()
            if bundle is None:
                return self._deny(getattr(self.observations, 'last_reason',
                                         getattr(self.lifecycle, 'last_reason', 'observation_unavailable')))
            # Observation may advance while snapshot reads the UDP tracker.
            # Compare against a clock sampled AFTER that read, not an earlier now.
            now = self.clock()
            if self.deadline is not None and now >= self.deadline: return self._deny('deadline_expired')
            ctx = self.admission.context
            if (life.scene.scene_name != ctx['sceneName'] or life.scene.scene_hash != ctx['sceneHash']
                    or life.scene.room_generation != ctx['roomGeneration']):
                return self._deny('scope_scene_changed')
            if (life.target_key != f"{ctx['target']['uuid']}@slot={ctx['target']['slot']}"
                    or life.target_incarnation != ctx['target']['incarnation']):
                return self._deny('target_identity_changed')
            if bundle.get('context') != ctx or bundle.get('certificateSHA256') != self.admission.certificate_sha:
                return self._deny('observation_binding_changed')
            if bundle.get('evidenceKind') != ('simulation' if self.simulation else
                    'engineering_state' if self.admission.mode == 'experimental_ground_trial' else 'recorded_real'):
                return self._deny('observation_kind_changed')
            start, end = bundle.get('captureStart'), bundle.get('captureEnd')
            if not finite(start) or not finite(end) or not start <= end <= now or not 0 <= now-start <= .5:
                return self._deny('observation_stale_or_future')
            experimental = self.admission.mode == 'experimental_ground_trial'
            for role in ('bot', 'target'):
                subject = bundle.get(role)
                if subject is None or subject.get('identity') != ctx[role]:
                    return self._deny(role+'_identity_changed')
                if (subject.get('supportKind') != 'empirical_provisional_patch'
                        or subject.get('locomotion') != 'dry_ground'
                        or (not experimental and any(type(subject.get(k)) is not int or subject[k] != 0
                               for k in ('air', 'water', 'touchWater', 'surf')))
                        or not inside_patch(self.admission, subject.get('position'), role)):
                    return self._deny(role+'_ground_scope_invalid')
            if math.dist(vec(bundle['bot']['position']), vec(self.controller.position)) > .1:
                return self._deny('own_pose_changed_during_read')
            self.last_permission_reason = 'ready'
            return life
        except Exception:
            return self._deny('permission_read_failed')

    def scope_key(self):
        return self._permission()

    def terrain_ground(self, x, z, near_y=None):
        """Bound scene's real ground in Actor-origin coordinates; None if unavailable.

        This only reads the static-world surface loaded with the same asset as the
        walking worker. It does not sample the worker, advance time, or relax any
        admission check, so the Actor thread may call it directly.
        """
        if getattr(self, '_closed', False) or self._scope is None:
            return None
        method = getattr(self.backend, 'terrain_ground', None)
        return method(x, z, near_y) if callable(method) else None

    def install(self):
        require(self.enabled and not self._attached and self._permission() is not None,
                'pilot_admission_not_current')
        require(getattr(self.controller, '_offline_route_pump_owner', None) is None
                and getattr(self.controller, '_bounded_pilot_owner', None) is None,
                'controller_already_owned')
        self.backend.acquire_route_scheduler(self._owner)
        try:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='bounded-pilot-cold')
            self.controller._bounded_pilot_owner = self._owner
            self.started = self.clock()
            self.deadline = self.started + 3600.
            self.controller.attach_terrain(self, self.scope_key, prepared=True)
            self._attached = True
        except Exception:
            if getattr(self.controller, '_bounded_pilot_owner', None) is self._owner:
                self.controller._bounded_pilot_owner = None
            self.backend.release_route_scheduler(self._owner)
            if self._executor:
                self._executor.shutdown(wait=False, cancel_futures=True)
            raise

    def bind_scope(self, opaque):
        with self._lock:
            self._generation += 1
            self._scope, self._key = opaque, self._permission()
            return opaque is not None and self._key is not None

    def invalidate(self):
        with self._lock:
            self._generation += 1
            self._scope = self._key = None
            self._invalidation_reason = 'scope_invalidated'

    def set_social_owned(self, owned):
        require(type(owned) is bool, 'social_flag')
        with self._lock:
            if owned != self._social:
                self._generation += 1
            self._social = owned

    def _valid(self, operation):
        with self._lock:
            return (not self._closed and not self._social and self.enabled
                    and self._generation == operation and self._scope is not None
                    and self._key is not None and self._permission() == self._key)

    def choose_motion(self, scope, start, goal, reason, limits, legacy_factory,
                      *, social_owns_movement=False):
        # NEVER invoke legacy_factory, including disabled/closed/error cases.
        with self._lock:
            self.last_decision = Decision('unavailable', reason='pilot_not_prepared')
            pending = self._prepared
            if (pending is None or scope is not self._scope or social_owns_movement
                    or self._social or self._permission() != self._key):
                return None
            request_id, life, old_start, old_goal, speed = pending
            if request_id != self._request_id:
                return None
            if tuple(start) != old_start or tuple(goal) != old_goal or limits.get('speed') != speed:
                return None
            self._prepared = None
            operation = self._generation
        result = self.backend.plan_move(life.scene, start, goal, reason, limits)
        if result.status != 'ready' or not self._valid(operation):
            return None
        budgeted = _BudgetedMotion(self, result.motion, operation)
        kwargs = ({'arrival_tolerance': math.hypot(.12, .35)+1e-5}
                  if self.admission.mode == 'experimental_ground_trial' else {})
        motion = CompatibleMotion(budgeted, lambda: self._valid(operation), **kwargs)
        self._motion = motion
        self._motion_operation = operation
        self.last_decision = Decision('ready', motion)
        return motion

    def try_retarget(self, goal):
        """Actor-only continuous follow update; never samples or replaces a motor."""
        co = getattr(self, "_follow_hop", None)
        if co is not None and co.suppress_ground_replan:
            self.last_retarget_reason = "hop_owns_motion"
            return False
        self.last_retarget_reason = 'retarget_not_current'
        c, motion = self.controller, self._motion
        if (self.admission.mode != 'experimental_ground_trial' or motion is None
                or threading.get_ident() != self._thread or not c.follow
                or c._terrain_social_owns() or c.motion is not motion
                or getattr(c, '_terrain_motion', None) is not motion
                or not self._valid(self._motion_operation)):
            return False
        if not motion._lock.acquire(blocking=False):
            return False
        try:
            budgeted = motion._worker
            raw = budgeted.raw
            sample = motion._sample
            start, goal = vec(c.position), vec(goal)
            if (motion._halted or motion._interrupted.is_set() or budgeted.halted
                    or sample.arrived or sample.blocked or sample.grounded is not True
                    or not budgeted.ground_initialized or tuple(sample.position) != start
                    or tuple(budgeted.last) != start or raw._now != budgeted.last_time
                    or motion._last_now != budgeted.last_time or self._permission() != self._key):
                return False
            distance = math.dist(start, goal)
            segment = self.admission.max_requested_distance or self.admission.max_distance
            if distance > segment:
                goal = tuple(a+(b-a)*((segment-1e-6)/distance) for a,b in zip(start,goal))
            if not inside_patch(self.admission, goal) or math.dist(start,goal)<.1:
                return False
            reply = self.backend.retarget_motion(raw, start, goal, self.admission.max_speed)
            if reply.get('status') != 'ready':
                self.last_retarget_reason = reply.get('reason','retarget_unavailable')
                return False
            if (c.motion is not motion or getattr(c,'_terrain_motion',None) is not motion
                    or c._terrain_social_owns() or not c.follow
                    or not self._valid(self._motion_operation) or vec(c.position) != start):
                self.cancel('retarget_permission_changed')
                return False
            projected = vec(reply['goal'])
            if not inside_patch(self.admission, projected) or reply['path_length']>self.admission.max_distance+1e-6:
                self.cancel('retarget_reply_bounds')
                return False
            # Exact last published timestamp, NOT current wall/Actor time. The
            # next sample retains the accumulated fixed-step time/debt.
            anchor = budgeted.last_time
            for obj in (raw, budgeted, motion):
                obj.a, obj.b = start, projected
                obj.desired_goal = goal
                obj.t0, obj.seconds, obj.speed = anchor, reply['seconds'], reply['speed']
            motion.planned_goal = projected
            self.backend._desired_goal = goal
            budgeted.distance = budgeted.horizontal_distance = 0.
            # Preserve speed_started/speed_distance, all cached sample clocks,
            # velocity, grounded state, heading and cancellation generations.
            self.last_retarget_reason = 'ready'
            self.last_retarget_distance = math.dist(start,projected)
            return True
        finally:
            motion._lock.release()

    def request(self, goal, *, speed=None, follow_intent=None):
        goal = vec(goal)
        speed = self.admission.max_speed if speed is None else speed
        require(self._attached and self._permission() is not None, 'pilot_observation_unavailable')
        require(finite(speed) and 0 < speed <= self.admission.max_speed, 'pilot_speed')
        start = vec(self.controller.position)
        require(inside_patch(self.admission, start) and inside_patch(self.admission, goal)
                and math.dist(start, goal) <= (self.admission.max_requested_distance or self.admission.max_distance), 'pilot_route_bounds')
        if follow_intent is not None:
            require(self.admission.mode == 'experimental_ground_trial' and self.controller.follow
                    and not self.controller._terrain_social_owns(), 'experimental_follow_required')
            follow_intent = validate_follow_intent(follow_intent,
                segment_limit=self.admission.max_requested_distance or self.admission.max_distance)
            require(inside_patch(self.admission, follow_intent['goal'])
                    and inside_patch(self.admission, follow_intent['peer']), 'follow_intent_patch')
        with self._lock:
            self.cancel('request_replaced')
            self._intent = (self._request_id, goal, float(speed))
            self._follow_intent = (self._request_id, follow_intent) if follow_intent is not None else None
            return self._request_id

    def cancel(self, reason='pilot_cancelled'):
        co = getattr(self, "_follow_hop", None)
        if co is not None and co.suppress_ground_replan:
            if co.active and reason in ("user_cancel", "follow_stopped") and co.request_stop():
                return  # Keep the existing lease until true contact.
            co.hard_cancel(reason)
        # No RPC or controller mutation on the command thread. Existing motion's
        # guard becomes false immediately; actual Actor commit enforces zero.
        with self._lock:
            self._generation += 1
            self._request_id += 1
            self._intent = self._prepared = None
            self._follow_intent = None
            if self._lease is not None:
                self._lease.cancel()
            self._invalidation_reason = reason
            self.last_decision = Decision('blocked', reason=reason)

    def tick(self):
        thread = threading.get_ident()
        if self._thread is None:
            self._thread = thread
        require(thread == self._thread, 'single_controller_thread_required')
        co = getattr(self, '_follow_hop', None)
        if co is not None and co.active:
            return 'hopping'  # Dedicated Actor publisher owns all hop samples.
        c = self.controller
        if self._permission() is None:
            reason = self.last_permission_reason
            if self._permission_fault != reason:
                self.cancel('lifecycle_'+reason)
            self._permission_fault = reason
        else:
            self._permission_fault = None
        if self._motion is not None:
            if c.motion is not self._motion:
                # Normal arrival or a controller-owned stop is not revocation.
                self._motion = None
            elif not self._valid(self._motion_operation):
                reason = self._invalidation_reason or 'permission_revoked'
                c._terrain_failed('pilot_'+reason, expected=self._motion)
                self._motion = None
        # At most one cold job exists. A replacement intent waits until the old
        # cancelled job returns; it cannot consume that job's prepared token.
        if self._future is not None:
            if not self._future.done():
                return 'preparing'
            future, request_id = self._future, self._future_request
            self._future = None
            try:
                prepared = future.result()
            except Exception:
                prepared = Decision('unavailable', reason='cold_prepare_failed')
            self.last_prepare_decision = prepared
            with self._lock:
                pending = self._prepared
                current = request_id == self._request_id
            if current:
                if (pending is None or prepared.status != 'ready'
                        or self._permission() != pending[1]
                        or vec(c.position) != pending[2] or not self._lease.unconsumed_valid):
                    self.cancel(prepared.reason or 'prepare_stale')
                    return 'blocked_prepare_stale'
                # Cancellation can still happen here. choose_motion rechecks
                # request id, generation and scope before/after the hot plan.
                from hop_follow_runtime_v20 import is_hop_selection, activate_runtime_hop
                if is_hop_selection(prepared):
                    return activate_runtime_hop(self, prepared, pending)
                return ('moving' if c.walk_to(pending[3], 'bounded_pilot', speed=pending[4])
                        else 'blocked_start')
        with self._lock:
            intent = self._intent
        if intent is None:
            return 'idle' if self._motion is None else 'moving'
        if c._terrain_social_owns() or c.motion is not None:
            self.cancel()
            return 'blocked_other_owner'
        request_id, goal, speed = intent
        c._terrain_invalidate(clear_prepared=False)
        c._terrain_sync()
        life, start = self._permission(), vec(c.position)
        if life is None:
            self.cancel()
            return 'blocked_permission'
        with self._lock:
            if request_id != self._request_id or self._intent != intent:
                return 'cancelled_before_prepare'
            cold_timeout = getattr(self.backend.transport, 'timeout', 2.)
            cold_cap = 10.5 if getattr(self.backend.transport, '_on_demand_cold_budget', False) is True else 6.
            ttl = (min(cold_cap, cold_timeout+.5)
                   if self.admission.mode == 'experimental_ground_trial' and cold_timeout > 2. else 2.)
            self._lease = RouteLease(ttl, owner=self._owner)
            self._prepared = (request_id, life, start, goal, speed)
            self._intent = None
            self._future_request = request_id
            follow_intent = getattr(self, '_follow_intent', None)
            extra = ({'follow_intent': follow_intent[1]} if follow_intent is not None
                     and follow_intent[0] == request_id else {})
            from hop_follow_runtime_v20 import prepare_runtime_follow
            self._future = self._executor.submit(prepare_runtime_follow, self,
                life.scene, start, goal, speed=speed, lease=self._lease, **extra)
        return 'preparing'

    def shutdown(self):
        """Cold caller only. A closed adapter remains attached and fail-closed."""
        self.cancel()
        self._closed = True
        if self._motion is not None:
            self.controller._terrain_failed('pilot_shutdown', expected=self._motion)
        self._motion = None
        self.backend.release_route_scheduler(self._owner)
        if self._executor:
            self._executor.shutdown(wait=False, cancel_futures=True)
        self.backend.close()
        if getattr(self.controller, '_bounded_pilot_owner', None) is self._owner:
            self.controller._bounded_pilot_owner = None
