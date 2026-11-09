"""Explicit follow-only hop coordinator. Not installed by the existing entry.

Run prepare_follow in the existing cold job BEFORE its failed-ground branch
cancels the RouteLease. A HopSelection is not a CompatibleMotion and must never
enter _BudgetedMotion. The owner must share publication_lock with social/map
takeover and call tick_actor on the current outgoing Actor, not a saved capture.
"""
from dataclasses import dataclass
import copy
import math
from pathlib import Path
import hashlib
import threading
import time
import types

from skyterrain.backend import RouteLease
from skyterrain.hop_adapter_v20 import HopAdapter, HopMotion, PreparedHop, normalize_hop_scope
from skyterrain.integration import Decision, _number, _vector
from hop_actor_profile_v20 import HopActorEncoder, EngineeringTrial
import hop_actor_profile_v20 as actor_profile

_SEAL=object()
_CAUSES={'collision_blocked','small_step_blocked','incomplete_polygon_corridor','goal_support_slope','ground_contact_gap','ground_route_incomplete'}


def need(value,reason):
    if not value:raise ValueError(reason)


@dataclass(frozen=True)
class LiveFollowState:
    """From live controller memory, never inferred from a stale control file.

    request_seq is the explicit dispatcher command revision, not the counter
    advanced by terrain subsegments. scope is the backend's canonical SceneScope.
    hop_scope remains readable after a same-owner stop clears controller.follow.
    """
    scope: object
    hop_scope: dict
    request_seq: int
    target_key: str | None
    explicit_follow: bool
    social_owned: bool
    connected: bool
    deadline: float


@dataclass(frozen=True)
class FollowPermit:
    coordinator: object
    request_seq: int
    target_key: str
    scope: object
    hop_scope: tuple
    expires: float
    session_deadline: float
    _seal: object


@dataclass(frozen=True)
class HopSelection:
    """Only this tagged cold result may select the independent air publisher."""
    coordinator: object
    permit: FollowPermit
    prepared: PreparedHop
    start: tuple
    goal: tuple
    speed: float
    lease: RouteLease
    generation: int
    _seal: object


@dataclass(frozen=True)
class HopTick:
    body: bytes
    emitted: bool
    phase: str | None=None
    needs_follow_resume: bool=False
    reason: str | None=None
    world_feet: tuple | None=None
    wire_origin: tuple | None=None


