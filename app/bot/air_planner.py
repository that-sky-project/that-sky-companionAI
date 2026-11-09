"""Decide walk / jump / drop / fly from real static-world geometry.

Input
-----
``terrain`` must provide ``height(x, z, near_y)`` and ``hits(x, z)`` in
Actor-origin coordinates (``work/bot/air_terrain.py`` builds one from the live
walking adapter).  ``hits`` returns ``[(triangle_id, y, |normal_y|), ...]``
sorted by y, exactly like ``skyterrain.ground.GroundSurface``.

Output
------
A plain dict.  ``kind`` is one of ``walk`` / ``jump`` / ``drop`` / ``fly`` /
``none``.  ``phases`` is a continuous, time-stamped trajectory (see
``Trajectory``); ``waypoints`` is the human-readable key-point list used in
logs/tests.  There is no hand-tuned height offset anywhere in this module.

Rules implemented (TASK1-B):
1. Terrain is drivable/fliable; no geometry => no air move.
2. Takeoff must be >=1.5 m from the target and not against a wall.  A wall is
   a surrounding 0.8 m sample whose ground differs by >0.8 m, or a surface in
   that column between y+0.3 and y+2.5.
3. Headroom at the takeoff column: no surface from y+0.2 to cruise+2.  The
   flight corridor is sampled every 0.5 m with three lines (centre, +/-0.4 m)
   and both bands (h-1.8..h-0.3 and h+0.3..h+1.8) must be empty.
4. If the start is blocked, search same-layer ground 2..12 m around it, pick
   the shortest total route; walk there first.  Retry up to +6 m cruise
   altitude.  Otherwise ``none`` with a reason.
5. Landing is behind/beside the target at the follow distance with 2 m
   headroom; fall back to the target's own ground/y.
6. Speeds: walk 3.5 m/s, horizontal flight <=6.5 m/s, climb <=3.5 m/s, fall
   <=12 m/s (gravity 18 m/s^2, terminal speed capped).  Every phase is timed,
   so two samples can never teleport.
"""
from __future__ import annotations

import math

GRAVITY = 18.0
WALK_SPEED = 3.5
FLY_SPEED = 6.5
CLIMB_SPEED = 3.5
MAX_FALL = 12.0
MAX_3D_SPEED = 13.7

TAKEOFF_TARGET_GAP = 1.5
WALL_PROBE = 0.8
WALL_HEIGHT_DIFF = 0.8
HEADROOM_LOW = 0.2
CRUISE_MARGIN = 1.5
ROUTE_STEP = 0.5
ROUTE_SIDE = 0.4
ROUTE_NEAR = 0.3
ROUTE_FAR = 1.8
LANDING_HEADROOM = 2.0
CANDIDATE_RADII = tuple(range(2, 13))
CANDIDATE_ANGLES = 16
HEIGHT_RETRIES = (0.0, 2.0, 4.0, 6.0)


class PlanError(ValueError):
    """Planner rejected a candidate; ``str(error)`` is the short reason."""


# --------------------------------------------------------------------------- #
# geometry helpers


def _finite(value):
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _xz_dist(a, b):
    return math.hypot(b[0] - a[0], b[2] - a[2])


