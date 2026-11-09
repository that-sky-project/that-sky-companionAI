"""Default-disabled bounded-hop backend adapter, separate from ground motion.

Coordinates: proof local + translation origin = world feet; world feet plus
the already calibrated backend offset = avatar wire origin. No animation ID or
native-physics parity is inferred here. Worker loss is fail-stop, not teleport.
"""
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
import threading
import time

from .integration import Decision, _number, _vector
from .hop_contract_v20 import HopLimits, canonical_sha256, parse_hop_contract

_SEAL = object()
_FRAME_KEYS = ('step','phase','position','velocity','grounded','stateVerticalSpeed')
_CAUSES = {'collision_blocked','small_step_blocked','incomplete_polygon_corridor'}


def _need(value, reason):
    if not value:raise ValueError(reason)


def normalize_hop_scope(value):
    _need(type(value) is dict and set(value)=={'actorId','epoch','incarnation','mapId','roomId'},'hop_scope_schema')
    for key in ('epoch','incarnation','mapId'):
        _need(type(value[key]) is int and 0<=value[key]<=2**53-1,'hop_scope_number')
    for key in ('actorId','roomId'):
        _need(type(value[key]) is str and 0<len(value[key])<=256,'hop_scope_id')
    return {key:value[key] for key in ('actorId','epoch','incarnation','mapId','roomId')}


def _sha(value):
    _need(type(value) is str and re.fullmatch('[0-9a-f]{64}',value),'hop_sha256')
    return value


def _scope_sha(scope):
    # Node hashes JSON.stringify(normalized scope); all numeric fields are ints.
    return hashlib.sha256(json.dumps(scope,ensure_ascii=False,separators=(',',':')).encode('utf-8')).hexdigest()


def _envelope(receipt):
    _need(receipt['schema']=='bounded-hop-proof-v20' and receipt['warmupExecutorIntegrated'] is True,
          'hop_realtime_grounding_required')
    main=[{k:f[k] for k in _FRAME_KEYS} for f in receipt['mainTrace']]
    return dict(schema='bounded-hop-contract-v20',hopId=receipt['hopId'],
        scopeBindingSHA256=receipt['scopeBindingSHA256'],worldSHA256=receipt['worldSHA256'],
        receiptSHA256=canonical_sha256(receipt),fixedDt=receipt['fixedDt'],requestedStart=receipt['requestedStart'],
        startContactTrace=receipt['startContactTrace'],start=receipt['start'],landing=main[-1]['position'],
        mainTrace=main,settleTraces=[dict(fromStep=b['fromStep'],frames=[{k:f[k] for k in _FRAME_KEYS} for f in b['frames']])
                                   for b in receipt['settleTraces'] if b['fromStep']>0])


@dataclass(frozen=True)
class HopSample:
    position: tuple
    world_feet: tuple
    velocity: tuple=(0.,0.,0.)
    grounded: bool | None=None
    phase: str='grounding'
    state_vertical_speed: float | None=None
    hop_id: str | None=None
    arrived: bool=False
    blocked: bool=False
    reason: str | None=None
    heading: float | None=None
    validated_frames: tuple=()
    total_step: int=0
    _seal: object=None


@dataclass(frozen=True)
class HopPublicationFrame:
    """Actual validated fixed step; positions are not batch-average velocities."""
    position: tuple
    world_feet: tuple
    velocity: tuple
    grounded: bool
    phase: str
    state_vertical_speed: float | None
    total_step: int
    warmup_step: int
    step: int | None


class PreparedHop:
    def __init__(self, adapter, *, proof, token, binding, frame, expires_at, local_expires,
                 snapshot, start, goal, speed, lease, ownership_epoch, session_deadline, generation):
        self.adapter,self.proof,self.token=adapter,proof,token
        self.binding,self.frame=binding,frame
        self.expires_at,self.local_expires=expires_at,local_expires
        self.snapshot,self.start,self.goal=snapshot,start,goal
        self.speed,self.lease,self.ownership_epoch=speed,lease,ownership_epoch
        self.session_deadline,self.generation=session_deadline,generation
        self.claimed=False;self._seal=_SEAL;self.lock=threading.Lock()


