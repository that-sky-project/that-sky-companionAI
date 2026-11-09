"""AirMotion: a timed 3D trajectory plus real-capture air clip rendering.

Same public surface as ``anim_pose.Motion`` (``a b t0 seconds speed heading
final_heading velocity pos(now) arrived(now)``) and one extra method
``render(body, now) -> body``.

Rendering rules:
* pose / animation key / generation / start / mode / code90 come from the
  recorded real-player clips in ``air_clips.json``;
* both pose blocks are rotated to the current heading;
* the speed code is derived from the *actual* trajectory velocity (so it can
  never claim a speed the Actor is not moving at);
* the air-state tokens recorded with the clip are written by ``label|occurrence``
  (never by global token index);
* a new animation event bumps generation and pins ``start`` to the real Actor
  clock, and writes the previous event into the history block;
* after landing the body goes back to the existing standing recipe and all
  air flags are cleared;
* ``cancel()`` replaces the remaining route with a brake + real-ground descent
  from the *current* position.  It never hovers and never snaps back.
"""
from __future__ import annotations

import json
import math
import struct
import time
from dataclasses import replace
from pathlib import Path

import anim_pose

try:  # social_pose is part of the release bot tree; keep this testable alone.
    import social_pose
except Exception:  # pragma: no cover - only for isolated unit import
    social_pose = None

import air_planner

CLIP_FILE = Path(__file__).with_name("air_clips.json")
FLOAT_LABELS = {"0x21dcf64", "0x21dcf94"}
VECTOR_LABELS = {"0x1715a60", "0x1715a88"}

# Ground token values observed on stand/walk frames (also in social_pose).
LANDED_AIR = {
    "0x1f80044|1": 0,
    "0x1f80070|1": 0,
    "0x21dcc10|1": 0,
    "0x21dd4cc|1": 0,
    "0x21dd4ec|1": 0,
    "0x21dd044|1": 0,
    "0x21dcfb4|1": 1,
    "0x21dd1e0|1": 0,
    "0x21dd170|1": 16383,
    "0x21dd190|1": 127,
    "0x1715a60|1": 7,
    "0x1715a88|1": 7,
    "0x21dcf54|1": 0,
    "0x21dcf64|1": 0.0,
    "0x21dcf84|1": 0,
    "0x21dcf94|1": 0.0,
    "0x21dee4c|1": 488,
}
WALK_AIR = dict(LANDED_AIR, **{
    "0x21dcc10|1": 1,
    "0x21dd4cc|1": 31,
    "0x1715a60|1": 0,
    "0x1715a88|1": 0,
})


def _heading_to(a, b):
    return math.degrees(math.atan2(b[0] - a[0], b[2] - a[2]))


def _clamp(value, low, high):
    return max(low, min(high, value))


def _token_index(o):
    """{(label, token_occurrence): [token index]} in packet order.

    Occurrence counts *tokens*, not schema boundaries.  This is what
    ``air_keys`` uses: a scalar token has occurrence 1, the two components of
    ``0x1715a60`` have occurrences 1 and 2.
    """
    starts = anim_pose._starts(o)
    bounds = sorted(o._boundaries, key=lambda row: row[1])
    out, j, cur, occ = {}, 0, None, {}
    for i, bit in enumerate(starts):
        while j < len(bounds) and bounds[j][1] <= bit:
            cur = bounds[j][0]
            j += 1
        if cur is not None:
            occ[cur] = occ.get(cur, 0) + 1
            out[(cur, occ[cur])] = [i]
    return out, starts


