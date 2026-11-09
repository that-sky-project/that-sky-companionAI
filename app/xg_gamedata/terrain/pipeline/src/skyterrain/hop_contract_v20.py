"""Default-disabled, independent contract for a preflighted bounded hop.

This module does not run physics, publish poses, or provide worker-loss recovery.
The caller must pin the proof digest at cold preparation and keep it separate
from later untrusted replies. Only the engine can establish collision clearance.
"""
from dataclasses import dataclass
import hashlib
import json
import math
import re
from types import MappingProxyType


class HopContractError(ValueError):
    pass


def _require(ok, reason):
    if not ok:
        raise HopContractError(reason)


def _number(value):
    _require(type(value) in (int, float) and math.isfinite(value), 'finite_number_required')
    return float(value)


def _vec(value):
    _require(type(value) in (list, tuple) and len(value) == 3, 'xyz_required')
    return tuple(_number(x) for x in value)


def _sha(value):
    _require(type(value) is str and re.fullmatch(r'[0-9a-f]{64}', value), 'sha256_required')
    return value


def canonical_sha256(value):
    try:
        raw = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    except (TypeError, ValueError) as exc:
        raise HopContractError('canonical_json_required') from exc
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class HopLimits:
    """Explicit engineering limits supplied by the owner, not native constants."""
    max_airtime: float
    max_apex: float
    max_drop: float
    max_horizontal_speed: float
    max_vertical_speed: float
    max_distance: float
    max_sample_gap: float = .25
    deadline_margin: float = .1
    position_tolerance: float = 1e-5
    max_grounding_seconds: float = .5

    def __post_init__(self):
        for key in self.__dataclass_fields__:
            n = _number(getattr(self, key))
            _require(n > 0, 'positive_hop_limit_required')
        _require(self.max_airtime <= 3 and self.max_apex <= 3 and self.max_drop <= 3
                 and self.max_horizontal_speed <= 3 and self.max_vertical_speed <= 12
                 and self.max_distance <= 8 and self.max_sample_gap <= .5
                 and self.max_grounding_seconds <= 1
                 and self.position_tolerance <= 1e-4, 'hop_limit_ceiling')


@dataclass(frozen=True)
class HopFrame:
    step: int
    phase: str
    position: tuple
    velocity: tuple
    grounded: bool
    state_vertical_speed: float


_FRAME_KEYS = {'step', 'phase', 'position', 'velocity', 'grounded', 'stateVerticalSpeed'}
_SAMPLE_KEYS = _FRAME_KEYS | {'hopId', 'scopeBindingSHA256', 'worldSHA256', 'totalStep', 'warmupStep'}
_PROOF_KEYS = {'schema', 'hopId', 'scopeBindingSHA256', 'worldSHA256', 'receiptSHA256',
               'fixedDt', 'requestedStart', 'startContactTrace', 'start', 'landing', 'mainTrace', 'settleTraces'}
_SEAL = object()


def _frame(raw):
    _require(type(raw) is dict and set(raw) == _FRAME_KEYS, 'exact_frame_schema_required')
    _require(type(raw['step']) is int and 0 <= raw['step'] <= 1000, 'invalid_step')
    _require(type(raw['grounded']) is bool, 'grounded_boolean_required')
    _require(raw['phase'] in ('takeoff', 'rising', 'falling', 'landed', 'settling',
                              'cancelled_landed'), 'invalid_hop_phase')
    return HopFrame(raw['step'], raw['phase'], _vec(raw['position']),
                    _vec(raw['velocity']), raw['grounded'], _number(raw['stateVerticalSpeed']))