class FollowHopCoordinator:
    def __init__(self,backend,*,enabled=False,current_state=None,adapter=None,
                 encoder_factory=None,publication_lock=None,clock=time.monotonic):
        need(type(enabled) is bool,'follow_hop_enabled_boolean')
        self.backend,self.enabled,self.clock=backend,enabled,clock
        self.current_state,self.adapter,self.encoder_factory=current_state,adapter,encoder_factory
        self.lock=publication_lock if publication_lock is not None else threading.RLock()
        self._generation=0;self._pending=None;self._motion=None;self._encoder=None
        self._permit=None;self._soft_stopping=False;self._cold=False;self._retired=None
        self._last_sample_sent=None
        self._last_patch=None;self._last_emit_time=None
        self._dispatch_permit=None
        self.state='idle';self.last_reason=None;self.last_prepare_timings=None
        if enabled:
            need(type(adapter) is HopAdapter and adapter.backend is backend and adapter.enabled,'explicit_hop_adapter')
            need(callable(current_state) and callable(encoder_factory) and publication_lock is not None,
                 'live_controller_and_publication_owner_required')

    def _live(self):
        value=self.current_state()
        need(type(value) is LiveFollowState,'live_follow_state_required')
        need(type(value.request_seq) is int and value.request_seq>=0,'dispatch_revision_required')
        need(type(value.explicit_follow) is bool and type(value.social_owned) is bool
             and type(value.connected) is bool,'live_follow_flags')
        normalize_hop_scope(value.hop_scope);_number(value.deadline)
        return value

    def _same_owner(self,permit):
        live=self._live()
        need(live.scope is permit.scope and tuple(normalize_hop_scope(live.hop_scope).items())==permit.hop_scope,
             'follow_owner_or_scene_changed')
        need(live.connected and not live.social_owned,'follow_social_or_connection_takeover')
        need(self.clock()<min(live.deadline,permit.session_deadline),'follow_session_expired')
        return live

    def _current(self,permit,*,preparing=False,allow_stopped=False):
        need(type(permit) is FollowPermit and permit.coordinator is self and permit._seal is _SEAL,'follow_permit_required')
        live=self._same_owner(permit)
        if not allow_stopped:
            need(live.explicit_follow and live.request_seq==permit.request_seq and live.target_key==permit.target_key,
                 'explicit_follow_changed_or_cleared')
        if preparing:need(self.clock()<permit.expires,'follow_permit_expired')
        return live

    def authorize_dispatched_follow(self,*,request_seq,target_key,scope,ttl=5.):
        """Called by the existing structured follow dispatcher, not an LLM reply.

        Its fields must still match live controller.follow and the command
        revision after dispatch. Neither a file labelled follow nor plain chat
        can create authority through this interface on its own.
        """
        need(self.enabled,'follow_hop_disabled');ttl=_number(ttl)
        need(0<ttl<=10,'follow_permit_ttl')
        with self.lock:
            live=self._live()
            need(live.explicit_follow and live.scope is scope and live.request_seq==request_seq
                 and type(target_key) is str and target_key and live.target_key==target_key,'dispatcher_follow_not_current')
            permit=FollowPermit(self,request_seq,target_key,scope,tuple(normalize_hop_scope(live.hop_scope).items()),
                                self.clock()+ttl,live.deadline,_SEAL)
            self._current(permit,preparing=True)
            self._dispatch_permit=permit
            return permit

    def authorize_slash_follow(self,text,**kwargs):
        need(type(text) is str and text.strip() in ('/follow','/跟我走','/跟着我'),'explicit_slash_follow_required')
        return self.authorize_dispatched_follow(**kwargs)

    def refresh_dispatched_follow(self,permit,*,ttl=5.):
        """Renew an existing unchanged live follow, never create it from a file.

        The original session deadline is retained even if a caller reports a
        later deadline. Clearing/replacing the command or owner forbids renewal.
        """
        ttl=_number(ttl);need(0<ttl<=10,'follow_permit_ttl')
        with self.lock:
            need(permit is self._dispatch_permit,'dispatcher_permit_not_current')
            self._current(permit)
            renewed=FollowPermit(self,permit.request_seq,permit.target_key,permit.scope,permit.hop_scope,
                                 self.clock()+ttl,permit.session_deadline,_SEAL)
            self._dispatch_permit=renewed
            return renewed

    @property
    def suppress_ground_replan(self):
        with self.lock:return self._pending is not None or self._motion is not None or self._cold

    @property
    def active(self):
        with self.lock:return self._motion is not None

    def prepare_follow(self,scope,start,goal,*,speed,lease=None,follow_intent=None,permit=None):
        """Single cold job: original ground success is returned unchanged.

        The caller's failed-ground cancellation branch must run only after this
        method returns a final non-ready result. No ground token is substituted.
        """
        with self.lock:
            if self.suppress_ground_replan:return Decision('unavailable',reason='hop_blocks_ground_replan')
        if not self.enabled or permit is None:
            return self.backend.prepare_route(scope,start,goal,speed=speed,lease=lease,follow_intent=follow_intent)
        generation=None;started=time.perf_counter();ground_ms=hop_ms=0.
        try:
            with self.lock:
                self._current(permit,preparing=True)
                need(scope is permit.scope and follow_intent is not None,'explicit_follow_route_required')
                need(type(lease) is RouteLease and lease.unconsumed_valid,'follow_route_lease_required')
                need(not self.suppress_ground_replan and self._retired is None,'hop_blocks_ground_replan')
                self._generation+=1;generation=self._generation;self._cold=True
                start,goal,speed=_vector(start),_vector(goal),_number(speed)
            ground_started=time.perf_counter()
            try:
                result=self.backend.prepare_route(scope,start,goal,speed=speed,lease=lease,
                    follow_intent=follow_intent,hop_scope=dict(permit.hop_scope),
                    continuation_key=hashlib.sha256((str(permit.request_seq)+'\0'+permit.target_key).encode()).hexdigest())
            finally:ground_ms=(time.perf_counter()-ground_started)*1000.
            with self.lock:
                self._current(permit,preparing=True)
                need(generation==self._generation and lease.unconsumed_valid,'follow_prepare_revoked')
            if result.status=='ready':
                self.last_reason=None
                return result
            failure=self.backend.last_hop_ground_failure
            if not failure or failure.get('cause') not in _CAUSES:return result
            hop_started=time.perf_counter()
            try:
                hop=self.adapter.prepare(scope,start,goal,speed=speed,lease=lease,
                                         session_deadline=permit.session_deadline,
                                         **({'ttl_seconds':12.} if getattr(self.adapter,'aerial_profiles',()) else {}))
            finally:hop_ms=(time.perf_counter()-hop_started)*1000.
            if hop.status!='ready':return hop
            with self.lock:
                self._current(permit,preparing=True)
                need(generation==self._generation and lease.unconsumed_valid,'hop_prepare_revoked')
                selection=HopSelection(self,permit,hop.motion,start,goal,speed,lease,generation,_SEAL)
                self._pending=selection;self.state='hop_prepared';self.last_reason=None
                return Decision('ready',selection)
        except Exception as exc:
            self.last_reason=str(exc);return Decision('unavailable',reason=str(exc))
        finally:
            if generation is not None:
                with self.lock:
                    self._cold=False
                    self.last_prepare_timings=dict(ground_ms=round(ground_ms,3),hop_ms=round(hop_ms,3),
                        total_ms=round((time.perf_counter()-started)*1000.,3),
                        permit_remaining_ms=round(max(0.,permit.expires-self.clock())*1000.,3))

    def start_hop(self,selection,*,now,published_start):
        with self.lock:
            try:
                need(self.enabled and type(selection) is HopSelection and selection is self._pending
                     and selection._seal is _SEAL and selection.coordinator is self
                     and selection.generation==self._generation,'hop_selection_identity')
                self._current(selection.permit,preparing=True)
                encoder=self.encoder_factory()
                need(type(encoder) is HopActorEncoder and encoder.engineering_trial is not None,'explicit_engineering_actor_encoder')
                encoder.engineering_trial.validate(encoder.profile_id)
                result=self.adapter.start(selection.prepared,now=now,published_start=published_start)
                self._pending=None
                if result.status!='ready':return result
                need(type(result.motion) is HopMotion,'sealed_hop_motion_required')
                self._motion,self._encoder,self._permit=result.motion,encoder,selection.permit
                self._last_sample_sent=None
                self._last_patch=None;self._last_emit_time=None
                self._entry_raw_identity=None;self._entry_bridge_stamp=None
                self._soft_stopping=False;self.state='hopping';self.last_reason=None
                return result
            except Exception as exc:
                self._pending=None;self.last_reason=str(exc)
                return Decision('unavailable',reason=str(exc))

    def hard_cancel(self,reason='follow_hard_cancel'):
        """Immediate zero-publication revocation, no RPC under owner lock."""
        with self.lock:
            self._generation+=1;self._pending=None
            self._dispatch_permit=None
            if self._motion is not None:
                self._motion.invalidate();self._retired=self._motion
            if self._encoder is not None:self._encoder.closed=True
            self._motion=self._encoder=None;self.state='stopped';self.last_reason=reason

    def drain_cleanup(self):
        """Optional cold owner cleanup; never called from the Actor callback."""
        with self.lock:motion,self._retired=self._retired,None
        if motion is not None:motion.cancel('follow_coordinator_revoked')

    def request_stop(self):
        """Same-owner user stop: preserve lease and finish proven natural landing.

        Call this instead of ordinary runtime.cancel until contact. A map/social
        takeover never enters this branch and must immediately hard_cancel.
        """
        with self.lock:
            try:
                need(self._motion is not None,'no_hop_to_settle')
                self._same_owner(self._permit)
                need(self._motion.request_landing(),'hop_soft_cancel_failed')
                self._soft_stopping=True;self.state='settling';return True
            except Exception as exc:
                self.hard_cancel(str(exc));return False

    def tick_actor(self,body,*,now,emit):
        """Publish only using the dedicated engineering encoder, never ground KCC guards.

        emit receives HopActorPatch synchronously while publication_lock is held.
        The caller updates controller.position only inside that guarded callback.
        """
        with self.lock:
            if self._motion is None:return HopTick(body,False,reason='no_active_hop')
            motion=self._motion
            try:
                self._current(self._permit,allow_stopped=self._soft_stopping)
                sample=motion.sample(now)
                need(not sample.blocked,sample.reason or 'hop_sample_blocked')
                if sample is self._last_sample_sent and self._last_patch is not None:
                    return self._hold_fixed_frame(body,motion,sample,now,emit)
                if not sample.validated_frames:
                    return HopTick(body,False,phase=sample.phase,reason='no_new_fixed_step')
                published=[]
                def guarded_emit(patch):
                    self._current(self._permit,allow_stopped=self._soft_stopping)
                    need(self._motion is motion and motion.is_current_sample(sample),'hop_owner_changed_before_emit')
                    emit(patch);published.append(patch)
                self._encoder.engineering_trial_emit(body,motion,sample,guarded_emit)
                need(len(published)==1,'hop_not_emitted_once')
                self._last_sample_sent=sample
                self._last_patch=published[0];self._last_emit_time=now
                patch=published[0];resume=False
                if sample.arrived:
                    need(sample.grounded is True and patch.ground_handoff_required,'real_kcc_landing_handoff_required')
                    resume=not self._soft_stopping
                    self._motion=self._encoder=None;self.state='landed';self._generation+=1
                return HopTick(patch.actor_bytes,True,sample.phase,resume,None,sample.world_feet,sample.position)
            except Exception as exc:
                self.hard_cancel(str(exc));return HopTick(body,False,reason=str(exc))

    def _hold_fixed_frame(self,body,motion,sample,now,emit):
        """Sub-fixed-step packet only: retain, never advance/reconsume, a sealed frame."""
        need(0<=now-self._last_emit_time<=1/60+1e-6,'held_fixed_frame_expired')
        need(motion.is_current_sample(sample),'held_frame_revoked')
        e=self._encoder;a=e.anim_pose
        before=a.inspect_actor_wire(body,verified_profile_sha256=actor_profile.CODEC_SHA256,check_zero_padding=False)
        loc=a._locate(before);fields={n:a._val(n,before._tokens[i]) for n,i in loc.items()}
        stamp=tuple(fields[n] for n in ('key','gen','start'))
        identity=actor_profile._source_identity(body,before,a)
        need((stamp==e.seed_stamp and identity==e.seed_identity)
             or (stamp==e.expected_animation and identity==e.output_identity),'held_frame_source_changed')
        clock=copy.copy(e.clock);tick=clock.observe_body(body,a,owner=motion)
        frame=sample.validated_frames[-1]
        values=dict(zip(('ox','oy','oz'),frame.position));values.update(zip(('ax','ay','az'),frame.position))
        values.update(zip(('vx','vy','vz'),a.velocity_codes(frame.velocity)))
        values.update(zip(('key','gen','start'),e.expected_animation))
        values.update(e.facing_values())
        patched,changed=actor_profile._verified_write(body,values,a,before,loc)
        if getattr(e.engineering_trial,'aerial_profiles',()):
            patched=e.project_retained_aerial(patched)
            changed=sum((x^y).bit_count() for x,y in zip(body,patched))
        self._current(self._permit,allow_stopped=self._soft_stopping)
        need(self._motion is motion and motion.is_current_sample(sample),'held_frame_changed_before_emit')
        old=self._last_patch
        result=actor_profile.HopActorPatch(patched,old.phase,old.grounded_by_kcc,False,old.native_state_name,
            tuple(sorted(values)),changed,tick,old.total_step,'retained_fixed_step',heading=e.heading)
        # Publication is a re-advertisement of the same proven step, not a new
        # sample permit. Never consume_publication or advance the proof here.
        emit(result);e.clock=clock
        after=a.inspect_actor_wire(patched,verified_profile_sha256=actor_profile.CODEC_SHA256,check_zero_padding=False)
        e.output_identity=actor_profile._source_identity(patched,after,a)
        return HopTick(patched,True,old.phase,False,'retained_fixed_step',sample.world_feet,sample.position)


