"""Offline-only local JSONL terrain worker. Never logs in or touches game transport.

Two fixed allowlisted worker profiles (legacy default), one outstanding RPC,
bounded frames/timeouts, separate
bounded reader/writer queues. A poisoned process is restarted only on an explicit
new plan/restart; a sample never restarts or replays an old token.
"""
from __future__ import annotations
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path,PurePosixPath
import queue
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
for _p in Path(__file__).resolve().parents:
    if (_p/'xg_exec.py').is_file():
        sys.path.insert(0,str(_p));break
import xg_exec  # noqa: E402
from .integration import Decision,MotionSample,_number,_vector

WORKER=Path(__file__).resolve().parents[2]/'engine/worker.mjs'
STAGED_WORKER=WORKER.with_name('staged_worker.mjs')
STAGED_PROFILE='preserving_imported_nav'
MAX_FRAME=65536
PROTOCOL_VERSION=1
_MOTOR_POLICY_SEAL=object()
_STEP_POLICY='bounded_step_0_6_preflight_v14'

class WorkerError(RuntimeError):
    pass

def validate_follow_intent(value, *, segment_limit=6.):
    """Logical follow request, not permission to publish its distant endpoint."""
    if type(value) is not dict or set(value)!={'goal','peer','distance','segmentLength','maxEndpointDistance'}:
        raise ValueError('follow_intent_schema')
    goal,peer=_vector(value['goal']),_vector(value['peer'])
    distance,segment,endpoint=(_number(value[k]) for k in ('distance','segmentLength','maxEndpointDistance'))
    if not (0<=distance<=endpoint<=1.3 and 0<segment<=min(6.,segment_limit)):
        raise ValueError('follow_intent_bounds')
    if abs(math.hypot(goal[0]-peer[0],goal[2]-peer[2])-distance)>1e-4 or abs(goal[1]-peer[1])>1e-4:
        raise ValueError('follow_intent_standoff')
    return dict(goal=goal,peer=peer,distance=distance,segmentLength=segment,maxEndpointDistance=endpoint)

def _check_follow_prefix(reply, intent):
    """Validate the returned short route separately from its long-range intent."""
    audit=reply.get('followIntentAudit')
    if type(audit) is not dict:raise ValueError('follow_intent_audit_required')
    peer,selected=_vector(audit['peer']),_vector(audit['selectedGoal'])
    prefix,full=_number(audit['prefixLength']),_number(audit['fullPathLength'])
    path=reply.get('path')
    if type(path) is not list or not 2<=len(path)<=512:raise ValueError('follow_prefix_path')
    path=[_vector(p) for p in path];actual=sum(math.dist(a,b) for a,b in zip(path,path[1:]))
    if (peer!=tuple(intent['peer']) or type(audit.get('truncated')) is not bool
            or not .1<=prefix<=intent['segmentLength']+1e-5 or full+1e-5<prefix
            or abs(actual-prefix)>1e-4 or abs(_number(reply['length'])-prefix)>1e-4
            or abs(_number(reply['pathLength'])-prefix)>1e-4
            or math.dist(_vector(reply['goal']),path[-1])>1e-5
            or math.hypot(selected[0]-peer[0],selected[2]-peer[2])>intent['maxEndpointDistance']+1e-5):
        raise ValueError('follow_prefix_bounds')
    if not audit['truncated'] and math.dist(path[-1],selected)>1e-4:
        raise ValueError('follow_prefix_endpoint')
    return dict(audit)