class HopAdapter:
    def __init__(self,backend,*,enabled=False,proof_directory=None,expected_asset_sha256=None,
                 current_hop_scope=None,social_owned=None,clock=None,limits=None):
        _need(type(enabled) is bool,'hop_enabled_boolean')
        self.backend=backend;self.enabled=enabled
        self.clock=time.monotonic if clock is None else clock
        self.current_hop_scope,self.social_owned=current_hop_scope,social_owned
        self._generation=0
        self.directory=None
        self.asset_sha=None
        self.limits=limits or HopLimits(max_airtime=1.1,max_apex=1,max_drop=1,
                                       max_horizontal_speed=3,max_vertical_speed=6,max_distance=4)
        if enabled:
            _need(getattr(backend.transport,'hop_enabled',False) is True,'hop_transport_disabled')
            _need(proof_directory is not None and Path(proof_directory).is_absolute(),'hop_absolute_proof_directory')
            self.directory=Path(proof_directory).resolve()
            _need(self.directory==backend.transport.hop_proof_directory,'hop_proof_directory_binding')
            self.asset_sha=_sha(expected_asset_sha256)
            _need(callable(current_hop_scope) and callable(social_owned) and callable(self.clock),'hop_owner_callbacks')

    def _owner(self, hop_scope):
        try:
            return (self.social_owned() is False
                    and normalize_hop_scope(self.current_hop_scope())==hop_scope)
        except Exception:return False

    def _current(self, prepared, *, unclaimed=False):
        b=self.backend
        with b._hot_lock() as acquired:
            if not acquired:return False
            epoch,process,scope,scope_id,manifest,offset,hop_scope=prepared.snapshot
            try:now=_number(self.clock())
            except Exception:return False
            return (self.enabled and b._hop_owner is self and prepared.generation==self._generation
                    and b._epoch==epoch and b.transport.generation==process and not b.transport.failed
                    and b._scope is scope and b._scope_id==scope_id and b.manifest==manifest and b.offset==offset
                    and self._owner(hop_scope) and now<prepared.session_deadline and now<prepared.local_expires
                    and (prepared.lease is None or (prepared.lease.unconsumed_valid if unclaimed else prepared.lease.valid))
                    and b._route_owner_epoch(prepared.lease)==prepared.ownership_epoch)

    def _read_proof(self, reply, scope, start, offset):
        desc=reply['proofFile'];binding=reply['binding'];frame=reply['coordinateFrame'];asset=reply['assetBinding']
        _need(type(desc) is dict and desc['hashKind']=='sha256_exact_utf8_file_bytes'
              and desc['schema']=='bounded-hop-proof-v20','hop_proof_descriptor')
        _need(type(desc['bytes']) is int and 0<desc['bytes']<=8*1024*1024,'hop_proof_byte_budget')
        _sha(desc['sha256'])
        _need(type(binding) is dict and set(binding)=={'hopId','scopeBindingSHA256','worldSHA256'},'hop_binding_schema')
        _need(type(binding['hopId']) is str and re.fullmatch('[A-Za-z0-9_-]{1,128}',binding['hopId']),'hop_id')
        path=Path(desc['file'])
        _need(path.is_absolute() and not path.is_symlink(),'hop_proof_path')
        resolved=path.resolve(strict=True)
        _need(resolved.parent==self.directory and self.directory.resolve(strict=True)==self.directory
              and resolved.name=='hop-'+binding['hopId']+'.json','hop_proof_path_escape')
        with resolved.open('rb') as handle:
            raw=handle.read(8*1024*1024+1)
        _need(len(raw)==desc['bytes'] and hashlib.sha256(raw).hexdigest()==desc['sha256'],'hop_raw_proof_digest')
        def reject(_):raise ValueError('hop_nonfinite_json')
        receipt=json.loads(raw,parse_constant=reject)
        _need(receipt['scope']==scope and _scope_sha(scope)==binding['scopeBindingSHA256'], 'hop_scope_binding')
        _need(all(receipt[k]==v for k,v in binding.items()),'hop_receipt_binding')
        _need(asset['assetManifestSHA256']==self.asset_sha
              and asset['expectedWorldSHA256']==binding['worldSHA256'],'hop_asset_binding')
        _need(type(frame) is dict and set(frame)=={'kind','origin'} and frame['kind']=='translation_only','hop_frame_kind')
        origin=_vector(frame['origin']);local_start=_vector(receipt['requestedStart'])
        _need(math.dist(tuple(local_start[i]+origin[i]+offset[i] for i in range(3)),start)<=1e-5,'hop_start_frame_binding')
        envelope=_envelope(receipt)
        proof=parse_hop_contract(envelope,expected_proof_sha256=canonical_sha256(envelope),
            expected_receipt_sha256=canonical_sha256(receipt),expected_scope_binding_sha256=_scope_sha(scope),
            expected_world_sha256=_sha(binding['worldSHA256']),limits=self.limits,enabled=True)
        return proof,dict(binding),dict(kind='translation_only',origin=list(origin))

    def prepare(self,scope,start,goal,*,speed,lease=None,session_deadline,ttl_seconds=5.):
        if not self.enabled:return Decision('unavailable',reason='hop_disabled')
        b=self.backend
        with b._lock:
            try:
                start,goal,speed=_vector(start),_vector(goal),_number(speed)
                deadline,ttl=_number(session_deadline),_number(ttl_seconds)
                _need(0<speed<=3 and 1.5<=ttl<=15,'hop_speed_or_ttl')
                row=b._hop_ground_failure
                _need(row is not None and row['epoch']==b._epoch and row['transport_epoch']==b.transport.generation
                      and row['scope_object'] is scope and row['start']==start and row['goal']==goal
                      and row['speed']==speed and row['scope_id']==b._scope_id and row['lease'] is lease,
                      'hop_ground_failure_binding')
                failure=row['receipt'];hop_scope=row['hop_scope']
                _need(failure.get('cause') in _CAUSES and type(failure.get('id')) is str,'hop_actual_ground_failure_required')
                _need(b.prepared and b.offset is not None and self._owner(hop_scope),'hop_owner_or_asset')
                owner=b._route_owner_epoch(lease)
                _need(owner is not None and owner==row['ownership_epoch'],'hop_route_owner')
                if lease is not None:_need(lease.unconsumed_valid,'hop_lease_revoked')
                now=self.clock();_need(now<deadline,'hop_session_expired')
                self._generation+=1;b._hop_owner=self
                generation=self._generation
                snapshot=(b._epoch,b.transport.generation,scope,b._scope_id,b.manifest,b.offset,dict(hop_scope))
                reply=b.transport.request('prepareHop',scope=b._scope_id,hopScope=hop_scope,
                    start=[x-y for x,y in zip(start,b.offset)],goal=[x-y for x,y in zip(goal,b.offset)],
                    speed=speed,ttlSeconds=ttl,artifactDirectory=str(self.directory),groundFailureId=failure['id'])
                _need(reply.get('status')=='ready','hop_prepare_unavailable')
                token=reply['token'];_need(type(token) is str and 0<len(token)<=128,'hop_token')
                proof,binding,frame=self._read_proof(reply,hop_scope,start,b.offset)
                prepared=PreparedHop(self,proof=proof,token=token,binding=binding,frame=frame,
                    expires_at=_number(reply['expiresAt']),local_expires=now+ttl,snapshot=snapshot,
                    start=start,goal=goal,speed=speed,lease=lease,ownership_epoch=owner,
                    session_deadline=deadline,generation=generation)
                _need(self._current(prepared,unclaimed=True),'hop_changed_during_prepare')
                _need(self.clock()+len(proof.warmup)*proof.dt+proof.duration+self.limits.deadline_margin<min(deadline,prepared.local_expires),
                      'hop_insufficient_time_to_land')
                return Decision('ready',prepared)
            except Exception as exc:return Decision('unavailable',reason=str(exc))

    def start(self,prepared,*,now,published_start):
        start_sent=False
        try:
            _need(type(prepared) is PreparedHop and prepared.adapter is self and prepared._seal is _SEAL,'hop_prepared_identity')
            _need(prepared.lock.acquire(blocking=False),'hop_start_busy')
            try:
                _need(not prepared.claimed and self._current(prepared,unclaimed=True),'hop_prepared_revoked')
                prepared.claimed=True
                _need(math.dist(_vector(published_start),prepared.start)<=self.limits.position_tolerance,'hop_published_start_changed')
                now=_number(now);_need(abs(now-self.clock())<=.05,'hop_caller_clock')
                if prepared.lease is not None:_need(prepared.lease.claim(),'hop_lease_revoked')
                b=self.backend;start_sent=True
                reply=b.transport.request('startHop',scope=prepared.snapshot[3],hopScope=prepared.snapshot[6],token=prepared.token)
                _need(reply.get('status')=='ready' and self._current(prepared),'hop_start_unavailable')
                motion=HopMotion(prepared,reply,now)
                return Decision('ready',motion)
            finally:prepared.lock.release()
        except Exception as exc:
            if start_sent:
                self._generation+=1
                try:self.backend.transport.request('hardCancelHop',scope=prepared.snapshot[3],hopScope=prepared.snapshot[6],
                                                   token=prepared.token,reason='python_start_validation_failed')
                except Exception:pass
            return Decision('unavailable',reason=str(exc))