def is_hop_selection(decision):
    return type(decision) is Decision and decision.status=='ready' and type(decision.motion) is HopSelection


def attach_bounded_runtime(runtime,coordinator):
    """Explicit install only after the proposed runtime/entry seam is applied."""
    need(getattr(runtime,'hop_follow_seam_v20',False) is True,'hop_runtime_seam_not_installed')
    need(type(coordinator) is FollowHopCoordinator and coordinator.enabled
         and coordinator.backend is runtime.backend,'hop_runtime_backend_identity')
    stop=getattr(runtime.controller,'_terrain_stop',None)
    need(stop is not None and stop[0] is coordinator.lock,'shared_actor_publication_lock_required')
    need(getattr(runtime,'_follow_hop',None) is None,'hop_coordinator_already_attached')
    runtime._follow_hop=coordinator


def prepare_runtime_follow(runtime,scope,start,goal,**kwargs):
    """The existing executor calls this once instead of a second fallback job."""
    co=getattr(runtime,'_follow_hop',None)
    if co is None:return runtime.backend.prepare_route(scope,start,goal,**kwargs)
    if not co.active:co.drain_cleanup()
    permit=co._dispatch_permit if kwargs.get('follow_intent') is not None else None
    if permit is not None:
        # A new cold job must not inherit only the last few milliseconds of the
        # preceding job's permit. Renewal revalidates the unchanged dispatcher,
        # actor/scene/target and keeps the original session deadline.
        try:permit=co.refresh_dispatched_follow(permit)
        except Exception as exc:return Decision('unavailable',reason=str(exc))
    result=co.prepare_follow(scope,start,goal,permit=permit,**kwargs)
    timing=co.last_prepare_timings
    log=getattr(runtime.controller,'log',None)
    if callable(log) and timing is not None:
        log('experimental_hop_prepare_timing',phase='cold_follow',status=result.status,
            reason=_log_reason(result.reason),**timing)
    return result


