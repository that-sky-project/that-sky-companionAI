"""持续空中跟随的单一运动对象；复用 AirMotion 真人动画和原落地收尾。

只有 advance 推进空间状态；pos/render/arrived 都只读同一个已验证时间点。
跟随段仅保存有界历史，不把长会话伪装成一个无限长旧 AirMotion 计划。
"""
from __future__ import annotations

from collections import deque
import copy
import math
import time

import air_motion
import air_planner
import air_follow
import anim_pose

MAX_STEP = .05
MAX_ELAPSED = .5
MAX_ACCELERATION = 6.0
HISTORY_STEPS = 512
LEASE_SECONDS = 20.0


def _point(value):
    result=tuple(float(v) for v in value)
    if len(result)!=3 or not all(math.isfinite(v) and abs(v)<=1e6 for v in result):
        raise ValueError('air_follow_point')
    return result


class _LiveTrajectory:
    """以真实运动积分段回放 Actor 的位置/速度，动画时钟不随目标更新复位。"""
    def __init__(self,position,velocity,heading,animation_offset=0.):
        self.rows=deque(maxlen=HISTORY_STEPS)
        self.origin=_point(position);self.current=self.origin
        self.current_velocity=_point(velocity)
        self.duration=0.;self._heading=heading
        self.animation_offset=max(0.,float(animation_offset))
        self.phases=[dict(phase='cruise',mode='linear',duration=0.,t0=0.,t1=0.,
                         **{'from':self.origin,'to':self.origin})]

    def append(self,start,end,position,velocity,acceleration):
        p,v,a=map(_point,(position,velocity,acceleration));dt=end-start
        if not 0<dt<=MAX_STEP+1e-8 or start<self.duration-1e-7:
            raise ValueError('air_follow_segment_time')
        self.rows.append((start,end,p,v,a))
        self.current=tuple(p[i]+v[i]*dt+.5*a[i]*dt*dt for i in range(3))
        self.current_velocity=tuple(v[i]+a[i]*dt for i in range(3))
        self.duration=end
        self.phases=[dict(phase='cruise',mode='linear',duration=dt,t0=start,t1=end,
                         **{'from':p,'to':self.current})]

    def _row(self,t):
        for row in reversed(self.rows):
            if t>=row[0]-1e-9:return row
        return self.rows[0] if self.rows else None

    def pos(self,t):
        if not self.rows:return self.current
        if t>=self.duration:return self.current
        row=self._row(t);dt=max(0.,min(row[1]-row[0],t-row[0]))
        return tuple(row[2][i]+row[3][i]*dt+.5*row[4][i]*dt*dt for i in range(3))

    def velocity(self,t):
        if not self.rows or t>=self.duration:return self.current_velocity
        row=self._row(t);dt=max(0.,min(row[1]-row[0],t-row[0]))
        return tuple(row[3][i]+row[4][i]*dt for i in range(3))

    def heading(self,t):
        vx,_,vz=self.velocity(t)
        if math.hypot(vx,vz)>1e-4:self._heading=math.degrees(math.atan2(vx,vz))
        return self._heading

    def phase_at(self,t):
        # 保持同一巡航动画时间轴，不按每个50ms控制段重播动画。
        return dict(phase='cruise',mode='linear',t0=-self.animation_offset,t1=self.duration,
                    duration=self.duration+self.animation_offset,**{'from':self.origin,'to':self.current})


