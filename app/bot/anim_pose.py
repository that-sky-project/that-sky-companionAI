"""0.16.8 Actor 动画字段：按线上礼包（/opt/sky-gift-api/current，v17a）的实际做法重建。

线上流程：旧格式 Actor 上用 native_take_candle_anim_v1 改动画 → migrate_actor_body_0167 逐 token 转 0.16.8
→ production_pose_lifecycle_0167 按时间表改当前姿态 code90。转换不改顺序，所以 0.16.8 里同一组字段：
  当前动画：generation 0x21ddbe0(raw4) · key 0x21ddbf0(raw8) · 起点 0x21ddc10(12 位) · mode 0x21ddc88(raw1)
  历史动画：动画状态块 0x21defc0 里姿态之后的 [bool][generation raw4][key raw8][起点 9 位]
  当前姿态 code90：块 0x21def88 内 0x1715ed0（3 位）；“现在”的相位：同块内 0x1715e80（12 位）
动画 key = FNV-1a64(动画名)。起点与“现在”同为 [0,3) 秒的 12 位相位码；历史起点是 9 位（=12 位 >> 3）。
"""
import struct
import math
import operator
import warnings
from dataclasses import replace

from qfsky.long0167.actor_codec.actor_wire_0167 import inspect_actor_wire
from qfsky.long0167.actor_codec.actor_wire_schema_0167 import PUBLIC_ELF_SHA256 as SHA
from qfsky.long0167.actor_codec.bit_stream import BitWriter

CURRENT, ANIM_BLOCK = "0x21def88", "0x21defc0"
POSE90, CLOCK, POSE_END = "0x1715ed0", "0x1715e80", "0x1716a94"
GEN, KEY, START, MODE = "0x21ddbe0", "0x21ddbf0", "0x21ddc10", "0x21ddc88"
# 单人持续表情专用字段；只在 Enter/Exit 过渡中接管当前姿态。
EMOTE_HOLD, EMOTE_STATE = "0x21ddc40", "0x1f80844"
POSE_F48, POSE_F70 = "0x1715f48", "0x1715f70"
# 朝向（2026-10-30 用路人 0x4f 的 351 个移动帧拟合）：heading = atan2(dx, dz)，0° 朝 +z
YAW1, YAW2, FWD = "0x21dd1dc", "0x21dd234", "0x21dd350"
# 速度：0x21dd298 三个 8 位 = 127 + 0.9·v（x, y, z，单位/秒）；客户端靠它播走路腿部动作
VEL = "0x21dd298"
VEL_SCALE = 0.9
# 坐椅子（2026-10-30 抓真人坐遇境桌边 4 次）：坐着 = 当前动画 GoSit2 + mode 2（保持），
# 在座位上 0x21dd820=1，座位号 0x21dd858（13 位），角色状态 0x21de944 由 2 变 15，入座瞬间 0x21dd830=1
SEATED, SIT_ENTER, SEAT_ID, AVATAR_STATE = "0x21dd820", "0x21dd830", "0x21dd858", "0x21de944"

# 线上策略：资源帧 1196..1260，pack_fps 30，播放速率 0.4 → 64/30/0.4 秒；之后 0.9 秒 gate
TAKE_CANDLE_SECONDS = 64 / 30 / 0.4
MODE7_CODES = ((0.217, 0), (0.100, 3), (0.0, 6))                       # 播放期：6 → 3 → 0
RESTORE_CODES = ((0.450, 7), (0.333, 5), (0.217, 3), (0.117, 2), (0.0, 0))  # 恢复期：0 → 2 → 3 → 5 → 7


def anim_key(name):
    h = 0xcbf29ce484222325
    for c in name.encode():
        h = ((h ^ c) * 0x100000001b3) & 0xFFFFFFFFFFFFFFFF
    return h


TAKE_CANDLE = anim_key("TakeCandle")
assert TAKE_CANDLE == 0x1A0B23EE716A1D1B


def _starts(o):
    out, bit = [], 0
    for t in o._tokens:
        if t.kind == "raw":
            bit = (bit + 7) & ~7
        out.append(bit)
        bit += t.extent * 8 if t.kind == "raw" else t.extent
    return out


