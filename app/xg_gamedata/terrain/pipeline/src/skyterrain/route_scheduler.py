"""Explicit OFFLINE cold route scheduler. Never used by default/live paths.

The caller owns backend lifetime. Scheduler ownership is enforced by the
backend; hot submit/poll/cancel never issue backend operations. One worker, one running request, one replacing pending request. A lease
prevents a superseded result from being published or used by the backend itself.
"""
from dataclasses import dataclass
import threading
import time
from .backend import RouteLease,STAGED_PROFILE
from .integration import Decision,_number,_vector


def _identity(value,depth=0):
    # Identity plus incarnation, supplied by lifecycle code. No repr/log output.
    if depth>4:raise ValueError('target_identity')
    if type(value) is tuple:
        if not 1<=len(value)<=8:raise ValueError('target_identity')
        return tuple(_identity(x,depth+1) for x in value)
    if type(value) is str and 0<len(value)<=256:return value
    if type(value) is int and 0<=value<=2**64-1:return value
    raise ValueError('target_identity')


@dataclass(frozen=True)
class _Request:
    generation:int
    scope:object
    target:tuple
    start:tuple
    goal:tuple
    speed:float
    lease:RouteLease

    def same(self,scope,target,start,goal,speed):
        return (self.scope is scope and self.target==target and self.start==start
                and self.goal==goal and self.speed==speed)


class RouteScheduler:
    def __init__(self,backend,*,enabled=False,ttl=2.0):
        if type(enabled) is not bool:raise ValueError('enabled')
        ttl=_number(ttl)
        if not 0<ttl<=30:raise ValueError('route_ttl')
        if enabled and (getattr(backend.transport,'offline',False) is not True
                        or getattr(backend.transport,'worker_profile',None)!=STAGED_PROFILE):
            raise ValueError('offline_staged_backend_required')
        self._backend=backend;self._enabled=enabled;self._ttl=ttl
        self._condition=threading.Condition();self._closed=False;self._generation=0
        self._current=None;self._pending=None;self._inflight=None;self._result=None;self._delivered=False
        self._finished=threading.Event();self._thread=None;self._owner=object() if enabled else None
        if enabled:
            backend.acquire_route_scheduler(self._owner)
            try:
                self._thread=threading.Thread(target=self._run,daemon=True,name='terrain-cold-route')
                self._thread.start()
            except Exception:
                backend.release_route_scheduler(self._owner)
                raise
        else:self._finished.set()

    def submit(self,scope,target_identity_and_incarnation,start,goal,*,speed,force_new=False):
        if scope is None:raise ValueError('scope_required')
        if type(target_identity_and_incarnation) is not tuple or len(target_identity_and_incarnation)<2:
            raise ValueError('target_identity_and_incarnation')
        target=_identity(target_identity_and_incarnation)
        start,goal=_vector(start),_vector(goal);speed=_number(speed)
        if not 0<speed<=10:raise ValueError('speed')
        if type(force_new) is not bool:raise ValueError('force_new')
        with self._condition:
            if self._closed:raise RuntimeError('scheduler_closed')
            if not self._enabled:return None
            current=self._current
            # Expiry/failure/consumption never silently starts another cold job.
            if not force_new and current is not None and current.same(scope,target,start,goal,speed):
                return current.generation
            if current is not None:current.lease.cancel()
            self._generation+=1
            request=_Request(self._generation,scope,target,start,goal,speed,RouteLease(self._ttl,owner=self._owner))
            self._current=self._pending=request;self._result=None;self._delivered=False
            self._condition.notify()
            return request.generation

    def poll(self,generation):
        if generation is None:return None
        if type(generation) is not int or generation<=0:raise ValueError('generation')
        with self._condition:
            current=self._current
            if (self._closed or current is None or generation!=current.generation or self._delivered):return None
            if not current.lease.valid:
                current.lease.cancel();self._delivered=True
                return Decision('unavailable',reason='route_lease_revoked')
            if self._result is None:return None
            result=self._result;self._result=None;self._delivered=True
            return result

    def _cancel_locked(self):
        if self._current is not None:self._current.lease.cancel()
        self._generation+=1;self._current=self._pending=self._result=None;self._delivered=False
        self._condition.notify()

    def cancel(self):
        with self._condition:self._cancel_locked()

    def close(self):
        # Does not join, close a process, or wait on the backend's cold lock.
        with self._condition:
            self._closed=True;self._cancel_locked()
            if self._owner is not None:self._backend.release_route_scheduler(self._owner)

    def wait_closed(self,timeout=2.5):
        timeout=_number(timeout)
        if not 0<=timeout<=5:raise ValueError('close_wait_budget')
        return self._finished.wait(timeout)

    def _run(self):
        try:
            while True:
                with self._condition:
                    while self._pending is None and not self._closed:self._condition.wait()
                    if self._closed:return
                    request=self._pending;self._pending=None;self._inflight=request.generation
                try:
                    if not request.lease.unconsumed_valid:
                        result=Decision('unavailable',reason='route_lease_revoked')
                    else:
                        result=self._backend.prepare_route(request.scope,request.start,request.goal,
                            speed=request.speed,lease=request.lease)
                except Exception:
                    result=Decision('unavailable',reason='route_prepare_failed')
                with self._condition:
                    self._inflight=None
                    if self._current is request and not self._closed:
                        if not request.lease.valid:result=Decision('unavailable',reason='route_lease_revoked')
                        self._result=result
                    # No retry, backend publication or cancel RPC from this path.
        finally:self._finished.set()