class AirFollowMotion(air_motion.AirMotion):
    """一次起飞、持续巡航/悬停、一次降落；原始协议字段与 clips 完全复用。"""
    air_follow=True

    def __init__(self,plan,started=None,*,clips=None,walk_clip=None,terrain=None,
                 log=None,kind=None,observation=None):
        super().__init__(plan,started,clips=clips,walk_clip=walk_clip,terrain=terrain,log=log,kind=kind)
        self.plan['air_follow_session']=True
        self.follow_mode='takeoff'
        self.observation=observation if observation is not None else plan.get('air_follow_observation')
        self._session_started=self.t0;self._lease_at=self.t0;self.lease_count=1
        self._last_now=self.t0;self._committed=self.a
        self._current_velocity=tuple(self.trajectory.velocity(0.))
        self._guided=air_follow.Guidance()
        self._static_takeoff_state=None
        self._prop_takeoff_state=None
        self._land_requested=None;self._last_update_log=-math.inf
        self.last_result=dict(ok=True,phase=self.follow_mode,position=self.a,
                              velocity=self._current_velocity,reason='takeoff')
        self._preview=[self.a]
        self._frame_error=None

    @classmethod
    def from_cruise(cls,existing,now,*,terrain,observation,path_check=None):
        """从普通fly巡航连续接续，不改原对象、不重播takeoff。

        调用方短锁内复核当前motion/所有权；本方法只做有界RAM胶囊验证。
        初始豁免证明仍挂在计划上时不接管，避免丢失原验证状态；恢复、
        座位、平台转移等专用航班仍由各自原所有者完整收尾。
        """
        try:
            if type(existing) is not air_motion.AirMotion or existing.kind!='fly':return None
            if existing.cancelled or existing._landed or existing.arrived(now):return None
            if existing.phase(now)!='cruise' or terrain is None:return None
            if any(existing.plan.get(key) for key in (
                    'recovery','stuck_recovery','recovery_intent','seat_exit','seat_rollback',
                    'prop_seat_approach','prop_platform_transfer','prop_exterior_landing',
                    'static_overlap_escape','prop_overlap_escape','air_follow_session')):return None
            if any(tag in str(existing.plan.get('reason','')).lower() for tag in ('recovery','seat_exit','rollback')):return None
            clock=float(now)
            if not math.isfinite(clock) or clock<existing.t0:return None
            position=_point(existing.pos(clock));local=existing._local(clock)
            velocity=_point(existing.trajectory.velocity(local))
            if (math.hypot(velocity[0],velocity[2])>air_planner.FLY_SPEED+1e-6
                    or abs(velocity[1])>air_planner.CLIMB_SPEED+1e-6):return None
            from skyterrain.capsule import CapsuleProbe
            if CapsuleProbe(terrain).intersecting(position):return None
            # 已在飞，接续前还须有可执行的安全刹停余量，而不是只验单点。
            safety=air_follow.Guidance().step(terrain,position,velocity,
                dict(mode='lost',position=position,velocity=(0.,0.,0.)),dt=MAX_STEP,now=clock)
            if not safety.get('clear'):return None
            preview=[position]+[_point(p) for p in safety.get('coverage_points',())]
            if callable(path_check) and not path_check(preview):return None
            phase=existing.trajectory.phase_at(local)
            animation_offset=max(0.,local-float(phase.get('t0',0.)))
            endpoint=tuple(position[i]+velocity[i]*MAX_STEP for i in range(3))
            plan=dict(kind='fly',reason='air_follow_resume_cruise',start=position,goal=endpoint,
                target=tuple((observation or {}).get('position') or position),landing=endpoint,
                phases=[dict(phase='cruise',mode='linear',duration=MAX_STEP,**{'from':position,'to':endpoint})],
                waypoints=preview,air_follow_session=True,air_follow_observation=observation,
                promoted_from_reason=existing.plan.get('reason'))
            result=cls(plan,clock,clips=existing.clips,walk_clip=existing.walk_clip,
                terrain=terrain,log=existing.log,observation=observation)
            result._committed=position;result._current_velocity=velocity;result.velocity=velocity
            result.heading=(math.degrees(math.atan2(velocity[0],velocity[2]))
                            if math.hypot(velocity[0],velocity[2])>1e-6 else existing.heading)
            result.final_heading=result.heading
            result._begin_cruise(clock)
            result.trajectory.animation_offset=animation_offset
            result._cursors=dict(existing._cursors)
            for key in ('_anim','_prev_stamp','_last_clip_frame'):
                setattr(result,key,copy.deepcopy(getattr(existing,key)))
            result._preview=preview
            result.last_result=dict(ok=True,phase='cruise',position=position,velocity=velocity,
                                    reason='ordinary_cruise_promoted')
            return result
        except (ValueError,TypeError,AttributeError,KeyError,IndexError):return None

    @property
    def preview_points(self):return tuple(self._preview)

    @property
    def current_velocity(self):return tuple(self._current_velocity)

    def pos(self,now):
        # 旧调用者可能在advance之前看未来now；这不构成移动许可。
        return tuple(self._committed)

    def arrived(self,now):
        return bool(self._landed or self._frame_error is None and self.follow_mode=='landing'
                    and self._last_now-self.t0>=self.seconds-1e-8)

    def phase(self,now):
        if self._landed:return 'landed'
        if self.follow_mode=='cruise':return 'cruise'
        phase=self.trajectory.phase_at(max(0.,self._last_now-self.t0))
        return (phase or {}).get('phase','climb' if self.follow_mode=='takeoff' else 'land')

    def refresh(self,terrain,observation,now):
        if terrain is not None:self.terrain=terrain
        if observation is not None:self.observation=observation
        # 仅交换RAM快照；完整落点搜索在冷线程、motion锁外执行。
        return True

    def _check_timed_interval(self,now,path_check=None):
        """takeoff/landing也逐帧复核，不能只信起飞时冻结的道具列表。"""
        if self.terrain is None:return 'air_follow_geometry_unavailable'
        import stuck_recovery
        begin=max(0.,self._last_now-self.t0);end=min(max(0.,now-self.t0),self.seconds)
        points=[self._committed];at=begin
        while at<end-1e-9:
            at=min(end,at+.01);points.append(self.trajectory.pos(at))
        if callable(path_check) and not path_check(points):return 'air_follow_path_coverage_missing'
        if self.plan.get('prop_overlap_escape'):
            state,reason=stuck_recovery.prop_overlap_frame(self.terrain,self,self._committed,
                now-self.t0,self._prop_takeoff_state)
            if reason is None:self._prop_takeoff_state=state
            return reason
        if self.plan.get('static_overlap_escape'):
            state,reason=stuck_recovery.static_overlap_frame(self.terrain,self,self._committed,
                now-self.t0,self._static_takeoff_state)
            if reason is None:self._static_takeoff_state=state
            return reason
        from skyterrain.capsule import CapsuleProbe
        probe=CapsuleProbe(self.terrain)
        previous=self._committed;t=begin
        if probe.intersecting(previous):return 'air_follow_initial_contact_changed'
        while t<end-1e-9:
            t=min(end,t+.01);point=self.trajectory.pos(t)
            if probe.sweep(previous,point):return 'air_follow_new_collision'
            previous=point
        return None

    def _hold(self,now,reason):
        self._frame_error=str(reason);self._land_requested=self._land_requested or str(reason)
        # 突然增加的墙/未知几何不能通过平滑假运动消除；守住最后已发位置并明确上报。
        self._current_velocity=(0.,0.,0.)
        self._last_now=now
        self.last_result=dict(ok=False,phase=self.follow_mode,reason=str(reason),
                              position=self.pos(now),velocity=self.current_velocity,emergency_hold=True)
        return self.last_result

    def _begin_cruise(self,now):
        self.follow_mode='cruise';self.t0=now
        # 初始接触已由整段/逐帧证明清出；移除的是过期证明，不是碰撞。
        self.plan.pop('static_overlap_escape',None)
        self.plan.pop('prop_overlap_escape',None)
        self._static_takeoff_state=None
        self._prop_takeoff_state=None
        self.trajectory=_LiveTrajectory(self._committed,self._current_velocity,self.heading)
        self.seconds=1.;self.a=self._committed
        self._last_now=now

    def _step(self,now,dt,start,v0,path_check=None):
        observation=self.observation
        if self._land_requested:
            observation=dict(observation or {},mode='lost',state='lost',velocity=(0.,0.,0.))
            observation.setdefault('position',start)
        result=self._guided.step(self.terrain,start,v0,observation,dt=dt,now=now)
        if not result.get('clear'):return dict(ok=False,reason=result.get('reason','air_follow_step_blocked'))
        p,v,a=map(_point,(result['position'],result['velocity'],result['acceleration']))
        integrated=tuple(start[i]+v0[i]*dt+.5*a[i]*dt*dt for i in range(3))
        integrated_v=tuple(v0[i]+a[i]*dt for i in range(3))
        if (math.dist(p,integrated)>1e-6 or math.dist(v,integrated_v)>1e-6
                or math.dist(a,(0.,0.,0.))>MAX_ACCELERATION+1e-6
                or math.hypot(v[0],v[2])>air_planner.FLY_SPEED+1e-6
                or v[1]>air_planner.CLIMB_SPEED+1e-6 or v[1]<-air_planner.MAX_FALL-1e-6):
            return dict(ok=False,reason='air_follow_kinematic_budget')
        preview=[start,p]+[tuple(x) for x in result.get('coverage_points',())][:48]
        if callable(path_check) and not path_check(preview):return dict(ok=False,reason='air_follow_path_coverage_missing')
        return dict(result,ok=True,position=p,velocity=v,acceleration=a,preview=preview,
                    segment_start=start,velocity_start=v0,now=now,dt=dt)

    def _commit_step(self,result):
        start=result['segment_start'];v0=result['velocity_start'];dt=result['dt'];now=result['now']
        p,v,a=result['position'],result['velocity'],result['acceleration']
        local=max(0.,now-self.t0)
        self.trajectory.append(local-dt,local,start,v0,a)
        self._committed=p;self._current_velocity=v;self.velocity=v
        self.b=p;self.seconds=max(self.seconds,local+1.)
        goal=result.get('goal') or p
        self._preview=result['preview']
        self.plan.update(start=start,goal=p,target=tuple(goal),phases=[dict(phase='cruise',mode='linear',
            duration=dt,**{'from':start,'to':p})],waypoints=list(self._preview))
        heading=self.trajectory.heading(local)
        if heading is not None:self.heading=heading
        self.final_heading=self.heading
        self._last_now=now
        self.last_result=dict(result,ok=True,phase='cruise',position=p,velocity=v)
        return self.last_result

    def advance(self,now,terrain=None,observation=None,*,path_check=None):
        now=float(now)
        if not math.isfinite(now):raise ValueError('air_follow_time')
        if terrain is not None:self.terrain=terrain
        if observation is not None:self.observation=observation
        if now<=self._last_now+1e-9:return dict(self.last_result)
        dt=now-self._last_now
        if dt>MAX_ELAPSED:return self._hold(now,'air_follow_clock_gap')
        if self._frame_error:return self._hold(now,self._frame_error)
        if self.follow_mode!='cruise':
            try:reason=self._check_timed_interval(now,path_check)
            except (ValueError,TypeError,AttributeError,KeyError,IndexError):reason='air_follow_geometry_error'
            if reason:return self._hold(now,reason)
            self._last_now=now
            local=min(now-self.t0,self.seconds)
            self._committed=tuple(self.trajectory.pos(local))
            self._current_velocity=tuple(self.trajectory.velocity(local))
            self.velocity=self._current_velocity
            self._preview=[self._committed]
            self.last_result=dict(ok=True,phase=self.follow_mode,position=self._committed,
                                  velocity=self._current_velocity,reason='timed_phase')
            if self.follow_mode=='takeoff' and local>=self.seconds-1e-8:
                self._current_velocity=_point(self.plan.get('end_velocity',(0.,0.,0.)))
                self._begin_cruise(now)
            return dict(self.last_result)
        remaining=dt;position=self._committed;velocity=self._current_velocity
        cursor=self._last_now;pending=[]
        while remaining>1e-9:
            step=min(MAX_STEP,remaining);target_time=cursor+step
            try:result=self._step(target_time,step,position,velocity,path_check)
            except (ValueError,TypeError,AttributeError,KeyError,IndexError):
                return self._hold(now,'air_follow_step_error')
            if not result.get('ok'):return self._hold(now,result.get('reason','air_follow_step_blocked'))
            pending.append(result);position=result['position'];velocity=result['velocity'];cursor=target_time
            remaining-=step
        for result in pending:self._commit_step(result)
        if now-self._lease_at>=LEASE_SECONDS:
            self._lease_at=now;self.lease_count+=1
        return dict(self.last_result)

    def request_land(self,reason,now,terrain=None,observation=None):
        """单线程便捷接口；并发运行时用锁外规划+短锁install_landing。"""
        if self._landed or self.follow_mode=='landing':return True
        if terrain is not None:self.terrain=terrain
        if observation is not None:self.observation=observation
        self._land_requested=str(reason)
        if self.terrain is None:return False
        plan=air_follow.landing_plan(self.terrain,self._committed,self._current_velocity,self.observation)
        return self.install_landing(plan,now,reason,require_stopped=False)

    def install_landing(self,plan,now,reason='target_grounded',*,require_stopped=True):
        """原子提交已验证的冷计划，不做碰撞查询/搜索/IO。

        调用者在短motion锁内复核owner、room与geometry版本。此处再次核对
        位置/速度，拒绝锁外规划期间已被热帧推进的过期计划。
        """
        if self._landed:return False
        if self.follow_mode=='landing':return True
        try:
            if not plan or plan.get('kind') not in ('fly','drop','jump'):return False
            start=_point(plan['start']);phases=[dict(p) for p in plan['phases']]
            if not phases or len(phases)>air_follow.DEFAULT_POLICY.max_plan_phases:return False
            if math.dist(start,self._committed)>1e-7:return False
            expected_velocity=_point(phases[0].get('velocity_from',(0.,0.,0.)))
            if math.dist(expected_velocity,self._current_velocity)>1e-6:return False
            if require_stopped and math.dist(self._current_velocity,(0.,0.,0.))>1e-5:return False
            clock=float(now)
            if not math.isfinite(clock) or clock<self._last_now-1e-9:return False
            trajectory=air_planner.Trajectory(phases)
            if not 0<trajectory.duration<=air_follow.DEFAULT_POLICY.max_plan_seconds:return False
            landing=_point(plan.get('landing') or plan['goal'])
            if math.dist(trajectory.pos(0.),self._committed)>1e-7:return False
            if math.dist(trajectory.pos(trajectory.duration),landing)>1e-7:return False
        except (ValueError,TypeError,AttributeError,KeyError,IndexError):return False
        self.plan=dict(plan,air_follow_session=True,air_follow_landing_reason=str(reason))
        self.trajectory=trajectory
        self.a=self._committed;self.b=landing
        self.t0=clock;self.seconds=self.trajectory.duration;self._last_now=clock
        self.follow_mode='landing';self._frame_error=None;self._static_takeoff_state=None;self._prop_takeoff_state=None
        self._land_requested=str(reason)
        self.cancelled=False;self.cancel_reason=str(reason)
        self._preview=[self.a,self.b]
        self.last_result=dict(ok=True,phase='landing',position=self.a,velocity=self.current_velocity,reason=str(reason))
        return True

    def cancel(self,now=None,terrain=None,reason='interrupt',prefer=None):
        # 该API也会在_post中调用：只排队并刹停，不在Actor线程搜索落点。
        # True表示已受理，绝不表示已降落；冷tick调用request_land完成整段验证。
        if terrain is not None:self.terrain=terrain
        self._land_requested=str(reason)
        return True

    def render(self,body,now):
        # 保留原clip/key/gen/pose写入；未advance的未来时间不能提前演完起飞或落地。
        rendered=super().render(body,self._last_now)
        return anim_pose.write_velocity(rendered,(0.,0.,0.)) if self._frame_error else rendered