def activate_runtime_hop(runtime,decision,pending):
    co=getattr(runtime,'_follow_hop',None)
    need(co is not None and is_hop_selection(decision),'hop_selection_not_owned_by_runtime')
    with runtime._lock:
        need(runtime._prepared==pending and runtime._request_id==pending[0]
             and runtime._permission()==pending[1] and runtime._lease.unconsumed_valid,'hop_runtime_request_changed')
        result=co.start_hop(decision.motion,now=runtime.clock(),published_start=runtime.controller.position)
        runtime._prepared=None;runtime.last_decision=result
        log=getattr(runtime.controller,'log',None)
        if callable(log):log('experimental_hop_started' if result.status=='ready' else 'experimental_hop_stopped',
            phase='grounding' if result.status=='ready' else 'blocked',reason=_log_reason(result.reason),
            source='dedicated_hop_motion',udp_ack_verified=False)
        # Deliberately no controller.motion, CompatibleMotion or _BudgetedMotion.
        return 'hopping' if result.status=='ready' else 'blocked_hop_start'


def publish_runtime_hop(runtime,body,*,now,read_origin,animation_codec=None):
    co=getattr(runtime,'_follow_hop',None)
    if co is None or not co.active:return HopTick(body,False,reason='no_active_hop')
    def commit(patch):
        # Persist the coordinates actually serialized by the existing codec.
        runtime.controller.position=_vector(read_origin(patch.actor_bytes))
        runtime.controller._terrain_last_position=runtime.controller.position
        if patch.heading is not None:
            runtime.controller.heading=patch.heading
        if patch.ground_handoff_required and animation_codec is not None:
            stamp=animation_codec.read(patch.actor_bytes)
            runtime.controller.event=animation_codec.ClearEvent({k:stamp[k] for k in ('gen','key','start')})
    return co.tick_actor(body,now=now,emit=commit)