def _locate(o):
    starts = _starts(o)
    bounds = {}
    for label, pos in o._boundaries:
        bounds.setdefault(label, []).append(pos)

    def tok(pos, kind, extent):
        i = next(k for k, s in enumerate(starts) if s >= pos)
        t = o._tokens[i]
        if t.kind != kind or t.extent != extent:
            raise ValueError("anim_shape_" + str(pos))
        return i

    def only(label):
        v = bounds.get(label, [])
        if len(v) != 1:
            raise ValueError("anim_label_not_unique_" + label)
        return v[0]

    cur, blk = only(CURRENT), only(ANIM_BLOCK)
    loc = {
        "gen": tok(only(GEN), "raw", 4), "key": tok(only(KEY), "raw", 8),
        "start": tok(only(START), "bits", 12), "mode": tok(only(MODE), "raw", 1),
        "emote_hold": tok(only(EMOTE_HOLD), "raw", 1),
        "emote_state": tok(only(EMOTE_STATE), "raw", 1),
    }
    counts = dict(o._counts)
    # A valid Actor may carry no current POSE. Do not borrow the history POSE.
    for name, label, width in (("code90", POSE90, 3), ("clock", CLOCK, 12),
                               ("code_f48", POSE_F48, 3), ("code_f70", POSE_F70, 3)):
        positions = [p for p in bounds.get(label, ()) if cur <= p < blk]
        if len(positions) != counts["pose_records"]:
            raise ValueError("anim_pose_count_shape")
        if positions:
            loc[name] = tok(positions[0], "bits", width)
    # 坐标两份：开头 0x21dd054 的 float×3；动画状态块开头 [raw1][地图ID raw4] 之后的 float×3
    o0 = tok(bounds["0x21dd054"][0], "raw", 4)
    loc.update(ox=o0, oy=o0 + 1, oz=o0 + 2)
    b = next(k for k, s in enumerate(starts) if s >= blk)
    if not (o._tokens[b].kind == "raw" and o._tokens[b].extent == 1 and o._tokens[b + 1].extent == 4
            and all(o._tokens[b + 2 + k].kind == "raw" and o._tokens[b + 2 + k].extent == 4 for k in range(3))):
        raise ValueError("anim_origin_copy_shape")
    loc.update(ax=b + 2, ay=b + 3, az=b + 4)
    # 动画状态块坐标副本之后的 6 位 = 第三份偏航角
    if not (o._tokens[b + 5].kind == "bits" and o._tokens[b + 5].extent == 6):
        raise ValueError("anim_yaw_copy_shape")
    loc.update(yaw3=b + 5)
    # history() always writes its header and animation fields, even at count=0.
    # At count>1 animation follows the LAST history POSE, never the first.
    count = o._tokens[b + 6]
    if count.kind != "bits" or count.extent != 3 or count.value != counts["history_pose_records"]:
        raise ValueError("anim_history_count_shape")
    end = only("0x21deffc")
    ends = [p for p in bounds.get(POSE_END, ()) if blk < p < end]
    if len(ends) != count.value:
        raise ValueError("anim_history_pose_count_shape")
    i = tok(ends[-1], "bits", 1) + 1 if ends else b + 7
    if [(t.kind, t.extent) for t in o._tokens[i:i + 3]] != [("raw", 4), ("raw", 8), ("bits", 9)]:
        raise ValueError("anim_history_shape")
    loc.update(h_gen=i, h_key=i + 1, h_start=i + 2)
    # 朝向：0x21dd1dc / 0x21dd234 首个 6 位 = 偏航角；0x21dd350 三个 5 位 = 前向单位向量 (x, y, z)
    loc.update(yaw1=tok(only(YAW1), "bits", 6), yaw2=tok(only(YAW2), "bits", 6))
    f0 = tok(only(FWD), "bits", 5)
    loc.update(fx=f0, fy=f0 + 1, fz=f0 + 2)
    v0 = tok(only(VEL), "bits", 8)
    loc.update(vx=v0, vy=v0 + 1, vz=v0 + 2)
    loc.update(seated=tok(only(SEATED), "bits", 1), sit_enter=tok(only(SIT_ENTER), "bits", 1),
               seat=tok(only(SEAT_ID), "bits", 13), avatar_state=tok(only(AVATAR_STATE), "bits", 5))
    return loc