def _dist(a, b):
    return math.sqrt((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2 + (b[2] - a[2]) ** 2)


def _height(terrain, x, z, near_y=None):
    try:
        value = terrain.height(x, z) if near_y is None else terrain.height(x, z, near_y)
    except Exception:
        return None
    if value is None or not _finite(value):
        return None
    return float(value)


def _hits(terrain, x, z):
    if not getattr(terrain, "has_hits", True):
        return []
    try:
        rows = terrain.hits(x, z)
    except Exception:
        return []
    out = []
    for row in rows:
        try:
            y = float(row[1])
        except (TypeError, ValueError, IndexError):
            continue
        if _finite(y):
            out.append(y)
    return out


def _has_face(terrain, x, z, y0, y1):
    """True when any surface hit lies in the closed [y0, y1] band."""
    if y1 < y0:
        y0, y1 = y1, y0
    for y in _hits(terrain, x, z):
        if y0 - 1e-6 <= y <= y1 + 1e-6:
            return True
    return False


def _column_open(terrain, x, z, y0, y1):
    return not _has_face(terrain, x, z, y0, y1)


def _wall_at(terrain, point):
    """Rule 2: surrounding 0.8 m columns and [y+0.3, y+2.5] faces."""
    x, y, z = point
    for i in range(8):
        a = math.tau * i / 8.0
        px, pz = x + WALL_PROBE * math.cos(a), z + WALL_PROBE * math.sin(a)
        h = _height(terrain, px, pz, y)
        if h is None or abs(h - y) > WALL_HEIGHT_DIFF:
            return True
        if _has_face(terrain, px, pz, y + 0.3, y + 2.5):
            return True
    return False


def _takeoff_ok(terrain, point, target, cruise):
    x, y, z = point
    if _xz_dist(point, target) < TAKEOFF_TARGET_GAP:
        return False
    if _wall_at(terrain, point):
        return False
    if not _column_open(terrain, x, z, y + HEADROOM_LOW, cruise + 2.0):
        return False
    return True


def _route_clear(terrain, takeoff, landing, cruise):
    """Rule 3 corridor: centre + +/-0.4 m, both bands, every 0.5 m."""
    if not getattr(terrain, "has_hits", True):
        return False
    length = _xz_dist(takeoff, landing)
    ux = (landing[0] - takeoff[0]) / length if length > 1e-9 else 0.0
    uz = (landing[2] - takeoff[2]) / length if length > 1e-9 else 0.0
    px, pz = -uz, ux
    count = max(1, int(math.ceil(length / ROUTE_STEP)))
    for i in range(count + 1):
        f = i / count
        x = takeoff[0] + (landing[0] - takeoff[0]) * f
        z = takeoff[2] + (landing[2] - takeoff[2]) * f
        for side in (-ROUTE_SIDE, 0.0, ROUTE_SIDE):
            sx, sz = x + px * side, z + pz * side
            if _has_face(terrain, sx, sz, cruise - ROUTE_FAR, cruise - ROUTE_NEAR):
                return False
            if _has_face(terrain, sx, sz, cruise + ROUTE_NEAR, cruise + ROUTE_FAR):
                return False
    # Descent at the landing column: 2 m headroom is rule 5; the part between
    # that headroom and cruise must be open too or the approach clips a roof.
    if not _column_open(terrain, landing[0], landing[2],
                        landing[1] + HEADROOM_LOW, cruise + 2.0):
        return False
    return True


def _front_edge(terrain, start, target):
    """Last standable point before a >1.2 m front drop toward the target."""
    length = _xz_dist(start, target)
    if length < 0.1 or not getattr(terrain, "has_hits", True):
        return None
    ux, uz = (target[0] - start[0]) / length, (target[2] - start[2]) / length
    prev = (start[0], _height(terrain, start[0], start[2], start[1]), start[2])
    if prev[1] is None:
        return None
    for step in range(1, 25):
        d = 0.25 * step
        x, z = start[0] + ux * d, start[2] + uz * d
        h = _height(terrain, x, z, start[1])
        if h is None or h < prev[1] - 1.2:
            return prev
        prev = (x, h, z)
    return None


def _landing(terrain, target, follow_distance, start):
    """Rule 5. Returns ((x, y, z), fallback_reason)."""
    dx, dz = start[0] - target[0], start[2] - target[2]
    length = math.hypot(dx, dz)
    if length < 1e-6:
        dx, dz, length = 0.0, 1.0, 1.0
    follow = max(0.0, float(follow_distance))
    x, z = target[0] + dx / length * follow, target[2] + dz / length * follow
    h = _height(terrain, x, z, target[1])
    if h is not None and _column_open(terrain, x, z, h + HEADROOM_LOW, h + LANDING_HEADROOM):
        return (x, h, z), None
    h = _height(terrain, target[0], target[2], target[1])
    if h is not None:
        return (target[0], h, target[2]), "target_foot"
    return (target[0], float(target[1]), target[2]), "target_y"


# --------------------------------------------------------------------------- #
# trajectory


class Trajectory:
    """Piecewise continuous 3D path; ``phases`` come straight from a plan.

    Phase modes:
      ``linear``    - constant velocity p0 -> p1 over ``duration``.
      ``ballistic`` - x/z linear, y = y0 + vy0*t - 0.5*gravity*t^2.
    """

    def __init__(self, phases):
        self.phases = list(phases)
        t = 0.0
        for phase in self.phases:
            phase["t0"] = t
            t += float(phase.get("duration", 0.0))
            phase["t1"] = t
        self.duration = t
        self._heading = None

    def phase_index(self, now):
        if not self.phases:
            return -1
        if now <= 0:
            return 0
        if now >= self.duration:
            return len(self.phases) - 1
        for i, phase in enumerate(self.phases):
            if now < phase["t1"]:
                return i
        return len(self.phases) - 1

    def phase_at(self, now):
        i = self.phase_index(now)
        return None if i < 0 else self.phases[i]

    def pos(self, now):
        if not self.phases:
            raise ValueError("empty_trajectory")
        phase = self.phase_at(now)
        a, b = phase["from"], phase["to"]
        local = now - phase["t0"]
        duration = float(phase["duration"])
        if duration <= 0 or local >= duration:
            return tuple(float(v) for v in b)
        local = max(0.0, local)
        u = local / duration
        if phase.get("mode") == "ballistic":
            y = float(a[1]) + float(phase["vy0"]) * local \
                - 0.5 * float(phase.get("gravity", GRAVITY)) * local * local
            return (a[0] + (b[0] - a[0]) * u, y, a[2] + (b[2] - a[2]) * u)
        return (a[0] + (b[0] - a[0]) * u,
                a[1] + (b[1] - a[1]) * u,
                a[2] + (b[2] - a[2]) * u)

    def velocity(self, now):
        phase = self.phase_at(now)
        a, b = phase["from"], phase["to"]
        duration = max(1e-9, float(phase["duration"]))
        local = max(0.0, min(duration, now - phase["t0"]))
        vx = (b[0] - a[0]) / duration
        vz = (b[2] - a[2]) / duration
        if phase.get("mode") == "ballistic":
            vy = float(phase["vy0"]) - float(phase.get("gravity", GRAVITY)) * local
            return (vx, vy, vz)
        return (vx, (b[1] - a[1]) / duration, vz)

    def heading(self, now):
        vx, _vy, vz = self.velocity(now)
        if math.hypot(vx, vz) > 1e-6:
            self._heading = math.degrees(math.atan2(vx, vz))
        return self._heading

    def sample(self, dt=1.0 / 30.0):
        if not self.phases:
            return
        t = 0.0
        while t < self.duration:
            yield t, self.pos(t)
            t += dt
        yield self.duration, self.pos(self.duration)


BODY_LOW, BODY_HIGH, BODY_SIDE = 0.15, 1.8, 0.35


def _trajectory_clear(terrain, phases, dt=0.05):
    """2026-11-07 实测：爬升是斜着飞向落点的，原来只查了起飞点正上方和巡航航线，
    斜爬升那段钻进了头顶的岩檐。这里把整条空中轨迹（walk 以外）逐点检查：
    身体高度带 [y+0.15, y+1.8] 内（中心 + 左右 0.35 m）不能有面，
    相邻两点之间也不能穿过任何面（落点地面本身除外）。"""
    traj = Trajectory([dict(p) for p in phases])
    prev = None
    for phase in traj.phases:
        if phase.get("phase") == "walk" or float(phase.get("duration", 0.0)) <= 0:
            prev = None
            continue
        t, t1 = phase["t0"], phase["t1"]
        while True:
            pos = traj.pos(min(t, t1))
            vx, _vy, vz = traj.velocity(min(t, t1))
            n = math.hypot(vx, vz)
            sx, sz = ((-vz / n) * BODY_SIDE, (vx / n) * BODY_SIDE) if n > 1e-6 else (0.0, 0.0)
            for ox, oz in ((0.0, 0.0), (sx, sz), (-sx, -sz)):
                if _has_face(terrain, pos[0] + ox, pos[2] + oz, pos[1] + BODY_LOW, pos[1] + BODY_HIGH):
                    return False, pos
            if prev is not None:
                lo, hi = min(prev[1], pos[1]) + 0.05, max(prev[1], pos[1]) - 0.05
                if hi > lo and _has_face(terrain, pos[0], pos[2], lo, hi):
                    return False, pos
            prev = pos
            if t >= t1:
                break
            t = min(t + dt, t1)
    return True, None


# --------------------------------------------------------------------------- #
# builders


def _walk_phases(terrain, start, end, speed=WALK_SPEED):
    length = _xz_dist(start, end)
    if length < 1e-6:
        return []
    count = max(1, int(math.ceil(length / 1.0)))
    points = [tuple(float(v) for v in start)]
    prev = points[0]
    for i in range(1, count + 1):
        f = i / count
        x = start[0] + (end[0] - start[0]) * f
        z = start[2] + (end[2] - start[2]) * f
        h = _height(terrain, x, z, prev[1])
        y = h if h is not None else prev[1] + (end[1] - start[1]) * (1.0 / count)
        points.append((x, y, z))
        prev = (x, y, z)
    points[-1] = tuple(float(v) for v in end)
    phases = []
    for a, b in zip(points, points[1:]):
        duration = max(1e-3, _dist(a, b) / speed)
        phases.append({"phase": "walk", "mode": "linear", "duration": duration,
                       "from": a, "to": b})
    return phases


def _waypoints(phases, extra=()):
    out = []
    t = 0.0
    if phases:
        out.append({"t": 0.0, "pos": tuple(phases[0]["from"]),
                    "phase": phases[0]["phase"]})
    for phase in phases:
        t += float(phase["duration"])
        out.append({"t": t, "pos": tuple(phase["to"]), "phase": phase["phase"]})
    for t, pos, phase in extra:
        out.append({"t": t, "pos": tuple(pos), "phase": phase})
    out.sort(key=lambda row: row["t"])
    return out


def _check_limits(phases, *, airborne=False, jump=False):
    for phase in phases:
        duration = float(phase["duration"])
        if not _finite(duration) or duration < 0:
            raise PlanError("phase_duration")
        a, b = phase["from"], phase["to"]
        if not all(_finite(v) for v in a + b):
            raise PlanError("phase_nonfinite")
        if duration <= 1e-9:
            continue
        h = _xz_dist(a, b) / duration
        if h > FLY_SPEED + 1e-6 and phase["phase"] != "walk":
            raise PlanError("horizontal_speed")
        if phase.get("mode") == "ballistic":
            vy0 = float(phase["vy0"])
            if vy0 > 0 and vy0 > (11.5 if jump else CLIMB_SPEED + 1e-6):
                raise PlanError("climb_speed")
            vy_end = vy0 - float(phase.get("gravity", GRAVITY)) * duration
            if vy_end < 0 and -vy_end > MAX_FALL + 1e-6:
                raise PlanError("fall_speed")
            if abs(vy0) > MAX_3D_SPEED + 1e-6:
                raise PlanError("jump_speed")
        else:
            v = _dist(a, b) / duration
            if v > MAX_3D_SPEED + 1e-6:
                raise PlanError("phase_speed")
            if airborne and abs(b[1] - a[1]) / duration > CLIMB_SPEED + 1e-6 \
                    and b[1] > a[1]:
                raise PlanError("climb_speed")


def _fly_phases(terrain, start, takeoff, landing, cruise):
    phases = _walk_phases(terrain, start, takeoff)
    dx, dz = landing[0] - takeoff[0], landing[2] - takeoff[2]
    total = math.hypot(dx, dz)
    if total > 1e-9:
        ux, uz = dx / total, dz / total
    else:
        ux, uz = 0.0, 1.0

    gain = max(0.0, cruise - takeoff[1])
    climb_time = max(0.45, gain / CLIMB_SPEED)
    takeoff_h = min(total * 0.25, 1.0 * climb_time)
    descent = max(0.0, cruise - landing[1])
    brake_time = max(0.6, descent / 3.0)
    brake_h = min(total * 0.35, 1.6 * brake_time)
    if takeoff_h + brake_h > total:
        scale = total / max(1e-9, takeoff_h + brake_h)
        takeoff_h *= scale
        brake_h *= scale
    cruise_h = max(0.0, total - takeoff_h - brake_h)

    p1 = (takeoff[0] + ux * takeoff_h, cruise, takeoff[2] + uz * takeoff_h)
    p2 = (landing[0] - ux * brake_h, cruise, landing[2] - uz * brake_h)
    p3 = (landing[0], landing[1] + 0.4, landing[2])

    phases.append({"phase": "climb", "mode": "linear", "duration": climb_time,
                   "from": tuple(float(v) for v in takeoff), "to": p1})
    if cruise_h > 1e-6:
        phases.append({"phase": "cruise", "mode": "linear",
                       "duration": max(0.05, cruise_h / FLY_SPEED),
                       "from": p1, "to": p2})
    else:
        p2 = p1
    phases.append({"phase": "brake", "mode": "linear", "duration": brake_time,
                   "from": p2, "to": p3})
    phases.append({"phase": "land", "mode": "linear", "duration": 0.3,
                   "from": p3, "to": tuple(float(v) for v in landing)})
    return phases


def _try_fly(terrain, start, target, landing, follow_distance, cruise):
    """One cruise altitude: candidate takeoffs sorted by total route length."""
    candidates = []
    base_h = _height(terrain, start[0], start[2], start[1])
    if base_h is None:
        raise PlanError("no_ground_at_start")
    candidates.append((0.0, (start[0], base_h, start[2])))
    for radius in CANDIDATE_RADII:
        for i in range(CANDIDATE_ANGLES):
            a = math.tau * i / CANDIDATE_ANGLES
            x, z = start[0] + radius * math.cos(a), start[2] + radius * math.sin(a)
            h = _height(terrain, x, z, start[1])
            if h is None or abs(h - start[1]) > 0.5:
                continue
            candidates.append((float(radius), (x, h, z)))

    scored = []
    for walk_len, point in candidates:
        if not _takeoff_ok(terrain, point, target, cruise):
            continue
        flight = _xz_dist(point, landing)
        scored.append((walk_len + flight, walk_len, point))
    scored.sort(key=lambda row: (row[0], row[1]))

    tries = 0
    for _score, walk_len, point in scored:
        tries += 1
        if not _route_clear(terrain, point, landing, cruise):
            continue
        phases = _fly_phases(terrain, start, point, landing, cruise)
        _check_limits(phases, airborne=True)
        if not _trajectory_clear(terrain, phases)[0]:
            continue                          # 斜爬升/下降段会撞到东西：换下一个起飞点
        plan = {
            "kind": "fly",
            "reason": "target_above" if target[1] > start[1] + 1.2 else "terrain_failed",
            "start": tuple(float(v) for v in start),
            "target": tuple(float(v) for v in target),
            "goal": tuple(float(v) for v in landing),
            "takeoff": tuple(float(v) for v in point),
            "landing": tuple(float(v) for v in landing),
            "cruise_height": float(cruise),
            "walk_distance": float(walk_len),
            "flight_distance": float(_xz_dist(point, landing)),
            "takeoff_tries": tries,
            "phases": phases,
            "waypoints": _waypoints(phases),
        }
        return plan
    raise PlanError("no_clear_takeoff" if not scored else "no_route_clearance")


def _plan_jump(terrain, start, target, target_ground):
    landing = (target[0], target_ground, target[2])
    horizontal = _xz_dist(start, landing)
    if horizontal > 4.0 + 1e-6:
        raise PlanError("jump_too_far")
    duration = max(0.55, horizontal / 4.0)
    dy = landing[1] - start[1]
    vy0 = (dy + 0.5 * GRAVITY * duration * duration) / duration
    if vy0 > 11.5:
        raise PlanError("jump_too_steep")
    if vy0 < -MAX_FALL:
        raise PlanError("jump_fall_speed")
    apex = start[1] + vy0 * vy0 / (2.0 * GRAVITY) if vy0 > 0 else start[1]
    if not _column_open(terrain, start[0], start[2], start[1] + HEADROOM_LOW, apex + 0.5):
        raise PlanError("jump_headroom")
    if not _column_open(terrain, landing[0], landing[2],
                        landing[1] + HEADROOM_LOW, landing[1] + 2.0):
        raise PlanError("landing_headroom")
    phase = {"phase": "jump", "mode": "ballistic", "duration": duration,
             "from": tuple(float(v) for v in start), "to": tuple(float(v) for v in landing),
             "vy0": vy0, "gravity": GRAVITY}
    _check_limits([phase], airborne=True, jump=True)
    if not _trajectory_clear(terrain, [phase])[0]:
        raise PlanError("jump_blocked")
    return {
        "kind": "jump",
        "reason": "small_step",
        "start": tuple(float(v) for v in start),
        "target": tuple(float(v) for v in target),
        "goal": tuple(float(v) for v in landing),
        "takeoff": tuple(float(v) for v in start),
        "landing": tuple(float(v) for v in landing),
        "cruise_height": float(apex),
        "walk_distance": 0.0,
        "flight_distance": float(horizontal),
        "takeoff_tries": 1,
        "phases": [phase],
        "waypoints": _waypoints([phase], ((duration * 0.5, (start[0] + (landing[0] - start[0]) * 0.5,
                                                             apex,
                                                             start[2] + (landing[2] - start[2]) * 0.5),
                                          "apex"),)),
    }


def _plan_drop(terrain, start, target, landing, edge):
    drop = edge[1] - landing[1]
    if drop <= 1.2:
        raise PlanError("not_a_cliff")
    horizontal = _xz_dist(edge, landing)
    if drop <= 4.0:
        fall_time = math.sqrt(2.0 * drop / GRAVITY)
        fall = [{"phase": "fall", "mode": "ballistic", "duration": fall_time,
                 "from": tuple(float(v) for v in edge),
                 "to": tuple(float(v) for v in landing), "vy0": 0.0, "gravity": GRAVITY}]
    else:
        t_accel = MAX_FALL / GRAVITY
        h_accel = 0.5 * GRAVITY * t_accel * t_accel
        t_const = (drop - h_accel) / MAX_FALL
        total_t = t_accel + t_const
        f1 = t_accel / total_t
        mid = (edge[0] + (landing[0] - edge[0]) * f1, edge[1] - h_accel,
               edge[2] + (landing[2] - edge[2]) * f1)
        fall = [
            {"phase": "fall", "mode": "ballistic", "duration": t_accel,
             "from": tuple(float(v) for v in edge), "to": mid,
             "vy0": 0.0, "gravity": GRAVITY},
            {"phase": "fall", "mode": "linear", "duration": t_const,
             "from": mid, "to": tuple(float(v) for v in landing)},
        ]
    total_time = sum(float(p["duration"]) for p in fall)
    speed = horizontal / total_time if total_time > 0 else 0.0
    if speed > FLY_SPEED + 1e-6:
        raise PlanError("drop_unreachable")
    # Constant horizontal speed over the whole fall: rewrite phase endpoints
    # so both halves share the required x/z velocity and stay continuous.
    if speed > 0 and horizontal > 1e-9:
        ux, uz = (landing[0] - edge[0]) / horizontal, (landing[2] - edge[2]) / horizontal
        for phase in fall:
            d = float(phase["duration"])
            phase["to"] = (phase["from"][0] + ux * speed * d, phase["to"][1],
                           phase["from"][2] + uz * speed * d)
        fall[-1]["to"] = tuple(float(v) for v in landing)
    # Recompute y endpoints for ballistic consistency.
    if fall[0]["mode"] == "ballistic":
        fall[0]["to"] = (fall[0]["to"][0], edge[1] - 0.5 * GRAVITY * fall[0]["duration"] ** 2,
                         fall[0]["to"][2])
        if len(fall) > 1:
            fall[1]["from"] = fall[0]["to"]
            fall[1]["to"] = (fall[-1]["to"][0], landing[1], fall[-1]["to"][2])
    if not _column_open(terrain, edge[0], edge[2], edge[1] + HEADROOM_LOW, edge[1] + 2.0):
        raise PlanError("drop_headroom")
    if not _column_open(terrain, landing[0], landing[2],
                        landing[1] + HEADROOM_LOW, landing[1] + 2.0):
        raise PlanError("landing_headroom")
    approach = _walk_phases(terrain, start, edge)
    phases = approach + fall
    _check_limits(phases, airborne=True)
    if not _trajectory_clear(terrain, phases)[0]:
        raise PlanError("drop_blocked")
    return {
        "kind": "drop",
        "reason": "front_cliff",
        "start": tuple(float(v) for v in start),
        "target": tuple(float(v) for v in target),
        "goal": tuple(float(v) for v in landing),
        "takeoff": tuple(float(v) for v in edge),
        "landing": tuple(float(v) for v in landing),
        "cruise_height": float(edge[1]),
        "walk_distance": float(_xz_dist(start, edge)),
        "flight_distance": float(horizontal),
        "takeoff_tries": 1,
        "phases": phases,
        "waypoints": _waypoints(phases),
    }


def _none(start, target, reason, **extra):
    out = {
        "kind": "none",
        "reason": reason,
        "start": tuple(float(v) for v in start),
        "target": tuple(float(v) for v in target),
        "goal": tuple(float(v) for v in target),
        "takeoff": None,
        "landing": None,
        "cruise_height": None,
        "walk_distance": None,
        "flight_distance": None,
        "takeoff_tries": 0,
        "phases": [],
        "waypoints": [],
    }
    out.update(extra)
    return out


def plan(terrain, start, target, follow_distance=1.2, *, terrain_failed_seconds=0.0,
         allow_fly=True):
    """Return the movement plan (see module docstring)."""
    start = tuple(float(v) for v in start)
    target = tuple(float(v) for v in target)
    if not all(_finite(v) for v in start + target):
        return _none(start, target, "nonfinite_position")
    if terrain is None:
        return _none(start, target, "no_terrain")
    if not getattr(terrain, "has_hits", True):
        # Height-only adapters can answer walk, never a proved air route.
        return _none(start, target, "no_geometry_hits")

    start_ground = _height(terrain, start[0], start[2], start[1])
    target_ground = _height(terrain, target[0], target[2], target[1])
    if start_ground is None:
        return _none(start, target, "no_ground_at_start")
    if target_ground is None:
        return _none(start, target, "no_ground_at_target")

    dy = target_ground - start_ground
    horizontal = _xz_dist(start, target)
    failed = float(terrain_failed_seconds or 0.0) >= 2.0 \
        and horizontal > float(follow_distance) + 2.0
    common = {"start_ground": start_ground, "target_ground": target_ground,
              "dy": dy, "horizontal": horizontal,
              "height_retries": 0, "landing_fallback": None}

    if allow_fly and dy > 1.2:
        want = "fly"
    elif 0.3 <= dy <= 1.2 and horizontal <= 4.0:
        want = "jump"
    elif dy < -1.2:
        want = "drop"
    elif allow_fly and failed:
        want = "fly"
    else:
        want = "walk"

    if want == "walk":
        out = _none(start, target, "walk")
        out.update(common)
        out["kind"] = "walk"
        return out

    if want == "jump":
        try:
            out = _plan_jump(terrain, start, target, target_ground)
        except PlanError as error:
            out = _none(start, target, str(error))
            out.update(common)
            return out
        out.update(common)
        out["takeoff_tries"] = 1
        return out

    drop_reason = None
    if want == "drop":
        # 2026-11-07 实测：哥哥跳下悬崖后站在 19 m 外，直接掉落够不着 →
        # 一直 drop_unreachable 原地不动。真人会跳下去再滑翔过去：
        # 直接掉落不行（或前方没崖、地面又走不通）就改走下面的飞行（滑翔下降）路线。
        edge = _front_edge(terrain, start, target)
        if edge is None:
            if not (allow_fly and failed):
                out = _none(start, target, "walk")
                out.update(common)
                out["kind"] = "walk"
                return out
            drop_reason = "no_front_edge"
        else:
            landing, fallback = _landing(terrain, target, follow_distance, start)
            if landing[1] > edge[1] - 1.2:
                out = _none(start, target, "walk")
                out.update(common)
                out["kind"] = "walk"
                return out
            try:
                out = _plan_drop(terrain, start, target, landing, edge)
            except PlanError as error:
                drop_reason = str(error)
                if not allow_fly:
                    out = _none(start, target, drop_reason)
                    out.update(common)
                    return out
            else:
                out.update(common)
                out["landing_fallback"] = fallback
                return out

    # fly
    landing, fallback = _landing(terrain, target, follow_distance, start)
    base_cruise = max(start_ground, landing[1]) + CRUISE_MARGIN
    last_reason = "no_clear_takeoff"
    for retry, extra in enumerate(HEIGHT_RETRIES):
        cruise = base_cruise + extra
        try:
            out = _try_fly(terrain, start, target, landing, follow_distance, cruise)
        except PlanError as error:
            last_reason = str(error)
            continue
        out.update(common)
        out["height_retries"] = retry
        out["landing_fallback"] = fallback
        out["base_cruise"] = base_cruise
        if drop_reason is not None:
            out["reason"] = "glide_down"          # 跳下去滑翔过去（直接掉落够不着）
            out["drop_reason"] = drop_reason
        return out
    out = _none(start, target, last_reason)
    out.update(common)
    out["height_retries"] = len(HEIGHT_RETRIES) - 1
    out["base_cruise"] = base_cruise
    return out


class AirPlanner:
    """Tiny stateful wrapper; keeps the last plan for logging/tests."""

    def __init__(self, terrain, **defaults):
        self.terrain = terrain
        self.defaults = dict(defaults)
        self.last = None

    def plan(self, start, target, follow_distance=1.2, **kwargs):
        options = dict(self.defaults)
        options.update(kwargs)
        self.last = plan(self.terrain, start, target, follow_distance, **options)
        return self.last


STEP_MIN, STEP_MAX, STEP_LOOK, STEP_BEYOND = 0.25, 1.2, 4.0, 0.6


def plan_step_jump(terrain, start, target, follow_distance=1.2):
    """2026-11-07 哥哥要求：地上跟随遇到高低石块，地面寻路要停一下再走 → 直接跳上/跳下。
    朝目标方向每 0.25 m 看一次同层地面（near_y 沿路接力），找到第一处 0.25–1.2 m 的高差，
    落点取台阶后 0.6 m（不超过跟随站位）；落点地面要和台阶顶连续。没有台阶返回 None。"""
    start = tuple(float(v) for v in start)
    target = tuple(float(v) for v in target)
    if terrain is None or not getattr(terrain, "has_hits", True):
        return None
    total = _xz_dist(start, target)
    reach = min(STEP_LOOK, total - float(follow_distance) + 0.3)
    if reach < 0.5:
        return None
    ux, uz = (target[0] - start[0]) / total, (target[2] - start[2]) / total
    prev = _height(terrain, start[0], start[2], start[1])
    if prev is None or abs(prev - start[1]) > 0.4:
        return None
    base = prev
    d = 0.25
    while d <= reach + 1e-6:
        x, z = start[0] + ux * d, start[2] + uz * d
        h = _height(terrain, x, z, prev)
        if h is None:
            return None
        if abs(h - prev) >= STEP_MIN:
            if abs(h - base) > STEP_MAX:
                return None                    # 太高/太深：交给飞或跳下
            land_d = min(d + STEP_BEYOND, reach)
            lx, lz = start[0] + ux * land_d, start[2] + uz * land_d
            lh = _height(terrain, lx, lz, h)
            if lh is None or abs(lh - h) > 0.35 or abs(lh - base) < STEP_MIN:
                return None
            try:
                out = _plan_jump(terrain, start, (lx, lh, lz), lh)
            except PlanError:
                return None
            out["reason"] = "stone_step"
            out["target"] = target
            return out
        prev = h
        d += 0.25
    return None


def plan_stall_hop(terrain, start, spot, target):
    """T23-5：地面寻路卡住、前方又没有明确台阶时，离跟随站位 <=4 m 就直接小跳过去（障碍物、石头缝、斜坡边）。
    落点用站位的真实地面（near 哥哥脚下高度），高差 <=1.2 m；轨迹同样要过整条净空检查。"""
    start = tuple(float(v) for v in start)
    spot = tuple(float(v) for v in spot)
    target = tuple(float(v) for v in target)
    if terrain is None or not getattr(terrain, "has_hits", True):
        return None
    if _xz_dist(start, spot) > 4.0 or _xz_dist(start, spot) < 0.5:
        return None
    g = _height(terrain, spot[0], spot[2], target[1])
    base = _height(terrain, start[0], start[2], start[1])
    if g is None or base is None or abs(g - base) > STEP_MAX:
        return None
    try:
        out = _plan_jump(terrain, start, (spot[0], g, spot[2]), g)
    except PlanError:
        return None
    out["reason"] = "stall_hop"
    out["target"] = target
    return out


def plan_from_air(terrain, pos, target, follow_distance=1.2):
    """2026-11-07 实测：飞行中哥哥一挪动就取消→半空落地→重新起飞，飞一段落一段。
    改为在空中直接改航线：从当前空中位置（已经离地，不需要起飞点）飞向哥哥新位置。
    落点用哥哥脚下的真实高度校验（他站的地方一定是真实地面）。失败返回 None。"""
    pos = tuple(float(v) for v in pos)
    target = tuple(float(v) for v in target)
    if terrain is None or not getattr(terrain, "has_hits", True):
        return None
    target_ground = _height(terrain, target[0], target[2], target[1])
    if target_ground is None or abs(target_ground - target[1]) > 1.0:
        target_ground = target[1]
    landing, fallback = _landing(terrain, target, follow_distance, pos)
    if abs(landing[1] - target_ground) > 1.0:
        landing, fallback = (target[0], target_ground, target[2]), "target_foot"
    for retry, extra in enumerate((0.0, 2.0, 4.0)):
        cruise = max(pos[1], landing[1] + CRUISE_MARGIN) + extra
        try:
            if not _route_clear(terrain, pos, landing, cruise):
                continue
            phases = _fly_phases(terrain, pos, pos, landing, cruise)
            _check_limits(phases, airborne=True)
        except PlanError:
            continue
        if not _trajectory_clear(terrain, phases)[0]:
            continue
        return {
            "kind": "fly", "reason": "retarget",
            "start": pos, "target": target, "goal": tuple(float(v) for v in landing),
            "takeoff": pos, "landing": tuple(float(v) for v in landing),
            "cruise_height": float(cruise), "walk_distance": 0.0,
            "flight_distance": float(_xz_dist(pos, landing)), "takeoff_tries": 0,
            "height_retries": retry, "landing_fallback": fallback,
            "phases": phases, "waypoints": _waypoints(phases), "airborne_start": True,
            "ground_ref": (target[0], float(target_ground), target[2]),
        }
    return None


def describe(result):
    """Short log-ready view: kind/reason plus rounded geometry."""
    def r(value):
        return None if value is None else [round(float(v), 2) for v in value]
    return {
        "kind": result.get("kind"), "reason": result.get("reason"),
        "start": r(result.get("start")), "takeoff": r(result.get("takeoff")),
        "landing": r(result.get("landing")),
        "cruise": None if result.get("cruise_height") is None else round(result["cruise_height"], 2),
        "retries": result.get("height_retries"), "tries": result.get("takeoff_tries"),
    }

