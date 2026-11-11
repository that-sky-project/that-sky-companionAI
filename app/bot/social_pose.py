"""接受双人互动后小光自己 Actor 要进入的状态。三类（2026-10-31 两部真机互做牵手/拥抱/背背/熊抱抓包，gamemsg-1031-0345，接受方 0x9e）：

  once  拥抱/熊抱/击掌…（一次性）：0x21df190=1 + [0x21df1a8 对象 slot][0x21df1e0 三个 float 0]，0x21dd820=1，
        约 0.5 秒后结构消失、mode 7，双方客户端各自播 Give/Take 动画。接受方会挪到离对方 0.3~0.4 米处。
  hold  牵手（持续）：0x21df130 = 带路方 slot、0x21df158 = 1，一直保持到松手；接受方跟在带路方身边 0.3~1.7 米。
  carry 背背/公主抱（持续）：0x21de648 = 背人方 slot，mode(0x21ddc88)=6；接受方上报的坐标与背人方完全重合，
        一直跟着走；下来时清掉、写落地坐标并发 AvatarTeleportRpc（见 social_accept.teleport_game）。
邀请方自己身上没有持续的关系字段，结束由接受方决定（松手/跳下）。

模板 = 接受方“接受前一帧”与“进入状态那一帧”，用 candle_pose 的字段级移植只搬这几个字段，再把对象 slot 换成邀请方。
"""
import json
import os
import struct
from pathlib import Path

import xg_paths

import anim_pose
import candle_pose

TEMPLATE_FILE = Path(os.environ.get("XG_SOCIAL_TEMPLATES") or (Path(__file__).with_name("social_templates.json")))
HUG_FILE = Path(os.environ.get("XG_SOCIAL_TEMPLATE") or (Path(__file__).with_name("social_template.json")))       # 旧的拥抱模板（墓土路人，2315）
CAP = str(xg_paths.CAPTURES / "gamemsg-1031-0345.jsonl")
SLOT = 0x9E
# kind -> (前一帧时间, 进入状态那一帧时间, 移植的字段, 写对象 slot 的字段)
SPECS = {
    "hold": (17.95, 18.1, ("0x21df130", "0x21df158"), "0x21df130"),
    "carry": (36.0, 36.3, ("0x21de648", "0x21ddc88"), "0x21de648"),
}
SPEC_MINUTE = "03:51"
HUG_FIELDS = ("0x21df190", "0x21df1a8", "0x21df1e0")
HOLD_SECONDS = 1.2          # 一次性互动：“互动对象”结构保持的时间

KIND = {2: "hold", 40: "carry", 41: "carry", 63: "carry", 64: "carry"}   # 其余类型都是一次性
# 双人舞类（有 XxxTakeExit 的）：旋转舞（0358 实测 mode 9 循环）、双人舞、悄悄话（同规律推断）
KIND.update({t: "dance" for t in (56, 57, 94, 95, 70, 71, 76, 77, 72, 73, 78, 79)})


def kind_of(offer_type):
    return KIND.get(offer_type, "once")


def _templates():
    if TEMPLATE_FILE.exists():
        return json.loads(TEMPLATE_FILE.read_text())
    import time
    import tools_actor as A
    rows = []
    for t, data in A.snapshots(CAP):
        if time.strftime("%H:%M", time.localtime(t)) == SPEC_MINUTE and SLOT in A.records(data):
            rows.append((t % 60, A.records(data)[SLOT][1]))
    out = {"source": CAP, "slot": hex(SLOT)}
    for kind, (before, at, _f, _s) in SPECS.items():
        b = min(rows, key=lambda r: abs(r[0] - before))[1]
        a = min(rows, key=lambda r: abs(r[0] - at))[1]
        out[kind] = {"before": b.hex(), "at": a.hex()}
    TEMPLATE_FILE.write_text(json.dumps(out, indent=1))
    return out


def _transplant(base, before, at, fields):
    new, _log = candle_pose.transplant_fields(
        candle_pose.tokens(base), candle_pose.labels(base),
        candle_pose.tokens(before), candle_pose.labels(before),
        candle_pose.tokens(at), candle_pose.labels(at), only=fields)
    return candle_pose.write(new)