def _trace(raw, *, prefix, settle, dt, start, limits):
    _require(type(raw) is list and 1 <= len(raw) <= 256, 'bounded_trace_required')
    frames = tuple(_frame(x) for x in raw)
    previous = prefix
    distance = 0.
    was_falling = False
    for i, frame in enumerate(frames):
        expected_step = (previous.step + 1) if previous is not None else 0
        _require(frame.step == expected_step, 'noncontiguous_trace')
        _require(frame.step * dt <= limits.max_airtime + 1e-8, 'airtime_budget')
        dy = frame.position[1] - start[1]
        _require(-limits.max_drop <= dy <= limits.max_apex, 'height_budget')
        vx, vy, vz = frame.velocity
        _require(math.hypot(vx, vz) <= limits.max_horizontal_speed + 1e-6
                 and abs(vy) <= limits.max_vertical_speed + 1e-6
                 and abs(frame.state_vertical_speed) <= limits.max_vertical_speed + 1e-6,
                 'velocity_budget')
        final = i == len(frames) - 1
        if settle:
            _require(frame.phase == ('cancelled_landed' if final else 'settling'),
                     'settle_phase_order')
        elif i == 0:
            _require(frame.phase == 'takeoff' and frame.grounded and frame.step == 0
                     and frame.position == start and frame.velocity == (0., 0., 0.),
                     'real_grounded_takeoff_origin_required')
        elif final:
            _require(frame.phase == 'landed', 'landed_endpoint_required')
        else:
            _require(frame.phase in ('rising', 'falling'), 'main_phase_order')
            _require(not (was_falling and frame.phase == 'rising'), 'second_takeoff_forbidden')
            was_falling = was_falling or frame.phase == 'falling'
            _require((frame.phase != 'rising' or frame.state_vertical_speed >= -1e-6)
                     and (frame.phase != 'falling' or frame.state_vertical_speed <= 1e-6),
                     'phase_velocity_mismatch')
        if not (not settle and i == 0):
            _require(frame.grounded is final, 'grounded_phase_mismatch')
        if final:
            # Measured last-step velocity can include the final downward motion.
            # The actual physics state must clear its vertical speed on landing.
            _require(frame.grounded and abs(frame.state_vertical_speed) <= 1e-6,
                     'grounded_landing_required')
        if previous is not None:
            length = math.dist(previous.position, frame.position)
            horizontal = math.hypot(frame.position[0]-previous.position[0],
                                    frame.position[2]-previous.position[2])
            _require(horizontal <= limits.max_horizontal_speed * dt + 2e-4,
                     'horizontal_step_budget')
            _require(abs(frame.position[1]-previous.position[1]) <= limits.max_vertical_speed * dt + 2e-4,
                     'vertical_step_budget')
            distance += length
        previous = frame
    _require(distance <= limits.max_distance, 'trace_travel_budget')
    return frames, distance


class HopContract:
    """Immutable proof, created only by strict parsing; no network authority."""
    __slots__ = ('hop_id', 'scope', 'world', 'receipt_sha256', 'proof_sha256', 'dt',
                 'start', 'requested_start', 'warmup', 'landing', 'main', 'settles', 'limits', 'duration', '_seal', '_frozen')

    def __setattr__(self, name, value):
        if getattr(self, '_frozen', False):
            raise AttributeError('immutable_hop_contract')
        object.__setattr__(self, name, value)

    def start_grounding(self, *, now, deadline, scope, world, published_start, social_owned=False):
        _require(getattr(self, '_seal', None) is _SEAL, 'unsealed_hop_contract')
        return GroundingRun(self, now=now, deadline=deadline, scope=scope, world=world,
                            published_start=published_start, social_owned=social_owned)

    def start_run(self, *, now, deadline, scope, world, social_owned=False, grounding_receipt=None):
        _require(getattr(self, '_seal', None) is _SEAL, 'unsealed_hop_contract')
        if self.warmup:
            _require(type(grounding_receipt) is _GroundedHandoff and grounding_receipt.proof is self
                     and grounding_receipt._seal is _SEAL and not grounding_receipt.claimed,
                     'published_grounding_handoff_required')
            _require(grounding_receipt.source._terminal == 'grounded'
                     and now == grounding_receipt.hop_start and deadline == grounding_receipt.deadline,
                     'grounding_handoff_time_or_owner')
            grounding_receipt.claimed = True
        return HopRun(self, now=now, deadline=deadline, scope=scope, world=world,
                      social_owned=social_owned)


