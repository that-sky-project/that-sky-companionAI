"""Bounded stuck recovery, separate from normal walking and air policy.

All coordinates are Actor-origin coordinates. Geometry is the already loaded
GroundSurface behind TerrainView; no geometry, network, or account is loaded
here. Candidates are sampled at 0.25 m resolution, ordered by 3-D distance
(target direction only breaks distance ties), then checked for foot support,
headroom and swept body/triangle clearance. A recovery never walks to takeoff.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import air_planner


@dataclass(frozen=True)
class RecoveryPolicy:
    invalid_failures: int = 2
    invalid_span: float = 0.75
    prepare_failures: int = 3
    prepare_span: float = 2.0
    stall_seconds: float = 6.0
    progress_meters: float = 0.25
    cooldown_seconds: float = 8.0
    exhausted_cooldown_seconds: float = 30.0
    max_attempts: int = 2
    search_radius: float = 6.0
    search_step: float = 0.25
    max_height_delta: float = 3.0
    jump_distance: float = 3.0
    jump_height_delta: float = 1.2
    body_radius: float = 0.27
    body_height: float = 1.8
    support_delta: float = 0.25

    def __post_init__(self):
        for name, value in vars(self).items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) \
                    or not math.isfinite(value) or value <= 0:
                raise ValueError("stuck_recovery_policy:" + name)
        for name in ("invalid_failures", "prepare_failures", "max_attempts"):
            if not isinstance(getattr(self, name), int):
                raise ValueError("stuck_recovery_policy_integer:" + name)
        # Configuration must not accidentally turn the bounded search into an
        # unbounded hot-loop, or a failure into continuous airborne motion.
        if self.search_radius > 12 or self.search_step < 0.15 or self.max_attempts > 3:
            raise ValueError("stuck_recovery_policy_budget")
        if self.invalid_failures < 2 or self.prepare_failures < 2 \
                or self.cooldown_seconds < 1.0 or self.stall_seconds < 2.0 \
                or self.exhausted_cooldown_seconds < 8.0 \
                or self.invalid_span < 0.25 or self.prepare_span < 0.5 \
                or self.body_radius < 0.27 or self.body_height < 1.8 \
                or self.max_height_delta > 6.0 or self.support_delta > 0.25:
            raise ValueError("stuck_recovery_policy_safety")


def classify_failure(reason):
    text = str(reason or "").lower()
    # 目标无支撑不等于脚下卡住；放在泛型 blocked/prepare 前，保留外层前缀。
    if any(word in text for word in (
            "goal_support_missing", "goal_not_standable", "goal_not_supported",
            "goal_unsupported", "goal_not_grounded", "goal_no_support",
            "no_ground_at_goal", "goal_projection_failed", "goal_off_nav",
            "goal_not_on_nav", "goal_outside_nav", "invalid_goal",
            "target_support_missing", "target_not_standable", "target_no_support",
            "route_endpoint_no_ground")):
        return "invalid_goal"
    if any(word in text for word in (
            "timeout", "deadline", "worker_failed", "worker_unavailable",
            "worker_recovery", "cold", "permission", "not_admitted", "lease",
            "invalidated", "social_owned", "scope", "session", "transport",
            "worker_prepare_failed", "worker_not_prepared", "worker_prepare_unavailable")):
        return "ignore"
    if any(word in text for word in (
            "start_overlaps_collision", "start_not_on_nav", "start_off_nav",
            "start_outside_nav", "start_not_standable", "invalid_start",
            "start_not_supported", "start_unsupported", "no_ground_at_start",
            "start_not_grounded", "start_no_support", "start_projection_failed")):
        return "invalid_start"
    if any(word in text for word in ("no_support_in_start_band", "support_outside_start_band",
                                     "grounded_start_required", "preflight_initial_settle_bounds",
                                     "start_support_missing")):
        return "invalid_start"
    if any(word in text for word in (
            "blocked", "no_route", "no_path", "unreachable", "disconnected",
            "no_prevalidated_or_connected_follow_route", "prepare_failed",
            "prepare_unavailable", "route_failed", "route_unavailable",
            "follow_failed_route_cooldown", "stalled", "no_progress")):
        return "prepare_failure"
    if any(word in text for word in ("off_navmesh", "goal_support_missing", "preflight_support",
                                     "route_endpoint_no_ground", "no_complete_short_route",
                                     "no_short_triangle_routes")):
        return "prepare_failure"
    return "ignore"


class RecoveryTracker:
    """One logical intent, not one changing follow goal or route generation.

    The owner supplies a unique diagnostic token to avoid counting one failed
    prepare on every render tick. Progress resets evidence, never the attempt
    budget. A new explicit intent/room or an owner-approved exhausted cooldown
    may rearm it; cooldown always clears evidence and retains old receipt IDs.
    """
    def __init__(self, policy=None):
        self.policy = policy or RecoveryPolicy()
        self.reset()

    def reset(self):
        self.intent = None
        self.failures = self.invalid_failures = self.attempts = 0
        self.last_attempt = -math.inf
        self.last_progress = self.first_failure = self.first_invalid = None
        self.last_reason = None
        self.position = None
        self.moving = False
        self._tokens = set()
        self.cycles = 0

    @property
    def exhausted(self):
        return self.attempts >= self.policy.max_attempts

    def _clear_evidence(self, now, position=None):
        self.failures = self.invalid_failures = 0
        self.first_failure = self.first_invalid = None
        self.last_progress = float(now)
        self.last_reason = None
        if position is not None:
            self.position = tuple(position)

    def rearm_after_cooldown(self, now, position=None):
        """限额不是永久停机；重开一轮仍须重新积累未使用的失败凭据。"""
        now = float(now)
        wait = max(self.policy.exhausted_cooldown_seconds, 2*self.policy.cooldown_seconds)
        if not self.exhausted or not math.isfinite(now) or now-self.last_attempt < wait:
            return False
        self.attempts = 0
        self.last_attempt = -math.inf
        self.cycles += 1
        self._clear_evidence(now, position)
        # _tokens deliberately retained: a cached old failure cannot refill proof.
        return True

    def observe(self, intent, position, now, *, moving=True):
        now = float(now)
        position = tuple(float(v) for v in position)
        if intent != self.intent:
            self.reset()
            self.intent = intent
            self._clear_evidence(now, position)
        was_moving = self.moving
        self.moving = bool(moving and intent is not None)
        if not self.moving or not was_moving:
            self._clear_evidence(now, position)
        elif self.position is None or math.dist(self.position, position) >= self.policy.progress_meters:
            self._clear_evidence(now, position)
        elif self.last_progress is None or now < self.last_progress:
            self._clear_evidence(now, position)

    def note_failure(self, reason, now, *, token=None):
        kind = classify_failure(reason)
        if kind == "invalid_goal":
            self._clear_evidence(now)
            self.last_reason = str(reason)
            return kind
        if kind == "ignore" or not self.moving:
            return kind
        if token is not None:
            token = str(token)
            if token in self._tokens:
                return kind
            # Bound retained diagnostic IDs without permitting a cached recent
            # failure to be counted twice after real progress.
            if len(self._tokens) >= 256:
                self._tokens = {token}
            else:
                self._tokens.add(token)
        now = float(now)
        self.failures += 1
        self.last_reason = str(reason)
        if self.first_failure is None:
            self.first_failure = now
        if kind == "invalid_start":
            self.invalid_failures += 1
            if self.first_invalid is None:
                self.first_invalid = now
        return kind

    def trigger(self, now):
        p = self.policy
        now = float(now)
        if not self.moving or self.exhausted or now - self.last_attempt < p.cooldown_seconds:
            return None
        if classify_failure(self.last_reason) == "invalid_goal":
            return None
        if self.invalid_failures >= p.invalid_failures \
                and self.first_invalid is not None and now - self.first_invalid >= p.invalid_span:
            return "invalid_start"
        if self.failures >= p.prepare_failures \
                and self.first_failure is not None and now - self.first_failure >= p.prepare_span:
            return "repeated_prepare_failure"
        if self.last_progress is not None and now - self.last_progress >= p.stall_seconds:
            return "no_progress"
        return None

    def begin_attempt(self, now):
        if self.exhausted or float(now) - self.last_attempt < self.policy.cooldown_seconds:
            return False
        self.attempts += 1
        self.last_attempt = float(now)
        self._clear_evidence(now)
        return True

    def landed(self, now, position):
        self._clear_evidence(now, position)

    def failed(self, now, reason):
        self._clear_evidence(now)
        self.last_reason = str(reason)


def _point(value):
    try:
        out = tuple(float(v) for v in value)
    except (ValueError, TypeError):
        return None
    return out if len(out) == 3 and all(math.isfinite(v) for v in out) else None


class _Geometry:
    """Conservative triangle/box SAT; includes vertical faces missed by hits()."""
    def __init__(self, terrain, policy):
        import numpy as np
        self.np = np
        self.terrain, self.policy = terrain, policy
        self._exit_cache = {}
        self._prop_exit_cache = {}
        self._static_exit_cache = {}
        self._static_capsule_starts = set()
        self.surface = getattr(terrain, "surface", None)
        self.offset = _point(getattr(terrain, "offset", (0, 0, 0)))
        if self.offset is None or self.surface is None or not all(
                hasattr(self.surface, key) for key in ("_tri", "_ids", "_ny")):
            raise ValueError("triangle_geometry_required")

    def intersecting(self, a, b=None):
        """Swept conservative body AABB between nearby samples, in real mesh."""
        np, p = self.np, self.policy
        b = a if b is None else b
        lo = np.minimum(a, b) - np.asarray(self.offset)
        hi = np.maximum(a, b) - np.asarray(self.offset)
        # The lower 0.15 m is the existing AirPlanner foot contact allowance.
        lo += np.array((-p.body_radius, 0.15, -p.body_radius))
        hi += np.array((p.body_radius, p.body_height, p.body_radius))
        ids = set()
        for x in (lo[0], hi[0]):
            for z in (lo[2], hi[2]):
                ids.update(int(i) for i in self.surface._ids(float(x), float(z)))
        if not ids:
            return set()
        ids = np.asarray(sorted(ids), dtype=np.int32)
        tri = self.surface._tri[ids]
        live = np.all(tri.max(axis=1) >= lo - 1e-8, axis=1) \
            & np.all(tri.min(axis=1) <= hi + 1e-8, axis=1)
        ids, tri = ids[live], tri[live]
        if not len(ids):
            return set()
        centre, half = (lo + hi) * 0.5, (hi - lo) * 0.5
        vertices = tri - centre
        edges = (vertices[:, 1] - vertices[:, 0],
                 vertices[:, 2] - vertices[:, 1], vertices[:, 0] - vertices[:, 2])
        axes = [np.cross(edges[0], edges[1])]
        for edge in edges:
            for axis in np.eye(3):
                axes.append(np.cross(edge, axis))
        live = np.ones(len(ids), dtype=bool)
        for axis in axes:
            projection = np.einsum("nij,nj->ni", vertices, axis)
            extent = np.abs(axis) @ half
            live &= (projection.min(axis=1) <= extent + 1e-8) \
                & (projection.max(axis=1) >= -extent - 1e-8)
        return set(int(i) for i in ids[live])

    def standable(self, point):
        if self._static_capsule_starts:
            from skyterrain.capsule import CapsuleProbe
            return CapsuleProbe(self.terrain).standable(point)
        p = self.policy
        x, y, z = point
        for i in range(9):
            angle = math.tau * (i - 1) / 8
            dx, dz = ((0., 0.) if i == 0 else
                      (p.body_radius * math.cos(angle), p.body_radius * math.sin(angle)))
            hits = self.terrain.hits(x + dx, z + dz)
            supporting = [hy for _, hy, ny in hits
                          if ny >= math.cos(math.radians(42.0))
                          and abs(hy - y) <= (0.04 if i == 0 else p.support_delta)]
            if not supporting:
                return False
            # A second face just above the centre is not floor tolerance: it
            # can be the upper skin of a thin plank, with this candidate on
            # its underside. Land on the exposed upper face instead. The
            # surrounding footprint keeps its slope allowance separately.
            if i == 0 and any(y + 0.015 < hy <= y + p.body_height for _, hy, _ in hits):
                return False
            if any(y + 0.15 <= hy <= y + p.body_height for _, hy, _ in hits):
                return False
        return not self.intersecting(point)

    def prop_clearance(self, prop_id):
        """完整变换网格的世界XZ保守界；不把局部SAT空隙当作已离开家具。"""
        rows=[row for row in getattr(self.surface,'collider_ranges',()) if row.get('id')==prop_id]
        if len(rows)!=1 or not rows[0].get('geometry_sha256'):return None
        row=rows[0];begin,count=row.get('start'),row.get('count')
        if (type(begin) is not int or type(count) is not int or count<=0
                or begin<0 or begin+count>len(self.surface._tri)):return None
        tri=self.surface._tri[begin:begin+count]
        lo=tri.min(axis=(0,1))+self.np.asarray(self.offset)
        hi=tri.max(axis=(0,1))+self.np.asarray(self.offset)
        if not self.np.isfinite(lo).all() or not self.np.isfinite(hi).all():return None
        margin=self.policy.body_radius+.05
        return dict(prop_id=prop_id,geometry_sha256=row['geometry_sha256'],
            bounds_min=lo.tolist(),bounds_max=hi.tolist(),body_radius=self.policy.body_radius,
            coordinate_frame='actor_world',geometry_offset=list(self.offset),
            clearance_margin=.05,expanded_xz=[float(lo[0]-margin),float(hi[0]+margin),
                float(lo[2]-margin),float(hi[2]+margin)],
            source='complete_transformed_prop_mesh_world_aabb',
            rule='horizontal_body_outside_entire_prop_not_local_contact_only')

    def containing_props(self,point):
        """仅供外部invalid_start证明使用；局部SAT无接触也可能在家具包围盒内。"""
        found=[]
        for row in getattr(self.surface,'collider_ranges',()):
            proof=self.prop_clearance(row.get('id'))
            if (proof and not _outside_prop_xz(point,proof)
                    and point[1]+self.policy.body_height>=proof['bounds_min'][1]
                    and point[1]+.15<=proof['bounds_max'][1]+.05):found.append(proof)
        return found

    def _prop_initial_exit(self, start, initial):
        """只识别当前重叠实例；是否能跳出由实际弹道逐段证明。"""
        ranges = getattr(self.surface, 'collider_ranges', ())
        touching = [row for row in ranges if any(row['start'] <= i < row['start']+row['count'] for i in initial)]
        if not touching:
            return None
        if len(touching) != 1:
            return False
        row = touching[0]
        if (not row.get('id') or not row.get('geometry_sha256')
                or any(not row['start'] <= i < row['start']+row['count'] for i in initial)):
            return False  # Any static wall or second prop overlap is not exempt.
        # 游戏交互允许从当前家具中起跳，包括经过它自己的桌面/凳面。
        # 这不是永久关闭整件碰撞：仅上升初段，首次全清即撤销，不得重入。
        self._prop_exit_cache[start] = dict(prop_id=row['id'],
            geometry_sha256=row['geometry_sha256'], initial_faces=sorted(initial),
            local_faces=list(range(row['count'])), max_distance=1.5,
            max_horizontal=.75, max_seconds=.6, step=.01,
            policy='current_overlapping_prop_ballistic_until_clear_no_reentry')
        return True

    def initial_exit(self, start):
        if start in self._exit_cache:
            return self._exit_cache[start]
        initial = self.intersecting(start)
        if not initial:
            # 足边的 1–2cm 接触可落在旧 AABB 的 .15m 留空带中；胶囊仍须记录。
            from skyterrain.capsule import CapsuleProbe
            capsule_initial = CapsuleProbe(self.terrain).intersecting(start)
            ranges = getattr(self.surface, 'collider_ranges', ())
            if capsule_initial and not any(any(r['start'] <= i < r['start']+r['count']
                    for r in ranges) for i in capsule_initial):
                initial = capsule_initial
        if initial:
            prop_exit = self._prop_initial_exit(start, initial)
            if prop_exit is not None:
                self._exit_cache[start] = (prop_exit, initial)
                return prop_exit, initial
            # 静态初始接触不是拒绝起飞的理由。这里只允许进入候选搜索，
            # 不授予通行权；trajectory_clear 用同 motor 胶囊证明实际路径：
            # <=0.5m / 0.6s 初段、不得加深穿透、不得接触新面、清出即撤销。
            self._static_capsule_starts.add(start)
        self._exit_cache[start] = (True, initial)
        return True, initial

    def trajectory_clear(self, phases):
        traj = air_planner.Trajectory([dict(row) for row in phases])
        start = traj.pos(0.)
        safe_exit, initial = self.initial_exit(start)
        if not safe_exit:
            return False, "unsafe_initial_overlap"
        if start in self._static_capsule_starts:
            proof, reason = validate_static_takeoff(self.terrain, phases)
            if reason is not None:
                return False, reason
            if proof is not None:
                self._static_exit_cache[start] = proof
            return True, None
        allowed = set(initial)
        prop_exit = self._prop_exit_cache.get(start)
        if prop_exit is not None:
            first = phases[0]
            if first.get('mode') != 'ballistic' or first.get('vy0', 0.) <= 0.:
                return False, 'prop_overlap_ballistic_takeoff_required'
            row = next(r for r in self.surface.collider_ranges if r['id']==prop_exit['prop_id'])
            allowed = set(range(row['start'], row['start']+row['count']))
        prev, t = start, 0.
        released = False
        while t < traj.duration - 1e-9:
            t = min(traj.duration, t + 0.01)  # <=0.137 m swept travel at existing speed limit.
            point = traj.pos(t)
            current = self.intersecting(prev, point)
            if current - allowed:
                return False, "trajectory_collision"
            if current:
                if t > 0.6 or point[1] < prev[1] - 1e-7 \
                        or (math.dist(point,start) > prop_exit['max_distance']
                            or math.hypot(point[0]-start[0],point[2]-start[2]) > prop_exit['max_horizontal']
                            if prop_exit is not None else
                            math.hypot(point[0] - start[0], point[2] - start[2]) > 0.35):
                    return False, "initial_overlap_exit_budget"
                if prop_exit is None:allowed.intersection_update(current)
                if not self.intersecting(point):
                    allowed.clear()
                    if prop_exit is not None and not released:
                        prop_exit.update(exit_point=point,exit_time=t,exit_distance=math.dist(start,point))
                        released = True
            else:
                allowed.clear()  # No re-entry after escaping the initial faces.
                if prop_exit is not None and not released:
                    prop_exit.update(exit_point=point,exit_time=t,exit_distance=math.dist(start,point))
                    released = True
            prev = point
        if initial and allowed:
            return False, "initial_overlap_not_cleared"
        return True, None


def _static_contact_signature(surface, faces):
    """只绑定初始接触的真实三角形，不复制全场景，不把证明跨地图复用。"""
    import hashlib
    import numpy as np
    return hashlib.sha256(np.asarray(surface._tri[sorted(faces)], dtype='<f8').tobytes()).hexdigest()


def _static_trajectory_signature(phases):
    import hashlib, json
    rows=[{k:v for k,v in row.items() if k not in ('t0','t1')} for row in phases]
    return hashlib.sha256(json.dumps(rows,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def _static_escape_interval(probe, trajectory, start, begin, end, state, proof):
    allowed = set(state.get('remaining', proof['initial_faces']))
    released = bool(state.get('released', False))
    previous = trajectory.pos(begin)
    if probe.intersecting(previous) - allowed:
        return None, 'static_overlap_new_contact'
    t = begin
    while t < end - 1e-9:
        t = min(end, t + .01)
        point = trajectory.pos(t)
        swept = probe.sweep(previous, point, step=.025)
        if swept - allowed:
            return None, 'static_overlap_new_contact'
        current = probe.intersecting(point)
        if current:
            delta = tuple(point[k] - start[k] for k in range(3))
            if (t > proof['max_seconds'] + 1e-8
                    or math.dist(point, start) > proof['max_distance'] + 1e-8
                    or point[1] < previous[1] - 1e-7):
                return None, 'static_overlap_exit_budget'
            # 初始面只许沿离开自身的一侧退出，不许从墙的一侧借豁免穿到另一侧。
            if any(sum(n[k]*delta[k] for k in range(3)) < -.015
                   for n in proof['outward_normals']):
                return None, 'static_overlap_deeper_penetration'
            allowed.intersection_update(current)
        else:
            allowed.clear()
            released = True
        previous = point
    return dict(elapsed=end, released=released, remaining=sorted(allowed)), None


def validate_static_takeoff(terrain, phases):
    """完整胶囊扫掠；返回 (有限静态初始接触证明或 None, 失败原因或 None)。

    无碰撞的起点不凭空生成豁免。动态道具不走这个入口，继续使用原实例规则。
    """
    try:
        from skyterrain.capsule import CapsuleProbe
        trajectory = air_planner.Trajectory([dict(p) for p in phases])
        start = tuple(trajectory.pos(0.))
        if not phases or trajectory.duration <= 0:
            return None, 'static_overlap_empty_trajectory'
        probe = CapsuleProbe(terrain)
        contacts = probe.contacts(start)
        initial = {int(row['triangle_id']) for row in contacts}
        ranges = getattr(terrain.surface, 'collider_ranges', ())
        if any(any(r['start'] <= face < r['start']+r['count'] for r in ranges) for face in initial):
            return None, 'static_overlap_dynamic_initial_contact'
        proof = dict(initial_faces=sorted(initial),
            geometry_signature=_static_contact_signature(terrain.surface, initial),
            geometry_offset=list(terrain.offset), max_distance=.5, max_seconds=.6,
            outward_normals=[list(row['normal']) for row in contacts],
            capsule=dict(radius=.25,height=1.5,skin=.015),
            policy='initial_static_contacts_only_no_deeper_no_reentry',
            trajectory_signature=_static_trajectory_signature(phases),
            start=list(start))
        state, reason = _static_escape_interval(probe, trajectory, start, 0.,
            trajectory.duration, {}, proof)
        if reason is not None:
            return None, reason
        if state['remaining']:
            return None, 'static_overlap_not_cleared'
        return (proof if initial else None), None
    except (ValueError, TypeError, KeyError, AttributeError, ImportError, IndexError):
        return None, 'static_overlap_geometry_unavailable'


def static_overlap_frame(terrain, air, previous, elapsed, state=None):
    """实际 AirMotion 每帧复核；换几何、增加墙体、重入旧面均撤销动作。"""
    proof = air.plan.get('static_overlap_escape')
    if not isinstance(proof, dict):
        return state, None
    try:
        from skyterrain.capsule import CapsuleProbe
        state = dict(state or {})
        signature = _static_trajectory_signature(air.trajectory.phases)
        expected = state.get('trajectory_signature', proof['trajectory_signature'])
        if signature != expected:
            # cancel 在已清出后换为连续刹停下降；旧豁免永不随新轨迹重新发放。
            if (not getattr(air, 'cancelled', False) or not state.get('released')
                    or math.dist(previous, air.trajectory.pos(0.)) > 1e-5):
                return None, 'static_overlap_trajectory_changed'
            state = dict(elapsed=0., released=True, remaining=[])
        begin = float(state.get('elapsed', 0.))
        end = min(float(elapsed), float(air.seconds))
        if not math.isfinite(end) or end < begin-1e-8 or end-begin > 2.56:
            return None, 'static_overlap_frame_time_budget'
        if math.dist(previous, air.trajectory.pos(begin)) > 1e-5:
            return None, 'static_overlap_position_changed'
        if (list(terrain.offset) != proof['geometry_offset']
                or _static_contact_signature(terrain.surface, proof['initial_faces'])
                   != proof['geometry_signature']):
            return None, 'static_overlap_geometry_changed'
        result, reason = _static_escape_interval(CapsuleProbe(terrain), air.trajectory, tuple(air.a),
            begin, end, state, proof)
        if result is not None:
            result['trajectory_signature'] = signature
        return result, reason
    except (ValueError, TypeError, KeyError, AttributeError, ImportError, IndexError):
        return None, 'static_overlap_geometry_unavailable'


def prop_overlap_frame(terrain, air, previous, elapsed, state=None, *, policy=None):
    """Validate the actual owned AirMotion interval, without editing any mesh.

    The mutable proof contains only remaining local contact faces. A changed
    instance/transform cannot inherit them; once clear, even that instance has
    no exemption. Return a new state only after the entire interval succeeds.
    """
    proof = air.plan.get('prop_overlap_escape')
    if not isinstance(proof, dict):
        return state, None
    state = dict(state or {})
    begin = float(state.get('elapsed', 0.))
    end = min(float(elapsed), float(air.seconds))
    if not math.isfinite(end) or end < begin-1e-8 or end-begin > 2.56:
        return None, 'prop_overlap_frame_time_budget'
    geometry = _Geometry(terrain, policy or RecoveryPolicy())
    if math.dist(previous, air.trajectory.pos(begin)) > 1e-5:
        return None, 'prop_overlap_position_changed'
    released = bool(state.get('released', False))
    row = None
    if not released:
        matching = [r for r in getattr(geometry.surface, 'collider_ranges', ())
                    if r.get('id') == proof.get('prop_id')]
        if (len(matching) != 1 or matching[0].get('geometry_sha256') != proof.get('geometry_sha256')):
            return None, 'prop_overlap_instance_changed'
        row = matching[0]
    remaining = set(state.get('remaining', proof['local_faces'])) if not released else set()
    allowed = {row['start']+i for i in remaining} if row else set()
    point, t = tuple(previous), begin
    # Also check an unchanged timestamp against newly added geometry.
    if geometry.intersecting(point)-allowed:
        return None, 'prop_overlap_dynamic_collision'
    while t < end-1e-9:
        t = min(end, t+.01)
        nxt = air.trajectory.pos(t)
        swept = geometry.intersecting(point, nxt)
        if swept-allowed:
            return None, 'prop_overlap_dynamic_collision'
        if swept and (t > proof['max_seconds'] or nxt[1] < point[1]-1e-7
                      or math.dist(nxt, air.a) > proof['max_distance']
                      or math.hypot(nxt[0]-air.a[0],nxt[2]-air.a[2]) > proof.get('max_horizontal',.75)):
            return None, 'prop_overlap_exit_budget'
        current = geometry.intersecting(nxt)
        if current:
            if proof.get('policy')!='current_overlapping_prop_ballistic_until_clear_no_reentry':
                allowed.intersection_update(current)
        else:
            released = True
            allowed.clear()
        point = nxt
    return dict(elapsed=end, released=released,
                remaining=sorted(i-row['start'] for i in allowed) if row else []), None


def landing_valid(terrain, point, *, policy=None):
    point = _point(point)
    if point is None:
        return False
    try:
        if not getattr(getattr(terrain, 'surface', None), 'collider_ranges', ()):
            from skyterrain.capsule import CapsuleProbe
            return CapsuleProbe(terrain).standable(point)
        return _Geometry(terrain, policy or RecoveryPolicy()).standable(point)
    except (ValueError, TypeError, AttributeError, ImportError):
        return False


def prop_escape_clearance(terrain,position,*,prop_id=None,policy=None,include_near=False):
    """指定座具/唯一重叠道具的完整外轮廓；邻近模式只由已证invalid_start调用。"""
    position=_point(position)
    if position is None:return None
    try:
        geometry=_Geometry(terrain,policy or RecoveryPolicy())
        if prop_id is None:
            safe,_initial=geometry.initial_exit(position)
            proof=geometry._prop_exit_cache.get(position)
            if not safe:return None
            if proof is None:
                nearby=geometry.containing_props(position) if include_near else []
                return nearby[0] if len(nearby)==1 else None
            prop_id=proof['prop_id']
        return geometry.prop_clearance(prop_id)
    except (ValueError,TypeError,AttributeError,ImportError):return None


def _outside_prop_xz(point,clearance):
    x0,x1,z0,z1=clearance['expanded_xz']
    return point[0]<x0-1e-7 or point[0]>x1+1e-7 or point[2]<z0-1e-7 or point[2]>z1+1e-7


def prop_exterior_valid(terrain,position,*,prop_id=None,clearance=None,policy=None):
    """实体外且完整landing通过；证明不得跨实例/变换复用。"""
    position=_point(position)
    if position is None:return False
    try:
        geometry=_Geometry(terrain,policy or RecoveryPolicy())
        current=geometry.prop_clearance(prop_id or (clearance or {}).get('prop_id'))
        if current is None:return False
        if clearance is not None and current!=clearance:return False
        return bool(_outside_prop_xz(position,current) and geometry.standable(position))
    except (ValueError,TypeError,AttributeError,KeyError,ImportError):return False


def prop_interaction_clear(terrain, start, goal, prop_id, *, max_distance=1.5):
    """只证明一段有界入座交互，不证明支撑、不更改任何碰撞资产。"""
    start,goal=_point(start),_point(goal)
    if (start is None or goal is None or terrain is None
            or type(max_distance) not in (int,float) or not 0<max_distance<=2.75
            or math.dist(start,goal)>max_distance+1e-8):
        return False,'prop_interaction_distance'
    try:
        geometry=_Geometry(terrain,RecoveryPolicy())
        rows=[r for r in getattr(geometry.surface,'collider_ranges',()) if r.get('id')==prop_id]
        if len(rows)>1:return False,'prop_interaction_ambiguous_instance'
        ignored=(set(range(rows[0]['start'],rows[0]['start']+rows[0]['count'])) if rows else set())
        previous=start
        for i in range(1,max(1,int(math.ceil(math.dist(start,goal)/.05)))+1):
            count=max(1,int(math.ceil(math.dist(start,goal)/.05)))
            point=tuple(start[k]+(goal[k]-start[k])*i/count for k in range(3))
            if geometry.intersecting(previous,point)-ignored:
                return False,'prop_interaction_static_or_other_collision'
            previous=point
        return True,None
    except (ValueError,TypeError,AttributeError,ImportError):
        return False,'prop_interaction_geometry_unavailable'


def unmodeled_support_observation(terrain, position, props, observers=()):
    """疑似未知道具支撑，不生成面、不宣称可通行、也不豁免静态墙。"""
    position=_point(position)
    if position is None or terrain is None or not isinstance(props,(list,tuple)):
        return None
    try:
        geometry=_Geometry(terrain,RecoveryPolicy())
        static=getattr(geometry.surface,'static_surface',geometry.surface)
        offset=geometry.offset
        class StaticView:
            surface=static
            def __init__(self):self.offset=offset
            def hits(self,x,z):
                return [(i,y+offset[1],ny) for i,y,ny in static.hits(x-offset[0],z-offset[2])]
        static_view=StaticView()
        if _Geometry(static_view,geometry.policy).intersecting(position):
            return None  # 当前真实静态身体接触永远不是未知道具支撑豁免。
        floors=[y for _,y,ny in static_view.hits(position[0],position[2])
                if ny>=math.cos(math.radians(42)) and y<=position[1]+.04]
        floor=max(floors) if floors else None
        gap=None if floor is None else position[1]-floor
        loaded={row.get('id') for row in getattr(geometry.surface,'collider_ranges',())}
        witnesses=[]
        for observer in observers[:64]:
            value=_point(observer.get('position')) if isinstance(observer,dict) else None
            if (value is not None and (observer.get('seated') or observer.get('stationary'))
                    and math.hypot(value[0]-position[0],value[2]-position[2])<=1.25
                    and abs(value[1]-position[1])<=.65):
                witnesses.append(observer.get('slot'))
        if gap is not None and gap<.20 and not witnesses:return None
        candidates=[]
        for prop in props[:512]:
            if not isinstance(prop,dict) or prop.get('id') in loaded:continue
            origin=_point(prop.get('position'))
            scale=prop.get('scale',1.)
            if origin is None or type(scale) not in (int,float) or not math.isfinite(scale):continue
            radius=min(3.,max(1.,2.*abs(scale)))
            distance=math.hypot(position[0]-origin[0],position[2]-origin[2])
            if distance>radius or abs(position[1]-origin[1])>min(5.,max(2.,3.*abs(scale))):continue
            if not isinstance(prop.get('id'),str) or not isinstance(prop.get('name'),str):continue
            candidates.append((distance,prop['id'],prop))
        if not candidates:return None
        _,_,prop=min(candidates,key=lambda row:(row[0],row[1]))
        return dict(prop_id=prop['id'],name=prop['name'],position=list(position),
            static_support_y=floor,height_above_static=gap,witness_slots=witnesses,
            evidence=('fresh_seated_or_stationary_actor' if witnesses else
                      'self_above_static_near_unmodeled_prop'),
            physical_support_verified=False,creates_collision=False)
    except (ValueError,TypeError,AttributeError,ImportError):
        return None


def plan_prop_transfer(terrain, start, original):
    """仅把真实动态平台间的短程空中路线改为完整扫掠验证的起跳。"""
    if not isinstance(original,dict) or original.get('kind') not in ('jump','drop','fly'):
        return original
    start=_point(start);goal=_point(original.get('landing'))
    if start is None or goal is None:return original
    try:
        geometry=_Geometry(terrain,RecoveryPolicy())
        ranges=getattr(geometry.surface,'collider_ranges',())
        if not ranges:return original
        def dynamic_support(point):
            return any(abs(y-point[1])<=.08 and ny>=math.cos(math.radians(42))
                and any(r['start']<=face<r['start']+r['count'] for r in ranges)
                for face,y,ny in terrain.hits(point[0],point[2]))
        supported=(dynamic_support(start),dynamic_support(goal))
        horizontal=math.hypot(goal[0]-start[0],goal[2]-start[2])
        if not any(supported) or horizontal>3. or abs(goal[1]-start[1])>2.5:return original
        if not geometry.standable(start) or not geometry.standable(goal):return original
        for duration in (.65,.85,1.05):
            try:
                phases,cruise=_jump(start,goal,minimum_duration=duration)
            except (ValueError,air_planner.PlanError):continue
            if not geometry.trajectory_clear(phases)[0]:continue
            # 重复经过平台入口仍完整重验几何；仅保留最初 drop/fly 分类，不跳过验证。
            ancestry=original.get('prop_platform_transfer')
            ancestry=ancestry if isinstance(ancestry,dict) else {}
            result=dict(original,kind='jump',reason='prop_platform_transfer',start=start,
                goal=goal,takeoff=start,landing=goal,walk_distance=0.,flight_distance=horizontal,
                cruise_height=cruise,phases=phases,waypoints=air_planner._waypoints(phases),
                prop_platform_transfer=dict(original_kind=ancestry.get('original_kind',original['kind']),
                    original_reason=ancestry.get('original_reason',original.get('reason')),
                    dynamic_start=supported[0],dynamic_goal=supported[1],
                    full_collision=True,exempt_prop_ids=[],max_horizontal=3.,max_height_delta=2.5))
            return result
        # 不把已知穿过平台侧面的原drop作为失败后的备用路径。
        if not geometry.trajectory_clear(original.get('phases',[]))[0]:
            return dict(original,kind='none',reason='prop_platform_no_clear_transfer',phases=[],waypoints=[])
        return original
    except (ValueError,TypeError,AttributeError,ImportError):
        return original


def dynamic_support_valid(terrain, position):
    """真实道具支撑有效时，缺导航多边形不是脚下失效。"""
    position=_point(position)
    if position is None or terrain is None:return False
    try:
        geometry=_Geometry(terrain,RecoveryPolicy())
        ranges=getattr(geometry.surface,'collider_ranges',())
        if not ranges:return False
        return (any(abs(y-position[1])<=.08 and ny>=math.cos(math.radians(42))
            and any(r['start']<=face<r['start']+r['count'] for r in ranges)
            for face,y,ny in terrain.hits(position[0],position[2]))
            and geometry.standable(position))
    except (ValueError,TypeError,AttributeError,ImportError):
        return False


def prop_platform_frame(terrain, air, previous, elapsed, state=None):
    """悬空平台短跳每帧全碰撞复核；没有任何道具豁免。"""
    begin=float((state or {}).get('elapsed',0.));end=min(float(elapsed),float(air.seconds))
    if not math.isfinite(end) or end<begin-1e-8 or end-begin>2.56:
        return None,'prop_platform_frame_time_budget'
    if math.dist(previous,air.trajectory.pos(begin))>1e-5:
        return None,'prop_platform_position_changed'
    geometry=_Geometry(terrain,RecoveryPolicy())
    point=tuple(previous);t=begin
    if geometry.intersecting(point):return None,'prop_platform_dynamic_collision'
    while t<end-1e-9:
        t=min(end,t+.01);nxt=air.trajectory.pos(t)
        if geometry.intersecting(point,nxt):return None,'prop_platform_dynamic_collision'
        point=nxt
    if end>=air.seconds-1e-8 and not geometry.standable(point):
        return None,'prop_platform_landing_changed'
    return dict(elapsed=end),None


def plan_prop_seat_approach(terrain,start,seat_origin,prop_id,*,interaction_radius=1.25):
    """绑定道具的短程跳靠近：终点全支撑，沿途只允许穿这件家具。"""
    start,seat=_point(start),_point(seat_origin)
    none=dict(kind='none',reason='prop_seat_approach_no_landing',phases=[],waypoints=[])
    if (start is None or seat is None or not isinstance(prop_id,str)
            or not .25<=interaction_radius<=1.25 or math.dist(start,seat)>2.75):
        return dict(none,reason='prop_seat_approach_distance')
    try:
        geometry=_Geometry(terrain,RecoveryPolicy())
        rows=[r for r in getattr(geometry.surface,'collider_ranges',()) if r['id']==prop_id]
        if len(rows)>1:return dict(none,reason='prop_seat_approach_ambiguous')
        row=rows[0] if rows else None
        ignored=set(range(row['start'],row['start']+row['count'])) if row else set()
        if geometry.intersecting(start)-ignored:return dict(none,reason='prop_seat_approach_start_static_or_other')
        candidates=[]
        for ring in range(0,6):
            radius=interaction_radius*ring/5;count=1 if ring==0 else 16
            for i in range(count):
                angle=math.tau*i/count;x=seat[0]+radius*math.cos(angle);z=seat[2]+radius*math.sin(angle)
                for _,y,ny in terrain.hits(x,z):
                    goal=(x,y,z)
                    if (ny>=math.cos(math.radians(42)) and math.dist(goal,seat)<=interaction_radius
                            and .1<math.dist(start,goal)<=2.75):
                        candidates.append((math.dist(start,goal),goal))
        candidates.sort(key=lambda r:r[0])
        for _,goal in candidates[:96]:
            if not geometry.standable(goal):continue
            try:phases,cruise=_jump(start,goal)
            except (ValueError,air_planner.PlanError):continue
            trajectory=air_planner.Trajectory(phases)
            if trajectory.duration>1.05:continue
            previous=start;clear=True;t=0.
            while t<trajectory.duration-1e-9:
                t=min(trajectory.duration,t+.01);point=trajectory.pos(t)
                if (math.dist(start,point)>3. or geometry.intersecting(previous,point)-ignored):
                    clear=False;break
                previous=point
            if not clear:continue
            return dict(kind='jump',reason='prop_seat_approach',start=start,target=seat,goal=goal,
                takeoff=start,landing=goal,walk_distance=0.,flight_distance=math.hypot(goal[0]-start[0],goal[2]-start[2]),
                cruise_height=cruise,takeoff_tries=1,height_retries=0,phases=phases,waypoints=air_planner._waypoints(phases),
                prop_seat_approach=dict(prop_id=prop_id,geometry_sha256=row['geometry_sha256'] if row else None,
                    max_seconds=1.05,max_distance=3.,interaction_radius=interaction_radius,seat_origin=seat,
                    geometry_missing=row is None))
        return none
    except (ValueError,TypeError,AttributeError,ImportError):
        return dict(none,reason='prop_seat_approach_geometry_unavailable')


def prop_seat_approach_frame(terrain,air,previous,elapsed,state=None):
    proof=air.plan['prop_seat_approach'];geometry=_Geometry(terrain,RecoveryPolicy())
    rows=[r for r in getattr(geometry.surface,'collider_ranges',()) if r['id']==proof['prop_id']]
    if (len(rows)>1 or (None if not rows else rows[0]['geometry_sha256'])!=proof['geometry_sha256']):
        return None,'prop_seat_approach_instance_changed'
    ignored=set(range(rows[0]['start'],rows[0]['start']+rows[0]['count'])) if rows else set()
    begin=float((state or {}).get('elapsed',0.));end=min(float(elapsed),float(air.seconds))
    if not math.isfinite(end) or end<begin-1e-8 or end-begin>2.56 or end>proof['max_seconds']:
        return None,'prop_seat_approach_time_budget'
    if math.dist(previous,air.trajectory.pos(begin))>1e-5:return None,'prop_seat_approach_position_changed'
    point=tuple(previous);t=begin
    if geometry.intersecting(point)-ignored:return None,'prop_seat_approach_dynamic_collision'
    while t<end-1e-9:
        t=min(end,t+.01);nxt=air.trajectory.pos(t)
        if math.dist(air.a,nxt)>proof['max_distance'] or geometry.intersecting(point,nxt)-ignored:
            return None,'prop_seat_approach_dynamic_collision'
        point=nxt
    if end>=air.seconds-1e-8 and (not geometry.standable(point)
            or math.dist(point,proof['seat_origin'])>proof['interaction_radius']+1e-6):
        return None,'prop_seat_approach_landing_changed'
    return dict(elapsed=end),None


def nearest_supported_goal(terrain, target, *, radius=2.0, step=0.25, max_candidates=192):
    """仅选择新地面目的地，不起跳、不传送、不证明路径可达。

    候选来自已加载真实三角形（含道具），按到原目标的 3D 距离排序；
    保留完整脚印、坡度、头顶和身体碰撞检查，之后仍必须走原 worker 寻路。
    """
    target = _point(target)
    result = {"goal": None, "reason": "no_safe_goal_near_target", "tested": 0, "candidates": 0}
    if (target is None or terrain is None or not getattr(terrain, "has_hits", False)
            or type(max_candidates) is not int or not 1 <= max_candidates <= 256
            or not 0 < radius <= 3.0 or not 0.2 <= step <= 1.0):
        return dict(result, reason="invalid_goal_search_input")
    try:
        geometry = _Geometry(terrain, RecoveryPolicy())
        if geometry.standable(target):
            return dict(result, goal=target, reason="original_goal_now_supported", tested=1, candidates=1)
        candidates, seen = [], set()
        count = int(math.ceil(radius / step))
        for ix in range(-count, count + 1):
            for iz in range(-count, count + 1):
                dx, dz = ix * step, iz * step
                if math.hypot(dx, dz) > radius + 1e-8:
                    continue
                x, z = target[0] + dx, target[2] + dz
                for _tid, y, ny in terrain.hits(x, z):
                    if ny < math.cos(math.radians(42.0)) or abs(y - target[1]) > 2.0:
                        continue
                    point = (x, y, z)
                    key = tuple(round(v, 5) for v in point)
                    if key not in seen:
                        seen.add(key)
                        candidates.append((math.dist(point, target), point))
                        if len(candidates) > 4096:
                            return dict(result, reason="goal_candidate_budget", candidates=len(candidates))
        candidates.sort(key=lambda row: (row[0], row[1]))
        result["candidates"] = len(candidates)
        for _, point in candidates[:max_candidates]:
            result["tested"] += 1
            if geometry.standable(point):
                return dict(result, goal=point, reason="nearest_supported_goal")
        return result
    except (ValueError, TypeError, AttributeError, ImportError, OverflowError):
        return dict(result, reason="goal_geometry_unavailable")


def _jump(start, landing, *, minimum_duration=.65):
    horizontal = math.hypot(landing[0] - start[0], landing[2] - start[2])
    duration = max(minimum_duration, horizontal / 3.5)
    dy = landing[1] - start[1]
    vy0 = (dy + 0.5 * air_planner.GRAVITY * duration ** 2) / duration
    if vy0 < 1.0:  # Even stepping off a prop starts with an actual upward jump.
        duration = (1.0 + math.sqrt(1.0 - 2.0 * air_planner.GRAVITY * dy)) / air_planner.GRAVITY
        vy0 = 1.0
    phases = [{"phase": "jump", "mode": "ballistic", "duration": duration,
               "from": start, "to": landing, "vy0": vy0, "gravity": air_planner.GRAVITY}]
    air_planner._check_limits(phases, airborne=True, jump=True)
    return phases, start[1] + vy0 ** 2 / (2 * air_planner.GRAVITY)


def plan_recovery(terrain, start, target, *, policy=None, reject=None,
                  escape_prop_id=None,preferred_directions=None,escape_near_prop=False):
    """Nearest validated sampled landing and a continuous existing AirMotion plan.

    A ``none`` result is an attempted recovery too: the owner must consume its
    bounded attempt budget before calling, so bad geometry cannot busy-loop.
    """
    p = policy or RecoveryPolicy()
    start, target = _point(start), _point(target)
    none = {"kind": "none", "reason": "invalid_recovery_input", "start": start,
            "target": target, "phases": [], "waypoints": []}
    if start is None or target is None:
        return none
    if terrain is None or not getattr(terrain, "has_hits", False):
        return dict(none, reason="triangle_geometry_required")
    try:
        geometry = _Geometry(terrain, p)
    except (ValueError, AttributeError, ImportError) as error:
        return dict(none, reason=str(error))
    if not geometry.initial_exit(start)[0]:
        return dict(none, reason="unsafe_initial_overlap", landing_candidates=0, landing_tested=0)
    prop_exit = geometry._prop_exit_cache.get(start)
    prop_id=escape_prop_id or (prop_exit['prop_id'] if prop_exit else None)
    clearance=geometry.prop_clearance(prop_id) if prop_id else None
    if prop_id is not None and clearance is None:
        return dict(none,reason='prop_escape_instance_missing',landing_candidates=0,landing_tested=0)
    if clearance is None and escape_near_prop:
        nearby=geometry.containing_props(start)
        if len(nearby)>1:
            return dict(none,reason='prop_escape_ambiguous_instances',landing_candidates=0,landing_tested=0)
        if nearby:clearance=nearby[0]
    takeoff = start
    prefix = []
    tx, tz = target[0] - start[0], target[2] - start[2]
    angle0 = math.atan2(tz, tx)
    directions=[]
    if isinstance(preferred_directions,(list,tuple)):
        for value in preferred_directions[:8]:
            if not isinstance(value,(list,tuple)) or len(value) not in (2,3):continue
            x,z=value[0],value[-1]
            if (type(x) not in (float,int) or type(z) not in (float,int)
                    or not math.isfinite(x) or not math.isfinite(z)):continue
            norm=math.hypot(x,z)
            if norm>1e-8:directions.append((x/norm,z/norm))
    if directions:angle0=math.atan2(directions[0][1],directions[0][0])
    candidates, seen = [], set()
    # Dense local polar sampling. The target direction is first for exact ties;
    # distance still dominates, so this is not a shortcut to the distant goal.
    for ring in range(1, int(math.ceil(p.search_radius / p.search_step)) + 1):
        radius = min(p.search_radius, ring * p.search_step)
        angles = max(8, min(64, int(math.ceil(math.tau * radius / p.search_step))))
        for i in range(angles):
            angle = angle0 + math.tau * i / angles
            x, z = start[0] + radius * math.cos(angle), start[2] + radius * math.sin(angle)
            if clearance is not None and not _outside_prop_xz((x,start[1],z),clearance):continue
            for _tid, y, ny in terrain.hits(x, z):
                if ny < math.cos(math.radians(42.0)) or abs(y - start[1]) > p.max_height_delta:
                    continue
                point = (x, y, z)
                key = tuple(round(v, 5) for v in point)
                if key in seen or (callable(reject) and reject(point)):
                    continue
                seen.add(key)
                distance = math.dist(start, point)
                direction = (max((math.cos(angle)*d[0]+math.sin(angle)*d[1])*(1.-rank*.025)
                                 for rank,d in enumerate(directions)) if directions else math.cos(angle-angle0))
                candidates.append((distance, -direction, abs(y - start[1]), point))
    candidates.sort(key=lambda row: (round(row[0], 6), row[1], row[2]))
    tested, last_reason = 0, "no_supported_landing"
    for distance, _direction, dy, landing in candidates:
        if not geometry.standable(landing):
            continue
        tested += 1
        horizontal = math.hypot(landing[0] - start[0], landing[2] - start[2])
        options = []
        if horizontal <= p.jump_distance and dy <= p.jump_height_delta:
            for duration in ((.65,.85,1.05) if prop_exit is not None or start in geometry._static_capsule_starts else (.65,)):
                try:
                    phases, cruise = _jump(takeoff, landing,minimum_duration=duration)
                    options.append(("jump", phases, cruise))
                except (air_planner.PlanError, ValueError):
                    pass
        for extra in (0., 1., 2.):
            cruise = max(takeoff[1], landing[1]) + 1.0 + extra
            phases = prefix+air_planner._fly_phases(terrain, takeoff, takeoff, landing, cruise)
            try:
                air_planner._check_limits(phases, airborne=True)
            except air_planner.PlanError:
                continue
            options.append(("fly", phases, cruise))
        for kind, phases, cruise in options:
            clear, last_reason = geometry.trajectory_clear(phases)
            if not clear:
                continue
            return {"kind": kind, "reason": "stuck_recovery", "start": start,
                    "target": target, "goal": landing, "takeoff": takeoff, "landing": landing,
                    "cruise_height": cruise, "walk_distance": 0., "flight_distance": horizontal,
                    "takeoff_tries": 1, "height_retries": 0, "phases": phases,
                    "waypoints": air_planner._waypoints(phases), "recovery": True,
                    "landing_distance": distance, "landing_candidates": len(candidates),
                    "landing_tested": tested, "search_resolution": p.search_step,
                    "geometry_source": getattr(terrain, "source", "surface"),
                    **({'prop_exterior_landing':clearance} if clearance is not None else {}),
                    **({'static_overlap_escape':dict(geometry._static_exit_cache[start])}
                       if start in geometry._static_exit_cache else {}),
                    **({'prop_overlap_escape':dict(prop_exit)} if prop_exit is not None else {})}
        # Search latency remains bounded even in pathological enclosed meshes.
        if tested >= 48:
            last_reason = "recovery_trajectory_budget:" + str(last_reason)
            break
    return dict(none, reason=last_reason, landing_candidates=len(candidates), landing_tested=tested)
