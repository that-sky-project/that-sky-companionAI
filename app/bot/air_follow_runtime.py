"""持续空中跟随的控制器桥。只用 RAM 观测；热 Actor 不等 tracker/worker 锁。"""
from contextlib import contextmanager
import math
import threading
import time

import air_follow
from air_follow_motion import AirFollowMotion
import prop_motion_guard

OBSERVE_INTERVAL=.1
OBSERVATION_TTL=1.5
LOG_INTERVAL=5.


def _now(c):
    method=getattr(getattr(c,'_air_tick',None),'__func__',None)
    return float(getattr(method,'__globals__',{}).get('time',time).monotonic())


def _stop_state(c):
    if '_terrain_stop' in c.__dict__:return c._terrain_stop
    return c.__dict__.setdefault('_terrain_stop',[threading.RLock(),0,False,None])


def _owner_stamp(c):
    return (getattr(c,'_command_seq',None),_stop_state(c)[1],getattr(c,'map_id',None),
            getattr(getattr(c,'tracker',None),'room_generation',None))


def motion_type(plan,fallback):
    return _new_motion if plan.get('air_follow_session') is True else fallback


def _new_motion(plan,now,**kwargs):
    motion=AirFollowMotion(plan,now,**kwargs)
    metadata=plan.get('air_follow_runtime') or {}
    return _bind_motion(motion,metadata,now)


def _bind_motion(motion,metadata,now):
    # Populate before _air_start publishes the object to the Actor thread.
    motion._runtime_binding=metadata.get('binding')
    motion._runtime_target=metadata.get('target')
    motion._runtime_started=metadata.get('started',now)
    motion._runtime_level_change=metadata.get('level_change')
    motion._runtime_retire_reason=None;motion._runtime_land_logged=False
    motion._runtime_lock=threading.RLock()
    return motion


def _state(c):
    if '_continuous_air_follow' in c.__dict__:return c._continuous_air_follow
    return c.__dict__.setdefault('_continuous_air_follow',dict(lock=threading.RLock(),
        tracker=air_follow.TargetTracker(),observation=None,binding=None,
        snapshot=None,terrain=None,next_observe=0.,logs={}))


def _log(c,event,now,reason='',**fields):
    state=_state(c);key=(event,str(reason));previous=state['logs'].get(key)
    if previous is None or now-previous>=LOG_INTERVAL:
        if len(state['logs'])>=16:state['logs'].pop(next(iter(state['logs'])))
        state['logs'][key]=now;c.log(event,reason=reason,**fields)


def _enabled_cached(c):
    cfg=getattr(c,'_air_cfg',None) or {}
    return cfg.get('enabled') is True and cfg.get('follow') is True and cfg.get('airborneFollow',True) is not False


def _snapshot(c,now):
    follow=getattr(c,'follow',None)
    if not follow:return None,'no_follow'
    tracker=getattr(c,'tracker',None);lock=getattr(tracker,'lock',None)
    if lock is None or not lock.acquire(blocking=False):return None,'tracker_busy'
    try:
        engine=getattr(tracker,'_engine',None)
        if not tracker.ready or engine is None or getattr(engine,'_closed',False):return None,'room_unavailable'
        target=follow.get('target');slots=tracker.slots
        slot=next((slot for slot,uuid in slots.items() if uuid==target),None)
        if slot is None:
            try:slot=int(str(target),16) if str(target).lower().startswith('0x') else int(target)
            except (ValueError,TypeError):return None,'target_lost'
        if slot not in slots:return None,'target_lost'
        raw=tracker.origins.get(slot);seen=getattr(tracker,'position_observed',{}).get(slot)
        if raw is None or seen is None:return None,'target_unobserved'
        point=tuple(float(v) for v in raw[:3]);observed=float(seen[1])
        if len(point)!=3 or not all(math.isfinite(v) and abs(v)<10000 for v in point):return None,'target_invalid'
        if not math.isfinite(observed) or not -.05<=now-observed<=OBSERVATION_TTL:return None,'target_stale'
        incarnation=getattr(tracker,'_slot_versions',{}).get(slot)
        if incarnation is None:return None,'target_binding_missing'
        binding=(getattr(c,'map_id',None),tracker.room_generation,tracker.epoch,id(engine),
                 slot,slots[slot],incarnation)
        level=max((float(row[0]) for row in getattr(tracker,'level_changes',()) if len(row)>=2 and row[1]==slot),default=None)
        return dict(position=point,observed_at=observed,binding=binding,target=target,
                    level_change=level),None
    except (TypeError,ValueError,AttributeError,KeyError,OverflowError):return None,'target_snapshot_invalid'
    finally:lock.release()