FLOATS = {"ox", "oy", "oz", "ax", "ay", "az"}   # 其余字段按整数（raw 小端 / bits）读写
FIELD_NAMES = frozenset(FLOATS | {"gen", "key", "start", "mode", "code90", "clock",
    "h_gen", "h_key", "h_start", "yaw1", "yaw2", "yaw3", "fx", "fy", "fz",
    "vx", "vy", "vz", "seated", "sit_enter", "seat", "avatar_state",
    "emote_hold", "emote_state", "code_f48", "code_f70"})
# Native inclusive ranges can be smaller than the storage code's bit capacity.
FIELD_MAXIMUM = {"seat": 6657, "avatar_state": 25}


def _val(name, t):
    if name in FLOATS:
        return struct.unpack("<f", bytes(t.value))[0]
    return int.from_bytes(t.value, "little") if t.kind == "raw" else t.value


def read(body):
    """Read present named fields; clock/code90 are absent when pose_records=0."""
    o = inspect_actor_wire(body, verified_profile_sha256=SHA, check_zero_padding=False)
    return {name: _val(name, o._tokens[i]) for name, i in _locate(o).items()}


def field_info(body):
    """Describe named fields available in this specific CN mode2 Actor shape.

    Bit offsets describe this packet, not native object offsets. Unknown tokens
    remain opaque; availability is not proof of game-world validity.
    """
    o = inspect_actor_wire(body, verified_profile_sha256=SHA, check_zero_padding=False)
    loc, starts = _locate(o), _starts(o)
    fields = {}
    for name in sorted(FIELD_NAMES):
        if name not in loc:
            fields[name] = {"available": False, "reason": "no_current_pose"}
            continue
        i = loc[name]
        token = o._tokens[i]
        width = token.extent * 8 if token.kind == "raw" else token.extent
        fields[name] = {"available": True, "bit_offset": starts[i], "bit_width": width,
                        "encoding": "float32_le" if name in FLOATS else "uint_le" if token.kind == "raw" else "uint_bits",
                        "minimum": -10000 if name in FLOATS else 0,
                        "maximum": 10000 if name in FLOATS else FIELD_MAXIMUM.get(name, (1 << width) - 1)}
    return {"profile": "netease-android-0.16.8-421629", "counts": dict(o._counts), "fields": fields}


def read_origin(body):
    f = read(body)
    return (f["ox"], f["oy"], f["oz"])


def _required_clock(body):
    fields = read(body)
    if "clock" not in fields:
        raise ValueError("anim_field_unavailable_clock")
    return fields["clock"]


def write_origin(body, xyz):
    """两份坐标一起写（主坐标 + 动画状态块里的副本）。"""
    x, y, z = (float(v) for v in xyz)
    return write(body, ox=x, oy=y, oz=z, ax=x, ay=y, az=z)


def facing_codes(heading_deg):
    """heading（度，atan2(dx, dz)）→ (偏航 6 位, 前向 x/y/z 各 5 位)。种子朝 +z 时为 31 与 (15, 15, 31)。"""
    import math
    h = math.radians(heading_deg)
    # 截断取整（不是四舍五入）：这样 +z 正好得到种子里的 31 与 (15, 15, 31)
    yaw = int(((heading_deg + 180.0) % 360.0) / 360.0 * 63.0) % 64

    def q5(v):
        return max(0, min(31, int((v + 1.0) * 15.5)))
    return yaw, q5(math.sin(h)), 15, q5(math.cos(h))


def write_facing(body, heading_deg):
    yaw, fx, fy, fz = facing_codes(heading_deg)
    return write(body, yaw1=yaw, yaw2=yaw, yaw3=yaw, fx=fx, fy=fy, fz=fz)


def velocity_codes(v):
    return tuple(max(0, min(255, 127 + int(round(c * VEL_SCALE)))) for c in v)


def write_velocity(body, v):
    vx, vy, vz = velocity_codes(v)
    return write(body, vx=vx, vy=vy, vz=vz)


def heading_to(src, dst):
    import math
    return math.degrees(math.atan2(dst[0] - src[0], dst[2] - src[2]))