def parse_hop_contract(raw, *, expected_proof_sha256, expected_receipt_sha256,
                       expected_scope_binding_sha256, expected_world_sha256,
                       limits, enabled=False):
    _require(enabled is True, 'hop_opt_in_required')
    _require(type(limits) is HopLimits, 'explicit_hop_limits_required')
    _require(type(raw) is dict and set(raw) == _PROOF_KEYS, 'exact_proof_schema_required')
    _require(raw['schema'] == 'bounded-hop-contract-v20', 'unsupported_hop_schema')
    proof_sha = canonical_sha256(raw)
    _require(proof_sha == _sha(expected_proof_sha256), 'proof_digest_mismatch')
    _require(_sha(raw['receiptSHA256']) == _sha(expected_receipt_sha256), 'receipt_binding_mismatch')
    _require(_sha(raw['scopeBindingSHA256']) == _sha(expected_scope_binding_sha256), 'scope_binding_mismatch')
    _require(_sha(raw['worldSHA256']) == _sha(expected_world_sha256), 'world_binding_mismatch')
    _require(type(raw['hopId']) is str and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', raw['hopId']),
             'hop_id_required')
    dt = _number(raw['fixedDt'])
    _require(abs(dt-1/60) < 1e-12, 'fixed_60hz_required')
    start, landing = _vec(raw['start']), _vec(raw['landing'])
    requested_start = _vec(raw['requestedStart'])
    warmup_raw = raw['startContactTrace']
    _require(type(warmup_raw) is list and len(warmup_raw) <= 60, 'bounded_grounding_trace_required')
    _require(len(warmup_raw)*dt <= limits.max_grounding_seconds+1e-8, 'grounding_time_budget')
    warmup = []
    previous = requested_start
    warmup_distance = 0.
    for step, row in enumerate(warmup_raw, 1):
        _require(type(row) is dict and set(row) == {'step','position','velocity','grounded'}
                 and type(row['step']) is int and row['step'] == step
                 and type(row['grounded']) is bool, 'exact_grounding_trace_schema')
        position, velocity = _vec(row['position']), _vec(row['velocity'])
        _require(row['grounded'] is (step == len(warmup_raw)), 'grounding_contact_order')
        _require(math.hypot(velocity[0],velocity[2]) <= limits.max_horizontal_speed+1e-6
                 and abs(velocity[1]) <= limits.max_vertical_speed+1e-6, 'grounding_velocity_budget')
        _require(math.hypot(position[0]-previous[0],position[2]-previous[2]) <= limits.max_horizontal_speed*dt+2e-4
                 and abs(position[1]-previous[1]) <= limits.max_vertical_speed*dt+2e-4,
                 'grounding_displacement_budget')
        _require(-limits.max_drop <= position[1]-requested_start[1] <= limits.max_apex,
                 'grounding_height_budget')
        warmup_distance += math.dist(previous,position)
        warmup.append((step,position,velocity,row['grounded'])); previous=position
    _require(previous == start, 'grounding_end_binding')
    main, main_distance = _trace(raw['mainTrace'], prefix=None, settle=False, dt=dt, start=start, limits=limits)
    _require(warmup_distance+main_distance <= limits.max_distance, 'grounding_total_travel_budget')
    _require(len(main) >= 3 and any(not x.grounded for x in main), 'actual_air_segment_required')
    _require(main[-1].position == landing, 'landing_binding_mismatch')
    _require(type(raw['settleTraces']) is list and len(raw['settleTraces']) <= 255,
             'settle_traces_required')
    settles = {}
    prefix_distance = [0.]
    for a, b in zip(main, main[1:]):
        prefix_distance.append(prefix_distance[-1]+math.dist(a.position, b.position))
    for branch in raw['settleTraces']:
        _require(type(branch) is dict and set(branch) == {'fromStep', 'frames'}, 'exact_settle_schema_required')
        step = branch['fromStep']
        _require(type(step) is int and 0 < step < len(main)-1 and not main[step].grounded
                 and step not in settles, 'invalid_settle_origin')
        frames, travel = _trace(branch['frames'], prefix=main[step], settle=True, dt=dt,
                               start=start, limits=limits)
        _require(warmup_distance+prefix_distance[step]+travel <= limits.max_distance, 'settle_total_travel_budget')
        settles[step] = frames
    _require(set(settles) == {f.step for f in main if not f.grounded}, 'missing_air_state_settle_proof')
    contract = HopContract()
    for key, value in dict(hop_id=raw['hopId'], scope=raw['scopeBindingSHA256'], world=raw['worldSHA256'],
                           receipt_sha256=raw['receiptSHA256'], proof_sha256=proof_sha, dt=dt,
                           start=start, landing=landing, main=main,
                           requested_start=requested_start, warmup=tuple(warmup),
                           settles=MappingProxyType(settles), limits=limits,
                           duration=max([main[-1].step*dt]+[v[-1].step*dt for v in settles.values()]),
                           _seal=_SEAL, _frozen=True).items():
        setattr(contract, key, value)
    return contract


class _GroundedHandoff:
    def __init__(self, source):
        self.source, self.proof = source, source.proof
        self.hop_start = source.started+len(source.proof.warmup)*source.proof.dt
        self.deadline = source.deadline
        self.claimed = False
        self._seal = _SEAL


