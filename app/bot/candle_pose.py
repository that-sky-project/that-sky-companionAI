"""把 MuMu“对人举蜡烛”的姿态移植到小光自己的 Actor 种子上，生成 KeepCandle 版本的种子。

做法：对 MuMu 待机帧(I)与举烛帧(C)做 token 级比对得到编辑脚本，再套到小光种子(S)上：
  - 插入：照搬（举烛新增的字段，如“有目标坐标”后的坐标、0x21ddc30/0x21ddc40 子结构）
  - 替换：仅当 S 与 I 在该 token 相同（说明是固定字段、纯因举烛而变）才套用；
          时钟、坐标这类 S 与 I 本来就不同的 token 自动跳过
最后用位写入器重写、再用 inspect_actor_wire 解析一遍，确认结构合法。
"""
import xg_paths
import difflib
import json
import os
import struct
import sys

sys.path.insert(0, str(xg_paths.APP))
try:                     # 服务器上抓包分析用的辅助模块；安装版不带，只有从抓包重建模板时才需要
    import actor_diff as D  # noqa: E402
except ImportError:       # 模板文件已经提取好时完全用不到它
    D = None
from qfsky.long0167.actor_codec.actor_wire_0167 import inspect_actor_wire  # noqa: E402
from qfsky.long0167.actor_codec.actor_wire_schema_0167 import PUBLIC_ELF_SHA256 as SHA  # noqa: E402
from qfsky.long0167.actor_codec.bit_stream import BitWriter  # noqa: E402

CAP = str(xg_paths.CAPTURES / "gamemsg-1030-1516.jsonl")
MUMU, IDLE_AT, CANDLE_AT = 0x4d, "15:37:30", "15:37:40"
MUMU_TARGET_SLOT = 0x49


def tokens(body):
    return list(inspect_actor_wire(body, verified_profile_sha256=SHA, check_zero_padding=False)._tokens)


def labels(body):
    """每个 token 所属的字段标签（最近的边界标签），用于结构对齐。"""
    o = inspect_actor_wire(body, verified_profile_sha256=SHA, check_zero_padding=False)
    out, bit, j, cur = [], 0, 0, "start"
    bl = sorted(o._boundaries, key=lambda x: x[1])
    for t in o._tokens:
        if t.kind == "raw":
            bit = (bit + 7) & ~7
        while j < len(bl) and bl[j][1] <= bit:
            cur = bl[j][0]
            j += 1
        out.append(cur)
        bit += t.extent * 8 if t.kind == "raw" else t.extent
    return out


def sig(t):
    return (t.kind, t.extent, bytes(t.value) if isinstance(t.value, (bytes, bytearray)) else t.value)


def write(toks):
    w = BitWriter(max_bytes=0x2420)
    for t in toks:
        if t.kind == "raw":
            w.write_raw(bytes(t.value))
        else:
            w.write_bits(t.value, t.extent)
    return w.to_bytes()


def edit_script(idle, candle):
    sm = difflib.SequenceMatcher(a=[sig(t) for t in idle], b=[sig(t) for t in candle], autojunk=False)
    return [op for op in sm.get_opcodes() if op[0] != "equal"]


def idle_to_seed(seed, idle, seed_labels=None, idle_labels=None):
    """按 token 结构把 idle 的下标映射到 seed 下标；idle 独有的 token（如附近玩家列表项）映射为 None。
    带上字段标签对齐（墓土种子多出地图专属结构，只按类型/宽度对齐会错位，生成非法 Actor）。"""
    la = idle_labels or [""] * len(idle)
    lb = seed_labels or [""] * len(seed)
    sm = difflib.SequenceMatcher(a=[(l, t.kind, t.extent) for l, t in zip(la, idle)],
                                 b=[(l, t.kind, t.extent) for l, t in zip(lb, seed)], autojunk=False)
    m = [None] * (len(idle) + 1)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal" or (tag == "replace" and i2 - i1 == j2 - j1):
            for k in range(i2 - i1):
                m[i1 + k] = j1 + k
    m[len(idle)] = len(seed)
    return m


def _seed_pos(m, i):
    """idle 下标 i 处（或其后最近的已映射位置）对应的 seed 插入点。"""
    while m[i] is None:
        i += 1
    return m[i]