def _check_motor_policy(reply, *, requested_speed=None):
    policy,audit=reply.get('motorPolicy'),reply.get('motorPolicyAudit')
    if policy is None and audit is None:return None
    keys={'schema','passed','stepHeight','groundSnapMeters','collisionMarginMeters','phasesMs','phaseResults','maxRouteMeters','nativeCollisionVerified','requestedSpeedMps','commandedSpeedMps'}
    if (policy!=_STEP_POLICY or type(audit) is not dict or set(audit)!=keys
            or audit['schema']!='bounded-step-preflight-v14' or audit['passed'] is not True
            or _number(audit['stepHeight'])!=.6 or _number(audit['groundSnapMeters'])!=.6 or _number(audit['collisionMarginMeters'])!=24
            or type(audit['phasesMs']) is not list or len(audit['phasesMs'])!=3
            or any(type(dt) is not int for dt in audit['phasesMs'])
            or audit['phasesMs']!=[100,120,130] or _number(audit['maxRouteMeters'])!=8
            or audit['nativeCollisionVerified'] is not False):
        raise ValueError('motor_policy_preflight_required')
    requested,commanded=_number(audit['requestedSpeedMps']),_number(audit['commandedSpeedMps'])
    if (not 0<requested<=10 or commanded!=requested*.99
            or requested_speed is not None and requested!=requested_speed):
        raise ValueError('motor_policy_speed_receipt')
    rows=audit['phaseResults']
    if type(rows) is not list or len(rows)!=3:raise ValueError('motor_policy_phases')
    for dt,row in zip((100,120,130),rows):
        if (type(row) is not dict or set(row)!={'dtMs','arrived','blocked','groundedSamples','frames','travelMeters','maxStepRise','maxGroundGap'}
                or type(row['dtMs']) is not int or row['dtMs']!=dt or row['arrived'] is not True or row['blocked'] is not False
                or row['groundedSamples'] is not True or type(row['frames']) is not int
                or not 0<row['frames']<=2000 or not 0<_number(row['travelMeters'])<=8.00001
                or not 0<=_number(row['maxStepRise'])<=.63001 or _number(row['maxGroundGap'])!=0):
            raise ValueError('motor_policy_phase_failed')
    return policy

def verified_motion_step_height(motion):
    """Only a WorkerMotion with the internally sealed cold+hot receipt can opt in."""
    receipt=getattr(motion,'_motor_policy_receipt',None)
    return (.6 if isinstance(motion,WorkerMotion) and type(receipt) is tuple and len(receipt)==3
            and receipt[0]==_STEP_POLICY and receipt[2] is _MOTOR_POLICY_SEAL else .3)

class RouteLease:
    """Cancellation-only cross-thread guard; no backend/transport lock or RPC.

    TTL bounds an unconsumed preparation. Claiming it once removes only that
    TTL; cancellation remains effective throughout the resulting motion.
    """
    def __init__(self,ttl=2.0,*,clock=None,owner=None):
        ttl=_number(ttl)
        if not 0<ttl<=30:raise ValueError('route_ttl')
        self._owner=owner
        self._clock=time.monotonic if clock is None else clock
        self._deadline=math.nextafter(self._clock()+ttl,-math.inf)
        self._cancelled=threading.Event();self._claim_lock=threading.Lock();self._claimed=False
    def cancel(self):self._cancelled.set()
    @property
    def valid(self):
        return not self._cancelled.is_set() and (self._claimed or self._clock()<self._deadline)
    @property
    def unconsumed_valid(self):return not self._claimed and self.valid
    def claim(self):
        if not self._claim_lock.acquire(blocking=False):return False
        try:
            if not self.unconsumed_valid:return False
            self._claimed=True
            return not self._cancelled.is_set()
        finally:self._claim_lock.release()

