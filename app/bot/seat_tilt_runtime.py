"""房间证据绑定的道具坐姿；非道具座位保持旧行为，状态仅在 RAM。"""
from __future__ import annotations
import copy
import functools
import math
import threading
import time

import anim_pose
import seat_tilt
import prop_motion_guard
from seat_exit_profiles import PROFILES as EXIT_PROFILES

# TASK-i：工程保守交互值，不冒称APK原生按钮半径。静态座位的原规则不变。
PROP_SEAT_INTERACTION_RADIUS = 1.25
PROP_SEAT_AIR_WAIT_SECONDS = 20.0
# PlaceableDefs.json #439/#247；只移用相同GoSit2、scale=.075、marker scale=1.07的rig。
# (offset, native marker首rotation分量)。该分量作Y角已由TableXmas侧座实录证实。
PROP_SEAT_MARKERS = {
    'TableXmas': (((-14.51,0.,0.),-90.),((-4.91,0.,8.75),0.),
                 ((4.91,0.,8.75),0.),((14.51,0.,0.),90.)),
    'MischiefTable': (((-8.75,0.,0.),-90.),((8.75,0.,0.),90.)),
}


def infer_marker_binding(snapshot,seat,origin,prototype):
    """实时anchor+当前实例唯一空间匹配；不依赖seatID推断道具，不制造optional字段。"""
    if snapshot is None or not snapshot.get('validated'):return None
    origin=_vec(origin);matches=[]
    props=list(snapshot.get('props',()))+list(snapshot.get('unmodeled_props',()))
    for prop in props[:256]:
        markers=PROP_SEAT_MARKERS.get(prop.get('name'))
        if markers is None:continue
        try:
            rotation=seat_tilt._rotation(prop['rotation']);position=_vec(prop['position'])
            scale=.075*float(prop['scale'])
            if not .00375<=scale<=1.5:continue
            tag=int.from_bytes(bytes.fromhex(prop['tag']),'little')
            if not 0<=tag<0xffffffff:continue
            local=[sum(rotation[j][i]*(origin[j]-position[j]) for j in range(3))/scale for i in range(3)]
            for marker,(offset,degrees) in enumerate(markers):
                a=math.radians(degrees)
                # 同rig的椅子局部偏移从193个既有侧座样本得到；新marker是明确推断。
                v=(offset[0]-1.177*math.sin(a),offset[1]+9.397469520568848,
                   offset[2]-1.177*math.cos(a))
                world=[position[i]+sum(rotation[i][j]*v[j]*scale for j in range(3)) for i in range(3)]
                error=math.dist(world,origin)
                if error>.65:continue
                evidence=copy.deepcopy(prototype)
                evidence.update(attachment_kind=2,attachment_id=tag,optional_anchor=None,
                    local_origin=local,origin=list(origin))
                evidence['seat']=dict(seated=1,sit_enter=0,seat=int(seat),avatar_state=15)
                profile=seat_tilt.derive(prop,local,origin,-a,evidence=evidence,inferred_rig=True)
                matches.append((error,dict(prop=copy.deepcopy(prop),signature=_signature(prop),
                    profile=profile,observation=None,marker=marker,local_heading=-a,
                    room_generation=snapshot['room_generation'],props_revision=snapshot['revision'],
                    map_id=snapshot.get('map_id'),inferred=True,marker_error=error)))
        except (ValueError,TypeError,KeyError,OverflowError):continue
    matches.sort(key=lambda item:item[0])
    if not matches or (len(matches)>1 and matches[1][0]-matches[0][0]<.10):return None
    return matches[0][1]


class SeatTrackerPending(ValueError):
    """瞬时竞争只表示本轮尚未取得快照，不表示座位失败。"""


def require_local_prop_assets(snapshot, origin, *, binding=None, observed=None, transaction=None):
    """缺本地资产时不猜座位来源；生成完成后才沿用原静态/道具分流。"""
    ready = snapshot.get('assets_ready') is True
    if not ready:
        # No verified protocol field in this client proves that an unbound seat
        # is static. Missing/type-0 attachment or a distant unknown rig is not
        # evidence; fail closed even though some static seats are also deferred.
        raise ValueError('seat_prop_assets_unavailable')
    names = set(snapshot.get('available_names', ())) if ready else set()
    prop = (binding or {}).get('prop')
    if prop is not None and prop.get('name') not in names:
        raise ValueError('seat_prop_assets_unavailable')
    if not ready and ((observed or {}).get('attachment_kind') == 2
                      or (transaction or {}).get('requires_prop')):
        raise ValueError('seat_prop_assets_unavailable')
    # Unknown/missing-geometry nearby instances cannot be silently treated as
    # static seats simply because no dynamic collider was built for them.
    for prop in list(snapshot.get('props', ())) + list(snapshot.get('unmodeled_props', ())):
        if prop.get('name') not in names:
            try:
                nearby = math.dist(_vec(prop.get('position')), _vec(origin)) <= 3.
            except (ValueError, TypeError):
                nearby = True
            if nearby:
                raise ValueError('seat_prop_assets_unavailable')



def _vec(value):
    if not isinstance(value,(list,tuple)) or len(value)!=3:
        raise ValueError('seat_position_shape')
    if any(type(v) not in (int,float) or not math.isfinite(v) or abs(v)>10000 for v in value):
        raise ValueError('seat_position_range')
    return tuple(float(v) for v in value)


def _signature(prop):
    return (prop.get('id'),prop.get('name'),tuple(prop.get('position',())),
            tuple(prop.get('rotation',())),prop.get('scale'),prop.get('tag'),prop.get('state'))


def preferred_exit_path(prop, marker, start, *, inferred=False):
    """精确参考优先；显式推断rig可移用同GoSit2的相对轨迹，几何仍须批准。"""
    rotation=seat_tilt._rotation(prop['rotation']);origin=_vec(prop['position']);start=_vec(start)
    scale=float(prop['scale'])
    if not math.isfinite(scale) or not .05<=scale<=20:raise ValueError('seat_exit_scale')
    candidates=[p for p in EXIT_PROFILES if p['name']==prop.get('name') and p['marker']==marker]
    if not candidates:
        markers=PROP_SEAT_MARKERS.get(prop.get('name'),())
        if not inferred or type(marker) is not int or not 0<=marker<len(markers):
            raise ValueError('seat_exit_reference_missing')
        # 仅已共享GoSit2、定义scale=.075、marker scale=1.07的两个rig。
        # 取完整真人窗口的相对位移；它含玩家起身后的走动，不宣称原生root motion。
        donors=[p for p in EXIT_PROFILES if p['complete_window']
                and p['name'] in PROP_SEAT_MARKERS
                and p['marker']<len(PROP_SEAT_MARKERS[p['name']])]
        if not donors:raise ValueError('seat_exit_reference_missing')
        chosen=min(donors,key=lambda p:(math.dist(rotation[1],seat_tilt._rotation(p['rotation'])[1]),p['case_id']))
        donor_degrees=PROP_SEAT_MARKERS[chosen['name']][chosen['marker']][1]
        target_degrees=markers[marker][1]
        # 既有坐姿local_heading=-native marker角；转相同avatar heading框架后再乘完整R。
        delta_heading=math.radians(donor_degrees-target_degrees)
        c,s=math.cos(delta_heading),math.sin(delta_heading)
        points=[dict(elapsed=0.,point=start)]
        for row in chosen['samples']:
            delta=tuple(row['local_m'][i]-chosen['start_local_m'][i] for i in range(3))
            local=(c*delta[0]+s*delta[2],delta[1],-s*delta[0]+c*delta[2])
            world=tuple(start[i]+scale*sum(rotation[i][j]*local[j] for j in range(3)) for i in range(3))
            points.append(dict(elapsed=float(row['elapsed']),point=world))
        return dict(reference=chosen['case_id'],donor_name=chosen['name'],donor_marker=chosen['marker'],
            target_name=prop['name'],target_marker=marker,inferred=True,native_exit_verified=False,
            evidence_scope='inferred_same_GoSit2_avatar_heading_relative_capture',
            donor_evidence_scope=chosen['evidence_scope'],complete_window=True,preferred_path=points,
            reference_gravity_error=math.dist(rotation[1],seat_tilt._rotation(chosen['rotation'])[1]))
    # local gravity 不受世界 yaw 影响；两次 marker0 参考不平均、不伪造未观测动画。
    gravity=rotation[1]
    chosen=min(candidates,key=lambda p:(math.dist(gravity,seat_tilt._rotation(p['rotation'])[1]),p['case_id']))
    def world(local):
        return tuple(origin[i]+scale*sum(rotation[i][j]*local[j] for j in range(3)) for i in range(3))
    anchor=world(chosen['start_local_m']);delta=tuple(start[i]-anchor[i] for i in range(3))
    if math.dist(start,anchor)>.025:raise ValueError('seat_exit_reference_anchor_mismatch')
    points=[dict(elapsed=0.,point=start)]
    for row in chosen['samples']:
        value=world(row['local_m'])
        points.append(dict(elapsed=float(row['elapsed']),point=tuple(value[i]+delta[i] for i in range(3))))
    return dict(reference=chosen['case_id'],evidence_scope=chosen['evidence_scope'],
        complete_window=chosen['complete_window'],preferred_path=points,
        reference_gravity_error=math.dist(gravity,seat_tilt._rotation(chosen['rotation'])[1]))


def seat_exit_directions(state):
    """座位朝前/两侧的世界XZ偏好；只是搜索顺序，不代替完整几何检查。"""
    R=seat_tilt._rotation(state['prop']['rotation'])
    h=float(state['local_heading']);forward=(math.sin(h),0.,math.cos(h))
    right=(math.cos(h),0.,-math.sin(h))
    output=[]
    for local in (forward,right,tuple(-v for v in right),tuple(-v for v in forward)):
        world=tuple(sum(R[i][j]*local[j] for j in range(3)) for i in range(3))
        length=math.hypot(world[0],world[2])
        if length>1e-6:output.append((world[0]/length,world[2]/length))
    return output