def post_runtime_hop(runtime,body,*,now,original_post,read_origin,animation_codec=None):
    """Use the original sender body, not the already origin-rewritten ground post.

    A live hop is the only path that bypasses the original post. On no new fixed
    step, revocation or failure the original ground/social publisher still runs
    and keeps its own existing behavior. This does not manufacture an air frame
    or replay a consumed permit to fill a sub-fixed-step cadence gap.
    """
    co=getattr(runtime,'_follow_hop',None)
    raw_body=body
    if co is not None and co.active and animation_codec is not None:
        try:
            with co.lock:
                # Inherit only the controller's last actually constructed stamp,
                # not its origin/POSE. Keep the original raw seed identity pinned
                # independently so an animation/appearance takeover isn't hidden.
                e=co._encoder;a=animation_codec
                inspected=a.inspect_actor_wire(body,verified_profile_sha256=actor_profile.CODEC_SHA256,check_zero_padding=False)
                raw_id=actor_profile._source_identity(body,inspected,a)
                if e.seed_identity is None:
                    co._entry_raw_identity=raw_id
                    co._entry_bridge_stamp=getattr(runtime.controller,'_hop_last_constructed_stamp',None)
                    current_heading=getattr(runtime.controller,'heading',None)
                    if type(current_heading) in (float,int) and math.isfinite(current_heading):
                        e.heading=float(current_heading)
                else:need(raw_id==co._entry_raw_identity,'entry_raw_seed_changed')
                if co._entry_bridge_stamp is not None:
                    body=a.write(body,**dict(zip(('key','gen','start'),co._entry_bridge_stamp)))
        except Exception as exc:co.hard_cancel(str(exc));body=raw_body
    result=publish_runtime_hop(runtime,body,now=now,read_origin=read_origin,animation_codec=animation_codec)
    if co is not None:
        phase=result.phase if result.emitted else co.state
        reason=_log_reason(result.reason if result.emitted else co.last_reason)
        key=(phase,reason)
        if key!=getattr(runtime,'_hop_logged_phase',None):
            runtime._hop_logged_phase=key
            log=getattr(runtime.controller,'log',None)
            if callable(log):log('experimental_hop_landed' if result.emitted and result.phase in ('landed','cancelled_landed')
                else 'experimental_hop_stopped' if co.state=='stopped' else 'experimental_hop_phase',
                phase=phase,reason=reason,constructed=result.emitted,resume_follow=result.needs_follow_resume,
                udp_ack_verified=False)
    if not result.emitted:
        result=HopTick(original_post(raw_body),False,reason=result.reason,phase=result.phase)
    if animation_codec is not None and runtime is not None:
        fields=animation_codec.read(result.body)
        runtime.controller._hop_last_constructed_stamp=tuple(fields[k] for k in ('key','gen','start'))
    return result