def transplant(seed, idle, candle, ops, seed_labels=None, idle_labels=None):
    """返回 (新 token 列表, 应用记录)。编辑脚本在 idle 坐标上，先换算到 seed 坐标再套用。"""
    m = idle_to_seed(seed, idle, seed_labels, idle_labels)
    out, log, pos = [], [], 0
    for tag, i1, i2, j1, j2 in ops:
        s1 = _seed_pos(m, i1)
        out.extend(seed[pos:s1])
        if tag == "insert":
            out.extend(candle[j1:j2])
            log.append(("insert", s1, [sig(t) for t in candle[j1:j2]]))
            pos = s1
            continue
        if tag == "replace" and i2 - i1 == j2 - j1:
            for k in range(i2 - i1):
                si = m[i1 + k]
                if si is None:                 # idle 独有的环境 token：不移植
                    continue
                s, a, c = seed[si], idle[i1 + k], candle[j1 + k]
                if sig(s) == sig(a):          # 固定字段：跟着举烛改
                    out.append(c)
                    log.append(("set", si, sig(a), sig(c)))
                else:                          # 时钟/坐标等：保留小光自己的
                    out.append(s)
        elif tag == "replace":                 # 长度变化的替换：结构性改动，整段采用举烛版
            out.extend(candle[j1:j2])
            log.append(("restructure", s1, [sig(t) for t in candle[j1:j2]]))
        elif tag == "delete":
            log.append(("delete", s1))
        pos = _seed_pos(m, i2)
    out.extend(seed[pos:])
    return out, log


# 举烛时上半身/手的姿态向量（世界坐标，随朝向转）。旧 token 级移植把两份姿态块拼错（第二份成了
# (129, 78, 113) 这种混合值），客户端在两份之间来回切 → “举烛时上半身一直乱转”。现在两份都取模板，再由
# room_pose 按“朝向目标的角度 − 模板朝向”整体旋转。
UPPER_BODY = ("0x1716360", "0x171666c", "0x1716770")


def template_heading():
    """MuMu 举烛模板录制时的朝向（度）：举烛帧偏航码 → heading。"""
    import anim_pose
    _idle, cand, _il, cl = _template()
    yaw = next(t.value for t, l in zip(cand, cl) if l == "0x21dd1dc" and t.kind == "bits" and t.extent == 6)
    return anim_pose.yaw_heading(yaw)


def fields(toks, labs):
    """按字段切分：[(键 (标签, 第几次出现), [token…])]。连续同标签的 token 归同一字段。"""
    out, occ = [], {}
    for t, l in zip(toks, labs):
        if out and out[-1][2] == l and out[-1][3]:
            out[-1][1].append(t)
            continue
        occ[l] = occ.get(l, 0) + 1
        out.append([(l, occ[l]), [t], l, True])
    return [(k, ts) for k, ts, _l, _o in out]


def _shape(ts):
    return [(t.kind, t.extent) for t in ts]


def transplant_fields(seed, seed_labels, idle, idle_labels, candle, candle_labels, only=None):
    """字段级移植（2026-10-30）：token 级对齐在霞谷/墓土种子上会错位（种子有地图专属结构）→ 非法 Actor、掉线。
    规则：举烛没改的字段用种子；种子与待机一致而举烛改了的固定字段、或举烛改了结构的字段用举烛版；
    举烛新增字段插入；种子独有字段原样保留。日志格式与 transplant 相同（供 build 找目标 slot/坐标）。
    only：只移植这些标签的字段（其余一律用种子的、模板新增的也不插），给 social_pose 用。"""
    sf = fields(seed, seed_labels)
    idf = dict(fields(idle, idle_labels))
    upper = {}                                                     # 举烛上半身姿态：两份姿态块都用模板的
    cf = fields(candle, candle_labels)
    sm = difflib.SequenceMatcher(a=[k for k, _ in sf], b=[k for k, _ in cf], autojunk=False)
    out, log = [], []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for (k, s), (_k, c) in zip(sf[i1:i2], cf[j1:j2]):
                i = idf.get(k)
                if only is not None and k[0] not in only:
                    out.extend(s)
                elif only is None and k[0] in UPPER_BODY and _shape(s) == _shape(c):
                    log.append(("upper", len(out), k))
                    out.extend(c)                                  # 举烛的上半身：用模板（之后按朝向旋转）
                elif i is None or [sig(t) for t in i] == [sig(t) for t in c]:
                    out.extend(s)                                  # 举烛没动这个字段
                elif _shape(i) != _shape(c) or _shape(s) != _shape(c):
                    log.append(("restructure", len(out), [sig(t) for t in c]))
                    out.extend(c)                                  # 结构变了：整段用举烛版
                else:
                    for st, it, ct in zip(s, i, c):
                        if sig(it) != sig(ct) and sig(st) == sig(it):
                            log.append(("set", len(out), sig(it), sig(ct)))
                            out.append(ct)                         # 固定值：跟着举烛改
                        else:
                            out.append(st)                         # 时钟/坐标等：保留种子
            continue
        for k, s in sf[i1:i2]:                                     # 种子独有字段：保留
            out.extend(s)
        for k, c in cf[j1:j2]:
            if only is not None and k[0] not in only:
                continue
            if k not in idf:                                       # 举烛新增：插入
                log.append(("restructure", len(out), [sig(t) for t in c]))
                out.extend(c)
    return out, log