@contextmanager
def _hot_scene(c):
    backend=prop_motion_guard.backend(getattr(c,'_terrain_adapter',None))
    lock=getattr(backend,'_lock',None)
    acquired=lock is None or lock.acquire(blocking=False)
    try:
        if not acquired:yield None,None;return
        terrain=c._air_terrain()
        def covered(points):
            if backend is None:return True
            # backend uses the same reentrant lock already acquired without waiting.
            return backend.placeable_path_covered(list(points))
        yield terrain,covered
    finally:
        if acquired and lock is not None:lock.release()


def _observe(c,now,terrain,snapshot=None):
    state=_state(c)
    if snapshot is None:snapshot,reason=_snapshot(c,now)
    else:reason=None
    if snapshot is None:
        state['snapshot_reason']=reason
        return None
    if terrain is None:return None
    if not state['lock'].acquire(blocking=False):return state.get('observation')
    try:
        observation=state['tracker'].observe(now,snapshot['position'],terrain,
            binding=snapshot['binding'],observed_at=snapshot['observed_at'])
        state.update(observation=observation,snapshot=snapshot,binding=snapshot['binding'],terrain=terrain)
        return observation
    finally:state['lock'].release()


def observe_frame(c,now):
    # No start/landing planning and no file/config read in the packet callback.
    if not _enabled_cached(c) or not getattr(c,'follow',None):return None
    state=_state(c)
    if now<state['next_observe']:return state['observation']
    state['next_observe']=now+OBSERVE_INTERVAL
    with _hot_scene(c) as (terrain,_covered):
        return _observe(c,now,terrain)


def maybe_start(c,terrain,who,now):
    if not _enabled_cached(c) or not getattr(c,'follow',None):return False
    if getattr(c,'_air_motion',None) is not None:return False
    observation=_observe(c,now,terrain)
    if not observation or observation.get('mode')!='airborne':return False
    state=_state(c);snapshot=state['snapshot']
    if snapshot['level_change'] is not None and now-snapshot['level_change']<=3.:return False
    position=tuple(c.position);owner=getattr(c,'motion',None);stamp=_owner_stamp(c)
    selection=prop_motion_guard.selection(getattr(c,'_terrain_adapter',None))
    # The normal decorated _air_start remains the only ownership transfer.
    plan=air_follow.takeoff_plan(terrain,position,observation)
    if not plan:
        _log(c,'air_follow_deferred',now,'no_safe_takeoff');return False
    plan=dict(plan,air_follow_session=True,air_follow_observation=dict(observation),
              air_follow_runtime=dict(binding=snapshot['binding'],target=snapshot['target'],
                                      started=now,level_change=snapshot['level_change']))
    committed_at=_now(c)
    current,_why=_snapshot(c,committed_at)
    if current is None or current['binding']!=snapshot['binding'] or current['target']!=snapshot['target']:
        return False
    if math.dist(current['position'],snapshot['position'])>2.:return False
    with _hot_scene(c) as (fresh,_covered):
        if fresh is not terrain:return False
    stop=_stop_state(c)
    with stop[0]:
        if (getattr(c,'_air_motion',None) is not None or getattr(c,'motion',None) is not owner
                or _owner_stamp(c)!=stamp or tuple(c.position)!=position
                or not _enabled_cached(c) or c._air_block_reason(committed_at) is not None
                or current['level_change']!=snapshot['level_change']
                or selection!=prop_motion_guard.selection(getattr(c,'_terrain_adapter',None))):
            _log(c,'air_follow_deferred',committed_at,'takeoff_snapshot_changed');return False
        if not c._air_start(plan,committed_at):return False
    air=c._air_motion
    if not isinstance(air,AirFollowMotion):return False
    air._runtime_binding=snapshot['binding'];air._runtime_target=snapshot['target']
    air._runtime_started=now;air._runtime_level_change=snapshot['level_change']
    air._runtime_retire_reason=None;air._runtime_land_logged=False
    c.log('air_follow_start',reason=observation.get('reason'),at=list(c.position),
          target=list(snapshot['position']),gap=3.,speed_limit=air_follow.DEFAULT_POLICY.max_horizontal)
    return True


