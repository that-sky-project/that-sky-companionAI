"""Opt-in locomotion boundary. No assets, packets, native calls, or threads here.

The backend samples already-calibrated avatar-anchor coordinates. Foot/origin
conversion belongs to the backend; this module never guesses an offset.
"""
from __future__ import annotations
from collections.abc import Mapping
from dataclasses import dataclass
import math
from numbers import Real
import threading
import time
from typing import Any, Callable, Protocol


def _number(value):
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError('nonfinite_number')
    value=float(value)
    if not math.isfinite(value):raise ValueError('nonfinite_number')
    return value


def _vector(value):
    if not isinstance(value,(tuple,list)) or len(value)!=3:raise ValueError('invalid_vector')
    return tuple(_number(component) for component in value)


@dataclass(frozen=True)
class MotionSample:
    position: tuple[float,float,float]
    velocity: tuple[float,float,float]=(0.0,0.0,0.0)
    arrived: bool=False
    blocked: bool=False
    grounded: bool | None=None
    reason: str | None=None
    heading: float | None=None


@dataclass(frozen=True)
class Decision:
    status: str                         # ready | blocked | unavailable
    motion: Any=None
    reason: str | None=None


class Backend(Protocol):
    def plan_move(self,scope,start,goal,reason,limits)->Decision:...


class CompatibleMotion:
    """Legacy Motion view over one stateful worker sample per increasing now.

    A stable scope token and adapter generation guard every sample before AND
    after the worker call. Stale references freeze; they never report arrival.
    The sampler must be bounded and must not mutate a controller or packet.
    """
    def __init__(self,worker,valid:Callable[[],bool],*,arrival_tolerance=1e-4):
        if not callable(getattr(worker,'sample',None)):raise ValueError('missing_sampler')
        self.a,self.b=_vector(worker.a),_vector(worker.b)
        self.desired_goal=_vector(getattr(worker,'desired_goal',self.b))
        self.planned_goal=self.b
        self.t0,self.seconds=_number(worker.t0),_number(worker.seconds)
        if self.seconds<0:raise ValueError('invalid_duration')
        self.heading=self._angle(getattr(worker,'heading',None))
        self.final_heading=self._angle(getattr(worker,'final_heading',None))
        self.speed=_number(getattr(worker,'speed',0.0))
        self._tolerance=_number(arrival_tolerance)
        if self._tolerance<0:raise ValueError('invalid_arrival_tolerance')
        self._worker,self._valid=worker,valid
        self._lock=threading.Lock()
        self._last_now=None
        self._sample=MotionSample(self.a)
        self._halted=False
        self._interrupted=threading.Event()

    @staticmethod
    def _angle(value):
        return None if value is None else _number(value)

    @property
    def velocity(self):
        if not self._lock.acquire(timeout=.05):
            self._interrupted.set()
            return (0.0,0.0,0.0)
        try:
            if not self._valid() or self._interrupted.is_set():self._halt('invalidated')
            return self._sample.velocity
        finally:self._lock.release()

    @property
    def blocked(self):
        if not self._lock.acquire(timeout=.05):return True
        try:return self._sample.blocked
        finally:self._lock.release()

    @property
    def blocked_reason(self):
        if not self._lock.acquire(timeout=.05):return 'sample_busy'
        try:return self._sample.reason
        finally:self._lock.release()

    def _halt(self,reason):
        self._sample=MotionSample(self._sample.position,blocked=True,
                                  grounded=self._sample.grounded,reason=reason,heading=self.heading)
        self._halted=True
        return self._sample

    def invalidate(self):
        self._interrupted.set()
        if self._lock.acquire(timeout=.05):
            try:self._halt('invalidated')
            finally:self._lock.release()

    def _decode(self,result):
        if isinstance(result,MotionSample):
            values=result.__dict__
        elif isinstance(result,Mapping):
            values=result
        else:raise ValueError('invalid_sample')
        position,velocity=_vector(values['position']),_vector(values['velocity'])
        arrived,blocked=values.get('arrived',False),values.get('blocked',False)
        grounded=values.get('grounded')
        if type(arrived) is not bool or type(blocked) is not bool:raise ValueError('invalid_sample_flags')
        if grounded is not None and type(grounded) is not bool:raise ValueError('invalid_grounded')
        heading=self._angle(values.get('heading'))
        if blocked:
            return MotionSample(position,blocked=True,grounded=grounded,reason='backend_blocked',heading=heading)
        if arrived:
            if math.dist(position,self.b)>self._tolerance:raise ValueError('arrival_mismatch')
            velocity=(0.0,0.0,0.0)
        return MotionSample(position,velocity,arrived,False,grounded,None,heading)

    def sample(self,now):
        # Conservatively round the absolute deadline: at large monotonic values
        # float addition can otherwise yield a remaining budget just above .05.
        deadline=math.nextafter(time.monotonic()+.05,-math.inf)
        if not self._lock.acquire(timeout=.05):
            self._interrupted.set()
            value=self._sample
            return MotionSample(value.position,blocked=True,grounded=value.grounded,reason='sample_busy',heading=value.heading)
        try:
            if not self._valid() or self._interrupted.is_set():return self._halt('invalidated')
            if self._halted:return self._sample
            try:now=_number(now)
            except (ValueError,OverflowError):return self._halt('invalid_time')
            if now<self.t0:return self._sample
            if self._last_now is not None and now<=self._last_now:return self._sample
            if self._sample.arrived:return self._sample
            try:
                raw=(self._worker.sample(now,deadline=deadline) if getattr(self._worker,'supports_deadline',False)
                     else self._worker.sample(now))
                result=self._decode(raw)
            except Exception:return self._halt('sample_failed')
            if time.monotonic()>deadline:return self._halt('sample_timeout')
            if not self._valid() or self._interrupted.is_set():return self._halt('invalidated')
            self._last_now=now
            self._sample=result
            if result.arrived:self.b=result.position
            if result.heading is not None:self.heading=result.heading
            if result.blocked:self._halted=True
            return result
        finally:self._lock.release()

    def pos(self,now):return self.sample(now).position
    def arrived(self,now):return self.sample(now).arrived