def _log_reason(value):
    if value is None:return None
    value=str(value)
    return value if 0<len(value)<=96 and all(c in 'abcdefghijklmnopqrstuvwxyz0123456789_' for c in value) else 'hop_error_redacted'


def validate_entry_hop_config(config):
    """Explicit evidence-pinned opt-in; absence leaves all ground paths alone."""
    cfg=config.get('hopV20')
    if cfg is None:return None
    need(type(cfg) is dict,'hop_config_schema')
    if cfg=={'enabled':False}:return None
    required={'enabled','proofDirectory','backupManifestSHA256','codecSHA256','capturePins','profileId'}
    need(required<=set(cfg)<=required|{'aerialProfiles'},'hop_config_fields')
    aerial=cfg.get('aerialProfiles',[])
    need(type(aerial) is list and len(aerial)<=3
         and all(type(x) is str and x in ('drop_v22','flap_v22','jump_drop_v23') for x in aerial)
         and len(set(aerial))==len(aerial),'hop_aerial_profiles')
    need(cfg['enabled'] is True and type(cfg['proofDirectory']) is str
         and Path(cfg['proofDirectory']).is_absolute(),'hop_explicit_directory')
    need(type(cfg['capturePins']) is list and cfg['profileId'] in ('up_small_r2','diagonal_l','manual_stand_r'),
         'hop_evidence_profile')
    trial=EngineeringTrial(enabled=True,backup_manifest_sha256=cfg['backupManifestSHA256'],
        codec_sha256=cfg['codecSHA256'],capture_pins=tuple(cfg['capturePins']),profile_id=cfg['profileId'],
        **({'aerial_profiles':tuple(cfg['aerialProfiles'])} if cfg.get('aerialProfiles') else {}))
    trial.validate(cfg['profileId'])
    return cfg