class HopMotion:
    supports_deadline=True
    def __init__(self,prepared,initial,now):
        self._prepared=prepared;self._adapter=prepared.adapter;self._lock=threading.Lock();self._revoked=threading.Event()
        self._proof=prepared.proof;self._last_local=self._proof.requested_start;self._last_total=0
        self._worker_start=_number(initial['now']);self._last_worker_now=self._worker_start
        self._local_start=self._adapter.clock();self._now=now;self._published=None
        self._sample_received_at=self._local_start
        self._soft_landing=False
        self.a=prepared.start;self.desired_goal=prepared.goal
        self.b=self._wire(self._proof.landing);self.t0=now;self.speed=prepared.speed
        self.seconds=len(self._proof.warmup)*self._proof.dt+self._proof.duration
        self.heading=self.final_heading=None
        _need(initial['binding']==prepared.binding and initial['coordinateFrame']==prepared.frame,'hop_initial_binding')
        _need(_vector(initial['position'])==self._proof.requested_start and initial['totalStep']==0
              and initial['acceptedWarmupFrames']==[] and initial['acceptedFrames']==[]
              and type(initial['grounded']) is bool and initial['phase']=='grounding','hop_initial_moved')
        self._worker_deadline=min(prepared.expires_at,self._worker_start+prepared.session_deadline-self._local_start)
        self._ground=self._proof.start_grounding(now=self._worker_start,deadline=self._worker_deadline,
            scope=self._proof.scope,world=self._proof.world,published_start=self._proof.requested_start)
        self._run=None
        self._last=HopSample(self.a,self._feet(self._last_local),grounded=initial['grounded'],hop_id=self._proof.hop_id,_seal=_SEAL)

    def _feet(self,local):return tuple(x+y for x,y in zip(local,self._prepared.frame['origin']))
    def _wire(self,local):return tuple(x+y for x,y in zip(self._feet(local),self._prepared.snapshot[5]))
    def _valid(self):return not self._revoked.is_set() and self._adapter._current(self._prepared)

    def _halt(self,reason):
        self._revoked.set()
        self._ground.cancel(reason)
        if self._run:self._run.cancel(reason)
        self._last=HopSample(self._last.position,self._last.world_feet,grounded=self._last.grounded,
            phase=self._last.phase,hop_id=self._proof.hop_id,blocked=True,reason=reason,heading=self.heading,_seal=_SEAL)
        return self._last

    def invalidate(self):self._revoked.set()

    def cancel(self,reason='hard_cancelled'):
        self._revoked.set()
        if not self._lock.acquire(blocking=False):return
        try:
            self._halt(reason)
            try:self._rpc('hardCancelHop',reason=str(reason)[:128])
            except Exception:pass
        finally:self._lock.release()

    def _rpc(self,op,**extra):
        p=self._prepared
        return self._adapter.backend.transport.request(op,scope=p.snapshot[3],hopScope=p.snapshot[6],token=p.token,**extra)

    def is_current_sample(self,sample):
        try:
            return (type(sample) is HopSample and sample is self._last and sample._seal is _SEAL
                    and not sample.blocked and bool(sample.validated_frames) and self._valid()
                    and 0<=self._adapter.clock()-self._sample_received_at<=min(.1,self._proof.limits.max_sample_gap))
        except Exception:return False

    def consume_publication(self,sample):
        """One-shot permit for the latest sealed sample; not a blanket air override."""
        if not self._lock.acquire(blocking=False):return False
        try:
            if not self.is_current_sample(sample) or self._published is sample:return False
            self._published=sample;return True
        finally:self._lock.release()

    def _validate_packet(self,packet):
        p=self._prepared;proof=self._proof
        _need(packet.get('status')=='ready' and packet['binding']==p.binding
              and packet['coordinateFrame']==p.frame,'hop_batch_binding')
        worker_now=_number(packet['now']);local_now=self._adapter.clock()
        _need(worker_now>=self._last_worker_now and abs((worker_now-self._worker_start)-(local_now-self._local_start))<=.10,
              'hop_worker_clock_drift')
        _need(worker_now<self._worker_deadline,'hop_worker_deadline')
        warm,frames=packet['acceptedWarmupFrames'],packet['acceptedFrames']
        # The engine labels the physical final contact frame "grounding" while
        # its authoritative top-level cancellation result is cancelled_landed.
        # Project only that control-state label into the sealed contract. Never
        # change contact flags, times, positions or velocities.
        if (self._soft_landing and self._run is None and packet.get('phase')=='cancelled_landed'
                and packet.get('terminal') is True):
            _need(type(warm) is list and warm and not frames and warm[-1]['grounded'] is True
                  and warm[-1]['phase'] in ('grounding','cancelled_landed'),'hop_grounding_cancel_terminal')
            warm=[dict(f) for f in warm];warm[-1]['phase']='cancelled_landed'
        accepted=[]
        args=dict(now=worker_now,scope=proof.scope,world=proof.world)
        if warm:
            self._ground.accept_batch(warm,**args)
            if self._ground.terminal_reason=='grounded' and self._run is None:
                ticket=self._ground.handoff()
                self._run=proof.start_run(now=ticket.hop_start,deadline=self._worker_deadline,
                                         scope=proof.scope,world=proof.world,grounding_receipt=ticket)
            for f in warm:accepted.append(HopPublicationFrame(self._wire(f['position']),self._feet(f['position']),
                _vector(f['velocity']),f['grounded'],f['phase'],None,f['totalStep'],f['warmupStep'],None))
        if frames:
            _need(self._run is not None,'hop_before_grounding')
            self._run.accept_batch(frames,**args)
            for f in frames:accepted.append(HopPublicationFrame(self._wire(f['position']),self._feet(f['position']),
                _vector(f['velocity']),f['grounded'],f['phase'],_number(f['stateVerticalSpeed']),f['totalStep'],f['warmupStep'],f['step']))
        position=_vector(packet['position']);velocity=_vector(packet['velocity'])
        _need(type(packet['totalStep']) is int and packet['totalStep']>=self._last_total,'hop_batch_total_step')
        if not accepted:
            _need(position==self._last_local and packet['totalStep']==self._last_total,'hop_unproved_movement')
            self._last_worker_now=worker_now
            return self._last
        last=accepted[-1];n=packet['totalStep']-self._last_total
        _need(last.total_step==packet['totalStep'] and self._wire(position)==last.position
              and packet['grounded'] is last.grounded and packet['phase']==last.phase,'hop_top_frame_mismatch')
        if n>0:
            average=tuple((position[i]-self._last_local[i])/(n*proof.dt) for i in range(3))
            _need(math.dist(average,velocity)<=1e-4,'hop_average_velocity_mismatch')
        else:_need(velocity==(0.,0.,0.),'hop_zero_time_velocity')
        if 'worldFeetPosition' in packet:_need(math.dist(_vector(packet['worldFeetPosition']),last.world_feet)<=1e-5,'hop_world_feet_binding')
        terminal=(self._run is not None and self._run.terminal_reason in ('landed','cancelled_landed')) or self._ground.terminal_reason=='cancelled_landed'
        _need(type(packet['terminal']) is bool and packet['terminal']==terminal,'hop_terminal_mismatch')
        if math.hypot(velocity[0],velocity[2])>1e-8:self.heading=math.degrees(math.atan2(velocity[0],velocity[2]))
        self._last_local=position;self._last_total=packet['totalStep'];self._last_worker_now=worker_now
        return HopSample(last.position,last.world_feet,velocity,last.grounded,last.phase,last.state_vertical_speed,
            proof.hop_id,terminal,False,None,self.heading,tuple(accepted),packet['totalStep'],_SEAL)

    def sample(self,now,*,deadline=None):
        if not self._lock.acquire(blocking=False):
            self._revoked.set();return HopSample(self._last.position,self._last.world_feet,blocked=True,reason='hop_busy')
        try:
            if not self._valid():return self._halt('hop_invalidated')
            if self._last.arrived or self._last.blocked:return self._last
            now=_number(now)
            _need(now>=self._now,'hop_caller_clock_reversed')
            if now==self._now:return self._last
            _need(abs(now-self._adapter.clock())<=.10,'hop_caller_clock')
            deadline=min(time.monotonic()+.05,_number(deadline)) if deadline is not None else time.monotonic()+.05
            packet=self._rpc('sampleHop',_deadline=deadline)
            result=self._validate_packet(packet)
            _need(time.monotonic()<=deadline and self._valid(),'hop_changed_during_sample')
            self._now=now
            if result is not self._last:self._sample_received_at=self._adapter.clock()
            self._last=result;return result
        except Exception as exc:return self._halt(str(exc))
        finally:self._lock.release()

    def request_landing(self):
        if not self._lock.acquire(blocking=False):self._revoked.set();return False
        try:
            _need(self._valid() and not self._last.arrived,'hop_cancel_not_current')
            reply=self._rpc('softCancelHop')
            _need(reply.get('status')=='ready' and reply['binding']==self._prepared.binding,'hop_soft_cancel_binding')
            if 'fromStep' in reply:
                _need(reply['fromStep']==max(0,self._last_total-len(self._proof.warmup)), 'hop_cancel_origin')
            args=dict(now=_number(reply['now']),scope=self._proof.scope,world=self._proof.world)
            if self._run:self._run.request_landing(**args)
            else:self._ground.request_landing(**args)
            self._soft_landing=True
            if reply.get('terminal') is True:
                _need(self._last.phase=='takeoff' and self._last.grounded is True and reply.get('phase')=='cancelled_landed'
                      and reply.get('grounded') is True and self._wire(_vector(reply['position']))==self._last.position,
                      'hop_cancel_takeoff_terminal')
                self._last=HopSample(self._last.position,self._last.world_feet,grounded=True,phase='cancelled_landed',
                    state_vertical_speed=0.,hop_id=self._proof.hop_id,arrived=True,heading=self.heading,
                    validated_frames=self._last.validated_frames,total_step=self._last_total,_seal=_SEAL)
                self._sample_received_at=self._adapter.clock()
            _need(self._valid(),'hop_changed_during_soft_cancel')
            return True
        except Exception as exc:self._halt(str(exc));return False
        finally:self._lock.release()