def _retire_reason(c,air,now,snapshot,reason):
    if not _enabled_cached(c):return 'disabled'
    if (getattr(c,'social',None) is not None or getattr(c,'sitting',None) is not None
            or getattr(c,'holding',False) or getattr(c,'candle',None) is not None
            or getattr(c,'_solo_gate',None) is not None or c._active_running()):return 'priority_owner'
    if not getattr(c,'follow',None):return 'no_follow'
    if snapshot is None:
        # A transient lock miss is not a lost player; the latest validated sample has a short lease.
        old=_state(c).get('snapshot')
        if reason=='tracker_busy' and old and 0<=now-old['observed_at']<=OBSERVATION_TTL:return None
        return reason or 'target_lost'
    if snapshot['binding']!=getattr(air,'_runtime_binding',None):return 'target_binding_changed'
    if snapshot['target']!=getattr(air,'_runtime_target',None):return 'target_changed'
    changed=snapshot['level_change']
    if changed is not None and changed!=getattr(air,'_runtime_level_change',None):return 'target_map_changed'
    return None


def _same_scene(c,air):
    old=getattr(air,'_runtime_binding',None);tracker=getattr(c,'tracker',None)
    current=(getattr(c,'map_id',None),getattr(tracker,'room_generation',None),
             getattr(tracker,'epoch',None),id(getattr(tracker,'_engine',None)))
    return bool(old and tuple(old[:4])==current)


def _release_old_scene(c,air,now):
    if c._air_motion is air and c.motion is air:
        c._air_motion=c.motion=c._air_phase=None
        c.log('air_follow_stop',reason='own_room_changed',landed=False,at=list(c.position))
    _state(c)['tracker'].reset()


def _queue_promotion(c,air,now):
    # Do not restart an existing ordinary flight. The next Actor frame joins its cruise.
    if (air is None or getattr(c,'motion',None) is not air or getattr(air,'kind',None)!='fly'
            or not _enabled_cached(c) or not getattr(c,'follow',None)
            or c._air_block_reason(now) is not None or getattr(c,'_solo_gate',None) is not None):return None
    if air.phase(now)!='cruise':return None
    fresh,_why=_snapshot(c,now)
    if fresh is None:return None
    if not prop_motion_guard.focus(getattr(c,'_terrain_adapter',None),c.position,fresh['position']):return None
    with _hot_scene(c) as (terrain,covered):
        observation=_observe(c,now,terrain)
        if not observation or observation.get('mode')!='airborne':return None
        # Prove eligibility before suppressing the legacy tick (especially seat/recovery flights).
        if AirFollowMotion.from_cruise(air,now,terrain=terrain,observation=observation,path_check=covered) is None:
            return None
    snapshot=_state(c).get('snapshot')
    if snapshot is None or (snapshot['level_change'] is not None and now-snapshot['level_change']<=3.):return None
    _state(c)['promotion']=(air,dict(snapshot),_owner_stamp(c))
    return True