def write_air(body, values):
    """Patch air-state tokens addressed as ``label|occurrence``.

    Values are ints for bit/raw integer tokens and floats for raw-4 floats.
    Missing labels are skipped (different Actor shapes are tolerated).
    """
    if not values:
        return body
    o = anim_pose.inspect_actor_wire(body, verified_profile_sha256=anim_pose.SHA,
                                     check_zero_padding=False)
    index, starts = _token_index(o)
    patched = bytearray(body)
    for key, value in values.items():
        try:
            label, occurrence = key.split("|")
            ids = index.get((label, int(occurrence)))
        except (ValueError, AttributeError):
            continue
        if not ids:
            continue
        targets = [ids[0]]
        if label in VECTOR_LABELS:
            # Two-component bit groups: clips store the group value once.
            nxt = index.get((label, int(occurrence) + 1))
            if nxt:
                targets.append(nxt[0])
        # Some labels are multi-component quant groups (e.g. 0x1715a60 /
        # 0x1715a88 have two 3-bit components).  The clip stores the group
        # value once; apply it to every component of that label occurrence.
        for token_id in targets:
            token = o._tokens[token_id]
            if token.kind == "raw" and token.extent == 4 and isinstance(value, float):
                raw = struct.pack("<f", float(value))
                for k, byte in enumerate(raw):
                    bit = starts[token_id] + 8 * k
                    patched[bit // 8] = byte
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                ivalue = int(value)
                width = token.extent * 8 if token.kind == "raw" else token.extent
                if not 0 <= ivalue < (1 << width):
                    continue
                if token.kind == "raw":
                    patched[starts[token_id] // 8:starts[token_id] // 8 + token.extent] = \
                        ivalue.to_bytes(token.extent, "little")
                else:
                    for shift in range(token.extent):
                        bit = starts[token_id] + shift
                        byte, offset = divmod(bit, 8)
                        mask = 1 << offset
                        patched[byte] = (patched[byte] & ~mask) | (((ivalue >> shift) & 1) << offset)
    return bytes(patched)


class AirClips:
    """Loads and indexes ``air_clips.json``; safe with a missing file."""

    def __init__(self, path=CLIP_FILE):
        self.path = Path(path)
        self.pose_keys = []
        self.air_keys = []
        self.clips = {}
        self.ok = False
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
            self.pose_keys = list(doc.get("pose_keys") or [])
            self.air_keys = list(doc.get("air_keys") or [])
            for kind, rows in (doc.get("clips") or {}).items():
                self.clips[kind] = [row for row in rows if row.get("frames")]
            self.ok = bool(self.clips)
        except (OSError, ValueError, TypeError):
            self.ok = False

    def pick(self, kind, index=0):
        rows = self.clips.get(kind) or []
        if not rows:
            rows = self.clips.get("cruise_flap") or []
        if not rows:
            return None
        return rows[index % len(rows)]


class _ClipCursor:
    """Last-frame lookup for a clip at a local time."""

    def __init__(self, clip):
        self.clip = clip
        self.frames = clip["frames"]
        self.duration = max(0.05, float(clip.get("duration") or self.frames[-1]["t"] or 0.05))

    def at(self, local, loop=False):
        if local < 0:
            local = 0.0
        if loop:
            local = local % self.duration
        else:
            local = min(local, self.duration)
        # Frames are ordered by t; choose the newest frame at/before local.
        lo, hi = 0, len(self.frames) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.frames[mid]["t"] <= local + 1e-9:
                lo = mid
            else:
                hi = mid - 1
        frame = dict(self.frames[lo])
        if loop and local >= self.duration - 1e-9:
            frame = dict(self.frames[-1])
        frame["_clip_heading"] = float(self.clip.get("heading", 0.0))
        frame["_clip_id"] = self.clip.get("id")
        return frame


class AirMotion:
    """One planned jump/drop/fly. ``render`` is called from PoseController._post."""

    air_motion = True

    def __init__(self, plan, started=None, *, clips=None, walk_clip=None, terrain=None,
                 log=None, kind=None):
        self.plan = dict(plan)
        self.kind = kind or self.plan.get("kind", "fly")
        self.a = tuple(float(v) for v in self.plan["start"])
        self.b = tuple(float(v) for v in self.plan["goal"])
        self.t0 = float(time.monotonic() if started is None else started)
        self.trajectory = air_planner.Trajectory(self.plan["phases"])
        self.seconds = max(0.05, self.trajectory.duration)
        self.speed = air_planner.WALK_SPEED
        self.heading = self.trajectory.heading(0.0)
        if self.heading is None:
            self.heading = _heading_to(self.a, self.plan.get("target", self.b))
        self.final_heading = _heading_to(self.b, self.plan.get("target", self.b))
        if abs(self.final_heading) < 1e-9:
            self.final_heading = self.heading
        self.velocity = self.trajectory.velocity(0.0)
        self.log = log
        self.terrain = terrain
        self.clips = clips if clips is not None else AirClips()
        self.walk_clip = walk_clip
        self._cursors = {}
        self._anim = None           # {"key","gen","start"}
        self._prev_stamp = None
        self._last_clip_frame = None
        self._landed = False
        self._landed_at = None
        self.cancelled = False
        self.cancel_reason = None
        self._cancel_phases = None

    # ------------------------------------------------------------------ basics
    def _local(self, now):
        return max(0.0, float(now) - self.t0)

    def pos(self, now):
        if self._landed:
            return self.b
        local = min(self._local(now), self.seconds)
        return self.trajectory.pos(local)

    def arrived(self, now):
        return self._landed or self._local(now) >= self.seconds - 1e-9

    def phase(self, now):
        if self._landed:
            return "landed"
        if self.cancelled:
            phase = self.trajectory.phase_at(min(self._local(now), self.seconds))
            return "cancel_" + (phase or {}).get("phase", "fall")
        phase = self.trajectory.phase_at(min(self._local(now), self.seconds))
        return (phase or {}).get("phase", "cruise")

    def _log(self, event, **fields):
        if callable(self.log):
            try:
                self.log(event, **fields)
            except Exception:
                pass

    def _cursor(self, kind):
        clip = self.clips.pick(kind)
        if clip is None:
            return None
        key = id(clip)
        if key not in self._cursors:
            self._cursors[key] = _ClipCursor(clip)
        return self._cursors[key]

    # --------------------------------------------------------------- rendering
    def _frame_for(self, now):
        local = self._local(now)
        phase = self.trajectory.phase_at(min(local, self.seconds))
        name = (phase or {}).get("phase")
        # Time inside this phase, used to align the clip.
        in_phase = local - float((phase or {}).get("t0", 0.0))
        if self.cancelled:
            return self._cursor("brake_land"), in_phase, True, "brake_land"
        if name == "walk":
            return None, in_phase, False, "walk"
        if name in ("jump",):
            return self._cursor("jump"), in_phase, False, "jump"
        if name in ("fall",):
            return self._cursor("drop"), in_phase, False, "drop"
        if name in ("climb",):
            return self._cursor("takeoff"), in_phase, False, "takeoff"
        if name in ("brake", "land"):
            return self._cursor("brake_land"), in_phase, False, "brake_land"
        # cruise & anything else
        return self._cursor("cruise_flap"), in_phase, True, "cruise_flap"

    def _write_anim(self, body, frame):
        fields = anim_pose.read(body)
        key = int(frame["key"])
        frame_gen = int(frame.get("gen", 0))
        mode = int(frame.get("mode", 0))
        code90 = frame.get("code90")
        clock = fields.get("clock")
        if clock is None:
            clock = int(frame.get("clock", 0))
        first = self._anim is None
        new_event = first
        if not first:
            new_event = (key != self._anim["key"]
                         or (self._last_clip_frame is not None
                             and frame_gen != int(self._last_clip_frame.get("gen", frame_gen))))
        if new_event:
            previous = self._anim
            if previous is None:
                previous = {"key": fields["key"], "gen": fields["gen"], "start": fields["start"]}
            self._anim = {"key": key, "gen": (int(previous["gen"]) + 1) & 0xFFFFFFFF,
                          "start": int(clock) & 0xFFF}
            self._prev_stamp = previous
        values = dict(gen=self._anim["gen"], key=self._anim["key"], start=self._anim["start"],
                      mode=mode, h_gen=int(self._prev_stamp["gen"]),
                      h_key=int(self._prev_stamp["key"]),
                      h_start=(int(self._prev_stamp["start"]) >> 3) & 0x1FF)
        if code90 is not None:
            values["code90"] = int(code90)
        self._last_clip_frame = frame
        return anim_pose.write(body, **values)

    def _write_pose(self, body, frame, heading):
        if not self.clips.pose_keys:
            return body
        raw = frame.get("pose") or []
        if len(raw) != len(self.clips.pose_keys) * 3:
            return body
        values = {key: list(raw[i * 3:i * 3 + 3])
                  for i, key in enumerate(self.clips.pose_keys)}
        delta = heading - float(frame.get("_clip_heading", 0.0))
        values = anim_pose.rotate_pose(values, delta)
        return anim_pose.write_pose_fields(body, values)

    def _air_values(self, frame):
        keys = self.clips.air_keys
        raw = frame.get("air") or []
        if not keys or len(raw) != len(keys):
            return {}
        return {key: value for key, value in zip(keys, raw)}

    def _walk_pose(self, body, heading, now):
        clip = self.walk_clip
        if clip is None:
            return body, False
        values = anim_pose.rotate_pose(clip.at(self._local(now)), heading - clip.heading)
        return anim_pose.write_pose_fields(body, values), True

    def _stand(self, body):
        """Existing standing recipe + explicit end of the air animation."""
        if social_pose is not None:
            try:
                body = social_pose.stand(body)
            except Exception:
                pass
        fields = anim_pose.read(body)
        if int(fields["key"]) != 0 or int(fields["mode"]) != 0:
            body = anim_pose.write(
                body, key=0, gen=(int(fields["gen"]) + 1) & 0xFFFFFFFF,
                start=int(fields.get("clock", 0)) & 0xFFF, mode=0, code90=7,
                h_gen=int(fields["gen"]), h_key=int(fields["key"]),
                h_start=(int(fields["start"]) >> 3) & 0x1FF)
        body = write_air(body, LANDED_AIR)
        return anim_pose.write_velocity(body, (0.0, 0.0, 0.0))

    def render(self, body, now):
        """Apply clip pose/animation and state tokens for ``now``."""
        heading = self.trajectory.heading(self._local(now))
        if heading is None:
            heading = self.heading
        self.heading = heading

        if self.arrived(now):
            if not self._landed:
                self._landed = True
                self._landed_at = float(now)
                self.b = tuple(self.pos(now))
            body = self._stand(body)
            body = anim_pose.write_velocity(body, (0.0, 0.0, 0.0))
            if self.final_heading is not None:
                body = anim_pose.write_facing(body, self.final_heading)
            return body

        cursor, local, loop, label = self._frame_for(now)
        if label == "walk" or cursor is None:
            body, posed = self._walk_pose(body, heading, now)
            if not posed:
                # No walk clip: keep stand pose, just face/velocity.
                body = anim_pose.write_facing(body, heading)
            body = write_air(body, WALK_AIR)
            body = anim_pose.write_velocity(body, self.trajectory.velocity(self._local(now)))
            return body

        frame = cursor.at(local, loop=loop)
        body = self._write_pose(body, frame, heading)
        body = self._write_anim(body, frame)
        body = write_air(body, self._air_values(frame))
        body = anim_pose.write_velocity(body, self.trajectory.velocity(self._local(now)))
        body = anim_pose.write_facing(body, heading)
        return body

    # ---------------------------------------------------------------- cancel
    def _ground(self, pos):
        if self.terrain is None:
            return None
        try:
            from air_terrain import ground_below
            return ground_below(self.terrain, pos[0], pos[2], pos[1])
        except Exception:
            try:
                return self.terrain.height(pos[0], pos[2], pos[1])
            except Exception:
                return None

    def cancel(self, now=None, terrain=None, reason="interrupt", prefer=None):
        """Abort the plan; if airborne, brake + fall to real ground below.

        Returns True when a descent was installed (or already landed).  The
        descent starts exactly at the current rendered position, so this is
        continuous from the next frame on.
        """
        if self._landed:
            return True
        if self.cancelled:
            # 2026-11-07 实测：每秒重复 cancel 会从当前位置重建刹停下落，永远落不了地。
            # 已经在刹停/下落就让它落完。
            return True
        now = float(time.monotonic() if now is None else now)
        if terrain is not None:
            self.terrain = terrain
        pos = self.pos(now)
        ground = self._ground(pos)
        # 2026-11-07 实测：碰撞数据在某处缺了真实地面，往下找到的是 5 m 下的地层 → 落进地底。
        # 有参考高度（起飞点/哥哥脚下）且找到的地面比它低 1.5 m 以上：改用参考那一层附近的地面，
        # 那一层查不到就落在参考高度（那是小光/哥哥刚站过的真实地面）。
        # prefer 是地面上的参考点 (x, y, z)，只在离它水平 3 m 内才用它的高度兜底。
        prefer_y = None
        if prefer is not None and math.hypot(pos[0] - prefer[0], pos[2] - prefer[2]) <= 3.0:
            prefer_y = float(prefer[1])
        if prefer_y is not None and prefer_y <= pos[1] + 0.5 and (ground is None or ground < prefer_y - 1.5):
            near = None
            try:
                near = self.terrain.height(pos[0], pos[2], prefer_y) if self.terrain is not None else None
            except Exception:
                near = None
            ground = near if near is not None and abs(near - prefer_y) <= 1.0 else prefer_y
        if ground is None:
            self.cancel_reason = "no_ground_below"
            self._log("air_cancel", reason=self.cancel_reason,
                      at=[round(v, 2) for v in pos])
            return False
        if ground >= pos[1] - 1e-3:
            self.b = (pos[0], ground, pos[2])
            self.seconds = max(0.0, now - self.t0)
            self._landed = True
            self._landed_at = now
            self.cancelled = True
            self.cancel_reason = reason
            self._log("air_cancel", reason=reason, landed=True,
                      at=[round(v, 2) for v in self.b])
            return True

        vx, vy, vz = self.trajectory.velocity(max(0.0, now - self.t0))
        hs = math.hypot(vx, vz)
        if hs > 3.5:
            vx, vz = vx / hs * 3.5, vz / hs * 3.5
            hs = 3.5
        heading = math.atan2(vx, vz) if hs > 1e-6 else math.atan2(
            self.plan.get("target", self.b)[0] - pos[0],
            self.plan.get("target", self.b)[2] - pos[2])
        drop = pos[1] - ground
        # AirBrakeStart ~0.8 s, then descend at <=6 m/s, then a 0.25 s settle.
        brake_time = 0.8
        p1 = (pos[0] + vx * brake_time, pos[1] - min(0.6, drop * 0.15),
              pos[2] + vz * brake_time)
        fall_speed = min(6.0, max(2.0, drop / 1.2))
        p2 = (p1[0] + vx * 0.35, ground + 0.25, p1[2] + vz * 0.35)
        fall_time = max(0.2, (p1[1] - p2[1]) / fall_speed)
        phases = [
            {"phase": "brake", "mode": "linear", "duration": brake_time,
             "from": tuple(float(v) for v in pos), "to": p1},
            {"phase": "fall", "mode": "linear", "duration": fall_time,
             "from": p1, "to": p2},
            {"phase": "land", "mode": "linear", "duration": 0.25,
             "from": p2, "to": (p2[0], ground, p2[2])},
        ]
        try:
            air_planner._check_limits(phases, airborne=True)
        except Exception:
            # Extreme geometry (very long drop / steep brake): fall straight
            # down at the capped speed instead of refusing to cancel.
            fall_time = max(0.3, drop / 6.0)
            p1 = (pos[0], pos[1] - 0.4, pos[2])
            p2 = (pos[0], ground, pos[2])
            phases = [
                {"phase": "brake", "mode": "linear", "duration": 0.5,
                 "from": tuple(float(v) for v in pos),
                 "to": (pos[0], pos[1] - 0.4, pos[2])},
                {"phase": "fall", "mode": "linear", "duration": fall_time,
                 "from": p1, "to": p2},
                {"phase": "land", "mode": "linear", "duration": 0.2,
                 "from": p2, "to": p2},
            ]
        self.trajectory = air_planner.Trajectory(phases)
        self.t0, self.seconds = now, self.trajectory.duration
        self.b = (p2[0], ground, p2[2])
        self.heading = math.degrees(heading)
        self.final_heading = self.heading
        self.cancelled = True
        self.cancel_reason = reason
        self._cancel_phases = phases
        self._log("air_cancel", reason=reason, at=[round(v, 2) for v in pos],
                  ground=round(ground, 2), seconds=round(self.seconds, 2))
        return True

    def landed(self, now=None):
        return self._landed or self.arrived(time.monotonic() if now is None else now)