def _exit_schedule(plan):
    """保留参考时序下界，同时限制每段2m/s；不把延迟帧变成瞬移。"""
    points=[_vec(p) for p in plan.get('points',())]
    times=plan.get('times',())
    if not 1<=len(points)<=1024 or len(times)!=len(points):raise ValueError('seat_exit_plan_shape')
    if any(type(t) not in (int,float) or not math.isfinite(t) or t<0 for t in times):
        raise ValueError('seat_exit_plan_time')
    schedule=[0.]
    for i in range(1,len(points)):
        schedule.append(max(float(times[i])-float(times[0]),schedule[-1]+math.dist(points[i-1],points[i])/2.))
    if schedule[-1]>15.:raise ValueError('seat_exit_plan_duration')
    return points,schedule


def _exit_sample(points,times,elapsed):
    if elapsed>=times[-1]:return points[-1]
    for i in range(1,len(points)):
        if elapsed<=times[i]:
            f=(elapsed-times[i-1])/max(1e-12,times[i]-times[i-1])
            return tuple(points[i-1][j]+f*(points[i][j]-points[i-1][j]) for j in range(3))
    return points[-1]


def bind_observation(snapshot, observation, seat, origin):
    """仅绑定观测过的稳定 TableXmas 侧座；从不按 seatID 推测实例。"""
    if not observation or observation.get('attachment_kind')!=2:
        return None
    if snapshot is None or snapshot.get('validated') is not True:
        raise ValueError('seat_room_unvalidated')
    if observation.get('seat',{}).get('seat')!=int(seat):
        raise ValueError('seat_observation_id')
    local=_vec(observation['local_origin']);observed=_vec(observation['origin'])
    if math.dist(observed,_vec(origin))>.02:
        raise ValueError('seat_observation_origin_mismatch')
    # 已实测两个侧座，不把未知前后座、其它道具或 avatar 高度硬编码为已验证。
    if abs(abs(local[0])-13.3330078125)>.15 or abs(local[2])>.05 or not 0<local[1]<20:
        raise ValueError('seat_marker_rig_unverified')
    heading=math.pi/2 if local[0]<0 else -math.pi/2
    matches=[]
    for prop in snapshot.get('props',())[:256]:
        if prop.get('name')!='TableXmas':continue
        try:
            tag=int.from_bytes(bytes.fromhex(prop.get('tag','')),'little')
            if tag!=observation['attachment_id']:continue
            R=seat_tilt._rotation(prop['rotation']);p=_vec(prop['position'])
            scale=.075*float(prop['scale'])
            world=tuple(p[i]+sum(R[i][j]*local[j]*scale for j in range(3)) for i in range(3))
            if math.dist(world,observed)>.02:continue
            computed=seat_tilt.derive(prop,local,observed,heading,evidence=observation)
            # 入座转身尚未收敛的观察不能用于推算另一个 Actor 的稳定坐姿。
            if any(computed[k]!=observation[k] for k in ('yaw1','yaw2','forward')):
                continue
            for name in ('pose_current','pose_history'):
                rows=observation.get(name,())
                if not rows or any(row['position_codes']!=computed[name][0]['position_codes']
                                   or row['yaw_code']!=computed[name][0]['yaw_code'] for row in rows):
                    raise ValueError('seat_pose_reference_not_steady')
            matches.append(dict(prop=copy.deepcopy(prop),signature=_signature(prop),
                profile=computed,observation=copy.deepcopy(observation),marker=0 if local[0]<0 else 3,
                local_heading=heading,room_generation=snapshot['room_generation'],
                props_revision=snapshot['revision'],map_id=snapshot.get('map_id')))
        except (ValueError,TypeError,KeyError,OverflowError):
            continue
    if len(matches)!=1:raise ValueError('seat_prop_binding_not_unique')
    return matches[0]


def upright_action(function):
    """只拦截本模块持有的道具坐姿；旧静态座位/社交路径完全透传。"""
    @functools.wraps(function)
    def wrapped(self,*args,**kwargs):
        state=getattr(self,'_prop_seat_state',None)
        if state is None:return function(self,*args,**kwargs)
        if function.__name__ in ('reset','switch_map','relocate'):
            self._prop_seat_abort(function.__name__)
            return function(self,*args,**kwargs)
        self._prop_seat_exit('before_'+function.__name__)
        state=getattr(self,'_prop_seat_state',None)
        if state is not None:
            if function.__name__=='_air_start':
                self._prop_seat_log('seat_air_requires_replan')
                return False  # 起身后起点可能改变，不重放起身前生成的轨迹。
            state['deferred']=(function,args,dict(kwargs))
            self._prop_seat_log('seat_upright_deferred',action=function.__name__)
            return function.__name__ in ('walk_to','walk','_sit_direct')
        return function(self,*args,**kwargs)
    return wrapped


def seat_command(function):
    """命令整体延期，避免先改意图再被地面实例hook丢弃；stop撤销旧延期。"""
    @functools.wraps(function)
    def wrapped(self,cmd,*args,**kwargs):
        state=getattr(self,'_prop_seat_state',None)
        if (state is None or cmd.get('pose') in ('shout','voice')
                or (cmd.get('pose')=='anim' and state['phase']=='seated')):
            return function(self,cmd,*args,**kwargs)
        self._prop_seat_exit('command_'+str(cmd.get('pose')))
        state=getattr(self,'_prop_seat_state',None)
        if state is None:return function(self,cmd,*args,**kwargs)
        if cmd.get('pose') in ('stand','stop','idle'):
            state['deferred']=None
            return function(self,cmd,*args,**kwargs)
        state['deferred']=(function,(copy.deepcopy(cmd),)+args,dict(kwargs))
        self._prop_seat_log('seat_command_deferred',pose=cmd.get('pose'))
        return None
    return wrapped


class _GetUpEvent(anim_pose.AnimEvent):
    """动画字段保持既有写器；关键结束时刻按本次捕获窗口。"""
    def __init__(self,previous,started):
        super().__init__(anim_pose.GET_UP,previous,started,seconds=seat_tilt.GETUP_SECONDS)
        self.clear_start=None

    def post(self,body,now):
        if self.start is None:self.t0=now  # 首构造帧开始计时，命令线程延迟不跳过退倾斜。
        out=super().post(body,now);age=max(0.,now-self.t0)
        fields={'mode':0 if age>=seat_tilt.GETUP_SECONDS else 7}
        fields['code90']=(5 if age<.266 else 0 if age<seat_tilt.GETUP_SECONDS
                          else 1 if age<seat_tilt.GETUP_SECONDS+.45 else 7)
        if age>=.266:
            fields.update(h_gen=self.gen,h_key=self.key,h_start=self.start>>3)
        if age>=1.015:
            if self.clear_start is None:self.clear_start=anim_pose._required_clock(body)
            generation=(self.gen+1)&0xffffffff
            fields.update(gen=generation,key=0,start=self.clear_start,
                          h_gen=generation,h_key=0,h_start=self.clear_start>>3)
        return anim_pose.write(out,**fields)

    def stamp(self):
        if self.clear_start is not None:
            return {'gen':(self.gen+1)&0xffffffff,'key':0,'start':self.clear_start}
        return super().stamp()