def _promote_frame(c,air,body,now):
    state=c.__dict__.get('_continuous_air_follow')
    pending=None if state is None else state.pop('promotion',None)
    if not pending or pending[0] is not air:return None
    snapshot,why=_snapshot(c,now)
    if (snapshot is None or snapshot['binding']!=pending[1]['binding']
            or snapshot['target']!=pending[1]['target'] or snapshot['level_change']!=pending[1]['level_change']
            or _owner_stamp(c)!=pending[2] or not _enabled_cached(c)
            or c._air_block_reason(now) is not None or getattr(c,'_solo_gate',None) is not None):return None
    observation=state.get('observation')
    if not observation or observation.get('mode')!='airborne':return None
    with _hot_scene(c) as (terrain,covered):
        if terrain is None:return None
        replacement=AirFollowMotion.from_cruise(air,now,terrain=terrain,observation=observation,path_check=covered)
        if replacement is None:
            _log(c,'air_follow_deferred',now,'ordinary_cruise_not_safe_to_join');return None
        stop=_stop_state(c)
        if not stop[0].acquire(blocking=False):return None
        try:
            current,_why=_snapshot(c,now)
            if (c.motion is not air or c._air_motion is not air or _owner_stamp(c)!=pending[2]
                    or current is None or current['binding']!=snapshot['binding']
                    or current['target']!=snapshot['target'] or current['level_change']!=snapshot['level_change']
                    or c._air_block_reason(now) is not None or getattr(c,'_solo_gate',None) is not None):return None
            _bind_motion(replacement,dict(binding=snapshot['binding'],target=snapshot['target'],
                started=now,level_change=snapshot['level_change']),now)
            c.motion=c._air_motion=replacement;c._air_phase='cruise'
            c.log('air_follow_start',reason='ordinary_cruise_joined',at=list(replacement.pos(now)),
                  target=list(snapshot['position']),gap=3.,speed_limit=air_follow.DEFAULT_POLICY.max_horizontal)
            return frame(c,replacement,body,now)
        finally:stop[0].release()


def tick(c,now):
    air=getattr(c,'_air_motion',None)
    if not isinstance(air,AirFollowMotion):return _queue_promotion(c,air,now)
    if c.motion is not air:return None
    if not _same_scene(c,air):
        _release_old_scene(c,air,now);return False
    if air.arrived(now):return True
    if now-getattr(air,'_runtime_cold_at',-math.inf)<.15:return True
    air._runtime_cold_at=now
    snapshot,why=_snapshot(c,now)
    retire=_retire_reason(c,air,now,snapshot,why) or getattr(air,'_runtime_retire_reason',None)
    if retire:air._runtime_retire_reason=retire
    goal=snapshot['position'] if snapshot and not retire else c.position
    if not prop_motion_guard.focus(getattr(c,'_terrain_adapter',None),c.position,goal):
        _log(c,'air_follow_deferred',now,'props_focus_unavailable');return True
    terrain=c._air_terrain()
    if terrain is None:
        _log(c,'air_follow_deferred',now,'geometry_unavailable');return True
    observation=_observe(c,now,terrain,snapshot) if snapshot and not retire else None
    reason=retire or ('target_grounded' if observation and observation.get('mode')=='grounded' else None)
    reason=reason or getattr(air,'_land_requested',None)
    with air._runtime_lock:
        air.refresh(terrain,observation,now)
        if reason:air.cancel(now,terrain,reason)
        position=tuple(air.pos(now));velocity=tuple(air.current_velocity)
        can_land=bool(reason and air.follow_mode!='landing' and math.dist(velocity,(0.,0.,0.))<=.05)
    if can_land:
        # Slow geometry work is outside the motion lock. Hot frames continue braking/hovering.
        selection=prop_motion_guard.selection(getattr(c,'_terrain_adapter',None))
        plan=air_follow.landing_plan(terrain,position,velocity,observation if not retire else None)
        covered=plan is not None and prop_motion_guard.covers(getattr(c,'_terrain_adapter',None),plan)
        # Planning may outlive a room/target/geometry update. Never publish the stale plan.
        committed_at=_now(c)
        latest,latest_reason=_snapshot(c,committed_at)
        latest_retire=_retire_reason(c,air,committed_at,latest,latest_reason)
        latest_retire=latest_retire or getattr(air,'_runtime_retire_reason',None)
        target_current=bool(retire or (latest is not None and snapshot is not None
            and latest['binding']==snapshot['binding'] and latest['target']==snapshot['target']
            and latest['level_change']==snapshot['level_change']
            and math.dist(latest['position'],snapshot['position'])<=.5))
        with _hot_scene(c) as (current_terrain,_covered):
            # Keep the nonblocking-acquired geometry lock through the scalar publication.
            with air._runtime_lock:
                stop=_stop_state(c);locked=stop[0].acquire(blocking=False)
                try:
                    if (locked and covered and c.motion is air and c._air_motion is air
                            and _same_scene(c,air) and target_current and current_terrain is terrain and latest_retire==retire
                            and math.dist(position,air.pos(now))<1e-7
                            and math.dist(velocity,air.current_velocity)<1e-7
                            and selection==prop_motion_guard.selection(getattr(c,'_terrain_adapter',None))):
                        if not air.install_landing(plan,committed_at,reason):
                            _log(c,'air_follow_deferred',now,'landing_commit_rejected',cause=reason)
                    elif not covered:_log(c,'air_follow_deferred',now,'no_safe_landing',cause=reason)
                    else:_log(c,'air_follow_deferred',now,'landing_snapshot_changed',cause=reason)
                finally:
                    if locked:stop[0].release()
    _log(c,'air_follow_update',now,air.follow_mode,at=list(c.position),
         target=None if snapshot is None else list(snapshot['position']),
         retire=retire,lease=getattr(air,'lease_count',0))
    return True