class JsonlWorkerTransport:
    offline=True
    def __init__(self,assets_root,*,node_executable='C:/Program Files/nodejs/node.exe',timeout=2.0,worker_profile='default',hop_enabled=False,hop_proof_directory=None):
        if worker_profile not in ('default',STAGED_PROFILE):raise ValueError('worker_profile')
        if type(hop_enabled) is not bool:raise ValueError('hop_enabled_boolean')
        if hop_enabled and (worker_profile!=STAGED_PROFILE or hop_proof_directory is None
                            or not Path(hop_proof_directory).is_absolute()):
            raise ValueError('hop_staged_absolute_proof_directory_required')
        self.hop_enabled=hop_enabled
        self.hop_proof_directory=Path(hop_proof_directory).resolve() if hop_enabled else None
        self._worker_profile=worker_profile
        self._worker=STAGED_WORKER if worker_profile==STAGED_PROFILE else WORKER
        self.assets_root=Path(assets_root)
        self.node=Path(node_executable)
        if not self.node.is_absolute() or self.node.name.lower() not in ('node','node.exe'):
            raise ValueError('absolute_node_executable_required')
        self.timeout=_number(timeout)
        if not 0<self.timeout<=2:raise ValueError('worker_timeout_budget')
        self._requests=threading.Lock();self._state=threading.Lock()
        self._proc=None;self._failure=None;self._closed=False;self._generation=0
        self._replies=queue.Queue(maxsize=2);self._writes=queue.Queue(maxsize=1)
        self._threads=[]

    @property
    def worker_profile(self):return self._worker_profile

    def configure_cold_timeout(self, seconds):
        """Explicit staged-worker recovery budget, before spawning only.

        This never changes the separate 50 ms plan/sample ceiling or permits
        a sample to create/restart a worker. Normal constructors still cap 2 s.
        """
        seconds=_number(seconds)
        if self.worker_profile!=STAGED_PROFILE or not 0<seconds<=5:
            raise ValueError('staged_cold_timeout_budget')
        with self._state:
            if self._proc is not None or self._closed or self._failure is not None:
                raise ValueError('cold_timeout_requires_unstarted_transport')
            self.timeout=seconds

    @property
    def generation(self):
        with self._state:return self._generation
    @property
    def failed(self):
        with self._state:return self._failure is not None

    def _poison(self,reason,expected_proc=None):
        with self._state:
            if self._closed or expected_proc is not None and self._proc is not expected_proc:return
            if self._failure is None:self._failure=reason;self._generation+=1
            proc=self._proc
        if proc is not None:
            try:
                if proc.poll() is None:proc.kill()
            except OSError:
                with self._state:self._failure='worker_termination_failed'
        with self._state:reason=self._failure or reason
        try:self._replies.put_nowait(WorkerError(reason))
        except queue.Full:pass

    def _reader(self,proc,replies):
        try:
            while True:
                raw=proc.stdout.readline(MAX_FRAME+1)
                if not raw:self._poison('worker_exited',proc);return
                if len(raw)>MAX_FRAME or not raw.endswith(b'\n'):
                    self._poison('reply_budget',proc);return
                def reject_constant(_):raise ValueError('nonfinite_json')
                reply=json.loads(raw,parse_constant=reject_constant)
                if not isinstance(reply,dict):raise ValueError('reply_object')
                try:replies.put_nowait(reply)
                except queue.Full:self._poison('unsolicited_reply',proc);return
        except Exception:self._poison('worker_protocol',proc)

    def _writer(self,proc,writes):
        try:
            while True:
                frame=writes.get()
                if frame is None:return
                pending=memoryview(frame)
                while pending:
                    count=proc.stdin.write(pending)
                    if not count:raise BrokenPipeError()
                    pending=pending[count:]
                proc.stdin.flush()
        except Exception:self._poison('worker_write_failed',proc)

    def _start(self):
        if self._proc is not None:return
        try:
            if not self._worker.is_file() or not self.node.is_file():raise ValueError('worker_missing')
            assets=self.assets_root.resolve(strict=True)
            if not assets.is_dir():raise ValueError('asset_root')
            env={key:os.environ[key] for key in ('SYSTEMROOT','WINDIR','TEMP','TMP','PATH','PATHEXT') if key in os.environ}
            flags=getattr(subprocess,'CREATE_NO_WINDOW',0)
            args=[str(self.node),str(self._worker),'--assets',str(assets),'--offline']
            if self.hop_enabled:args+=['--hop-proofs',str(self.hop_proof_directory)]
            proc=xg_exec.popen(args,
                               stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                               cwd=str(self._worker.parent),shell=False,bufsize=0,env=env)
            with self._state:self._proc=proc;self._generation+=1
            self._threads=[threading.Thread(target=self._reader,args=(proc,self._replies),daemon=True,name='terrain-reader'),
                           threading.Thread(target=self._writer,args=(proc,self._writes),daemon=True,name='terrain-writer')]
            for thread in self._threads:thread.start()
        except Exception:
            self._poison('worker_start_failed');raise WorkerError('worker_start_failed') from None

    @contextmanager
    def _request_slot(self,deadline,budget):
        if not self._requests.acquire(timeout=min(budget,max(0.,deadline-time.monotonic()))):
            raise WorkerError('worker_busy_timeout')
        try:yield
        finally:self._requests.release()

    def request(self,op,*,_deadline=None,**payload):
        allowed=('ping','load','plan','sample','cancel','invalidate','ground')
        if self.worker_profile==STAGED_PROFILE:allowed+=('preparePlan','retarget')
        if self.hop_enabled:allowed+=('prepareHop','startHop','sampleHop','softCancelHop','hardCancelHop')
        if op not in allowed:
            raise WorkerError('unknown_operation')
        if 'id' in payload:raise WorkerError('reserved_request_id')
        request_id=uuid.uuid4().hex
        try:frame=(json.dumps({'id':request_id,'op':op,**payload},allow_nan=False,separators=(',',':'))+'\n').encode('utf8')
        except (TypeError,ValueError,OverflowError):raise WorkerError('invalid_request') from None
        if len(frame)>MAX_FRAME:raise WorkerError('request_budget')
        hot_ops=('plan','sample','retarget','startHop','sampleHop','softCancelHop','hardCancelHop')
        budget=min(self.timeout,.05) if op in hot_ops else self.timeout
        deadline=math.nextafter(time.monotonic()+budget,-math.inf)
        if _deadline is not None:deadline=min(deadline,_number(_deadline))
        if time.monotonic()>=deadline:raise WorkerError('worker_timeout')
        with self._request_slot(deadline,budget):
            with self._state:
                if self._closed:raise WorkerError('worker_closed')
                if self._failure:raise WorkerError(self._failure)
            if op in hot_ops and self._proc is None:
                raise WorkerError('worker_not_prepared')
            self._start()
            try:self._writes.put_nowait(frame)
            except queue.Full:self._poison('worker_queue_full');raise WorkerError('worker_queue_full') from None
            try:reply=self._replies.get(timeout=min(budget,max(0.,deadline-time.monotonic())))
            except queue.Empty:
                self._poison('worker_timeout')
                with self._state:failure=self._failure or 'worker_timeout'
                raise WorkerError(failure) from None
            if isinstance(reply,WorkerError):raise reply
            if reply.get('id')!=request_id:
                self._poison('reply_id_mismatch');raise WorkerError('reply_id_mismatch')
            if time.monotonic()>deadline:
                self._poison('worker_timeout');raise WorkerError('worker_timeout')
            return reply

    def _stop(self):
        with self._state:proc=self._proc;self._proc=None
        try:self._writes.put_nowait(None)
        except queue.Full:pass
        if proc is not None:
            try:
                if proc.poll() is None:proc.kill()
                proc.wait(timeout=.25)
            except (OSError,subprocess.TimeoutExpired):
                if proc.poll() is None:
                    with self._state:self._failure='worker_termination_failed'
                    return False
        for thread in self._threads:
            if thread is not threading.current_thread():thread.join(timeout=.25)
        if proc is not None:
            for stream in (proc.stdin,proc.stdout):
                try:stream.close()
                except OSError:pass
        self._threads=[]
        return True

    def restart(self):
        """Explicit process epoch change. Never reuse an old motion after this."""
        with self._requests:
            with self._state:self._closed=True;self._generation+=1
            if not self._stop():raise WorkerError('worker_termination_failed')
            with self._state:
                self._failure=None;self._closed=False
                self._replies=queue.Queue(maxsize=2);self._writes=queue.Queue(maxsize=1)

    def close(self):
        with self._requests:
            with self._state:self._closed=True;self._generation+=1
            if not self._stop():raise WorkerError('worker_termination_failed')

    def __enter__(self):return self
    def __exit__(self,*_):self.close()