def _set_slot(body, label, slot, zero_floats=None):
    toks, labs = candle_pose.tokens(body), candle_pose.labels(body)
    hit = False
    for i, l in enumerate(labs):
        t = toks[i]
        if l == label and t.kind == "raw" and t.extent == 1:
            toks[i] = type(t)("raw", 1, bytes([slot]))
            hit = True
        elif zero_floats and l == zero_floats and t.kind == "raw" and t.extent == 4:
            toks[i] = type(t)("raw", 4, struct.pack("<f", 0.0))
    if not hit:
        raise ValueError("social_slot_field_missing_" + label)
    return candle_pose.write(toks)


def build(base, partner_slot, kind="once"):
    """base 上加对应的互动状态（对象 = partner_slot）。结果解析校验失败会抛错。"""
    if kind == "once":
        d = json.loads(HUG_FILE.read_text())
        out = _transplant(base, bytes.fromhex(d["before"]), bytes.fromhex(d["at"]), HUG_FIELDS)
        out = _set_slot(out, "0x21df1a8", partner_slot, zero_floats="0x21df1e0")
        return anim_pose.write(out, seated=1)
    before, at, fields, slot_label = SPECS[kind]
    t = _templates()[kind]
    out = _transplant(base, bytes.fromhex(t["before"]), bytes.fromhex(t["at"]), fields)
    out = _set_slot(out, slot_label, partner_slot)
    if kind == "carry" and anim_pose.read(out)["mode"] != 6:   # 霞谷种子自带 mode 3，移植不改它
        out = anim_pose.write(out, mode=6)
    return out


CARRY_FORCE = {"0x21ddcc8": 31, "0x1f80844": 1, "0x1715ed0": 0, "0x1715f48": 0, "0x1715f70": 0}


# 走路中（2026-10-31 对照主号 gamemsg-1031-0358 走路帧）：0x21dd40c=1，0x21ddcc8=31，姿态块码 7/0/0（站着码 7/7/7、权重 0）。
# 不写的话别人客户端当成“站着被平移”，不播走路动画：飘着走、没有上下颠、脚乱翻。
WALK_FORCE = {"0x21dd40c": 1, "0x21ddcc8": 31, "0x1715f48": 0, "0x1715f70": 0}   # 0x21ddcc8 = 走路动画权重（站 0，走 31）


# 站着（2026-10-31 对照四张站稳录的图）：录图时角色在跑/转身的话，种子里就带着跑动状态，
# 别人看到小光原地跑步。每帧先把这些改回站立值，走路、互动、动作再在上面覆盖。
STAND_FORCE = {"0x21dd40c": 0, "0x21ddcc8": 0, "0x1715ed0": 7, "0x1715f48": 7, "0x1715f70": 7, "0x1f80844": 0}
STAND_FIELDS = {"mode": 0, "code90": 7, "fy": 15, "vx": 127, "vy": 127, "vz": 127}


def stand_basic(body):
    return anim_pose.write(force(body, STAND_FORCE), **STAND_FIELDS)


_STAND_GROUPS = None


def _stand_groups():
    global _STAND_GROUPS
    if _STAND_GROUPS is None:
        f = Path(__file__).with_name("stand_fields.json")
        _STAND_GROUPS = json.loads(f.read_text()) if f.exists() else {}
    return _STAND_GROUPS


