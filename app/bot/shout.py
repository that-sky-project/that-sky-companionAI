"""T23-2 小光的叫声事件（纯 Actor 字段，无独立 RPC）。

真人抓包结论（analysis2 + work2/analysis/shout_summary.json）：
  * 短叫 ShoutS：key 变 ShoutS、generation +1、start 重置、mode 0，
    0x21dd444(raw1)=1 约 0.4 s，0x1f80044(raw1)=1（类型码只维持一两帧）；
  * 中叫 ShoutM：同上，动画约 2.1 s，0x21dd444=1 约 0.8 s，0x1f80044=3；
  * 大叫：先 ShoutCharge（约 1.2 s，mode 0，flag/type 都为 0）再 ShoutL
    （generation 在 Charge 基础上再 +2、start 重置、0x21dd444=1 约 0.9 s、
    0x1f80044=2；ShoutL 动画本身约 3.6 s）。
  叫完把 0x21dd444 / 0x1f80044 恢复成叫之前的原值；动画 key 留在最后一次
  （真人也这样，直到下一个动画事件把它换掉）。

接口与 anim_pose.AnimEvent 一致：post(body, now) / stamp() / seconds。
字段按“标签 + 类型 + 位宽”定位，不依赖全局 token 序号。
"""
from dataclasses import replace

from qfsky.long0167.actor_codec.actor_wire_0167 import inspect_actor_wire
from qfsky.long0167.actor_codec.actor_wire_schema_0167 import PUBLIC_ELF_SHA256 as SHA

import anim_pose
import random
import struct

# 2026-11-07 哥哥实测：只改 Actor 叫声没有那圈光晕。光晕/叫声音效来自单独的 GameMsg RPC
# SpawnShout（key = FNV-1a32("SpawnShout") = 0x9b4aeaed，两份下行抓包约 560 条核对）：
#   正文 8 字节 [叫的人 slot][1a 1a 1a][强度 u8：短叫 0 / 大叫 255 / 中叫中间值][3 字节随机：最高字节 5–8]
#   每叫一声一条，比叫声动画早 0.1–0.3 s；大叫只在蓄力结束真正叫出来时一条（强度 255）。
#   真人坐着/举烛时也会发（没有叫声动画）→ 光晕完全靠这条 RPC。
SPAWN_SHOUT_KEY = 0x9B4AEAED
SPAWN_INTENSITY = {"short": 0, "medium": 100, "long": 255}


def spawn_shout_body(own_slot, kind, rng=random):
    tail = rng.randrange(0, 1 << 16) | (rng.randrange(5, 9) << 16)
    return bytes((own_slot & 0xFF, 0x1A, 0x1A, 0x1A, SPAWN_INTENSITY[kind])) + tail.to_bytes(3, "little")


def spawn_shout_game(epoch, own_slot, kind, rng=random):
    """上行 GameMsg：[2][epoch][0][自己 slot] + [key][hdr 0][size u16] + 正文（同 social_accept._game）。"""
    body = spawn_shout_body(own_slot, kind, rng)
    return bytes((2, epoch & 0xFF, 0, own_slot & 0xFF)) + struct.pack("<IBH", SPAWN_SHOUT_KEY, 0, len(body)) + body


def send_spawn_shout(epoch, own_slot, kind):
    """放进引擎发件箱，下一帧随 Actor 一起发出（与 social_accept._send 相同）。"""
    from qfsky.long0167.engine import Frame
    from qfsky.map_long0167 import map_long_engine
    game = spawn_shout_game(epoch, own_slot, kind)
    map_long_engine.OUTBOX.append(Frame(14, game, True))
    return game

FLAG = "0x21dd444"          # raw1：叫声触发/持续标志（叫的那段 =1）
TYPE = "0x1f80044"          # raw1：叫声类型/强度（短 1、大 2、中 3）
FLAG_TOKEN = (FLAG, "raw", 1)
TYPE_TOKEN = (TYPE, "raw", 1)

SHOUT_S = anim_pose.anim_key("ShoutS")
SHOUT_M = anim_pose.anim_key("ShoutM")
SHOUT_L = anim_pose.anim_key("ShoutL")
SHOUT_CHARGE = anim_pose.anim_key("ShoutCharge")

# 真人中位数（analysis2/SHOUT_NOTES.md；work2 复核后写进 REPORT）
DURATION = {"short": 0.42, "medium": 2.10, "long": 3.60}
CHARGE_SECONDS = 1.20
# 抓包里 0x21dd444=1 只覆盖“叫声触发窗口”，不是整段动画：
# 短叫约 0.2–0.5 s，中叫约 0.8 s，大叫约 0.9 s（ShoutL 动画本身 3.6 s）。
FLAG_SECONDS = {"short": 0.42, "medium": 0.80, "long": 0.90}
# 0x1f80044 的类型码在叫的那一帧写入，下一帧就回 0（抓包 6–10 Hz，约 0.1–0.2 s）。
TYPE_SECONDS = 0.16
KIND_KEY = {"short": SHOUT_S, "medium": SHOUT_M, "long": SHOUT_L}
KIND_TYPE = {"short": 1, "medium": 3, "long": 2}
# 叫完到下一次身体动画之间的缓冲（room_pose 的 _play 用 0.9 s gate）
GATE_SECONDS = 0.35


def _starts(o):
    return anim_pose._starts(o)