def _manifest_name(value):
    if not isinstance(value,str) or not 0<len(value)<=256:raise ValueError('manifest_path')
    value=value.replace('\\','/');path=PurePosixPath(value)
    if path.is_absolute() or '..' in path.parts or ':' in value:raise ValueError('manifest_path')
    return value

class WorkerMotion:
    supports_deadline=True
    def __init__(self,backend,scope,token,epoch,transport_epoch,start,goal,now,speed,seconds,*,lease=None,ownership_epoch=None):
        self._backend,self._scope,self._token=backend,scope,token
        self._lease=lease
        self._ownership_epoch=backend._route_owner_epoch(lease) if ownership_epoch is None else ownership_epoch
        self._epoch,self._transport_epoch=epoch,transport_epoch
        self.a,self.b=start,goal;self.desired_goal=backend._desired_goal
        self.t0,self.speed,self.seconds=now,speed,seconds
        dx,dz=goal[0]-start[0],goal[2]-start[2]
        self.heading=math.degrees(math.atan2(dx,dz)) if math.hypot(dx,dz)>1e-8 else None
        self.final_heading=self.heading
        self._last=MotionSample(start);self._now=None;self._halted=False;self._lock=threading.Lock();self._interrupted=threading.Event()
    def _halt(self,reason):
        self._halted=True
        self._last=MotionSample(self._last.position,blocked=True,reason=reason,heading=self.heading)
        return self._last
    def sample(self,now,*,deadline=None):
        own_deadline=math.nextafter(time.monotonic()+.05,-math.inf)
        deadline=min(own_deadline,deadline) if deadline is not None else own_deadline
        if not self._lock.acquire(blocking=False):
            self._interrupted.set()
            return MotionSample(self._last.position,blocked=True,reason='worker_busy',heading=self.heading)
        try:
            if self._interrupted.is_set() or not self._backend._valid(self):return self._halt('invalidated')
            if self._halted:return self._last
            now=_number(now)
            if self._now is not None and now<=self._now:return self._last
            try:
                reply=self._backend._sample(self,now,deadline=deadline)
                if reply.get('status')!='ready':return self._halt('worker_unavailable')
                position=tuple(x+y for x,y in zip(_vector(reply['position']),self._backend.offset))
                velocity=_vector(reply['velocity'])
                arrived,blocked,grounded=reply['arrived'],reply['blocked'],reply['grounded']
                if any(type(flag) is not bool for flag in (arrived,blocked,grounded)):raise ValueError('sample_flags')
                if math.hypot(velocity[0],velocity[2])>1e-8:self.heading=math.degrees(_number(reply['heading']))
                if (time.monotonic()>deadline or self._interrupted.is_set()
                        or not self._backend._valid(self)):return self._halt('invalidated')
                self._now=now
                if blocked:
                    self._last=MotionSample(position,grounded=grounded,heading=self.heading)
                    return self._halt('worker_blocked')
                self._last=MotionSample(position,velocity,arrived,False,grounded,None,self.heading)
                return self._last
            except Exception:return self._halt('worker_failed')
        finally:self._lock.release()