class TerrainIntegration:
    """Gated selector: disabled returns the exact zero-argument factory result.

    The caller binds one stable opaque scope object per room lifecycle. Reuse
    that SAME object for requests in that lifecycle, allocate/rebind on reset.
    None means explicitly not handled safely: inspect last_decision, clear any
    pending arrival callback, and do not silently substitute a straight line.
    Social ownership has priority even over the disabled factory path.
    """
    def __init__(self,backend:Backend|None=None,*,enabled=False,arrival_tolerance=1e-4):
        if type(enabled) is not bool:raise ValueError('enabled_must_be_bool')
        self._backend,self._enabled=backend,enabled
        self._scope=None
        self._generation=0
        self._social_owned=False
        self._lock=threading.Lock()
        self._tolerance=arrival_tolerance
        self._last_decision=None

    @property
    def enabled(self):
        with self._lock:return self._enabled

    @property
    def last_decision(self):
        with self._lock:return self._last_decision

    def bind_scope(self,scope):
        if scope is None:raise ValueError('scope_required')
        with self._lock:
            self._generation+=1
            self._scope=scope
            self._last_decision=None

    def invalidate(self):
        with self._lock:
            self._generation+=1
            self._scope=None
            self._last_decision=Decision('unavailable',reason='invalidated')

    def set_enabled(self,enabled):
        if type(enabled) is not bool:raise ValueError('enabled_must_be_bool')
        with self._lock:
            if self._enabled!=enabled:self._generation+=1
            self._enabled=enabled

    def set_social_owned(self,owned):
        with self._lock:
            owned=bool(owned)
            if self._social_owned!=owned:self._generation+=1
            self._social_owned=owned

    def _is_current(self,scope,generation):
        with self._lock:
            return (self._enabled and not self._social_owned and self._scope is scope
                    and self._generation==generation)

    def choose_motion(self,scope,start,goal,reason,limits,legacy_factory:Callable[[],Any],*,social_owns_movement=False):
        with self._lock:
            if social_owns_movement or self._social_owned:
                self._generation+=1
                self._last_decision=Decision('blocked',reason='social_owned')
                return None
            if not self._enabled:
                disabled=True
            else:
                disabled=False
                if scope is None or self._scope is not scope:
                    self._last_decision=Decision('unavailable',reason='scope_inactive')
                    return None
                self._generation+=1
                generation=self._generation
                backend=self._backend
        if disabled:return legacy_factory()  # Do not normalize/read anything else.
        try:
            start,goal=_vector(start),_vector(goal)
            if backend is None:raise ValueError('missing_backend')
            outcome=backend.plan_move(scope,start,goal,reason,limits)
            status=getattr(outcome,'status',None)
            if status not in ('ready','blocked','unavailable'):raise ValueError('invalid_decision')
            if status=='ready':
                worker=getattr(outcome,'motion',None)
                motion=CompatibleMotion(worker,lambda:self._is_current(scope,generation),
                                        arrival_tolerance=self._tolerance)
                if math.dist(motion.a,start)>self._tolerance:raise ValueError('start_mismatch')
                outcome=Decision('ready',motion)
            else:
                why=getattr(outcome,'reason',None)
                safe=(isinstance(why,str) and 0<len(why)<=96
                      and all(c.isascii() and (c.isalnum() or c in '_-.:') for c in why))
                outcome=Decision(status,reason=why if safe else 'backend_'+status)
        except Exception:
            outcome=Decision('unavailable',reason='backend_failed')
        with self._lock:
            if not (self._enabled and not self._social_owned and self._scope is scope
                    and self._generation==generation):
                return None  # Late result must not overwrite the newer request's status.
            self._last_decision=outcome
        return outcome.motion if outcome.status=='ready' else None