def _token_index(o, label, kind, extent):
    """按标签定位 token；同一标签多次出现时取第一次。找不到返回 None。"""
    starts = _starts(o)
    for name, pos in sorted(o._boundaries, key=lambda item: item[1]):
        if name != label:
            continue
        i = next((k for k, s in enumerate(starts) if s >= pos), None)
        if i is None:
            return None
        token = o._tokens[i]
        if token.kind == kind and token.extent == extent:
            return i
        return None
    return None


def _read_value(body, token_def):
    o = inspect_actor_wire(body, verified_profile_sha256=SHA, check_zero_padding=False)
    i = _token_index(o, *token_def)
    if i is None:
        return 0
    token = o._tokens[i]
    return int.from_bytes(bytes(token.value), "little") if token.kind == "raw" else token.value


def _patch_values(body, updates):
    """只改 (label, kind, extent) 指定的 token，并确认其它 token 一个都没变。"""
    o = inspect_actor_wire(body, verified_profile_sha256=SHA, check_zero_padding=False)
    loc, starts = {}, _starts(o)
    for token_def, value in updates.items():
        i = _token_index(o, *token_def)
        if i is not None:
            loc[i] = value
    if not loc:
        return body
    toks, patched = list(o._tokens), bytearray(body)
    for i, value in loc.items():
        t = toks[i]
        width = t.extent * 8 if t.kind == "raw" else t.extent
        if not 0 <= int(value) <= (1 << width) - 1:
            raise ValueError("shout_value_range")
        toks[i] = replace(t, value=(int(value).to_bytes(t.extent, "little")
                                    if t.kind == "raw" else int(value)))
    for i in loc:
        t, bit = toks[i], starts[i]
        if t.kind == "raw":
            patched[bit // 8:bit // 8 + t.extent] = bytes(t.value)
        else:
            for shift in range(t.extent):
                byte, offset = divmod(bit + shift, 8)
                mask = 1 << offset
                patched[byte] = (patched[byte] & ~mask) | (((int(t.value) >> shift) & 1) << offset)
    out = bytes(patched)
    chk = inspect_actor_wire(out, verified_profile_sha256=SHA, check_zero_padding=False)
    changed = {i for i, (a, b) in enumerate(zip(o._tokens, chk._tokens)) if a != b}
    if len(chk._tokens) != len(o._tokens) or not changed <= set(loc):
        raise ValueError("shout_unrelated_change")
    return out


class ShoutEvent:
    """一次叫声（短/中/大叫）。post 每帧调用；long 内部含 Charge 阶段。

    与 AnimEvent 一样，start 在第一次 post 时钉住；历史动画写事件前的 key/gen/start。
    """

    def __init__(self, kind, previous, started):
        if kind not in KIND_KEY:
            raise ValueError("shout_kind_" + str(kind))
        self.kind, self.prev, self.t0 = kind, dict(previous), float(started)
        self.seconds = DURATION[kind] + (CHARGE_SECONDS if kind == "long" else 0.0)
        self.gen_charge = (self.prev["gen"] + 1) & 0xFFFFFFFF
        # 抓包：pre-charge gen=84 → ShoutCharge 85 → ShoutL 87（Charge 后 +2，总共 +3）。
        self.gen_shout = (self.prev["gen"] + (3 if kind == "long" else 1)) & 0xFFFFFFFF
        self.begin_charge = None       # charge 的 start（long）
        self.begin_shout = None        # 实际叫的 start
        self.key = SHOUT_CHARGE if kind == "long" else KIND_KEY[kind]
        self.gen = self.gen_charge
        self.start = None
        self._orig_flag = None
        self._orig_type = None

    def _charge(self, now):
        return self.kind == "long" and now - self.t0 < CHARGE_SECONDS

    def phase(self, now):
        if self._charge(now):
            return "charge"
        return "shout" if now - self.t0 < self.seconds else "done"

    def post(self, body, now):
        if self.begin_charge is None:
            self.begin_charge = anim_pose._required_clock(body)
            self._orig_flag = _read_value(body, FLAG_TOKEN)
            self._orig_type = _read_value(body, TYPE_TOKEN)
        charge = self._charge(now)
        if charge:
            self.gen, self.key = self.gen_charge, SHOUT_CHARGE
            self.start = self.begin_charge
            hist = self.prev
        else:
            if self.begin_shout is None:
                # 大叫在 Charge 结束那一帧重置 start；短叫就是事件起点。
                self.begin_shout = (self.begin_charge if self.kind != "long"
                                    else (self.begin_charge + int(round(CHARGE_SECONDS * 4096.0 / 3.0))) & 0xFFF)
            self.gen, self.key, self.start = self.gen_shout, KIND_KEY[self.kind], self.begin_shout
            hist = ({"gen": self.gen_charge, "key": SHOUT_CHARGE, "start": self.begin_charge}
                    if self.kind == "long" else self.prev)
        active = not charge and now - self.t0 < self.seconds
        shout_age = now - self.t0 - (CHARGE_SECONDS if self.kind == "long" else 0.0)
        flag = 1 if active and shout_age < FLAG_SECONDS[self.kind] else self._orig_flag
        typ = KIND_TYPE[self.kind] if active and shout_age < TYPE_SECONDS else self._orig_type
        out = anim_pose.write(body, gen=self.gen, key=self.key, start=self.start, mode=0, code90=7,
                              h_gen=hist["gen"], h_key=hist["key"], h_start=(hist["start"] or 0) >> 3)
        return _patch_values(out, {FLAG_TOKEN: flag, TYPE_TOKEN: typ})

    def stamp(self):
        """事件结束后作为“上一个动画”的记录（下一段连叫/动画写历史用）。"""
        return {"gen": self.gen, "key": self.key, "start": self.start or 0}