class WorkerBackend:
    """One asset/offline calibration. Caller selects the asset explicitly per map."""
    def __init__(self,transport,manifest,*,origin_offset=None):
        self.transport=transport;self.manifest=_manifest_name(manifest)
        self.offset=None if origin_offset is None else _vector(origin_offset)
        self._lock=threading.RLock();self._epoch=0;self._scope=None;self._scope_id=None
        self._token=None;self._loaded_epoch=None;self._desired_goal=None
        self._prepared_route=None
        self._hop_ground_failure=None;self._hop_owner=None
        self._route_owner_lock=threading.Lock();self._route_scheduler_owner=None;self._route_ownership_epoch=0
    @contextmanager
    def _hot_lock(self):
        acquired=self._lock.acquire(blocking=False)
        try:yield acquired
        finally:
            if acquired:self._lock.release()

    @property
    def prepared(self):
        with self._hot_lock() as acquired:
            if not acquired:return False
            return (self.offset is not None and getattr(self.transport,'offline',False) is True
                    and not self.transport.failed and self._loaded_epoch==self.transport.generation)
    def prepare(self):
        """Explicit cold path only; never invoke from a protocol post callback."""
        with self._lock:
            self._epoch+=1;self._token=None;self._prepared_route=None
            if self.offset is None:return Decision('unavailable',reason='calibration_missing')
            if getattr(self.transport,'offline',False) is not True:return Decision('unavailable',reason='offline_only')
            try:
                if self.transport.failed:self.transport.restart()
                if self._loaded_epoch!=self.transport.generation:
                    loaded=self.transport.request('load',manifest=self.manifest)
                    if loaded.get('status')!='ready' or loaded.get('navigationReady') is not True:
                        return Decision('unavailable',reason='navigation_unavailable')
                    self._loaded_epoch=self.transport.generation
                return Decision('ready')
            except Exception:return Decision('unavailable',reason='worker_failed')

    def acquire_route_scheduler(self,owner):
        """Reserve one scheduler owner without acquiring the cold backend lock."""
        if owner is None:raise ValueError('route_scheduler_owner')
        with self._route_owner_lock:
            if self._route_scheduler_owner is not None:raise RuntimeError('route_scheduler_already_owned')
            self._route_scheduler_owner=owner;self._route_ownership_epoch+=1

    def release_route_scheduler(self,owner):
        with self._route_owner_lock:
            if owner is None or self._route_scheduler_owner is not owner:return False
            self._route_scheduler_owner=None;self._route_ownership_epoch+=1
            return True

    def _route_owner_epoch(self,lease):
        if lease is not None and type(lease) is not RouteLease:return None
        with self._route_owner_lock:
            owner=self._route_scheduler_owner
            lease_owner=None if lease is None else lease._owner
            return self._route_ownership_epoch if lease_owner is owner else None

    def prepare_route(self,scope,start,goal,*,speed,lease=None,follow_intent=None,hop_scope=None):
        """Explicit COLD staging for imported-nav only; never called by plan_move.

        Requires an already-prepared asset and exact canonical scene object.
        It revokes an old motion/route even when staging fails. The single-use
        route binds input coordinates, speed, backend and process epochs. A new
        plan may only consume it; it cannot rebuild a route on the hot path.
        """
        if lease is not None and type(lease) is not RouteLease:raise ValueError('route_lease')
        ownership_epoch=self._route_owner_epoch(lease)
        if ownership_epoch is None:return Decision('unavailable',reason='route_scheduler_owned')
        if getattr(self.transport,'worker_profile',None)!=STAGED_PROFILE:
            return Decision('unavailable',reason='cold_route_profile_required')
        with self._lock:
            if self._route_owner_epoch(lease)!=ownership_epoch:return Decision('unavailable',reason='route_owner_changed')
            self._epoch+=1;self._token=None;self._prepared_route=None
            self._hop_ground_failure=None
            if lease is not None and not lease.unconsumed_valid:return Decision('unavailable',reason='route_lease_revoked')
            if self.offset is None:return Decision('unavailable',reason='calibration_missing')
            if getattr(self.transport,'offline',False) is not True:return Decision('unavailable',reason='offline_only')
            try:
                if scope is None:raise ValueError('scope_required')
                start,goal=_vector(start),_vector(goal);speed=_number(speed)
                if not 0<speed<=10:raise ValueError('speed')
                if not self.prepared:return Decision('unavailable',reason='worker_not_prepared')
                if self._scope is not scope:self._scope=scope;self._scope_id=uuid.uuid4().hex
                extra={};intent=None
                if hop_scope is not None:
                    from .hop_adapter_v20 import normalize_hop_scope
                    if getattr(self.transport,'hop_enabled',False) is not True:
                        raise ValueError('hop_transport_disabled')
                    extra['hopScope']=normalize_hop_scope(hop_scope)
                if follow_intent is not None:
                    intent=validate_follow_intent(follow_intent)
                    intent=dict(intent,goal=[x-y for x,y in zip(intent['goal'],self.offset)],
                                peer=[x-y for x,y in zip(intent['peer'],self.offset)])
                    extra['followIntent']=intent
                reply=self.transport.request('preparePlan',scope=self._scope_id,
                    start=[x-y for x,y in zip(start,self.offset)],goal=[x-y for x,y in zip(goal,self.offset)],speed=speed,**extra)
                if reply.get('status')!='ready':
                    failure=reply.get('groundFailure')
                    if hop_scope is not None and type(failure) is dict:
                        self._hop_ground_failure=dict(scope_object=scope,start=start,goal=goal,speed=speed,
                            hop_scope=extra['hopScope'],receipt=json.loads(json.dumps(failure,allow_nan=False)),
                            epoch=self._epoch,transport_epoch=self.transport.generation,scope_id=self._scope_id,
                            lease=lease,ownership_epoch=ownership_epoch)
                    return Decision('blocked' if reply.get('status')=='blocked' else 'unavailable',reason='worker_prepare_failed')
                receipt=None
                if intent is not None:
                    self.last_follow_intent_audit=_check_follow_prefix(reply,intent)
                    policy=_check_motor_policy(reply,requested_speed=speed)
                    commanded=reply['motorPolicyAudit']['commandedSpeedMps'] if policy is not None else speed
                    if abs(_number(reply['estimatedSeconds'])-_number(reply['length'])/commanded)>1e-6:
                        raise ValueError('follow_prefix_speed_receipt')
                    receipt=json.loads(json.dumps({k:reply.get(k) for k in
                        ('goal','path','length','pathLength','estimatedSeconds','followIntentAudit','motorPolicy','motorPolicyAudit')},allow_nan=False))
                elif reply.get('motorPolicy') is not None or reply.get('motorPolicyAudit') is not None:
                    raise ValueError('motor_policy_without_follow_intent')
                token=reply.get('preparedToken')
                if not isinstance(token,str) or not 1<=len(token)<=128 or reply.get('scope')!=self._scope_id:
                    raise ValueError('prepared_route_identity')
                if self._route_owner_epoch(lease)!=ownership_epoch:return Decision('unavailable',reason='route_owner_changed')
                if lease is not None and not lease.unconsumed_valid:return Decision('unavailable',reason='route_lease_revoked')
                self._prepared_route=(scope,start,goal,speed,token,self.transport.generation,self._epoch,lease,ownership_epoch,receipt)
                return Decision('ready')
            except Exception:return Decision('unavailable',reason='worker_prepare_failed')

    @property
    def last_hop_ground_failure(self):
        """Diagnostic copy only; the adapter consumes the internally bound record."""
        with self._hot_lock() as acquired:
            row=self._hop_ground_failure if acquired else None
            if (row is None or row['epoch']!=self._epoch or row['transport_epoch']!=self.transport.generation
                    or row['scope_object'] is not self._scope or row['scope_id']!=self._scope_id):return None
            return json.loads(json.dumps(row['receipt']))

    def create_hop_adapter(self,**kwargs):
        """Explicit independent hop path; ordinary ground planning is unchanged."""
        from .hop_adapter_v20 import HopAdapter
        return HopAdapter(self,**kwargs)

    def _valid(self,motion):
        with self._hot_lock() as acquired:
            if not acquired:return False
            return (motion._epoch==self._epoch and motion._scope is self._scope and motion._token==self._token
                    and motion._transport_epoch==self.transport.generation and not self.transport.failed
                    and (motion._lease is None or motion._lease.valid)
                    and motion._ownership_epoch is not None
                    and self._route_owner_epoch(motion._lease)==motion._ownership_epoch)
    def _sample(self,motion,now,*,deadline=None):
        with self._hot_lock() as acquired:
            if not acquired:return {'status':'unavailable','reason':'backend_busy'}
            if not self._valid(motion):return {'status':'unavailable'}
            return self.transport.request('sample',scope=self._scope_id,token=self._token,now=now,_deadline=deadline)
    def retarget_motion(self,motion,start,goal,speed):
        """Hot same-motor path update. No epoch change, load, time advance or sample."""
        if getattr(self.transport,'worker_profile',None)!=STAGED_PROFILE:
            return {'status':'needs_cold_replan','reason':'retarget_profile'}
        if verified_motion_step_height(motion)>.3:
            return {'status':'needs_cold_replan','reason':'motor_policy_requires_cold_preflight'}
        if not motion._lock.acquire(blocking=False):
            return {'status':'needs_cold_replan','reason':'retarget_motion_busy'}
        try:
            with self._hot_lock() as acquired:
                if not acquired:return {'status':'needs_cold_replan','reason':'retarget_backend_busy'}
                start,goal,speed=_vector(start),_vector(goal),_number(speed)
                cached=motion._last
                if (not self._valid(motion) or motion._halted or motion._interrupted.is_set()
                        or cached.arrived or cached.blocked or cached.grounded is not True
                        or motion._now is None or tuple(cached.position)!=start or speed!=motion.speed):
                    return {'status':'needs_cold_replan','reason':'retarget_motion_not_current'}
                try:
                    reply=self.transport.request('retarget',scope=self._scope_id,token=self._token,
                        start=[x-y for x,y in zip(start,self.offset)],
                        goal=[x-y for x,y in zip(goal,self.offset)],speed=speed)
                    if reply.get('status')=='needs_cold_replan':return {'status':'needs_cold_replan','reason':'retarget_requires_cold_route'}
                    if reply.get('status')!='ready' or not self._valid(motion):
                        raise ValueError('retarget_not_ready')
                    actual_start=tuple(x+y for x,y in zip(_vector(reply['start']),self.offset))
                    projected=tuple(x+y for x,y in zip(_vector(reply['goal']),self.offset))
                    seconds=_number(reply['estimatedSeconds']);length=_number(reply['pathLength'])
                    if (reply.get('token')!=motion._token or reply.get('scope')!=self._scope_id
                            or reply.get('grounded') is not True or math.dist(actual_start,start)>1e-6
                            or seconds<0 or not 0<length<=8.000001 or abs(seconds-length/speed)>1e-6):
                        raise ValueError('retarget_reply_identity')
                    return dict(status='ready',start=start,goal=projected,desired_goal=goal,
                                seconds=seconds,speed=speed,path_length=length)
                except Exception:
                    # A timeout or malformed success may have changed the worker
                    # path: stop rather than publish against obsolete metadata.
                    motion._halt('retarget_failed')
                    return {'status':'unavailable','reason':'retarget_failed'}
        finally:motion._lock.release()
    def plan_move(self,scope,start,goal,reason,limits):
        with self._hot_lock() as acquired:
            if not acquired:return Decision('unavailable',reason='backend_busy')
            self._epoch+=1;self._token=None
            staged_route,self._prepared_route=self._prepared_route,None
            if self.offset is None:return Decision('unavailable',reason='calibration_missing')
            if getattr(self.transport,'offline',False) is not True:return Decision('unavailable',reason='offline_only')
            try:
                if scope is None:raise ValueError('scope_required')
                start,goal=_vector(start),_vector(goal)
                now,speed=_number(limits['now']),_number(limits['speed'])
                if not 0<speed<=10:raise ValueError('speed')
                if self._scope is not scope:self._scope=scope;self._scope_id=uuid.uuid4().hex
                if not self.prepared:return Decision('unavailable',reason='worker_not_prepared')
                feet_start=[x-y for x,y in zip(start,self.offset)];feet_goal=[x-y for x,y in zip(goal,self.offset)]
                payload=dict(scope=self._scope_id,start=feet_start,goal=feet_goal,now=now,speed=speed)
                lease=None;ownership_epoch=self._route_owner_epoch(None);receipt=None
                if getattr(self.transport,'worker_profile',None)==STAGED_PROFILE:
                    if staged_route is None:return Decision('unavailable',reason='cold_route_not_prepared')
                    old_scope,old_start,old_goal,old_speed,prepared_token,transport_epoch,backend_epoch,lease,ownership_epoch,receipt=staged_route
                    if (old_scope is not scope or old_start!=start or old_goal!=goal or old_speed!=speed
                            or transport_epoch!=self.transport.generation or backend_epoch!=self._epoch-1):
                        return Decision('unavailable',reason='prepared_route_mismatch')
                    if self._route_owner_epoch(lease)!=ownership_epoch:return Decision('unavailable',reason='route_owner_changed')
                    if lease is not None and not lease.claim():return Decision('unavailable',reason='route_lease_revoked')
                    payload['preparedToken']=prepared_token
                reply=self.transport.request('plan',**payload)
                if self._route_owner_epoch(lease)!=ownership_epoch:return Decision('unavailable',reason='route_owner_changed')
                if lease is not None and not lease.valid:return Decision('unavailable',reason='route_lease_revoked')
                if reply.get('status')!='ready':
                    return Decision('blocked' if reply.get('status')=='blocked' else 'unavailable',reason='worker_plan_failed')
                if receipt is not None:
                    returned={k:reply.get(k) for k in receipt}
                    if json.dumps(returned,sort_keys=True,allow_nan=False)!=json.dumps(receipt,sort_keys=True,allow_nan=False):
                        raise ValueError('prepared_motor_receipt_mismatch')
                elif reply.get('motorPolicy') is not None or reply.get('motorPolicyAudit') is not None:
                    raise ValueError('unprepared_motor_policy')
                token=reply.get('token')
                if not isinstance(token,str) or not 1<=len(token)<=128:raise ValueError('token')
                actual_goal=tuple(x+y for x,y in zip(_vector(reply['goal']),self.offset))
                seconds=_number(reply['estimatedSeconds'])
                if seconds<0:raise ValueError('duration')
                self._token,self._desired_goal=token,goal
                motion=WorkerMotion(self,scope,token,self._epoch,self.transport.generation,start,actual_goal,now,speed,seconds,lease=lease,ownership_epoch=ownership_epoch)
                motion.motor_policy='default_step_0_3'
                if receipt is not None and receipt['motorPolicy']==_STEP_POLICY:
                    motion._motor_policy_receipt=(_STEP_POLICY,receipt['motorPolicyAudit'],_MOTOR_POLICY_SEAL)
                    motion.motor_policy=_STEP_POLICY
                    motion.requested_speed=speed
                    motion.speed=receipt['motorPolicyAudit']['commandedSpeedMps']
                self.last_motion_policy=motion.motor_policy
                face=limits.get('face_after')
                if face is not None:
                    face=_vector(face)
                    motion.final_heading=math.degrees(math.atan2(face[0]-actual_goal[0],face[2]-actual_goal[2]))
                return Decision('ready',motion)
            except Exception:return Decision('unavailable',reason='worker_failed')
    def invalidate(self):
        with self._lock:
            self._epoch+=1;self._token=None;self._scope=None;self._prepared_route=None
            try:return self.transport.request('invalidate').get('status')=='ready'
            except WorkerError:return False
    def set_manifest(self,manifest):
        manifest=_manifest_name(manifest)
        with self._lock:
            self._epoch+=1;self._token=None;self._scope=None;self._loaded_epoch=None;self.manifest=manifest;self._prepared_route=None
    def close(self):
        with self._lock:self._epoch+=1;self._token=None;self._prepared_route=None
        self.transport.close()
