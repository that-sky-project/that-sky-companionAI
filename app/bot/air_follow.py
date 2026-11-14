"""持续空中跟随的有界本地引导；无线程、文件、RPC、Actor 新字段。

速度沿用旧 AirMotion 上限；加速度/观测门为本轮明确的工程策略。
所有坐标均为 Actor 原点，胶囊查询内部使用现有 TerrainView.offset。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
import air_planner
from skyterrain.capsule import CapsuleProbe


@dataclass(frozen=True)
class Policy:
    fast_height: float = 1.2
    minimum_air_height: float = .65
    fast_confirmation: float = .15
    air_confirmation: float = .6
    ground_height: float = .6
    ground_confirmation: float = .5
    ground_speed: float = 1.6
    stale_seconds: float = 1.5
    sample_gap: float = 2.
    max_observed_speed: float = 25.
    follow_distance: float = 3.
    max_horizontal: float = air_planner.FLY_SPEED
    max_vertical: float = air_planner.CLIMB_SPEED
    max_acceleration: float = 6.
    goal_refresh: float = .2
    max_step: float = .1
    point_budget: int = 512
    max_landing_distance: float = 48.
    max_plan_seconds: float = 30.
    max_plan_phases: int = 1600
    plan_point_budget: int = 4096


DEFAULT_POLICY = Policy()


def _point(value):
    try:
        p=tuple(float(v) for v in value)
        return p if len(p)==3 and all(math.isfinite(v) and abs(v)<=10000 for v in p) else None
    except (TypeError,ValueError,OverflowError):return None


def _vadd(a,b):return tuple(x+y for x,y in zip(a,b))
def _vscale(a,s):return tuple(x*s for x in a)
def _vsub(a,b):return tuple(x-y for x,y in zip(a,b))
def _norm(a):return math.sqrt(sum(x*x for x in a))


def _limit_velocity(v,policy):
    h=math.hypot(v[0],v[2]);scale=min(1.,policy.max_horizontal/max(h,1e-12))
    return (v[0]*scale,max(-policy.max_vertical,min(policy.max_vertical,v[1])),v[2]*scale)


def support_below(terrain, point):
    """只取脚下可站层；不能拿头顶层或云当落脚证据。"""
    if terrain is None:return None
    try:
        rows=[float(h) for _,h,ny in terrain.hits(point[0],point[2])
              if math.isfinite(float(h)) and float(ny)>=math.cos(math.radians(42))
              and h<=point[1]+.3]
        return max(rows) if rows else None
    except (AttributeError,TypeError,ValueError):return None


class TargetTracker:
    """最多32条新鲜观测。门槛不是游戏原生 flight flag 的反编译结论。"""
    def __init__(self,policy=None):
        self.policy=policy or DEFAULT_POLICY;self.reset()

    def reset(self):
        self.binding=None;self.samples=[];self.air_since=None;self.ground_since=None
        self.mode='unknown';self.velocity=(0.,0.,0.);self.heading=0.;self.last=None

    def observe(self,now,point,terrain,binding=None,observed_at=None):
        p=_point(point)
        try:t=float(now);stamp=t if observed_at is None else float(observed_at)
        except (TypeError,ValueError,OverflowError):
            self.reset();return dict(mode='unknown',reason='invalid_observation_time',position=p,velocity=(0.,0.,0.),heading=0.,observed_at=None)
        if (not math.isfinite(t) or not math.isfinite(stamp) or p is None
                or t-stamp < -.05 or t-stamp>self.policy.stale_seconds):
            self.reset();self.binding=binding
            return dict(mode='unknown',reason='stale_or_invalid_observation',position=p,velocity=(0.,0.,0.),heading=self.heading,observed_at=stamp)
        if binding!=self.binding:
            self.reset();self.binding=binding
        if self.samples and stamp<=self.samples[-1][0]:
            if stamp==self.samples[-1][0] and p==self.samples[-1][1] and self.last is not None:
                return dict(self.last)
            self.reset();self.binding=binding
        previous=self.samples[-1] if self.samples else None
        reason='observing'
        if previous:
            dt=stamp-previous[0];distance=math.dist(p,previous[1])
            if dt>self.policy.sample_gap or distance>.25+self.policy.max_observed_speed*dt:
                self.reset();self.binding=binding;previous=None;reason='target_discontinuity'
            else:
                raw=_vscale(_vsub(p,previous[1]),1./dt)
                alpha=1.-math.exp(-dt/.2)
                self.velocity=tuple(old+(new-old)*alpha for old,new in zip(self.velocity,raw))
                if math.hypot(self.velocity[0],self.velocity[2])>.3:
                    wanted=math.atan2(self.velocity[0],self.velocity[2])
                    delta=(wanted-self.heading+math.pi)%math.tau-math.pi
                    self.heading+=max(-math.pi*dt,min(math.pi*dt,delta))
        self.samples.append((stamp,p));self.samples=self.samples[-32:]
        ground=support_below(terrain,p);height=None if ground is None else p[1]-ground
        hs=math.hypot(self.velocity[0],self.velocity[2])
        if height is None:
            self.ground_since=None
            if self.mode=='airborne':reason='airborne_support_missing'
            else:self.air_since=None;self.mode='unknown';reason='support_unknown'
        elif height>self.policy.minimum_air_height:
            self.ground_since=None
            if self.air_since is None:self.air_since=stamp
            age=stamp-self.air_since
            fast=(height>self.policy.fast_height and age>=self.policy.fast_confirmation)
            sustained=age>=self.policy.air_confirmation
            if previous is not None and (fast or sustained):self.mode='airborne';reason='height_confirmed' if fast else 'air_duration_confirmed'
            elif self.mode!='airborne':self.mode='candidate';reason='air_confirmation_pending'
        elif height<=self.policy.ground_height and hs<self.policy.ground_speed and abs(self.velocity[1])<1.2:
            self.air_since=None
            if self.ground_since is None:self.ground_since=stamp
            if stamp-self.ground_since>=self.policy.ground_confirmation:
                self.mode='grounded';reason='ground_confirmed'
            else:self.mode='candidate_ground';reason='ground_confirmation_pending'
        else:
            self.air_since=None;self.ground_since=None
            # 低于起飞门但还没站稳时保持飞行，不在滞回带反复落地起飞。
            if self.mode!='airborne':self.mode='candidate';reason='transition'
        self.last=dict(mode=self.mode,reason=reason,position=p,velocity=self.velocity,heading=self.heading,
                       support=ground,height_above=height,observed_at=stamp,binding=binding,
                       airborne_seconds=0. if self.air_since is None else stamp-self.air_since)
        return dict(self.last)


class _BudgetProbe:
    def __init__(self,terrain,budget):self.probe=CapsuleProbe(terrain);self.budget=budget;self.count=0
    def clear(self,p):
        if self.count>=self.budget:raise ValueError('air_follow_query_budget')
        self.count+=1
        return not self.probe.intersecting(p)

    def curve_clear(self,p,v,a,duration):
        speed=_norm(v);end_speed=_norm(_vadd(v,_vscale(a,duration)))
        # A complete constant-deceleration stop follows one straight segment.
        # Equal spatial samples preserve the <=.025m capsule rule without
        # oversampling slow end-of-brake time or exhausting the hot-step budget.
        if speed>1e-9 and end_speed<1e-7 and duration>0:
            distance=speed*duration*.5;n=max(1,int(math.ceil(distance/.025)))
            direction=_vscale(v,1./speed)
            for i in range(n+1):
                if not self.clear(_vadd(p,_vscale(direction,distance*i/n))):return False
            return True
        speed=max(speed,end_speed)
        n=max(1,int(math.ceil(speed*duration/.025)),int(math.ceil(duration/.025)))
        for i in range(n+1):
            t=duration*i/n
            q=_vadd(_vadd(p,_vscale(v,t)),_vscale(a,.5*t*t))
            if not self.clear(q):return False
        return True


def follow_points(observation,policy=None):
    policy=policy or DEFAULT_POLICY;p=_point(observation.get('position'))
    if p is None:return []
    heading=float(observation.get('heading',0.));f=(math.sin(heading),0.,math.cos(heading));side=(f[2],0.,-f[0])
    out=[]
    for up in (0.,.8,1.6,2.1):
        for offset in (0.,1.5,-1.5):
            out.append((p[0]-f[0]*policy.follow_distance+side[0]*offset,p[1]+up,
                        p[2]-f[2]*policy.follow_distance+side[2]*offset))
    return out


class Guidance:
    """每步最大.1s；当前步及停止轨迹先验证，固定候选/查询预算。"""
    def __init__(self,policy=None):
        self.policy=policy or DEFAULT_POLICY;self.goal=None;self.selected_at=-math.inf;self.side_index=0

    def step(self,terrain,position,velocity,observation,dt=.05,now=0.):
        p=_point(position);v=_point(velocity);policy=self.policy
        try:dt=float(dt);now=float(now)
        except (TypeError,ValueError,OverflowError):return dict(clear=False,reason='air_follow_step_budget',position=p,velocity=(0.,0.,0.))
        if p is None or v is None or not math.isfinite(dt) or not 0<dt<=policy.max_step:
            return dict(clear=False,reason='air_follow_step_budget',position=p,velocity=(0.,0.,0.))
        if math.hypot(v[0],v[2])>policy.max_horizontal+1e-5 or abs(v[1])>policy.max_vertical+1e-5:
            return dict(clear=False,reason='air_follow_velocity_budget',position=p,velocity=(0.,0.,0.))
        target=_point(observation.get('position'))
        inactive=(observation.get('mode') in ('lost','unknown','grounded','candidate_ground') or target is None)
        try:probe=_BudgetProbe(terrain,policy.point_budget)
        except (ValueError,TypeError,AttributeError):return dict(clear=False,reason='air_follow_geometry_missing',position=p,velocity=(0.,0.,0.))
        options=follow_points(observation,policy) if not inactive else [p]
        if not options:return dict(clear=False,reason='air_follow_target_invalid',position=p,velocity=(0.,0.,0.))
        refreshed=self.goal is None or now-self.selected_at>=policy.goal_refresh
        if not refreshed:
            options=[self.goal]+options
        else:
            # 侧选稳定：不在左右候选等价时每帧翻转。
            i=min(self.side_index,len(options)-1);options=[options[i]]+options[:i]+options[i+1:]
        target_v=_limit_velocity(_point(observation.get('velocity')) or (0.,0.,0.),policy)
        stop_acc=_vscale(v,-policy.max_acceleration/max(_norm(v),1e-12))
        stop_time=_norm(v)/policy.max_acceleration
        braking_safe=False;fallback=None
        try:
            if not probe.clear(p):return dict(clear=False,reason='air_follow_current_overlap',position=p,velocity=(0.,0.,0.))
            braking_safe=probe.curve_clear(p,v,stop_acc,stop_time)
            brake_acc=stop_acc if stop_time>=dt else _vscale(v,-1./dt)
            brake_next=_vadd(_vadd(p,_vscale(v,dt)),_vscale(brake_acc,.5*dt*dt))
            if braking_safe and probe.curve_clear(p,v,brake_acc,dt):
                fallback=dict(clear=True,position=brake_next,velocity=_vadd(v,_vscale(brake_acc,dt)),
                    acceleration=brake_acc,goal=self.goal or p,reason='target_wait_brake' if inactive else 'obstacle_brake',
                    coverage_points=[p,brake_next,_vadd(p,_vscale(v,stop_time*.5))],dt=dt)
            for index,goal in enumerate(options[:12] if not inactive else []):
                if not probe.clear(goal):continue
                delta=_vsub(goal,p)
                # 位置误差主导，目标速度只作为有界前馈；悬停时衰减为精确停止。
                wanted=_limit_velocity(_vadd(target_v,_vscale(delta,1.3)),policy)
                change=_vsub(wanted,v);size=_norm(change)
                accel=_vscale(change,min(1./dt,policy.max_acceleration/max(size,1e-12)))
                nxtv=_vadd(v,_vscale(accel,dt))
                nxt=_vadd(_vadd(p,_vscale(v,dt)),_vscale(accel,.5*dt*dt))
                if not probe.curve_clear(p,v,accel,dt):continue
                brake_time=_norm(nxtv)/policy.max_acceleration
                brake_acc=_vscale(nxtv,-policy.max_acceleration/max(_norm(nxtv),1e-12))
                if not probe.curve_clear(nxt,nxtv,brake_acc,brake_time):continue
                stop=_vadd(nxt,_vscale(nxtv,brake_time*.5))
                self.goal=goal
                if refreshed:self.selected_at=now
                original=follow_points(observation,policy)
                self.side_index=min(range(len(original)),key=lambda i:math.dist(original[i],goal))
                return dict(clear=True,position=nxt,velocity=nxtv,acceleration=accel,goal=goal,
                            reason='tracking' if _norm(delta)>.15 else 'hover',queries=probe.count,
                            coverage_points=[p,nxt,stop],dt=dt)
        except (ValueError,TypeError,AttributeError):
            pass
        # 先前已验证的停止轨迹使正常障碍退化为受限减速，而非原地瞬停。
        if fallback is not None:
            fallback['queries']=probe.count
            return fallback
        return dict(clear=False,reason='emergency_geometry_hold',position=p,velocity=(0.,0.,0.),queries=probe.count)


def _smooth_line(a,b,phase,*,speed=4.,accel=4.,step=.025):
    distance=math.dist(a,b)
    if distance<1e-8:return []
    direction=_vscale(_vsub(b,a),1./distance)
    speed=min(speed,6.5/max(math.hypot(direction[0],direction[2]),1e-9),3.5/max(abs(direction[1]),1e-9))
    peak=min(speed,math.sqrt(distance*accel));ramp=peak/accel;flat=max(0.,distance/peak-ramp);total=2*ramp+flat
    def at(t):
        if t<ramp:s=.5*accel*t*t;v=accel*t
        elif t<ramp+flat:s=.5*peak*ramp+peak*(t-ramp);v=peak
        else:
            left=total-t;s=distance-.5*accel*left*left;v=accel*left
        return _vadd(a,_vscale(direction,s)),_vscale(direction,v)
    n=max(1,int(math.ceil(total/step)));result=[];p0,v0=tuple(a),(0.,0.,0.)
    for i in range(1,n+1):
        t=total*i/n;p1,v1=at(t)
        if i==n:p1=tuple(b);v1=(0.,0.,0.)
        result.append(dict(phase=phase,mode='linear',duration=total/n,**{'from':p0,'to':p1},velocity_from=v0,velocity_to=v1))
        p0,v0=p1,v1
    return result


def _brake_phases(position,velocity,accel=6.):
    speed=_norm(velocity)
    if speed<1e-6:return [],tuple(position)
    duration=speed/accel;a=_vscale(velocity,-accel/speed);n=max(1,int(math.ceil(duration/.025)))
    result=[];p0=tuple(position);v0=tuple(velocity)
    for i in range(1,n+1):
        t=duration*i/n;p1=_vadd(_vadd(position,_vscale(velocity,t)),_vscale(a,.5*t*t));v1=_vadd(velocity,_vscale(a,t))
        result.append(dict(phase='brake',mode='linear',duration=duration/n,**{'from':p0,'to':p1},velocity_from=v0,velocity_to=v1))
        p0,v0=p1,v1
    return result,p0


def _linear_route_clear(terrain,phases,budget=4096):
    # Collinear time subdivisions represent exactly the same geometric segment.
    # Keep the output phases unchanged; collapse only collision query vertices.
    segments=[]
    for phase in phases:
        if phase.get('mode')!='linear':return False
        a,b=tuple(phase['from']),tuple(phase['to'])
        if math.dist(a,b)<1e-10:continue
        if segments:
            olda,oldb=segments[-1];u=_vsub(oldb,olda);v=_vsub(b,a)
            cross=(u[1]*v[2]-u[2]*v[1],u[2]*v[0]-u[0]*v[2],u[0]*v[1]-u[1]*v[0])
            if (math.dist(oldb,a)<1e-8 and sum(x*y for x,y in zip(u,v))>=0
                    and _norm(cross)<=1e-8*max(_norm(u)*_norm(v),1e-12)):
                segments[-1]=(olda,b);continue
        segments.append((a,b))
    probe=_BudgetProbe(terrain,budget)
    if not segments:return probe.clear(phases[0]['from'])
    for a,b in segments:
        n=max(1,math.ceil(math.dist(a,b)/.025))
        for i in range(n+1):
            if not probe.clear(tuple(x+(y-x)*i/n for x,y in zip(a,b))):return False
    return True


def _make_plan(terrain,start,target,phases,reason,*,landing=None,observation=None,prop_exit=None):
    from stuck_recovery import validate_static_takeoff
    if not phases or len(phases)>DEFAULT_POLICY.max_plan_phases:return None
    if sum(p['duration'] for p in phases)>DEFAULT_POLICY.max_plan_seconds:return None
    try:
        air_planner._check_limits(phases,airborne=True,jump=prop_exit is not None)
        proof=None
        if prop_exit is None:
            if not CapsuleProbe(terrain).intersecting(start) and all(p.get('mode')=='linear' for p in phases):
                if not _linear_route_clear(terrain,phases,DEFAULT_POLICY.plan_point_budget):return None
            else:
                proof,why=validate_static_takeoff(terrain,phases)
                if why:return None
        else:
            import stuck_recovery as sr
            geometry=sr._Geometry(terrain,sr.RecoveryPolicy())
            clear,why=geometry.trajectory_clear(phases)
            if not clear:return None
            verified=geometry._prop_exit_cache.get(tuple(start))
            if verified is None or verified.get('prop_id')!=prop_exit.get('prop_id'):return None
            prop_exit=dict(verified)
            ranges=getattr(terrain.surface,'collider_ranges',())
            row=next((r for r in ranges if r.get('id')==prop_exit['prop_id']),None)
            if row is None:return None
            allowed=set(range(row['start'],row['start']+row['count']))
            probe=CapsuleProbe(terrain);released=False
            trajectory=air_planner.Trajectory([dict(q) for q in phases])
            for t,point in trajectory.sample(.01):
                contacts=probe.intersecting(point)
                if contacts-(set() if released else allowed):return None
                if contacts and (t>prop_exit['max_seconds'] or math.dist(start,point)>prop_exit['max_distance']
                        or math.hypot(point[0]-start[0],point[2]-start[2])>prop_exit.get('max_horizontal',.75)):return None
                if not contacts:released=True
            if not released:return None
    except (ValueError,TypeError,AttributeError):return None
    endpoint=tuple(phases[-1]['to'])
    result=dict(kind='fly',reason=reason,start=tuple(start),target=tuple(target),goal=endpoint,
                takeoff=tuple(start),landing=endpoint if landing is None else tuple(landing),
                phases=phases,waypoints=[tuple(start)]+[tuple(p['to']) for p in phases],
                air_follow=True,air_follow_session=True,air_follow_observation=observation,walk_distance=0.,
                end_velocity=(0.,0.,0.),landing_validation='motor_capsule_0.25_1.5_skin_0.015')
    if proof is not None:result['static_overlap_escape']=proof
    if prop_exit is not None:result['prop_overlap_escape']=prop_exit
    return result


def takeoff_plan(terrain,start,observation):
    start=_point(start);target=_point(observation.get('position'))
    if start is None or target is None:return None
    try:
        probe=CapsuleProbe(terrain);contacts=probe.intersecting(start)
        ranges=getattr(terrain.surface,'collider_ranges',())
        prop_initial=any(any(row['start']<=face<row['start']+row['count'] for row in ranges) for face in contacts)
        prop_exit=None
        if prop_initial:
            import stuck_recovery as sr
            escaped=sr.plan_recovery(terrain,start,target,escape_near_prop=True)
            prop_exit=escaped.get('prop_overlap_escape')
            if escaped.get('kind') not in ('jump','fly') or prop_exit is None:return None
            p=tuple(escaped['landing']);prefix=[dict(q) for q in escaped['phases']]
            # Existing ballistic exit lands outside the entity before smooth takeoff.
            prefix.append(dict(phase='land',mode='linear',duration=.1,**{'from':p,'to':p},velocity_from=(0.,0.,0.),velocity_to=(0.,0.,0.)))
        else:
            resolved=probe.resolve(start)
            if resolved is None:return None
            p=tuple(resolved['point']);prefix=_smooth_line(start,p,'climb',speed=1.5,accel=4.)
        direction=_vsub(target,p);h=math.hypot(direction[0],direction[2]);ux,uz=(direction[0]/h,direction[2]/h) if h>.1 else (0.,1.)
        for up,forward,side in ((1.4,0.,0.),(1.4,.6,0.),(1.4,0.,.6),(1.4,0.,-.6),(.9,0.,0.)):
            end=(p[0]+ux*forward+uz*side,p[1]+up,p[2]+uz*forward-ux*side)
            phases=prefix+_smooth_line(p,end,'climb',speed=3.0,accel=4.)
            result=_make_plan(terrain,start,target,phases,'air_follow_prop_exit_takeoff' if prop_exit else 'air_follow_takeoff',observation=observation,prop_exit=prop_exit)
            if result is not None:return result
    except (ValueError,TypeError,AttributeError):pass
    return None


def landing_plan(terrain,position,velocity,observation=None):
    p=_point(position);v=_point(velocity)
    if p is None or v is None:return None
    if math.hypot(v[0],v[2])>6.5+1e-5 or abs(v[1])>13.7:return None
    target=_point((observation or {}).get('position')) or p
    try:
        brake,stopped=_brake_phases(p,v)
        probe=CapsuleProbe(terrain)
        anchors=[];grounded=bool(observation and observation.get('mode')=='grounded')
        target_ground=support_below(terrain,target) if grounded else None
        if grounded:
            if target_ground is None:return None
            anchors.extend(follow_points(observation)[:3]+[target])
            for radius in (.3,.6,1.,1.5,2.):
                for i in range(8):
                    angle=math.tau*i/8
                    anchors.append((target[0]+radius*math.cos(angle),target_ground,target[2]+radius*math.sin(angle)))
        else:anchors.append(stopped)
        candidates=[]
        for anchor in anchors:
            base=support_below(terrain,anchor)
            if base is None or grounded and abs(base-target_ground)>.75:continue
            point=(anchor[0],base,anchor[2])
            q=point if probe.standable(point) else None
            if (q is not None and (not grounded or abs(q[1]-target_ground)<=.75)
                    and math.dist(stopped,q)<=DEFAULT_POLICY.max_landing_distance and q not in candidates):candidates.append(q)
            if len(candidates)>=12:break
        if not candidates:
            seed=(target[0],target_ground,target[2]) if grounded else stopped
            q=air_planner._capsule_landing(terrain,seed)
            if q is not None and (not grounded or abs(q[1]-target_ground)<=.75):candidates.append(q)
        if grounded:candidates.sort(key=lambda q:(abs(q[1]-target_ground)>.3,math.dist(q,follow_points(observation)[0]),math.dist(q,stopped)))
        for landing in candidates[:6]:
            if not probe.standable(landing):continue
            routes=[[landing]]
            for extra in (1.5,3.):
                height=max(stopped[1],landing[1]+extra)
                routes.append([(stopped[0],height,stopped[2]),(landing[0],height,landing[2]),landing])
            for points in routes:
                phases=list(brake);last=stopped
                for i,q in enumerate(points):
                    name='land' if i==len(points)-1 else ('climb' if q[1]>last[1] else 'cruise')
                    phases.extend(_smooth_line(last,q,name,speed=5.5,accel=4.));last=q
                if not phases:
                    phases=[dict(phase='land',mode='linear',duration=.05,**{'from':p,'to':p})]
                result=_make_plan(terrain,p,target,phases,'air_follow_land',landing=landing,observation=observation)
                if result is not None:
                    result['airborne_start']=True;result['start_velocity']=v;return result
    except (ValueError,TypeError,AttributeError):pass
    return None