class GroundingRun:
    """Real-time pre-takeoff contact validation, never hidden position snapping."""
    def __init__(self, proof, *, now, deadline, scope, world, published_start, social_owned=False):
        self.proof=proof;self.started=_number(now);self.deadline=_number(deadline)
        self._terminal=None;self._last_step=0;self._last_now=self.started;self._soft_cancel=False
        _require(proof.warmup, 'no_grounding_trace')
        _require(math.dist(_vec(published_start),proof.requested_start) <= proof.limits.position_tolerance,
                 'published_start_mismatch')
        self._check(now,scope,world,social_owned)
        _require(self.started+len(proof.warmup)*proof.dt+proof.duration+proof.limits.deadline_margin < self.deadline,
                 'insufficient_session_time_to_ground_and_land')

    @property
    def terminal_reason(self):return self._terminal

    def cancel(self, reason='hard_cancelled'):
        if self._terminal in (None, 'grounded'):
            self._terminal=reason if type(reason) is str and reason else 'hard_cancelled'

    def worker_failed(self):self.cancel('worker_failed_no_local_recovery_executor')

    def _check(self, now, scope, world, social_owned):
        try:
            _require(self._terminal is None, 'grounding_run_terminal')
            now=_number(now)
            _require(type(social_owned) is bool and not social_owned, 'social_owner_changed')
            _require(scope==self.proof.scope and world==self.proof.world, 'grounding_scope_changed')
            _require(self.started <= now < self.deadline and now >= self._last_now,
                     'grounding_clock_or_deadline')
            _require(now <= self.started+len(self.proof.warmup)*self.proof.dt+self.proof.limits.max_sample_gap,
                     'grounding_expired')
        except HopContractError as exc:
            self.cancel(str(exc));raise

    def request_landing(self, *, now, scope, world, social_owned=False):
        self._check(now,scope,world,social_owned);self._soft_cancel=True

    def accept_batch(self, frames, *, now, scope, world, social_owned=False):
        try:
            self._check(now,scope,world,social_owned)
            _require(type(frames) is list and 1 <= len(frames) <= 32, 'bounded_grounding_batch')
            accepted=[]
            keys={'hopId','scopeBindingSHA256','worldSHA256','warmupStep','totalStep','phase','position','velocity','grounded'}
            for raw in frames:
                _require(type(raw) is dict and set(raw)==keys, 'exact_grounding_sample_schema')
                _require(raw['hopId']==self.proof.hop_id and raw['scopeBindingSHA256']==scope
                         and raw['worldSHA256']==world, 'grounding_sample_binding')
                step=raw['warmupStep']
                _require(type(step) is int and step==self._last_step+1 and step<=len(self.proof.warmup)
                         and type(raw['totalStep']) is int and raw['totalStep']==step, 'grounding_step_order')
                age=now-self.started-step*self.proof.dt
                _require(-1e-7 <= age <= self.proof.limits.max_sample_gap, 'future_or_stale_grounding')
                expected=self.proof.warmup[step-1]
                phase='cancelled_landed' if self._soft_cancel and step==len(self.proof.warmup) else 'grounding'
                _require(raw['phase']==phase and type(raw['grounded']) is bool and raw['grounded']==expected[3]
                         and math.dist(_vec(raw['position']),expected[1])<=self.proof.limits.position_tolerance
                         and math.dist(_vec(raw['velocity']),expected[2])<=1e-5, 'grounding_trace_mismatch')
                accepted.append(expected);self._last_step=step
            self._last_now=float(now)
            if self._last_step==len(self.proof.warmup):
                self._terminal='cancelled_landed' if self._soft_cancel else 'grounded'
            return tuple(accepted)
        except HopContractError as exc:
            self.cancel(str(exc));raise

    def handoff(self):
        _require(self._terminal=='grounded' and not self._soft_cancel, 'grounding_not_complete')
        if not hasattr(self,'_handoff'):self._handoff=_GroundedHandoff(self)
        return self._handoff