class SeatTiltControllerMixin:
    def _prop_seat_log(self,event,**fields):
        try:self.log(event,**fields)
        except Exception:pass

    def _prop_seat_dynamic_work(self):
        """无道具、无本模块事务时保持静态座位原路径（不构建空中几何）。"""
        return bool(getattr(self,'_prop_seat_transaction',None)
            or getattr(self,'_prop_seat_pending_commit',None)
            or getattr(self,'_prop_seat_rollback_pending',None)
            or getattr(self,'_prop_seat_pending_air',None)
            or getattr(self,'_prop_seat_state',None)
            or getattr(getattr(self.tracker,'placeables',None),'props',None)
            or getattr(getattr(self.tracker,'placeables',None),'unmodeled_props',None))

    def _prop_seat_snapshot(self,*,locked=False):
        tracker=self.tracker
        if not locked and not tracker.lock.acquire(False):raise SeatTrackerPending('seat_tracker_pending')
        try:
            registry=getattr(tracker,'placeables',None)
            expired_snapshot=getattr(registry,'unmodeled_snapshot',None)
            unmodeled=(expired_snapshot() if callable(expired_snapshot)
                else list(getattr(registry,'unmodeled_props',{}).values())[:256])
            return dict(validated=tracker.ready,room_generation=tracker.room_generation,
                map_id=tracker.confirmed_map_id,revision=getattr(registry,'revision',0),
                assets_ready=getattr(tracker,'props_assets_ready',False),
                available_names=tuple(getattr(registry,'geometry_names',())),
                props=copy.deepcopy(list(getattr(registry,'props',{}).values())[:256]),
                unmodeled_props=copy.deepcopy(unmodeled))
        finally:
            if not locked:tracker.lock.release()

    def _prop_seat_interaction_binding(self,seat,info,*,locked=False):
        snapshot=self._prop_seat_snapshot(locked=locked)
        binding=infer_marker_binding(snapshot,seat,info[0],seat_tilt.observe(self.seed))
        return snapshot,binding

    def _prop_seat_interaction_covered(self,snapshot,here,origin,prop_id,*,view_out=None):
        """仅借RAM预算证明；最终提交持tracker锁时绝不等待backend/binding锁。"""
        backend=prop_motion_guard.backend(getattr(self,'_terrain_adapter',None))
        if backend is None:return True,'no_dynamic_binding'
        lock=getattr(backend,'_lock',None)
        if lock is None or not lock.acquire(False):return False,'seat_coverage_backend_busy'
        try:
            import air_terrain
            static=getattr(backend,'_static_ground_surface',None)
            if static is None:static=getattr(backend,'__dict__',{}).get('_ground_surface')
            def publish(surface):
                if surface is None:return False,'seat_interaction_static_geometry_pending'
                if view_out is not None:view_out.append(air_terrain.TerrainView(surface,backend.offset))
                return True,'seat_coverage_current'
            binding=getattr(backend,'_placeables',None)
            if binding is None or not binding.enabled:return publish(static)
            if not binding._lock.acquire(False):return False,'seat_coverage_binding_busy'
            try:
                key=(snapshot['room_generation'],snapshot['revision'])
                if (not snapshot['validated'] or not binding._room_valid
                        or binding.room_generation!=key[0] or binding.map_id!=snapshot['map_id']
                        or binding._key!=key or binding._load_epoch!=backend._collider_load_epoch
                        or binding._sent!=(key,backend.transport.generation,backend._collider_load_epoch)
                        or (binding.require_focus and (binding._focus is None or binding._selected_focus!=binding._focus))
                        or backend.offset is None):return False,'seat_coverage_revision_pending'
                points=[tuple(p[i]-backend.offset[i] for i in range(3)) for p in (here,origin)]
                # selection_blocked是旧长路径全局门；此短交互逐一审查全部excluded，
                # 仅允许当前唯一绑定家具自身缺覆盖，不传播为长路径豁免。
                excluded=tuple(binding._excluded)
                for collider,bounds in excluded:
                    # 唯一空间/观测绑定的当前家具可忽略；其它预算排除实例仍挡。
                    if collider['id']!=prop_id and binding._intersects(points[0],points[1],bounds,.75):
                        return False,'seat_coverage_excluded_other_prop'
                if binding._colliders:
                    if binding._overlay is None or binding._static is not static:
                        return False,'seat_coverage_overlay_pending'
                    return publish(binding._overlay)
                return publish(static)
            finally:binding._lock.release()
        except (ValueError,TypeError,AttributeError,KeyError,OverflowError):
            return False,'seat_coverage_unavailable'
        finally:lock.release()

    def _prop_seat_interaction_check(self,seat,info,here,*,locked=False):
        """近距离按钮交互不要求landing，只禁止穿静态/无关道具的短位移。"""
        import stuck_recovery
        if math.dist(_vec(here),_vec(info[0]))>PROP_SEAT_INTERACTION_RADIUS:
            return False,'seat_interaction_out_of_range',None
        _snapshot,binding=self._prop_seat_interaction_binding(seat,info,locked=locked)
        transaction=getattr(self,'_prop_seat_transaction',None)
        require_local_prop_assets(_snapshot,info[0],binding=binding,transaction=transaction)
        if (transaction is not None and transaction.get('requires_prop')
                and not _snapshot['props'] and not _snapshot['unmodeled_props']):
            return False,'seat_prop_destroyed',None
        if transaction is not None and transaction.get('prop_id') is not None:
            current=next((p for p in _snapshot['props']+_snapshot['unmodeled_props']
                          if p['id']==transaction['prop_id']),None)
            if current is None:return False,'seat_prop_destroyed',None
            if _signature(current)!=transaction['prop_signature']:
                return False,'seat_prop_changed',None
        adapter=getattr(self,'_terrain_adapter',None)
        selection=prop_motion_guard.selection(adapter)
        backend=prop_motion_guard.backend(adapter)
        dynamic=getattr(backend,'_placeables',None)
        if (not locked and dynamic is not None and dynamic._colliders and dynamic._overlay is None):
            self._air_terrain()  # 控制路径、尚未持tracker锁时才允许冷建overlay。
        prop_id=binding['prop']['id'] if binding is not None else None
        views=[]
        covered,coverage_reason=self._prop_seat_interaction_covered(_snapshot,here,info[0],prop_id,view_out=views)
        if not covered:return False,coverage_reason,binding
        if views:terrain=views[0]
        elif locked:
            # 无真实backend绑定的旧adapter；不存在surface()/revision()回调。
            import air_terrain
            terrain=air_terrain.terrain_view(adapter)
        else:terrain=self._air_terrain()
        if terrain is None:return False,'seat_interaction_static_geometry_pending',binding
        checker=getattr(stuck_recovery,'prop_interaction_clear',None)
        if not callable(checker):return False,'seat_interaction_checker_pending',binding
        ok,reason=checker(terrain,here,info[0],prop_id,max_distance=PROP_SEAT_INTERACTION_RADIUS)
        if selection!=prop_motion_guard.selection(adapter):
            return False,'seat_interaction_geometry_changed',binding
        return ok,reason,binding

    def _prop_seat_try_interact(self,seat,info,here,*,takeover=False):
        if not self._prop_seat_dynamic_work():return False
        if getattr(self,'_air_motion',None) is not None:return False
        pending=getattr(self,'_sit_pending',None)
        if not takeover and pending is not None:
            if pending.get('command_seq')!=getattr(self,'_command_seq',None):return False
            motion=getattr(self,'motion',None)
            if (motion is not None and motion is not getattr(self,'_terrain_motion',None)
                    and motion is not getattr(self,'_direct_walk_motion',None)):return False
        if math.dist(_vec(here),_vec(info[0]))>PROP_SEAT_INTERACTION_RADIUS:return False
        try:ok,reason,binding=self._prop_seat_interaction_check(seat,info,here)
        except SeatTrackerPending:
            ok,reason,binding=None,'seat_tracker_pending',None
        if ok is False:
            self._prop_seat_log('seat_interaction_wait',seat=seat,reason=reason)
            return False
        previous=getattr(self,'_prop_seat_transaction',None)
        safe=previous['safe_return'] if previous is not None else tuple(here)
        self._prop_seat_transaction=dict(seat=int(seat),info=copy.deepcopy(info),safe_return=safe,
            approach=tuple(here),room_generation=getattr(self.tracker,'room_generation',None),
            command_seq=getattr(self,'_command_seq',None),interaction=True,
            requires_prop=bool(binding is not None or (previous or {}).get('prop_id') is not None),
            prop_id=binding['prop']['id'] if binding else (previous or {}).get('prop_id'),
            prop_signature=binding['signature'] if binding else (previous or {}).get('prop_signature'))
        # 此命令已经接管；先取消旧地面请求，避免异步worker在坐下后装回旧motion。
        self._terrain_invalidate()
        self.follow,self.holding,self.candle=None,False,None
        self.motion=None;self._direct_walk_motion=None;self.after_arrive=None
        self.position=tuple(here)
        pending=getattr(self,'_home_sit_pending',None)
        self._prop_seat_log('seat_interaction',seat=seat,distance=math.dist(here,info[0]),
            radius=PROP_SEAT_INTERACTION_RADIUS,rule='near_anchor_no_landing_required')
        if ok is None:
            self._prop_seat_defer_commit(seat,info,pending['request'] if pending else None)
            return True
        try:self._sit_now(seat,info,home_request=pending['request'] if pending else None)
        except (ValueError,LookupError,TypeError,KeyError) as exc:
            self._sit_failed('sit_error',seat=seat,stop=True,detail=str(exc)[:160])
        return True

    def _prop_seat_pending_owner_guard(self,pending):
        if getattr(self,'_prop_seat_transaction',None) is None:return False
        motion=getattr(self,'motion',None)
        intent=getattr(self,'_stuck_intent',None)
        changed=(pending.get('command_seq')!=getattr(self,'_command_seq',None)
            or (motion is not None and motion is not getattr(self,'_terrain_motion',None)
                and motion is not getattr(self,'_direct_walk_motion',None)
                and motion is not pending.get('approach_air'))
              or (isinstance(intent,dict) and intent.get('label') not in ('seat','seat_rollback')))
        if not changed:return False
        home=getattr(self,'_home_sit_pending',None)
        self._prop_seat_cancel_commit(dict(seat=pending['seat'],
            home_request=(home or {}).get('request'),
            request=(pending['seat'],(home or {}).get('request'),
                getattr(self.tracker,'room_generation',None),pending.get('command_seq'))),
            'approach_owner_changed')
        return True

    def _prop_seat_approach_air_wait(self,pending):
        air=pending.get('approach_air')
        if air is None or getattr(self,'_air_motion',None) is not air or self.motion is not air:return False
        if self._prop_seat_pending_owner_guard(pending):return True
        transaction=getattr(self,'_prop_seat_transaction',None)
        if transaction is None:return True
        try:snapshot=self._prop_seat_snapshot()
        except SeatTrackerPending:return True
        prop=next((p for p in snapshot['props']+snapshot['unmodeled_props']
                   if p['id']==transaction.get('prop_id')),None)
        if (not snapshot['validated'] or snapshot['room_generation']!=transaction['room_generation']
                or prop is None or _signature(prop)!=transaction.get('prop_signature')):
            home=getattr(self,'_home_sit_pending',None)
            self._prop_seat_cancel_commit(dict(seat=pending['seat'],
                home_request=(home or {}).get('request'),request=(pending['seat'],
                    (home or {}).get('request'),transaction['room_generation'],pending.get('command_seq'))),
                'approach_prop_destroyed_or_changed')
        # 不在半空 stop/瞬移；只取消坐下意图，逐帧碰撞门决定能否继续原安全落点。
        return True

    def _prop_seat_approach_jump(self,pending,now):
        """同一座位事务最多两次短跳；只豁免绑定家具，落点仍须完整碰撞可站。"""
        import stuck_recovery
        transaction=getattr(self,'_prop_seat_transaction',None)
        if (transaction is None or getattr(self,'_sit_pending',None) is not pending
                or pending.get('command_seq')!=getattr(self,'_command_seq',None)
                or transaction.get('room_generation')!=getattr(self.tracker,'room_generation',None)
                or self.motion is not None or getattr(self,'_air_motion',None) is not None
                or self.social is not None or self._active_running()
                or not self._air_enabled() or self._air_block_reason(now) is not None):return False
        if pending.get('approach_jump_attempts',0)>=2 or now-pending.get('approach_jump_at',-float('inf'))<.75:
            return False
        here=tuple(self.position);origin=pending['info'][0]
        if not PROP_SEAT_INTERACTION_RADIUS<math.dist(here,origin)<=2.75:return False
        try:snapshot,binding=self._prop_seat_interaction_binding(pending['seat'],pending['info'])
        except SeatTrackerPending:return False
        if binding is None:return False
        prop=binding['prop']
        if transaction.get('prop_id') is not None and (transaction['prop_id']!=prop['id']
                or transaction['prop_signature']!=binding['signature']):return False
        adapter=getattr(self,'_terrain_adapter',None)
        if not prop_motion_guard.focus(adapter,here,origin):return False
        terrain=self._air_terrain();clips=self._air_clips()
        if terrain is None or clips is None or not clips.ok:return False
        pending['approach_jump_attempts']=pending.get('approach_jump_attempts',0)+1
        pending['approach_jump_at']=now
        plan=stuck_recovery.plan_prop_seat_approach(terrain,here,origin,prop['id'],
            interaction_radius=PROP_SEAT_INTERACTION_RADIUS)
        if plan.get('kind')!='jump':
            self._prop_seat_log('seat_approach_jump_rejected',seat=pending['seat'],
                reason=plan.get('reason'),attempt=pending['approach_jump_attempts'])
            return False
        try:current,current_binding=self._prop_seat_interaction_binding(pending['seat'],pending['info'])
        except SeatTrackerPending:return False
        if (getattr(self,'_sit_pending',None) is not pending
                or getattr(self,'_prop_seat_transaction',None) is not transaction
                or pending.get('command_seq')!=getattr(self,'_command_seq',None)
                or snapshot['room_generation']!=getattr(self.tracker,'room_generation',None)
                or current_binding is None or current_binding['signature']!=binding['signature']
                or self.motion is not None or self._air_block_reason(now) is not None
                or not prop_motion_guard.covers(adapter,plan)):return False
        transaction.update(requires_prop=True,prop_id=prop['id'],prop_signature=binding['signature'])
        plan['prop_seat_approach']['binding']=self._stuck_binding()[:2]
        if not self._air_start(plan,now):return False
        pending['approach_air']=self._air_motion
        self._prop_seat_log('seat_approach_jump_start',seat=pending['seat'],prop_id=prop['id'],
            attempt=pending['approach_jump_attempts'],start=list(here),landing=list(plan['landing']),
            interaction_distance=math.dist(plan['landing'],origin),full_landing=True)
        return True

    def _prop_seat_queue_after_air(self,cmd):
        now=time.monotonic()
        self._prop_seat_pending_air=dict(command=copy.deepcopy(cmd),command_seq=getattr(self,'_command_seq',None),
            room_generation=getattr(self.tracker,'room_generation',None),created=now,
            air=getattr(self,'_air_motion',None))
        self.follow,self.holding,self.candle=None,False,None
        self.after_arrive=None;self._sit_pending=None
        self._last_sit_failure=None
        self.state=dict(pose='sit_pending',reason='wait_air_landing')
        self._prop_seat_log('sit_after_air_queued',timeout=PROP_SEAT_AIR_WAIT_SECONDS)
        return True

    def _prop_seat_air_tick(self,now):
        pending=getattr(self,'_prop_seat_pending_air',None)
        if pending is None:return False
        if (pending['room_generation']!=getattr(self.tracker,'room_generation',None)
                or pending['command_seq']!=getattr(self,'_command_seq',None)
                or getattr(self,'social',None) is not None or self._active_running()):
            self._prop_seat_pending_air=None
            self._prop_seat_log('sit_after_air_cancelled',reason='new_intent_or_room')
            return False
        if now-pending['created']>=PROP_SEAT_AIR_WAIT_SECONDS:
            self._prop_seat_pending_air=None
            self._sit_failed('air_wait_timeout',stop=False,timeout=PROP_SEAT_AIR_WAIT_SECONDS)
            return True
        air=getattr(self,'_air_motion',None)
        if air is not None:return True
        if getattr(self,'motion',None) is not None:return True
        self._prop_seat_pending_air=None
        self._prop_seat_log('sit_after_air_resumed',elapsed=now-pending['created'])
        self._apply(pending['command'])
        return True

    def _prop_seat_resume_after_recovery(self,flight):
        if getattr(self,'_prop_seat_pending_air',None) is None:return False
        return self._prop_seat_air_tick(time.monotonic())

    def _prop_seat_observation(self,seat,*,locked=False):
        tracker=self.tracker
        if not locked and not tracker.lock.acquire(False):raise SeatTrackerPending('seat_tracker_pending')
        try:
            row=getattr(tracker,'seat_pose_book',{}).get(int(seat))
            if row is None:return None
            if (not tracker.ready or row['room_generation']!=tracker.room_generation
                    or row['seat_epoch']!=tracker.seat_epoch
                    or tracker.slots.get(row['slot'])!=row['identity']
                    or tracker._slot_versions.get(row['slot'])!=row['slot_version']):
                raise ValueError('seat_observation_stale')
            return copy.deepcopy(row['profile'])
        finally:
            if not locked:tracker.lock.release()

    def _prop_seat_prepare(self,seat,info,*,locked=False):
        # observation、实例表和room/epoch共用一次Lock；home提交已持锁时绝不重入。
        tracker=self.tracker
        if not locked and not tracker.lock.acquire(False):raise SeatTrackerPending('seat_tracker_pending')
        try:
            if not getattr(tracker,'props_assets_ready',False):
                raise ValueError('seat_prop_assets_unavailable')
            try:observed=self._prop_seat_observation(seat,locked=True)
            except (ValueError,KeyError):observed=None
            registry=getattr(tracker,'placeables',None)
            has_props=bool(getattr(registry,'props',None) or getattr(registry,'unmodeled_props',None))
            transaction=getattr(self,'_prop_seat_transaction',None)
            if (not has_props and not (transaction or {}).get('requires_prop')
                    and (observed is None or observed.get('attachment_kind')!=2)):
                return None  # 原静态座位不读取道具/ready快照，也不新增几何或marker门。
            snapshot=self._prop_seat_snapshot(locked=True)
            transaction=getattr(self,'_prop_seat_transaction',None)
            require_local_prop_assets(snapshot,info[0],observed=observed,transaction=transaction)
            if transaction is not None:
                props=snapshot['props']+snapshot['unmodeled_props']
                if transaction.get('requires_prop') and not props:raise ValueError('seat_prop_destroyed')
                if transaction.get('prop_id') is not None:
                    current=next((p for p in props if p['id']==transaction['prop_id']),None)
                    if current is None:raise ValueError('seat_prop_destroyed')
                    if _signature(current)!=transaction['prop_signature']:raise ValueError('seat_prop_changed')
            if not snapshot['props'] and not snapshot['unmodeled_props']:
                if observed is not None and observed.get('attachment_kind')==2:
                    raise ValueError('seat_prop_missing')
                return None
            if observed is not None and observed.get('attachment_kind')==2:
                try:
                    binding=bind_observation(snapshot,observed,seat,info[0])
                    require_local_prop_assets(snapshot,info[0],binding=binding,observed=observed)
                    return binding
                except ValueError:pass
            binding=infer_marker_binding(snapshot,seat,info[0],seat_tilt.observe(self.seed))
            require_local_prop_assets(snapshot,info[0],binding=binding,observed=observed)
            self._prop_seat_log('seat_pose_inference',seat=seat,
                source='same_rig_marker' if binding else 'live_anchor_yaw_SitEvent',
                inferred=True,optional_anchor_emitted=False)
            return binding
        finally:
            if not locked:tracker.lock.release()

    def _prop_seat_defer_commit(self,seat,info,home_request=None):
        previous=getattr(self,'_prop_seat_pending_commit',None)
        request=(int(seat),home_request,getattr(self.tracker,'room_generation',None),
                 getattr(self,'_command_seq',None))
        if previous is None or previous['request']!=request:
            self._prop_seat_pending_commit=dict(request=request,seat=int(seat),info=copy.deepcopy(info),
                home_request=home_request,created=time.monotonic(),next_try=0.,tries=0)
            self._prop_seat_log('seat_commit_deferred',seat=seat,reason='tracker_snapshot_pending')
        return False

    def _prop_seat_prepare_for_commit(self,seat,info,*,locked=False,home_request=None):
        if not locked and not self.tracker.lock.acquire(False):
            self._prop_seat_defer_commit(seat,info,home_request)
            return False,None
        try:
            if getattr(self,'_prop_seat_transaction',None) is not None:
                ok,reason,_binding=self._prop_seat_interaction_check(seat,info,self.position,locked=True)
                if not ok:
                    self._prop_seat_log('seat_commit_geometry_wait',seat=seat,reason=reason)
                    if reason in ('seat_prop_destroyed','seat_prop_changed'):raise ValueError(reason)
                    self._prop_seat_defer_commit(seat,info,home_request)
                    return False,None
            return True,self._prop_seat_prepare(seat,info,locked=True)
        except SeatTrackerPending:
            self._prop_seat_defer_commit(seat,info,home_request)
            return False,None
        finally:
            if not locked:self.tracker.lock.release()

    def _prop_seat_cancel_commit(self,pending,reason):
        """只撤销旧提交意图，不调用 stop、不改新 motion/event 的所有权。"""
        self._prop_seat_pending_commit=None
        home=getattr(self,'_home_sit_pending',None)
        same=home is not None and home.get('request')==pending.get('home_request')
        # 若已有更新的 home 请求，不触碰其接近事务/回调。
        if home is not None and not same:
            self._prop_seat_log('seat_commit_cancelled',seat=pending['seat'],reason=reason)
            return
        if home is None or same:self._prop_seat_cancel_approach(reason)
        if same:self._home_sit_pending=None
        sit=getattr(self,'_sit_pending',None)
        if (sit is not None and sit.get('seat')==pending['seat']
                and sit.get('command_seq')==pending['request'][3]):self._sit_pending=None
        after=getattr(self,'after_arrive',None)
        if (isinstance(after,dict) and after.get('pose')=='_sit_now'
                and after.get('seat')==pending['seat']
                and after.get('home_request')==pending.get('home_request')):self.after_arrive=None
        self._prop_seat_log('seat_commit_cancelled',seat=pending['seat'],reason=reason)

    def _prop_seat_commit_tick(self,now):
        pending=getattr(self,'_prop_seat_pending_commit',None)
        if pending is None:return False
        request=pending['request']
        if (request[2]!=getattr(self.tracker,'room_generation',None)
                or request[3]!=getattr(self,'_command_seq',None)):
            self._prop_seat_cancel_commit(pending,'commit_replaced')
            return False
        home=getattr(self,'_home_sit_pending',None)
        if (pending.get('home_request') is not None
                and (home is None or home.get('request')!=pending['home_request'])):
            self._prop_seat_cancel_commit(pending,'home_request_replaced')
            return False
        if (getattr(self,'motion',None) is not None or getattr(self,'_air_motion',None) is not None
                or getattr(self,'social',None) is not None or self._active_running()
                or getattr(self,'follow',None) or getattr(self,'holding',False)):
            self._prop_seat_cancel_commit(pending,'movement_or_social_owner_changed')
            return False
        if now-pending['created']>=20.:
            self._prop_seat_pending_commit=None
            self._sit_failed('snapshot_timeout',seat=pending['seat'],stop=True)
            return True
        if now<pending['next_try']:return True
        pending['tries']+=1;pending['next_try']=now+.1
        try:
            self._sit_now(pending['seat'],pending['info'],home_request=pending['home_request'])
        except (ValueError,LookupError) as exc:
            self._prop_seat_pending_commit=None
            self._sit_failed('sit_error',seat=pending['seat'],stop=True,detail=str(exc)[:120])
        return True

    def _prop_seat_approach(self,seat,info,stand,here):
        """道具附近才增加完整碰撞落脚门；静态座位继续原规则。"""
        import stuck_recovery
        if not getattr(self.tracker,'props_assets_ready',False):
            raise ValueError('seat_prop_assets_unavailable')
        if not self._prop_seat_dynamic_work():return stand
        snapshot=self._prop_seat_snapshot()
        require_local_prop_assets(snapshot,info[0],transaction=getattr(self,'_prop_seat_transaction',None))
        stand,here=_vec(stand),_vec(here)
        adapter=getattr(self,'_terrain_adapter',None)
        terrain=self._air_terrain()
        ranges=getattr(getattr(terrain,'surface',None),'collider_ranges',())
        if not ranges:
            # 缺几何不等于无道具：仍绑定近座事务，防worker投影后跳过距离/静态墙门。
            registry=getattr(self.tracker,'placeables',None)
            if getattr(registry,'unmodeled_props',None):
                try:snapshot,binding=self._prop_seat_interaction_binding(seat,info)
                except SeatTrackerPending:snapshot,binding=None,None
                nearby=(snapshot is not None and any(math.dist(_vec(p['position']),_vec(info[0]))<=3.
                    for p in snapshot['unmodeled_props']))
                if binding is not None or nearby:
                    self._prop_seat_transaction=dict(seat=int(seat),info=copy.deepcopy(info),
                        safe_return=tuple(here),approach=tuple(stand),
                        room_generation=getattr(self.tracker,'room_generation',None),
                        command_seq=getattr(self,'_command_seq',None),requires_prop=binding is not None,
                        prop_id=binding['prop']['id'] if binding else None,
                        prop_signature=binding['signature'] if binding else None)
            return stand
        if not prop_motion_guard.focus(adapter,here,stand):raise ValueError('seat_approach_focus_pending')
        stamp=self._prop_seat_geometry_stamp()
        terrain=self._air_terrain()
        ranges=getattr(getattr(terrain,'surface',None),'collider_ranges',())
        surface=terrain.surface;nearby=[]
        for row in ranges:
            triangles=surface._tri[row['start']:row['start']+row['count']]
            if len(triangles)==0:continue
            low=triangles.min(axis=(0,1));high=triangles.max(axis=(0,1))
            if all(low[k]-2.<=stand[k]<=high[k]+2. for k in (0,2)):nearby.append(row['id'])
        if not nearby:return stand
        if not getattr(terrain,'has_hits',False):raise ValueError('seat_approach_geometry_missing')
        candidates=[stand]
        for radius in (0.,.35,.6,.9,1.2,1.5):
            count=1 if radius==0 else 12
            for i in range(count):
                x=stand[0]+radius*math.cos(math.tau*i/count);z=stand[2]+radius*math.sin(math.tau*i/count)
                for _face,y,ny in terrain.hits(x,z):
                    if ny>=math.cos(math.radians(42)) and abs(y-stand[1])<=1.25:
                        candidates.append((x,y,z))
        candidates=sorted(set(candidates),key=lambda p:(math.dist(p,stand),math.dist(p,here),p))[:256]
        chosen=next((p for p in candidates if stuck_recovery.landing_valid(terrain,p)),None)
        if chosen is None:raise ValueError('seat_approach_no_safe_stand')
        if stamp!=self._prop_seat_geometry_stamp():raise ValueError('seat_approach_geometry_changed')
        phases=dict(phases=[dict(mode='linear',**{'from':here,'to':chosen})])
        if not prop_motion_guard.covers(adapter,phases):raise ValueError('seat_approach_coverage_missing')
        safe=here if stuck_recovery.landing_valid(terrain,here) else chosen
        self._prop_seat_transaction=dict(seat=int(seat),info=copy.deepcopy(info),safe_return=tuple(safe),
            approach=tuple(chosen),room_generation=getattr(self.tracker,'room_generation',None),
            command_seq=getattr(self,'_command_seq',None),geometry_stamp=stamp)
        self._prop_seat_log('seat_approach_validated',seat=seat,requested=list(stand),
            selected=list(chosen),safe_return=list(safe),props=nearby,full_collision=True)
        return tuple(chosen)

    def _prop_seat_arrival_check(self):
        """到达交互半径即可入座；所属道具重叠不是失败理由。"""
        transaction=getattr(self,'_prop_seat_transaction',None)
        if transaction is None:return True
        if transaction['room_generation']!=getattr(self.tracker,'room_generation',None):
            self._sit_failed('seat_room_changed',seat=transaction['seat'],stop=True)
            return False
        try:ok,reason,binding=self._prop_seat_interaction_check(transaction['seat'],transaction['info'],self.position)
        except SeatTrackerPending:
            self._prop_seat_defer_commit(transaction['seat'],transaction['info'],
                (getattr(self,'_home_sit_pending',None) or {}).get('request'))
            return False
        if not ok:
            self._prop_seat_log('seat_interaction_wait',seat=transaction['seat'],reason=reason)
            if reason in ('seat_prop_destroyed','seat_prop_changed'):
                self._sit_failed(reason,seat=transaction['seat'],stop=True)
            return False
        if binding is not None:
            transaction.update(prop_id=binding['prop']['id'],prop_signature=binding['signature'])
        return True

    def _prop_seat_saved_return(self,default):
        transaction=getattr(self,'_prop_seat_transaction',None)
        return tuple(transaction['safe_return']) if transaction is not None else default

    def _prop_seat_commit_complete(self):
        self._prop_seat_pending_commit=None
        self._prop_seat_transaction=None

    def _prop_seat_cancel_approach(self,reason):
        self._prop_seat_pending_commit=None
        transaction=getattr(self,'_prop_seat_transaction',None)
        self._prop_seat_transaction=None
        if transaction is None:return
        if transaction['room_generation']!=getattr(self.tracker,'room_generation',None):return
        # 仅取消尚未提交/部分失败的坐姿事务，已成功入座的旧行为不走此处。
        if getattr(self,'_prop_seat_state',None) is not None:self._prop_seat_abort('sit_rollback')
        if isinstance(getattr(self,'event',None),anim_pose.SitEvent):
            self.sitting=None;self.event=None
            for name in ('seed','base'):
                body=getattr(self,name,None)
                if body is not None:setattr(self,name,seat_tilt.upright(body))
        self._prop_seat_rollback_pending=dict(transaction,reason=str(reason),started=time.monotonic())
        self._prop_seat_log('seat_rollback_queued',reason=str(reason),target=list(transaction['safe_return']))

    def _prop_seat_rollback_tick(self,now):
        pending=getattr(self,'_prop_seat_rollback_pending',None)
        if pending is None:return False
        if pending['room_generation']!=getattr(self.tracker,'room_generation',None):
            self._prop_seat_rollback_pending=None;return False
        # 任意新运动（含地面）、社交/空中仍拥有发包状态；旧座位回退只保持排队。
        # 不能在下一 tick 以旧 safe_return 的 AirMotion 替换新地面 Motion。
        air=getattr(self,'_air_motion',None)
        if (getattr(self,'motion',None) is not None
                or getattr(self,'social',None) is not None or self._active_running()
                or (air is not None and self.motion is air)):
            return True
        import stuck_recovery
        terrain=self._air_terrain()
        if terrain is not None and stuck_recovery.landing_valid(terrain,self.position):
            self._prop_seat_rollback_pending=None
            self._prop_seat_log('seat_rollback_complete',reason='already_safe',at=list(self.position))
            return False
        rollback=getattr(self,'_stuck_seat_rollback',None)
        if callable(rollback) and rollback(pending['safe_return'],now=now):
            self._prop_seat_rollback_pending=None
            return True
        if now-pending.get('last_log',-float('inf'))>=5.:
            pending['last_log']=now
            self._prop_seat_log('seat_rollback_pending',reason='no_verified_return_yet')
        return True

    def _prop_seat_begin(self,binding,seat):
        if binding is None:return
        state=copy.deepcopy(binding)
        state.update(seat=int(seat),phase='seated',started=time.monotonic(),deferred=None,
            exit_origin=tuple(self.position),safe_exit=None,rendered_upright=False)
        state['profile']['seat'].update(seated=1,seat=int(seat),avatar_state=15,sit_enter=0)
        self._prop_seat_state=state
        if state.get('inferred'):
            self.heading=math.degrees(state['profile']['calculation']['world_heading'])
        self._prop_seat_log('seat_tilt_bound',seat=seat,marker=state['marker'],
            prop=state['prop']['id'],source=state['profile']['source'])

    def _prop_seat_valid(self,state):
        tracker=self.tracker
        if getattr(tracker,'room_generation',None)!=state['room_generation']:
            return False
        if not tracker.lock.acquire(False):return None
        try:
            registry=tracker.placeables
            if (not getattr(tracker,'props_assets_ready',False)
                    or state['prop'].get('name') not in getattr(registry,'geometry_names',())):
                return False
            expire=getattr(registry,'unmodeled_snapshot',None)
            if callable(expire):expire()
            current=(registry.props.get(state['prop']['id'])
                or getattr(registry,'unmodeled_props',{}).get(state['prop']['id']))
            return bool(tracker.ready and tracker.confirmed_map_id==state['map_id']
                        and current is not None and _signature(current)==state['signature'])
        finally:tracker.lock.release()

    def _prop_seat_exit(self,reason='stand'):
        state=getattr(self,'_prop_seat_state',None)
        if state is None or state['phase']!='seated':return
        state.update(phase='getup',started=time.monotonic(),exit_origin=tuple(self.position))
        previous=self.event.stamp() if self.event is not None else {
            k:anim_pose.read(self.seed)[k] for k in ('gen','key','start')}
        self.event=_GetUpEvent(previous,state['started'])
        # 内部 sitting 仍保持阻塞移动；wire SEATED 则由真实过渡首帧清除。
        self.state={'pose':'stand','seat':state['seat'],'upright_pending':True}
        self._prop_seat_log('seat_getup_begin',seat=state['seat'],reason=reason)
        self._set_override(self.base)
        self._prop_seat_request_exit(state)

    def _prop_seat_abort(self,reason):
        state=getattr(self,'_prop_seat_state',None)
        if state is None:return
        # 房间/所有权中断不能延续旧附件。只清我们拥有的输入底板，不覆盖新社交输出。
        for name in ('base','seed'):
            body=getattr(self,name,None)
            if body is not None:setattr(self,name,seat_tilt.upright(body))
        self._prop_seat_state=None
        self.sitting=None
        if isinstance(getattr(self,'event',None),(anim_pose.SitEvent,_GetUpEvent)):
            self.event=None
        self._prop_seat_log('seat_tilt_reset',reason=reason)

    def _prop_seat_frame_begin(self,body):
        state=getattr(self,'_prop_seat_state',None)
        if state is None:return body
        if state['phase']=='seated' and getattr(self,'sitting',None)!=state['seat']:
            self._prop_seat_abort('seat_owner_lost')
            return seat_tilt.upright(body)
        valid=self._prop_seat_valid(state)
        state['frame_binding_valid']=valid is True
        if valid is False:
            self._prop_seat_abort('binding_changed')
            return seat_tilt.upright(body)
        if valid is None:
            # 非阻塞锁忙不能证明旧附件仍属当前房间；该帧不复用倾斜附件。
            return seat_tilt.upright(body)
        if (getattr(self,'motion',None) is not None or getattr(self,'social',None) is not None
                or self._active_running()):
            # 新外部所有者不等待旧座位动画；先清旧附件再由其原逻辑写合法社交字段。
            self._prop_seat_abort('owner_changed')
            return seat_tilt.upright(body)
        if state['phase']=='getup':self._prop_seat_step(time.monotonic())
        return body

    def _prop_seat_frame_end(self,body,now):
        state=getattr(self,'_prop_seat_state',None)
        if state is None:return body
        valid=self._prop_seat_valid(state)
        if valid is False:
            self._prop_seat_abort('binding_changed_during_frame')
            return seat_tilt.upright(body)
        if valid is None:return seat_tilt.upright(body)
        if not state.get('frame_binding_valid',True):return seat_tilt.upright(body)
        if state['phase']=='seated':
            profile=copy.deepcopy(state['profile'])
            profile['seat']['sit_enter']=int(now-state['started']<anim_pose.SIT_ENTER_SECONDS)
            return seat_tilt.apply(body,profile)
        event=getattr(self,'event',None)
        if isinstance(event,_GetUpEvent):state['started']=event.t0
        # 字段清理时序不变；批准的出口运动只更新与当前/历史world origin对应的位置差。
        sampled=seat_tilt.getup_sample(state['profile'],max(0.,now-state['started']),allow_capture=False)
        if math.dist(self.position,state['exit_origin'])>1e-9:
            profile=sampled['profile'];observed=seat_tilt.observe(body)
            rotation=seat_tilt._rotation(state['prop']['rotation'])
            scale=.075*float(state['prop']['scale']);origin=state['prop']['position']
            def local(world):
                return [sum(rotation[j][i]*(world[j]-origin[j]) for j in range(3))/scale for i in range(3)]
            if profile['attachment_kind']==2:
                profile['local_origin']=local(observed['origin'])
            for scope,world_key in (('pose_current','origin'),('pose_history','history_origin')):
                # current先于history清除，继续遵循原1.015/1.143秒边界。
                if any(row['position_codes']!=[2047]*3 for row in profile[scope]):
                    position=[seat_tilt._f32(seat_tilt._f32(v*.075)*float(state['prop']['scale']))
                              for v in local(observed[world_key])]
                    codes=seat_tilt.position_codes(position,observed[world_key])
                    for row in profile[scope]:row['position_codes']=list(codes)
        result=seat_tilt.apply(body,sampled['profile'])
        if sampled['complete']:
            state['rendered_upright']=True
        return result

    def _prop_seat_exit_point(self,state):
        """冷规划端点查询；不改变位置，optional 1e0 从来不参与选点。"""
        plan=self._prop_seat_prepare_exit(state)
        return tuple(plan['points'][-1]) if plan.get('status')=='ready' else None

    def _prop_seat_geometry_stamp(self):
        revision=getattr(self.tracker,'props_revision',None)
        try:value=revision() if callable(revision) else None
        except Exception:value=None
        return value,prop_motion_guard.selection(getattr(self,'_terrain_adapter',None))

    def _prop_seat_prepare_exit(self,state,*,job_token=None):
        owner=state
        # 冷规划只写快照；后台结果通过所有权/修订检查后一次提交。
        state=copy.copy(state)
        stamp=self._prop_seat_geometry_stamp()
        cached=state.get('exit_plan')
        if cached is not None and state.get('exit_geometry_stamp')==stamp:return cached
        state['exit_plan']=None;state['path_done']=False
        try:
            from skyterrain.seat_exit import plan_exit
            reference=state.get('exit_reference')
            if reference is None:
                reference=state['exit_reference']=preferred_exit_path(state['prop'],state['marker'],state['exit_origin'],
                    inferred=state.get('inferred',False))
            path=copy.deepcopy(reference['preferred_path']);start=tuple(self.position);goal=path[-1]['point']
            if math.dist(start,path[0]['point'])>1e-8:
                path=[dict(elapsed=0.,point=start),dict(elapsed=path[-1]['elapsed'],point=goal)]
            adapter=getattr(self,'_terrain_adapter',None)
            if not prop_motion_guard.focus(adapter,start,goal):raise ValueError('seat_exit_focus_unavailable')
            stamp=self._prop_seat_geometry_stamp();terrain=self._air_terrain()
            if terrain is None or not getattr(terrain,'has_hits',False):raise ValueError('seat_exit_terrain_missing')
            plan=plan_exit(terrain,state['prop'],state['marker'],start,path)
            if plan.get('status')=='ready' and state.get('inferred'):
                import stuck_recovery
                if not stuck_recovery.prop_exterior_valid(terrain,plan['points'][-1],prop_id=state['prop']['id']):
                    plan=dict(status='blocked',reason='inferred_exit_endpoint_not_exterior',points=[],times=[])
            if plan.get('status')=='ready':
                points,times=_exit_schedule(plan)
                if math.dist(start,points[0])>1e-6:raise ValueError('seat_exit_plan_start_mismatch')
                phases=[dict(mode='linear',**{'from':a,'to':b}) for a,b in zip(points,points[1:])]
                if not phases:phases=[dict(mode='linear',**{'from':start,'to':start})]
                if not prop_motion_guard.covers(adapter,dict(phases=phases)):
                    raise ValueError('seat_exit_path_coverage_missing')
                plan.update(points=points,times=times)
                state.update(exit_elapsed=0.,exit_move_at=None,path_done=len(points)==1)
            if stamp!=self._prop_seat_geometry_stamp():raise ValueError('seat_exit_geometry_changed')
        except (ImportError,ValueError,TypeError,KeyError,OverflowError) as exc:
            plan=dict(status='blocked',reason=str(exc)[:180],points=[],times=[])
        if (getattr(self,'_prop_seat_state',None) is not owner or owner.get('phase')!='getup'
                or self._prop_seat_valid(owner) is not True or stamp!=self._prop_seat_geometry_stamp()
                or (job_token is not None and self.__dict__.get('_prop_seat_exit_job') is not job_token)):
            return dict(status='blocked',reason='seat_exit_owner_changed',points=[],times=[])
        state['exit_plan']=plan;state['exit_geometry_stamp']=stamp
        owner.update({key:state[key] for key in
            ('exit_plan','exit_geometry_stamp','exit_reference','exit_elapsed','exit_move_at','path_done')
            if key in state})
        self._prop_seat_log('seat_exit_plan',status=plan.get('status'),reason=plan.get('reason'),
            reference=state.get('exit_reference',{}).get('reference'),
            reference_inferred=state.get('exit_reference',{}).get('inferred',False),
            projection_error=plan.get('projection_error'),points=len(plan.get('points',())))
        return plan

    def _prop_seat_request_exit(self,state):
        """控制器最多一个后台冷规划；Actor线程从不跑近邻搜索。"""
        lock=self.__dict__.setdefault('_prop_seat_job_lock',threading.Lock())
        if not lock.acquire(False):return
        try:self._prop_seat_request_exit_locked(state)
        finally:lock.release()

    def _prop_seat_request_exit_locked(self,state):
        stamp=self._prop_seat_geometry_stamp();plan=state.get('exit_plan')
        if plan is not None and state.get('exit_geometry_stamp')==stamp:return
        job=self.__dict__.get('_prop_seat_exit_job')
        if job is not None and not job['done'].is_set():return
        if getattr(self,'_prop_seat_state',None) is not state or state.get('phase')!='getup':return
        attempts=state.get('exit_job_attempts',(None,0))
        count=attempts[1]+1 if attempts[0]==stamp else 1
        if count>3:
            state.update(exit_plan=dict(status='blocked',reason='seat_exit_planning_retry_budget',points=[],times=[]),
                         exit_geometry_stamp=stamp)
            self._prop_seat_log('seat_exit_planning_stopped',reason='retry_budget')
            return
        state.update(exit_plan=None,path_done=False,exit_job_attempts=(stamp,count))
        job=dict(state=state,done=threading.Event(),stamp=stamp,thread=None)
        self._prop_seat_exit_job=job
        def run():
            try:self._prop_seat_prepare_exit(state,job_token=job)
            except Exception as exc:
                if (getattr(self,'_prop_seat_state',None) is state
                        and self.__dict__.get('_prop_seat_exit_job') is job
                        and stamp==self._prop_seat_geometry_stamp()):
                    state.update(exit_plan=dict(status='blocked',reason='seat_exit_planning_error',points=[],times=[]),
                                 exit_geometry_stamp=stamp)
                self._prop_seat_log('seat_exit_planning_error',error=str(exc)[:180])
            finally:job['done'].set()
        job['thread']=threading.Thread(target=run,name='prop-seat-exit',daemon=True)
        job['thread'].start()

    def _prop_seat_poll_exit(self,state):
        if state.get('exit_geometry_stamp')!=self._prop_seat_geometry_stamp():
            state['exit_plan']=None;state['path_done']=False
        self._prop_seat_request_exit(state)
        return state.get('exit_plan')

    def _prop_seat_segment_safe(self,state,plan,start,end):
        from skyterrain.seat_exit import validate_exit_segment
        stamp=state.get('exit_geometry_stamp')
        if stamp!=self._prop_seat_geometry_stamp():return False
        adapter=getattr(self,'_terrain_adapter',None)
        if not prop_motion_guard.covers(adapter,dict(phases=[dict(mode='linear',**{'from':start,'to':end})])):
            return False
        terrain=self._air_terrain()
        return bool(terrain is not None and validate_exit_segment(terrain,plan,start,end)
                    and stamp==self._prop_seat_geometry_stamp())

    def _prop_seat_step(self,now):
        state=getattr(self,'_prop_seat_state',None)
        if state is None or state['phase']!='getup':return
        plan=self._prop_seat_poll_exit(state)
        event=getattr(self,'event',None)
        if plan is None or plan.get('status')!='ready' or not isinstance(event,_GetUpEvent) or event.start is None:return
        previous=state.get('exit_move_at');state['exit_move_at']=float(now)
        if previous is None:return
        age=state.get('exit_elapsed',0.);target_age=min(plan['times'][-1],age+max(0.,min(.25,now-previous)))
        current=tuple(self.position);point=_exit_sample(plan['points'],plan['times'],target_age)
        # 发包长间隔不允许大步跳过出口；最多18cm/Actor，仍检查真实连续段。
        if math.dist(current,point)>.18:
            low,high=age,target_age
            for _ in range(20):
                mid=(low+high)*.5
                if math.dist(current,_exit_sample(plan['points'],plan['times'],mid))>.18:high=mid
                else:low=mid
            target_age=low;point=_exit_sample(plan['points'],plan['times'],target_age)
        if not self._prop_seat_segment_safe(state,plan,current,point):
            state['exit_plan']=None
            self._prop_seat_log('seat_exit_revalidate_blocked',at=list(current))
            return
        if (getattr(self,'_prop_seat_state',None) is not state or self._prop_seat_valid(state) is not True
                or self.motion is not None or self.social is not None or self._active_running()):return
        self.position=tuple(point);state['exit_elapsed']=target_age
        state['path_done']=target_age>=plan['times'][-1]-1e-9

    def _prop_seat_finish(self,state):
        for name in ('base','seed'):
            body=getattr(self,name,None)
            if body is not None:setattr(self,name,seat_tilt.upright(body))
        deferred=state.get('deferred')
        self._prop_seat_state=None;self.sitting=None
        self.state={'pose':'idle','upright':True}
        # seat_exit是临时运动意图；不能让原地小goal继续压住后续follow的失败回执。
        temporary=getattr(self,'_stuck_intent',None)
        if (temporary is not None and temporary.get('label')=='seat_exit'
                and temporary.get('binding')==self._stuck_binding()
                and getattr(self,'_stuck_tracker',None) is state.get('exit_recovery_tracker')):
            self._stuck_intent=self._stuck_tracker=None
            self._stuck_request=self._stuck_seen_prepare=self._stuck_cold_wait=self._stuck_poll=None
            self._stuck_goal_seen=None
        verify=bool(state.get('inferred') or state.get('exit_recovery_used'))
        runtime=getattr(self,'_terrain_adapter',None)
        baseline=dict(runtime=runtime,request=getattr(runtime,'_future_request',None),
            decision=getattr(runtime,'last_prepare_decision',None),motion=getattr(runtime,'_motion',None))
        if not verify:self._prop_seat_log('seat_getup_complete',seat=state['seat'],at=list(self.position))
        if deferred is not None:
            function,args,kwargs=deferred
            if getattr(function,'__name__',None)=='_apply':
                # 重新经过HopCommandBinding取得真实的新指令许可；内部home代次不穿透外层。
                replay_kwargs=dict(kwargs);replay_kwargs.pop('_home_command',None)
                self._apply(*args,**replay_kwargs)
            else:function(self,*args,**kwargs)
        if verify:
            self._prop_seat_exit_ground_pending=dict(state=state,binding=self._stuck_binding(),
                baseline=baseline,landing=tuple(self.position),created=time.monotonic(),last_reason=None)
            self._prop_seat_log('seat_exit_ground_pending',seat=state['seat'],at=list(self.position),
                reason='fresh_ground_prepare_required',deferred_replayed=deferred is not None)

    def _prop_seat_ground_confirm(self,runtime,label=None,desired=None,now=None):
        """落地/排队不算恢复成功；只消费本次新许可下的真实地面ready回执。"""
        pending=getattr(self,'_prop_seat_exit_ground_pending',None)
        if pending is None:return False
        now=time.monotonic() if now is None else now
        state=pending['state'];baseline=pending['baseline']
        if (pending['binding']!=self._stuck_binding() or self._terrain_social_owns()
                or self.sitting is not None or getattr(self,'_prop_seat_state',None) is not None):
            self._prop_seat_exit_ground_pending=None
            self._prop_seat_log('seat_exit_ground_cancelled',reason='intent_or_owner_changed')
            return False
        intent=getattr(self,'_stuck_intent',None)
        bound=getattr(self,'_stuck_request',None)
        decision=getattr(runtime,'last_prepare_decision',None)
        if (not self._stuck_current(intent) or runtime is not baseline['runtime']
                or bound is None or bound[0] is not runtime or bound[2]!=intent['key']
                or bound[1]!=getattr(runtime,'_future_request',None)
                or bound[1]==baseline['request'] or decision is baseline['decision']):return False
        if decision is None or getattr(decision,'status',None)!='ready':
            # 不吸走回执、不调用note_failure；正常stuck通道仍按本次回执累计/脱困。
            reason=getattr(decision,'reason','ground_prepare_pending')
            if pending['last_reason']!=reason:
                pending['last_reason']=reason
                self._prop_seat_log('seat_exit_ground_not_ready',reason=reason,at=list(self.position))
            return False
        import stuck_recovery
        terrain=self._air_terrain()
        valid=self._prop_seat_valid(state)
        if valid is False:
            self._prop_seat_exit_ground_pending=None
            self._prop_seat_log('seat_exit_ground_cancelled',reason='seat_binding_changed_before_ground_ready')
            return False
        if (valid is not True or self._air_motion is not None or terrain is None
                or not stuck_recovery.landing_valid(terrain,self.position)
                or not stuck_recovery.prop_exterior_valid(terrain,self.position,prop_id=state['prop']['id'])):
            self._prop_seat_log('seat_exit_ground_not_ready',reason='landing_or_exterior_invalid',at=list(self.position))
            return False
        self._prop_seat_exit_ground_pending=None
        if state.get('exit_recovery_used'):
            self._prop_seat_log('seat_exit_recovery_complete',at=list(self.position),ground_verified=True)
        self._prop_seat_log('seat_getup_complete',seat=state['seat'],at=list(self.position),ground_verified=True)
        return True

    def _prop_seat_recover(self,state,now):
        """出口及近邻搜索已失败后，使用原脱困几何，不伪造地面失败计数。"""
        import stuck_recovery
        if not state.get('rendered_upright') or not self._air_enabled():return False
        event=getattr(self,'event',None)
        if isinstance(event,_GetUpEvent) and now<event.t0+event.seconds+.45:return False
        if (self.motion is not None or self.social is not None or self._active_running()
                or getattr(self,'_solo_gate',None) is not None):return False
        adapter=getattr(self,'_terrain_adapter',None);permission=getattr(adapter,'_permission',None)
        if not callable(permission) or permission() is None:return False
        if getattr(adapter,'_future',None) is not None:return False
        config=(self._air_config() or {}).get('stuck_recovery',{})
        if not isinstance(config,dict) or config.get('enabled',True) is not True:return False
        tracker=state.get('exit_recovery_tracker')
        if tracker is None:
            try:
                fields=stuck_recovery.RecoveryPolicy.__dataclass_fields__
                policy=stuck_recovery.RecoveryPolicy(**{k:v for k,v in config.items() if k in fields})
                tracker=state['exit_recovery_tracker']=stuck_recovery.RecoveryTracker(policy)
            except (TypeError,ValueError):return False
        if tracker.rearm_after_cooldown(now,self.position):
            state.pop('recovery_exhausted_logged',None);state['exit_rejected_landings']=[]
            self._prop_seat_log('seat_exit_recovery_rearmed',reason='exhausted_cooldown',cycle=tracker.cycles)
        if tracker.exhausted:
            if not state.get('recovery_exhausted_logged'):
                state['recovery_exhausted_logged']=True
                self._prop_seat_log('seat_exit_recovery_stopped',reason='attempt_limit',attempts=tracker.attempts)
            return False
        if now-tracker.last_attempt<tracker.policy.cooldown_seconds:return False
        path=state.get('exit_reference',{}).get('preferred_path',())
        goal=tuple(path[-1]['point']) if path else tuple(self.position)
        if not prop_motion_guard.focus(adapter,self.position,goal):return False
        stamp=self._prop_seat_geometry_stamp();terrain=self._air_terrain();clips=self._air_clips()
        if terrain is None or clips is None or not clips.ok:return False
        if not tracker.begin_attempt(now):return False
        try:
            # 完整场景，不把起身时局部自椅豁免传给空中脱困。
            rejected=state.get('exit_rejected_landings',[])
            plan=stuck_recovery.plan_recovery(terrain,self.position,goal,policy=tracker.policy,
                escape_prop_id=state['prop']['id'],preferred_directions=seat_exit_directions(state),
                reject=lambda point:any(math.dist(point,old)<.5 for old in rejected))
        except Exception as exc:
            tracker.failed(now,'geometry_error')
            self._prop_seat_log('seat_exit_recovery_failed',reason='geometry_error',error=str(exc)[:160])
            return False
        if plan.get('kind') not in ('jump','fly'):
            why=plan.get('reason','no_safe_landing');tracker.failed(now,why)
            self._prop_seat_log('seat_exit_recovery_failed',reason=why,attempt=tracker.attempts)
            return False
        if (permission() is None or self._prop_seat_valid(state) is not True
                or stamp!=self._prop_seat_geometry_stamp() or not prop_motion_guard.covers(adapter,plan)):
            tracker.failed(now,'owner_or_geometry_changed');return False
        with self.__dict__.setdefault('_solo_lock',threading.RLock()):
            if getattr(self,'_prop_seat_state',None) is not state:return False
            old_sitting=self.sitting
            self._prop_seat_state=None;self.sitting=None
            if self._air_block_reason(now) is not None:
                self._prop_seat_state=state;self.sitting=old_sitting;return False
            saved={k:v for k,v in self.__dict__.items() if k.startswith('_stuck_')}
            self._stuck_note_walk(goal,'seat_exit',speed=anim_pose.WALK_SPEED)
            intent=dict(self._stuck_intent);self._stuck_tracker=tracker
            tracker.intent=intent['key']
            flight=dict(intent=intent,tracker=tracker,started=now,route=[],after=None,
                landing=tuple(plan['landing']),seat_exit=state)
            plan['stuck_recovery']=True;plan['recovery_intent']='seat_exit'
            self._stuck_flight=flight
            for name in ('base','seed'):
                value=getattr(self,name,None)
                if value is not None:setattr(self,name,seat_tilt.upright(value))
            started=self._air_start(plan,now)
            if not started or self._air_motion is None or self.motion is not self._air_motion:
                for key in list(self.__dict__):
                    if key.startswith('_stuck_'):self.__dict__.pop(key,None)
                self.__dict__.update(saved)
                self._prop_seat_state=state;self.sitting=old_sitting
                tracker.failed(now,'air_start_rejected');return False
            flight['air']=self._air_motion
            state['exit_recovery_used']=True
            self.state={'pose':'seat_exit_recovery','upright':True}
            self._prop_seat_log('seat_exit_recovery_start',kind=plan['kind'],attempt=tracker.attempts,
                start=list(plan['start']),landing=list(plan['landing']),synthetic_ground_failures=0)
            return True

    def _prop_seat_recovery_resume(self,flight,now):
        state=flight.get('seat_exit')
        if state is None:return False
        current=getattr(self,'_stuck_intent',None)
        if (flight.get('resume_cancelled') or not self._stuck_current(flight['intent'])
                or not isinstance(current,dict) or current.get('label')!='seat_exit'
                or tuple(current.get('goal',()))!=tuple(flight['intent'].get('goal',()))
                or self._terrain_social_owns() or self._air_motion is not None
                or getattr(self.tracker,'room_generation',None)!=state['room_generation']):
            self._prop_seat_log('seat_exit_recovery_resume_skipped',reason='intent_or_owner_changed')
            return True
        import stuck_recovery
        terrain=self._air_terrain()
        if (terrain is None or not stuck_recovery.landing_valid(terrain,self.position,policy=flight['tracker'].policy)
                or not stuck_recovery.prop_exterior_valid(terrain,self.position,prop_id=state['prop']['id'],policy=flight['tracker'].policy)):
            flight['tracker'].failed(now,'landing_not_exterior_or_valid')
            self._prop_seat_recovery_landing_failed(flight,now,'landing_not_exterior_or_valid')
            return True
        self._prop_seat_finish(state)
        return True

    def _prop_seat_recovery_landing_failed(self,flight,now,reason):
        """真实落地失败后保留待执行命令与尝试预算，不把失败座位事务丢弃。"""
        state=flight.get('seat_exit')
        if state is None or flight.get('seat_retry_restored'):return False
        present=getattr(self,'_prop_seat_state',None)
        if (flight.get('resume_cancelled') or not self._stuck_current(flight.get('intent'))
                or self._terrain_social_owns() or self._air_motion is not None
                or getattr(self.tracker,'room_generation',None)!=state['room_generation']
                or (present is not None and present is not state)):
            self._prop_seat_log('seat_exit_recovery_resume_skipped',reason='failed_landing_owner_changed')
            return False
        if self._prop_seat_valid(state) is False:return False
        flight['seat_retry_restored']=True
        state.setdefault('exit_rejected_landings',[]).append(tuple(self.position))
        state.update(exit_plan=dict(status='blocked',reason=reason,points=[],times=[]),
            exit_geometry_stamp=self._prop_seat_geometry_stamp(),path_done=False,rendered_upright=True)
        self._prop_seat_state=state;self.sitting=None
        self.state={'pose':'seat_exit_retry','upright':True}
        self._prop_seat_log('seat_exit_recovery_retry',reason=reason,
            attempt=flight['tracker'].attempts,at=list(self.position))
        return True

    def _prop_seat_tick(self):
        state=getattr(self,'_prop_seat_state',None)
        if state is None or state['phase']!='getup' or not state['rendered_upright']:return
        plan=self._prop_seat_poll_exit(state)
        if plan is None:return
        point=tuple(plan['points'][-1]) if plan.get('status')=='ready' else None
        if getattr(self,'_prop_seat_state',None) is not state:return
        valid=self._prop_seat_valid(state)
        if valid is not True:
            if valid is False:self._prop_seat_abort('exit_binding_changed')
            return
        if point is None:
            if not state.get('exit_logged'):
                state['exit_logged']=True
                self._prop_seat_log('seat_exit_blocked',reason='no_verified_standing_exit')
            self._prop_seat_recover(state,time.monotonic())
            return
        if not state.get('path_done'):return
        import stuck_recovery
        terrain=self._air_terrain()
        # 不带座椅排除重检落脚，恢复正常寻路不得继承临时排除权。
        if (state.get('exit_geometry_stamp')!=self._prop_seat_geometry_stamp()
                or terrain is None or not stuck_recovery.landing_valid(terrain,self.position)):
            state['exit_plan']=None;return
        if state.get('inferred') and not stuck_recovery.prop_exterior_valid(terrain,self.position,prop_id=state['prop']['id']):
            state.update(exit_plan=dict(status='blocked',reason='inferred_exit_endpoint_not_exterior',points=[],times=[]),
                exit_geometry_stamp=self._prop_seat_geometry_stamp(),path_done=False)
            self._prop_seat_log('seat_exit_revalidate_blocked',reason='inferred_exit_endpoint_not_exterior',at=list(self.position))
            self._prop_seat_recover(state,time.monotonic())
            return
        self._prop_seat_finish(state)