def entry_hop_backend_options(config):
    cfg=validate_entry_hop_config(config)
    return {} if cfg is None else {'hop_enabled':True,'hop_proof_directory':cfg['proofDirectory']}


class HopCommandBinding:
    """Observe accepted structured commands at the existing controller dispatcher.

    No file polling, LLM text parsing or inferred follow intent. A follow object
    already present before installation is deliberately not authorized.
    """
    def __init__(self,controller):
        self.controller=controller
        self.lock=controller.__dict__.setdefault('_terrain_stop',[threading.RLock(),0,False,None])[0]
        self.revision=0;self.follow_ref=None;self.target=None;self.coordinator=None
        original=controller._apply
        self.original=original
        def apply(c,cmd):
            # 2026-11-07：小光自己逛（src=wander）的命令在跟随中会被 room_pose 忽略
            # （wander_cmd_ignored），但以前照样 revision+1、清掉 follow_ref，
            # 地面层的跟随许可随之失效，小光原地不动直到哥哥重新下跟随命令。
            # 跟随中的 wander 命令不改许可记账，直接交给原逻辑（它会忽略）。
            if type(cmd) is dict and cmd.get('src')=='wander' and type(getattr(c,'follow',None)) is dict:
                return original(cmd)
            with self.lock:
                co=self.coordinator
                pose=cmd.get('pose') if type(cmd) is dict else None
                self.revision+=1;self.follow_ref=None;self.target=None
                revision=self.revision
                if co is not None and co.active and pose in ('stop','idle'):
                    c.follow=None;c.state={'pose':'idle'}
                    if co.request_stop():
                        c.log('pose_set',pose='stop')
                        return
                if co is not None and co.suppress_ground_replan:co.hard_cancel('structured_command_changed')
            # Never invert active_offer's active_lock -> terrain_stop ordering.
            result=original(cmd)
            with self.lock:
                follow=getattr(c,'follow',None)
                if (revision==self.revision and pose=='follow' and cmd.get('src')!='wander' and type(follow) is dict
                        and follow.get('target')==cmd.get('target')):
                    self.follow_ref=follow;self.target=follow['target']
                return result
        controller._apply=types.MethodType(apply,controller)

    def is_follow_current(self):
        follow=getattr(self.controller,'follow',None)
        return self.revision>0 and follow is self.follow_ref and type(follow) is dict and follow.get('target')==self.target