def write(body, **fields):
    """只改给定字段（名称同 read 的键）；重写后重新解析，确认其余 token 一个不变。"""
    o = inspect_actor_wire(body, verified_profile_sha256=SHA, check_zero_padding=False)
    loc = _locate(o)
    toks = list(o._tokens)
    for name, value in fields.items():
        if name not in FIELD_NAMES:
            raise ValueError("anim_unknown_field_" + name)
        if value is None:
            continue
        if name not in loc:
            raise ValueError("anim_field_unavailable_" + name)
        i = loc[name]
        t = toks[i]
        if name in FLOATS:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or abs(value) > 10000:
                raise ValueError("anim_origin_range")
            toks[i] = replace(t, value=struct.pack("<f", value))
        else:
            try:
                integer = operator.index(value)
            except TypeError:
                raise ValueError("anim_value_type_" + name) from None
            width = t.extent * 8 if t.kind == "raw" else t.extent
            if not 0 <= integer <= FIELD_MAXIMUM.get(name, (1 << width) - 1):
                raise ValueError("anim_value_range_" + name)
            toks[i] = replace(t, value=integer.to_bytes(t.extent, "little") if t.kind == "raw" else integer)
    # Patch only requested bits. Re-encoding all tokens would zero unrelated
    # alignment padding that the native reader accepts and ignores.
    starts, patched = _starts(o), bytearray(body)
    allowed = {loc[n] for n in fields if fields[n] is not None}
    for i in allowed:
        t, bit = toks[i], starts[i]
        if t.kind == "raw":
            patched[bit // 8:bit // 8 + t.extent] = t.value
        else:
            for shift in range(t.extent):
                byte, offset = divmod(bit + shift, 8)
                mask = 1 << offset
                patched[byte] = (patched[byte] & ~mask) | (((t.value >> shift) & 1) << offset)
    out = bytes(patched)
    chk = inspect_actor_wire(out, verified_profile_sha256=SHA, check_zero_padding=False)
    changed = {i for i, (a, b) in enumerate(zip(o._tokens, chk._tokens)) if a != b}
    if len(chk._tokens) != len(o._tokens) or not changed <= allowed:
        raise ValueError("anim_unrelated_change")
    return out


# 姿态块里随走路节律摆动的字段（成对的 f7c/080、6184/6288 疑似左右脚），两份姿态块各一组
POSE_LABELS = ("0x1716174", "0x1716260", "0x1716360", "0x1716464", "0x1716568", "0x171666c", "0x1716770")


def _label_tokens(o):
    """{(label, 第几次出现): [token 下标]}；只收 POSE_LABELS。"""
    starts = _starts(o)
    bl = sorted(o._boundaries, key=lambda x: x[1])
    out, j, cur, occ = {}, 0, None, {}
    for i, s in enumerate(starts):
        while j < len(bl) and bl[j][1] <= s:
            cur = bl[j][0]
            occ[cur] = occ.get(cur, 0) + 1
            j += 1
        if cur in POSE_LABELS:
            out.setdefault((cur, occ[cur]), []).append(i)
    return out


def _scoped_label_tokens(o):
    """Stable (label, current/history, index-within-segment) token addresses."""
    bounds = dict(o._boundaries)
    cur, history, end = bounds[CURRENT], bounds[ANIM_BLOCK], bounds["0x21deffc"]
    starts, counters, out = _starts(o), {}, {}
    for (label, _occ), ids in _label_tokens(o).items():
        pos = starts[ids[0]]
        scope = "current" if cur <= pos < history else "history" if history <= pos < end else None
        if scope is None:
            raise ValueError("pose_segment_shape")
        key = (label, scope)
        counters[key] = counters.get(key, 0) + 1
        out[(label, scope, counters[key])] = ids
    counts = dict(o._counts)
    for label in POSE_LABELS:
        for scope, count_name in (("current", "pose_records"), ("history", "history_pose_records")):
            if counters.get((label, scope), 0) != counts[count_name]:
                raise ValueError("pose_segment_count_shape")
    return out


def pose_fields(body, *, scoped=False):
    """Legacy diagnostic tuple keys by default; opt in to segment-scoped keys."""
    o = inspect_actor_wire(body, verified_profile_sha256=SHA, check_zero_padding=False)
    idx = _scoped_label_tokens(o) if scoped else _label_tokens(o)
    return {k: tuple(o._tokens[i].value for i in v) for k, v in idx.items()}


_POSE_WARNED = set()
_POSE_WIDTHS = {label: 6 if label == "0x1716174" else 5 if label in ("0x1716464", "0x1716568") else 8
                for label in POSE_LABELS}


def _pose_warn_once(reason):
    if reason not in _POSE_WARNED:
        _POSE_WARNED.add(reason)
        warnings.warn(reason, RuntimeWarning, stacklevel=3)


def _pose_counts(value):
    if isinstance(value, dict):
        if set(value) != {"current", "history"}:
            raise ValueError("pose_source_counts")
        value = (value["current"], value["history"])
    if not isinstance(value, (tuple, list)) or len(value) != 2 or any(type(n) is not int or not 0 <= n <= 4 for n in value):
        raise ValueError("pose_source_counts")
    return tuple(value)


def normalize_pose_values(values, *, legacy_pose_counts=None):
    """Convert explicitly described legacy values to scoped keys; no guessing."""
    if not isinstance(values, dict):
        raise ValueError("pose_values_type")
    parsed, formats = [], set()
    counts = _pose_counts(legacy_pose_counts) if legacy_pose_counts is not None else None
    for key, vals in values.items():
        if not isinstance(key, str):
            raise ValueError("pose_key_format")
        parts = key.split("|")
        if len(parts) not in (2, 3) or parts[0] not in _POSE_WIDTHS:
            raise ValueError("pose_key_format")
        label, text = parts[0], parts[-1]
        if not text.isascii() or not text.isdecimal() or str(int(text)) != text or int(text) < 1:
            raise ValueError("pose_key_index")
        n = int(text)
        if len(parts) == 3 and (parts[1] not in ("current", "history") or n > 4):
            raise ValueError("pose_key_scope")
        if len(parts) == 2 and n > 8:
            raise ValueError("pose_key_index")
        if not isinstance(vals, (tuple, list)) or len(vals) != 3:
            raise ValueError("pose_vector_shape")
        if any(type(v) is not int or not 0 <= v < (1 << _POSE_WIDTHS[label]) for v in vals):
            raise ValueError("pose_vector_range")
        formats.add(len(parts))
        parsed.append((label, parts[1] if len(parts) == 3 else None, n, list(vals)))
    if len(formats) > 1:
        raise ValueError("pose_mixed_key_formats")
    if formats == {2} and counts is None:
        raise ValueError("pose_legacy_source_shape_missing")
    out = {}
    for label, scope, n, vals in parsed:
        if scope is None:
            if n > sum(counts):
                raise ValueError("pose_key_source_count")
            scope, n = ("current", n) if n <= counts[0] else ("history", n - counts[0])
        key = f"{label}|{scope}|{n}"
        if key in out:
            raise ValueError("pose_duplicate_target")
        out[key] = vals
    return out


def write_pose_fields(body, values, *, legacy_pose_counts=None):
    """Patch scoped pose groups without rewriting unrelated bits.

    Unknown legacy source shapes retain the historical (1,1) assumption ONLY
    for a (1,1) target, with a one-time warning. Other targets are unchanged.
    This compatibility exception is not proof of the legacy source's shape.
    """
    o = inspect_actor_wire(body, verified_profile_sha256=SHA, check_zero_padding=False)
    try:
        values = normalize_pose_values(values, legacy_pose_counts=legacy_pose_counts)
    except ValueError as error:
        if str(error) != "pose_legacy_source_shape_missing":
            raise
        counts = dict(o._counts)
        if (counts["pose_records"], counts["history_pose_records"]) != (1, 1):
            _pose_warn_once("legacy_pose_target_shape_rejected: source shape unknown; replay skipped")
            return body
        values = normalize_pose_values(values, legacy_pose_counts=(1, 1))
        _pose_warn_once("legacy_pose_source_shape_assumed_1_1: compatibility only, source shape unverified")
    idx, starts = _scoped_label_tokens(o), _starts(o)
    patched, expected = bytearray(body), {}
    for key, vals in values.items():
        label, scope, n = key.split("|")
        ids = idx.get((label, scope, int(n)))
        if ids is None:
            continue  # A missing current frame must never fall through to history.
        if len(ids) != 3 or any(o._tokens[i].kind != "bits" for i in ids):
            raise ValueError("pose_vector_token_shape")
        for i, value in zip(ids, vals):
            token = o._tokens[i]
            expected[i] = replace(token, value=value)
            for shift in range(token.extent):
                byte, offset = divmod(starts[i] + shift, 8)
                mask = 1 << offset
                patched[byte] = (patched[byte] & ~mask) | (((value >> shift) & 1) << offset)
    if not expected:
        return body
    out = bytes(patched)
    chk = inspect_actor_wire(out, verified_profile_sha256=SHA, check_zero_padding=False)
    if len(chk._tokens) != len(o._tokens) or any(t != expected.get(i, o._tokens[i]) for i, t in enumerate(chk._tokens)):
        raise ValueError("pose_unrelated_change")
    return out


def rotate_pose(values, delta_deg):
    """姿态向量按世界坐标存（路人站着时 0x1716360 的 x ≈ 127 + 11·sin(朝向)），
    所以换朝向要把每组 (x, y, z) 的水平分量绕 y 轴转 delta 度（heading 定义同 heading_to）。"""
    import math
    if not delta_deg:
        return values
    c, s = math.cos(math.radians(delta_deg)), math.sin(math.radians(delta_deg))
    out = {}
    for key, vals in values.items():
        if len(vals) != 3:
            out[key] = vals
            continue
        bits = {"0x1716174": 6, "0x1716464": 5, "0x1716568": 5}.get(key.split("|")[0], 8)
        mid, top = (1 << (bits - 1)) - 1, (1 << bits) - 1      # 8 位中点 127、6 位 31、5 位 15
        x, y, z = vals[0] - mid, vals[1], vals[2] - mid
        nx, nz = x * c + z * s, -x * s + z * c
        out[key] = [max(0, min(top, int(round(nx + mid)))), y, max(0, min(top, int(round(nz + mid))))]
    return out


def pose_values(body, *, scoped=True):
    """New captures use label|current/history|frame; legacy export is explicit."""
    return {"|".join(map(str, key)): list(value) for key, value in pose_fields(body, scoped=scoped).items()}


class WalkClip:
    """录下的真人走路循环；at(秒) 取循环里对应的一帧。heading = 录制时路人的行进方向。"""

    def __init__(self, path):
        import json
        with open(path, encoding="utf-8") as stream:
            d = json.load(stream)
        self.frames = d["frames"]
        # Optional source metadata permits exact conversion of old clips.
        # Without it write_pose_fields applies the explicit legacy exception.
        if "pose_counts" in d:
            self.frames = [[stamp, normalize_pose_values(values, legacy_pose_counts=d["pose_counts"])]
                           for stamp, values in self.frames]
        self.heading = float(d.get("heading", 90.0))   # 现有录制：路人 0x4f 沿 +x 直行
        self.length = self.frames[-1][0] + (self.frames[-1][0] - self.frames[-2][0])

    def at(self, seconds):
        t = seconds % self.length
        return next((f for ft, f in reversed(self.frames) if ft <= t), self.frames[0][1])


def _code(table, age):
    return next(code for at, code in table if age >= at)


class AnimEvent:
    """一次性动画事件（按线上时间表）。post(body, now) 每帧调用，返回改好字段的 Actor。

    stamp 在第一次 post 时取“现在”的相位作为起点，此后钉住；历史记录写成事件前的动画。
    """

    def __init__(self, key, previous, started, seconds=TAKE_CANDLE_SECONDS):
        self.key, self.prev, self.t0, self.seconds = key, previous, started, seconds
        self.gen = (previous["gen"] + 1) & 0xFFFFFFFF
        self.start = None

    def phase(self, now):
        age = now - self.t0
        if age < self.seconds:
            return "mode7", _code(MODE7_CODES, age)
        return ("restoring" if age < self.seconds + 0.45 else "idle"), _code(RESTORE_CODES, age - self.seconds)

    def post(self, body, now):
        if self.start is None:
            self.start = _required_clock(body)
        phase, code90 = self.phase(now)
        return write(body, gen=self.gen, key=self.key, start=self.start, mode=7 if phase == "mode7" else 0,
                     code90=code90, h_gen=self.prev["gen"], h_key=self.prev["key"], h_start=self.prev["start"] >> 3)

    def stamp(self):
        """事件结束后作为“上一个动画”的记录（供下一次事件写历史）。"""
        return {"gen": self.gen, "key": self.key, "start": self.start or 0}


# 真人蝴蝶抓包：Enter 首帧后约 0.135 秒进入保持；Exit 的两个恢复边界。
# 这些时间仅用于事件内部字段过渡，不会触发持续表情自动结束。
EMOTE_ENTER_SECONDS = 0.135
EMOTE_EXIT_MODE7_SECONDS = 0.65
EMOTE_EXIT_SECONDS = 1.03


class ContinuousEmoteEvent(AnimEvent):
    """单人 Enter 无限保持；只有动作切换显式创建 Exit 才结束。"""

    def __init__(self, key, exit_key, previous, started):
        super().__init__(key, previous, started)
        self.exit_key = exit_key

    def phase(self, now):
        return ("entering", 1) if now - self.t0 < EMOTE_ENTER_SECONDS else ("holding", 0)

    def post(self, body, now):
        if self.start is None:
            self.start = _required_clock(body)
            # 首帧可能晚于命令线程；Enter 过渡从实际构造首帧开始计时。
            self.t0 = now
        phase, code90 = self.phase(now)
        other = 4 if phase == "entering" else 0
        return write(body, gen=self.gen, key=self.key, start=self.start, mode=8,
                     code90=code90, code_f48=other, code_f70=other,
                     emote_hold=1, emote_state=2,
                     h_gen=self.prev["gen"], h_key=self.prev["key"], h_start=self.prev["start"] >> 3)


class EmoteExitEvent(AnimEvent):
    """单人持续表情收尾；结束后完全使用普通 AnimEvent 的 idle 写入集合。"""

    def __init__(self, key, previous, started):
        super().__init__(key, previous, started, seconds=EMOTE_EXIT_MODE7_SECONDS)

    def phase(self, now):
        age = now - self.t0
        if age < EMOTE_EXIT_MODE7_SECONDS:
            return "mode7", 0
        if age < EMOTE_EXIT_SECONDS:
            return "restoring", 2
        return "idle", 7

    def post(self, body, now):
        # 调用原 post 可保证 idle 不碰 f48/f70、历史姿态或额外状态字段。
        body = super().post(body, now)
        phase, _code90 = self.phase(now)
        if phase == "idle":
            return body
        other = 0 if phase == "mode7" else 5
        return write(body, emote_hold=0, emote_state=2, code_f48=other, code_f70=other)


class ClearEvent:
    """“没有在播动作”：key 0、generation +1、mode 0、code90 7（待机）。用来结束上一个动作的残留（举烛前）。"""

    def __init__(self, previous):
        self.prev = previous
        self.gen = (previous["gen"] + 1) & 0xFFFFFFFF
        self.key, self.start = 0, None

    def post(self, body, now):
        if self.start is None:
            self.start = _required_clock(body)
        return write(body, gen=self.gen, key=0, start=self.start, mode=0, code90=7,
                     h_gen=self.prev["gen"], h_key=self.prev["key"], h_start=self.prev["start"] >> 3)

    def stamp(self):
        return {"gen": self.gen, "key": 0, "start": self.start or 0}


GO_SIT = anim_key("GoSit2")
GET_UP = anim_key("GetUpL")
GET_UP_SECONDS = 1.2            # 真人站起：GetUpL 播放（mode 7）约 1.2 秒后归 0
SIT_ENTER_SECONDS = 0.7         # 入座标志只在开头约 0.7 秒为 1
SEATED_STATE = 15


class SitEvent:
    """坐下并一直坐着：GoSit2、mode 2、座位字段。起点在第一次 post 时钉住（同 AnimEvent）。"""

    def __init__(self, seat_id, previous, started):
        self.seat, self.prev, self.t0 = int(seat_id), previous, started
        self.key, self.gen, self.start = GO_SIT, (previous["gen"] + 1) & 0xFFFFFFFF, None

    def post(self, body, now):
        if self.start is None:
            self.start = _required_clock(body)
        return write(body, gen=self.gen, key=self.key, start=self.start, mode=2,
                     h_gen=self.prev["gen"], h_key=self.prev["key"], h_start=self.prev["start"] >> 3,
                     seated=1, sit_enter=1 if now - self.t0 < SIT_ENTER_SECONDS else 0,
                     seat=self.seat, avatar_state=SEATED_STATE)

    def stamp(self):
        return {"gen": self.gen, "key": self.key, "start": self.start or 0}


# 双人互动邀请（2026-10-30 抓主人对小光发起背背/牵手）：发起方 Actor 里
#   0x21df494 = 邀请类型（关系能力 id：2 牵手、3 拥抱、40 背背…），0x21df4ac = 邀请对象的 slot（raw1）
OFFER_TYPE, OFFER_TARGET = "0x21df494", "0x21df4ac"
CANDLE_TARGET = "0x21dd730"   # 举蜡烛对着谁（目标 slot，raw1；不举为 0）


def social_state(body):
    """别的玩家的 Actor → {"seat": 座位号或 0, "yaw": 偏航码, "offer": (类型, 对象 slot) 或 None}。
    只按 label 取这几组，不依赖完整结构；结构不认识就返回 None。"""
    try:
        o = inspect_actor_wire(body, verified_profile_sha256=SHA, check_zero_padding=False)
    except Exception:
        return None
    starts = _starts(o)
    out = {}
    for label, pos in o._boundaries:
        if label in (SEATED, SEAT_ID, YAW1, OFFER_TYPE, OFFER_TARGET, MODE, CANDLE_TARGET) and label not in out:
            i = next((k for k, s in enumerate(starts) if s >= pos), None)
            if i is not None:
                t = o._tokens[i]
                out[label] = t.value if t.kind == "bits" else int.from_bytes(bytes(t.value), "little")
    if SEAT_ID not in out:
        return None
    offer = (out[OFFER_TYPE], out.get(OFFER_TARGET, 0)) if out.get(OFFER_TYPE) else None
    return {"seat": out[SEAT_ID] if out.get(SEATED) else 0, "yaw": out.get(YAW1, 0), "offer": offer,
            "mode": out.get(MODE), "candle_target": out.get(CANDLE_TARGET) or 0}


def seat_state(body):
    """兼容旧调用：(座位号 或 0, 偏航码)。"""
    s = social_state(body)
    return None if s is None else (s["seat"], s["yaw"])


def yaw_heading(yaw):
    """6 位偏航码 → heading（度），facing_codes 的反函数（取格子中心）。"""
    return (yaw + 0.5) / 63.0 * 360.0 - 180.0


WALK_SPEED = 3.5  # 单位/秒；实测路人走动 4~6，太慢腿部动作会显得拖


class Motion:
    """匀速直线移动：pos(now) 给出当前位置，到达后停在终点；velocity 供腿部动作。"""

    def __init__(self, start, target, started, speed=WALK_SPEED, face_after=None):
        self.a, self.b, self.t0, self.speed = tuple(start), tuple(target), started, speed
        self.length = sum((q - p) ** 2 for p, q in zip(self.a, self.b)) ** 0.5
        self.seconds = self.length / speed if speed > 0 else 0.0
        self.heading = heading_to(self.a, self.b) if self.length > 1e-6 else None
        # 到达后的朝向：给了 face_after（某人的位置）就转向那人，否则保持行进方向
        self.final_heading = heading_to(self.b, face_after) if face_after is not None else self.heading
        k = speed / self.length if self.length > 1e-6 else 0.0
        self.velocity = tuple((q - p) * k for p, q in zip(self.a, self.b))

    def pos(self, now):
        if self.seconds <= 0:
            return self.b
        k = min(1.0, max(0.0, (now - self.t0) / self.seconds))
        return tuple(p + (q - p) * k for p, q in zip(self.a, self.b))

    def arrived(self, now):
        return now - self.t0 >= self.seconds