def set_groups(body, groups):
    """groups: {"label|第几次出现": [各 token 值]}；该组 token 个数/类型对不上就跳过。只动这些 token。"""
    from dataclasses import replace
    from qfsky.long0167.actor_codec.actor_wire_0167 import inspect_actor_wire
    from qfsky.long0167.actor_codec.bit_stream import BitWriter
    o = inspect_actor_wire(body, verified_profile_sha256=anim_pose.SHA, check_zero_padding=False)
    starts = anim_pose._starts(o)
    bl = sorted(o._boundaries, key=lambda x: x[1])
    idx, j, cur, occ = {}, 0, None, {}
    for i, st in enumerate(starts):
        while j < len(bl) and bl[j][1] <= st:
            cur = bl[j][0]
            occ[cur] = occ.get(cur, 0) + 1
            j += 1
        idx.setdefault(f"{cur}|{occ.get(cur, 0)}", []).append(i)
    toks, allowed = list(o._tokens), set()
    for key, vals in groups.items():
        ids = idx.get(key)
        if not ids or len(ids) != len(vals):
            continue
        new = []
        for i, v in zip(ids, vals):
            t = toks[i]
            if t.kind == "raw" and isinstance(v, str) and len(v) == 2 * t.extent:
                new.append((i, replace(t, value=bytes.fromhex(v))))
            elif t.kind == "bits" and isinstance(v, int) and 0 <= v < (1 << t.extent):
                new.append((i, replace(t, value=v)))
            else:
                break
        else:
            for i, t in new:
                toks[i] = t
                allowed.add(i)
    w = BitWriter(max_bytes=0x2420)
    for t in toks:
        w.write_raw(bytes(t.value)) if t.kind == "raw" else w.write_bits(t.value, t.extent)
    out = w.to_bytes()
    chk = inspect_actor_wire(out, verified_profile_sha256=anim_pose.SHA, check_zero_padding=False)
    if len(chk._tokens) != len(o._tokens) or not {i for i, (a, b) in enumerate(zip(o._tokens, chk._tokens)) if a != b} <= allowed:
        raise ValueError("stand_unrelated_change")
    return out


FEET = ("0x171666c", "0x1716770")


BODY = "0x1716360"                  # 身体（站直时 y 140..150）


def _feet_down(body):
    """站好了：两只脚踩地（y 95..101），身体直（y 136..152）。"""
    p = anim_pose.pose_values(body)
    ys = [v[1] for k, v in p.items() if k.split("|")[0] in FEET and len(v) == 3]
    torso = [v[1] for k, v in p.items() if k.split("|")[0] == BODY and len(v) == 3]
    return bool(ys) and bool(torso) and all(95 <= y <= 101 for y in ys) and all(136 <= y <= 152 for y in torso)


def stand_legs(body):
    """脚没踩地时换成朝向最近的站稳姿态（转到当前朝向）。"""
    if _feet_down(body):
        return body
    f = Path(__file__).with_name("stand_pose.json")
    if not f.exists():
        return body
    h = anim_pose.yaw_heading(anim_pose.read(body)["yaw1"])

    def gap(r):
        return abs((r["heading"] - h + 180) % 360 - 180)
    ref = min(json.loads(f.read_text()), key=gap)
    return anim_pose.write_pose_fields(body, anim_pose.rotate_pose(ref["values"], h - ref["heading"]))


def stand(body):
    out = set_groups(stand_basic(body), _stand_groups())
    return stand_legs(out) if out != body or not _feet_down(out) else out


def force(body, values):
    """把这些标签的定长字段（bits 或 1 字节 raw，所有副本）改成给定值。"""
    toks, labs = candle_pose.tokens(body), candle_pose.labels(body)
    for i, l in enumerate(labs):
        v = values.get(l)
        if v is None:
            continue
        t = toks[i]
        if t.kind == "bits":
            toks[i] = type(t)("bits", t.extent, v)
        elif t.kind == "raw" and t.extent == 1:
            toks[i] = type(t)("raw", 1, bytes([v]))
    return candle_pose.write(toks)


def force_carry(body):
    """被背时每帧写：挂接权重 31、0x1f80844=1、姿态块码 0（两份副本都写）、mode 6。"""
    return anim_pose.write(force(body, CARRY_FORCE), mode=6)


def partner_of(body, kind="once"):
    """读回对象 slot（校验用）；没有这个结构返回 None。"""
    label = {"once": "0x21df1a8", "hold": "0x21df130", "carry": "0x21de648"}[kind]
    toks, labs = candle_pose.tokens(body), candle_pose.labels(body)
    return next((bytes(t.value)[0] for t, l in zip(toks, labs) if l == label and t.kind == "raw"), None)