class HopRun:
    """Single-use publication validator. Failure permanently revokes this run.

    Soft cancellation selects a proven landing branch, but never generates a
    frame. A dead/timed-out worker is a hard failure until a separate executor
    and its live-state handoff are implemented by the owner.
    """
    def __init__(self, proof, *, now, deadline, scope, world, social_owned=False):
        _require(isinstance(proof, HopContract) and getattr(proof, '_seal', None) is _SEAL,
                 'unsealed_hop_contract')
        self.proof = proof
        self.started, self.deadline = _number(now), _number(deadline)
        self._terminal = None
        self._last_step = -1
        self._last_now = self.started
        self._branch = None
        self._check(now, scope, world, social_owned)
        _require(self.started + proof.duration + proof.limits.deadline_margin < self.deadline,
                 'insufficient_session_time_to_land')

    @property
    def terminal_reason(self):
        return self._terminal

    def cancel(self, reason='hard_cancelled'):
        if self._terminal is None:
            self._terminal = reason if type(reason) is str and reason else 'hard_cancelled'

    def worker_failed(self):
        self.cancel('worker_failed_no_local_recovery_executor')

    def _check(self, now, scope, world, social_owned):
        try:
            _require(self._terminal is None, 'hop_run_terminal')
            now = _number(now)
            _require(type(social_owned) is bool and not social_owned, 'social_owner_changed')
            _require(scope == self.proof.scope and world == self.proof.world, 'hop_scope_changed')
            _require(self.started <= now < self.deadline and now >= self._last_now, 'hop_clock_or_deadline')
            _require(now <= self.started+self.proof.duration+self.proof.limits.max_sample_gap,
                     'hop_airtime_expired')
        except HopContractError as exc:
            self.cancel(str(exc))
            raise

    def request_landing(self, *, now, scope, world, social_owned=False):
        self._check(now, scope, world, social_owned)
        if self._branch is not None:
            return self._branch
        if self._last_step <= 0:
            self.cancel('cancelled_before_air_publication')
            return None
        if self._last_step not in self.proof.settles:
            self.cancel('no_active_air_state')
            return None
        self._branch = self._last_step
        return self._branch

    def accept(self, raw, *, now, scope, world, social_owned=False):
        try:
            self._check(now, scope, world, social_owned)
            _require(type(raw) is dict and set(raw) == _SAMPLE_KEYS, 'exact_sample_schema_required')
            _require(raw['hopId'] == self.proof.hop_id and raw['scopeBindingSHA256'] == scope
                     and raw['worldSHA256'] == world, 'sample_binding_mismatch')
            frame = _frame({k:raw[k] for k in _FRAME_KEYS})
            _require(type(raw['totalStep']) is int and type(raw['warmupStep']) is int
                     and raw['warmupStep']==len(self.proof.warmup)
                     and raw['totalStep']==len(self.proof.warmup)+frame.step,
                     'hop_total_step_binding')
            _require(frame.step > self._last_step, 'stale_or_replayed_frame')
            expected_step = self._last_step+1
            if self._last_step == -1 and frame.step == 1:
                expected_step = 1  # Proven grounded step 0 has no displacement.
            _require(frame.step == expected_step, 'missing_fixed_step_in_sample')
            elapsed = _number(now)-self.started
            age = elapsed-frame.step*self.proof.dt
            _require(-1e-7 <= age <= self.proof.limits.max_sample_gap, 'future_or_stale_sample')
            trace = self.proof.main if self._branch is None else self.proof.settles[self._branch]
            expected = next((x for x in trace if x.step == frame.step), None)
            _require(expected is not None, 'frame_outside_sealed_trace')
            _require(frame.phase == expected.phase and frame.grounded is expected.grounded
                     and math.dist(frame.position, expected.position) <= self.proof.limits.position_tolerance
                     and math.dist(frame.velocity, expected.velocity) <= 1e-5
                     and abs(frame.state_vertical_speed-expected.state_vertical_speed) <= 1e-5,
                     'sample_trace_mismatch')
            self._last_step, self._last_now = frame.step, float(now)
            if frame.phase in ('landed', 'cancelled_landed'):
                self._terminal = frame.phase
            return frame
        except HopContractError as exc:
            self.cancel(str(exc))
            raise

    def accept_batch(self, raw_frames, *, now, scope, world, social_owned=False):
        """Validate each real fixed step; never equate batch velocity with one step.

        No frame is returned if any member fails. The owner must not publish
        intermediate results until this call returns successfully.
        """
        try:
            _require(type(raw_frames) is list and 1 <= len(raw_frames) <= 32,
                     'bounded_nonempty_frame_batch_required')
            self._check(now, scope, world, social_owned)
            accepted = []
            for raw in raw_frames:
                _require(type(raw) is dict and type(raw.get('step')) is int,
                         'invalid_batch_frame')
                expected_step = self._last_step+1
                # The first worker batch starts after the proven takeoff origin.
                if self._last_step == -1 and raw['step'] == 1:
                    expected_step = 1
                _require(raw['step'] == expected_step, 'missing_fixed_step_in_batch')
                accepted.append(self.accept(raw, now=now, scope=scope, world=world,
                                             social_owned=social_owned))
            return tuple(accepted)
        except HopContractError as exc:
            self.cancel(str(exc))
            raise