CHEST_HEIGHT = 0.45   # MuMu 样本：目标坐标 = 被举烛者坐标 + (0, 0.45, 0)
_TEMPLATE = None


TEMPLATE_FILE = os.environ.get("XG_KEEPCANDLE_TEMPLATE") or str(xg_paths.BOT_CODE / "keepcandle_template.json")


def _template():
    """MuMu 的待机帧与举烛帧：优先读提取好的模板文件，没有就从抓包里取一次并存下来。"""
    global _TEMPLATE
    if _TEMPLATE is None:
        try:
            doc = json.load(open(TEMPLATE_FILE))
            idle_b, cand_b = bytes.fromhex(doc["idle"]), bytes.fromhex(doc["candle"])
        except FileNotFoundError:
            if D is None:
                raise FileNotFoundError("没有 keepcandle_template.json，也没有 actor_diff.py 可从抓包重建")
            _, idle_b = D.at(CAP, MUMU, IDLE_AT)
            _, cand_b = D.at(CAP, MUMU, CANDLE_AT)
            json.dump({"source": CAP, "slot": hex(MUMU), "idle_at": IDLE_AT, "candle_at": CANDLE_AT,
                       "target_slot": hex(MUMU_TARGET_SLOT), "idle": idle_b.hex(), "candle": cand_b.hex()},
                      open(TEMPLATE_FILE, "w"), indent=1)
        _TEMPLATE = (tokens(idle_b), tokens(cand_b), labels(idle_b), labels(cand_b))
    return _TEMPLATE


def build(seed_body, target_slot=None, target_origin=None):
    """生成 KeepCandle 种子。target_slot/target_origin 给定时，把目标换成该玩家（坐标自动加胸口高度）。
    结果会再解析一遍；不合法直接抛错（不能把坏 Actor 交给引擎——墓土曾因此掉线）。"""
    idle, cand, idle_labels, cand_labels = _template()
    seed = tokens(seed_body)
    new, log = transplant_fields(seed, labels(seed_body), idle, idle_labels, cand, cand_labels)
    if target_slot is not None:
        slot_idx = [row[1] for row in log if row[0] == "set" and row[3] == ("raw", 1, bytes([MUMU_TARGET_SLOT]))]
        if len(slot_idx) != 1:
            raise ValueError("candle_target_slot_token_not_unique")
        new[slot_idx[0]] = type(new[slot_idx[0]])("raw", 1, bytes([target_slot]))
    if target_origin is not None:
        x, y, z = target_origin
        packed = [struct.pack("<f", v) for v in (x, y + CHEST_HEIGHT, z)]
        # 目标坐标 = 举烛新增的那段 3 个 raw4（字段级移植时它单独成一个字段：[bool=1][raw 00] 之后）
        rows = [row[1] for row in log if row[0] == "restructure"
                and [(k, e) for k, e, _v in row[2]] == [("raw", 4)] * 3]
        if len(rows) != 1:
            raise ValueError("candle_target_origin_not_unique")
        i = rows[0]
        for k in range(3):
            new[i + k] = type(new[i + k])("raw", 4, packed[k])
    out = write(new)
    inspect_actor_wire(out, verified_profile_sha256=SHA, check_zero_padding=False)   # 不合法就在这里抛错
    return out, log


def describe(body):
    o = inspect_actor_wire(body, verified_profile_sha256=SHA, check_zero_padding=False)
    labels = [l for l, _ in o._boundaries]
    origin = None
    for l, bit in o._boundaries:
        if l == "0x21dd054":
            byte = (bit + 7) // 8
            origin = struct.unpack_from("<3f", body, byte)
            break
    return {"bytes": len(body), "bits": o.bit_length, "has_0x21ddc30": "0x21ddc30" in labels,
            "has_0x21ddc40": "0x21ddc40" in labels, "origin": origin}