def frame(c,air,body,now):
    if not isinstance(air,AirFollowMotion):return _promote_frame(c,air,body,now)
    if c.motion is not air or c._air_motion is not air:return None
    lock=getattr(air,'_runtime_lock',None)
    if lock is None:return None
    if not lock.acquire(blocking=False):
        # Cold critical sections only publish scalar state; never wait on them in the packet thread.
        return body,c.position,c.heading
    try:return _frame_locked(c,air,body,now)
    finally:lock.release()


def _frame_locked(c,air,body,now):
    if not _same_scene(c,air):
        _release_old_scene(c,air,now)
        return body,c.position,c.heading
    stamp=_owner_stamp(c);source_body=body
    snapshot,why=_snapshot(c,now)
    retire=_retire_reason(c,air,now,snapshot,why)
    if retire:air._runtime_retire_reason=retire
    with _hot_scene(c) as (terrain,covered):
        if terrain is None:
            _log(c,'air_follow_deferred',now,'hot_geometry_busy')
            return body,c.position,c.heading
        observation=_state(c).get('observation')
        if retire or getattr(air,'_runtime_retire_reason',None):
            observation=dict(mode='unknown',reason='retiring',position=c.position,
                             velocity=(0.,0.,0.),observed_at=now,heading=0.)
        result=air.advance(now,terrain=terrain,observation=observation,path_check=covered)
        if not result.get('ok'):
            air._runtime_retire_reason=result.get('reason','frame_blocked')
            _log(c,'air_follow_deferred',now,result.get('reason','frame_blocked'))
        stop=_stop_state(c)
        if not stop[0].acquire(blocking=False):return source_body,c.position,c.heading
        try:
            def current():
                return (c.motion is air and c._air_motion is air and _same_scene(c,air)
                        and stamp==_owner_stamp(c))
            if not current():return source_body,c.position,c.heading
            if air.arrived(now):
                if not getattr(air,'_runtime_land_logged',False):
                    air._runtime_land_logged=True
                    c.log('air_follow_land',reason=air.cancel_reason or 'target_grounded',at=list(air.b),
                          seconds=round(now-getattr(air,'_runtime_started',now),3))
                # The original _air_render performs the only landed Actor/ground handoff.
                return None
            body=air.render(body,now)
            if not current():return source_body,c.position,c.heading
            position=air.pos(now);heading=air.heading
            c.position=position
            if heading is not None:c.heading=heading
            return body,position,heading
        finally:stop[0].release()


def maintenance(c,now):
    """冷线程：即使 active/solo 优先级让 _follow_tick 提前返回，也兑现排队降落。"""
    air=getattr(c,'_air_motion',None)
    if isinstance(air,AirFollowMotion) and c.motion is air:
        snapshot,why=_snapshot(c,now)
        if _retire_reason(c,air,now,snapshot,why) or getattr(air,'_land_requested',None):
            tick(c,now)
