"""换号只录一张（2026-10-31）：用别的号录好的某张图当模板，把角色数据里“属于号本身”的字段换成新号的，
合成新号在这张图的录制。地图部分（scene/cache）两个号之间只差录制时间戳，直接沿用模板。

依据（synth_ident.py 对比老号 4 张、新号 23 张）：角色 262 组标签里，号本身的只有 IDENTITY 这些；
位置/朝向/动作/姿态等随图变的沿用模板（就是模板那个号站在那张图的状态），再用 social_pose.stand 归成站立。

  synth_capture.py make <模板录制目录> <新号任一真录目录> <新号 uuid> <输出目录名>
"""
import datetime
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

import xg_paths

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(xg_paths.APP))

# 用户数据根：服务器默认就是代码根，安装版由启动器设 XG_DATA_DIR。
ROOT = xg_paths.DATA
sys.path.insert(0, str(xg_paths.QFSKY_CODE.parent))
try:
    import actor_diff  # noqa: F401  服务器上导入时把 qfsky 加进路径（原来经 candle_pose 间接导入）
except ImportError:
    pass
from qfsky.long0167.actor_codec.actor_wire_0167 import inspect_actor_wire, reencode_actor_wire  # noqa: E402
from qfsky.join0167_profile import NATIVE_SHA256, account_binding  # noqa: E402
PROFILES = xg_paths.CURRENT_PROFILES
# 号本身：两号各自恒定且不同的 9 组 + 新号恒定、老号录制间有变化的 7 组（装扮/解锁会随时间变）
IDENTITY = ["0x1f80cd8|1", "0x1f80ce4|1", "0x21dd11c|1", "0x21dd8dc|1", "0x21dd924|1", "0x21dd948|1",
            "0x21dea98|1", "0x21dec48|1", "0x21defa4|1",
            "0x1f8009c|1", "0x1f80d68|1", "0x1f80d7c|1", "0x21dd8ec|1", "0x21de8b4|1", "0x21de8e8|1", "0x21dea24|1"]


def _index(o):
    starts, bit = [], 0
    for t in o._tokens:
        if t.kind == "raw":
            bit = (bit + 7) & ~7
        starts.append(bit)
        bit += t.extent * 8 if t.kind == "raw" else t.extent
    bl = sorted(o._boundaries, key=lambda x: x[1])
    idx, j, cur, occ = {}, 0, None, {}
    for i, s in enumerate(starts):
        while j < len(bl) and bl[j][1] <= s:
            cur = bl[j][0]
            occ[cur] = occ.get(cur, 0) + 1
            j += 1
        idx.setdefault(f"{cur}|{occ.get(cur, 0)}", []).append(i)
    return idx


def actor_with_identity(template, donor):
    """模板角色数据里 IDENTITY 各组换成 donor 的值（形状必须一致，不一致就报错，不猜）。"""
    from dataclasses import replace
    from qfsky.long0167.actor_codec.bit_stream import BitWriter
    ot = inspect_actor_wire(template, verified_profile_sha256=NATIVE_SHA256, check_zero_padding=False)
    od = inspect_actor_wire(donor, verified_profile_sha256=NATIVE_SHA256, check_zero_padding=False)
    it, idd = _index(ot), _index(od)
    toks = list(ot._tokens)
    for key in IDENTITY:
        a, b = it.get(key), idd.get(key)
        if a is None or b is None or len(a) != len(b):
            raise ValueError(f"synth_identity_shape {key}")
        for i, k in zip(a, b):
            t, s = toks[i], od._tokens[k]
            if (t.kind, t.extent) != (s.kind, s.extent):
                raise ValueError(f"synth_identity_shape {key}")
            toks[i] = replace(t, value=s.value)
    w = BitWriter(max_bytes=0x2420)
    for t in toks:
        w.write_raw(bytes(t.value)) if t.kind == "raw" else w.write_bits(t.value, t.extent)
    out = w.to_bytes()
    chk = inspect_actor_wire(out, verified_profile_sha256=NATIVE_SHA256, check_zero_padding=True,
                             require_canonical_writer=True)
    if reencode_actor_wire(chk, verified_profile_sha256=NATIVE_SHA256) != out:
        raise ValueError("synth_actor_roundtrip")
    return out


def make(template_dir, donor_dir, uuid, out_name, stand=True):
    t, dn = PROFILES / template_dir, PROFILES / donor_dir
    out = PROFILES / out_name
    if out.exists():
        raise ValueError("输出目录已存在")
    rec = json.loads((t / "record.json").read_text())
    actor = actor_with_identity((t / "actor.bin").read_bytes(), (dn / "actor.bin").read_bytes())
    if stand:
        import social_pose  # 延迟导入：只做目录合成时不依赖姿态模板
        actor = social_pose.stand(actor)
    obs = inspect_actor_wire(actor, verified_profile_sha256=NATIVE_SHA256, check_zero_padding=True)
    timed = dict(obs._counts).get("timed_raw9_records", 0)
    out.mkdir()
    shutil.copyfile(t / "cache.bin", out / "cache.bin")
    shutil.copyfile(t / "scene.bin", out / "scene.bin")
    (out / "actor.bin").write_bytes(actor)
    shutil.copyfile(t / "capture_evidence.json", out / "capture_evidence.json")
    rec["account_binding"] = dict(rec["account_binding"], value=account_binding(uuid))
    rec["files"] = {n: {"size": len((out / n).read_bytes()), "sha256": hashlib.sha256((out / n).read_bytes()).hexdigest()}
                    for n in ("cache.bin", "scene.bin", "actor.bin")}
    rec["checks"] = dict(rec["checks"], actor_bytes=len(actor), actor_timed_record_count=timed)
    rec["synthesized"] = {"template": template_dir, "identity_from": donor_dir,
                          "at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    (out / "record.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1))
    return out


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["make"] and len(a) == 5:
        print(make(*a[1:]))
    else:
        print(__doc__)