def install_entry_hop(hook,runtime,config):
    """Actor-owner installation after runtime.install, with the real dispatcher."""
    cfg=validate_entry_hop_config(config)
    if cfg is None:return None
    from skyterrain.tracker_lifecycle import TrackerLifecycleProvider
    binding=getattr(hook,'_hop_dispatch',None)
    need(type(binding) is HopCommandBinding,'hop_dispatcher_not_installed')
    c=hook.c;t=c.tracker;ctx=runtime.admission.context
    scene=runtime.lifecycle.scene;engine=runtime.lifecycle.expected_engine
    fixed=TrackerLifecycleProvider(t,scene,lambda:ctx['target']['uuid'],expected_engine=engine,monotonic=hook.clock)
    expected_epoch=t.epoch
    scope_id=dict(actorId=hashlib.sha256((ctx['sessionBindingDigest']+ctx['bot']['uuid']).encode()).hexdigest(),
        epoch=expected_epoch,incarnation=ctx['bot']['incarnation'],mapId=scene.scene_hash,
        roomId=str(scene.room_generation)+':'+ctx['sessionBindingDigest'])
    def current():
        life=fixed();own_ok=False
        if t.lock.acquire(False):
            try:
                own=ctx['bot'];own_ok=(t._engine is engine and t.epoch==expected_epoch
                    and t.own_uuid==own['uuid'] and t._own_social_binding_locked()==own['slot']
                    and t._slot_versions.get(own['slot'])==own['incarnation'])
            finally:t.lock.release()
        connected=(life is not None and own_ok and not hook._closed and not hook._expired
                   and hook.runtime is runtime)
        # T23-4：AirMotion 拥有 Actor 时，旧 hop 必须像 social 接管一样立即
        # 让位（tick 里的 _current 会 hard_cancel；start_hop 也会被拒绝）。
        # 这两个运动同时驱动会互相写位置，线上 18:42:13 就是 hop 和 air 同帧启动。
        air=getattr(c,'_air_motion',None)
        social=(c._terrain_social_owns() or (air is not None and getattr(c,'motion',None) is air))
        explicit=binding.is_follow_current()
        if explicit:
            selected=runtime.lifecycle()
            explicit=selected is not None and life is not None and selected.target_key==life.target_key
        deadline=min(hook.deadline,runtime.deadline if runtime.deadline is not None else hook.deadline)
        return LiveFollowState(scene,dict(scope_id),binding.revision,
            life.target_key if explicit and life is not None else None,explicit,social,connected,deadline)
    adapter=runtime.backend.create_hop_adapter(enabled=True,proof_directory=cfg['proofDirectory'],
        expected_asset_sha256=config['bindingRow']['asset_manifest_sha256'],current_hop_scope=lambda:dict(scope_id)
        if current().connected else {},social_owned=c._terrain_social_owns,clock=hook.clock,
        aerial_profiles=tuple(cfg.get('aerialProfiles',())))
    post_globals=hook._original_post.__func__.__globals__;A=post_globals['anim_pose']
    trial=EngineeringTrial(enabled=True,backup_manifest_sha256=cfg['backupManifestSHA256'],
        codec_sha256=cfg['codecSHA256'],capture_pins=tuple(cfg['capturePins']),profile_id=cfg['profileId'],
        **({'aerial_profiles':tuple(cfg['aerialProfiles'])} if cfg.get('aerialProfiles') else {}))
    co=FollowHopCoordinator(runtime.backend,enabled=True,current_state=current,adapter=adapter,
        encoder_factory=lambda:HopActorEncoder(A,profile_id=cfg['profileId'],engineering_trial=trial,
            **({'walk_clip':getattr(c,'walk_clip',None),'walk_force':post_globals['social_pose'].WALK_FORCE}
               if cfg.get('aerialProfiles') else {})),
        publication_lock=binding.lock,clock=hook.clock)
    attach_bounded_runtime(runtime,co);binding.coordinator=co
    runtime._hop_entry_binding=binding
    return co


def authorize_entry_follow(runtime):
    co=getattr(runtime,'_follow_hop',None)
    if co is None:return True
    if co.suppress_ground_replan:return False
    try:
        live=co._live()
        if not live.explicit_follow:return False
        old=co._dispatch_permit
        if old is not None:
            co._current(old)
            if co.clock()>=old.expires:co.refresh_dispatched_follow(old)
        else:
            co.authorize_dispatched_follow(request_seq=live.request_seq,target_key=live.target_key,scope=live.scope)
        return True
    except Exception:
        # A new accepted dispatcher revision replaces an expired old permission.
        try:
            live=co._live()
            co.authorize_dispatched_follow(request_seq=live.request_seq,target_key=live.target_key,scope=live.scope)
            return True
        except Exception:return False
