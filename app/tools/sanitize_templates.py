# -*- coding: utf-8 -*-
"""动作模板脱敏：把 keepcandle/social 模板帧里的“号本身”字段清零。

背景
====
`keepcandle_template.json`、`social_template.json`、`social_templates.json` 里的
idle/candle/before/at 都是从真人抓包里取出来的整帧 Actor（hex）。这些帧带有
录制那个号的身份字段（uuid/账号绑定/装扮解锁等）。随包分发前要把它们去掉。

做法
====
用 `synth_capture.IDENTITY`（16 组）∪ `synth_identity.json` 的 identity（9 组）
作为“身份组”清单：按 Actor wire 的字段标签 + 第几次出现定位到 token，把值清零
（bits → 0，raw → 等长全 0）。token 的种类和宽度不变，所以帧形状不变；写回后
用 inspect_actor_wire(require_canonical_writer=True) 复核，并要求 reencode 与
写出的字节完全一致（round-trip）。任何一组对不上就报错退出，不猜。

用法
====
  python tools/sanitize_templates.py --check  <file.json> [...]
  python tools/sanitize_templates.py --out DIR <file.json> [...]
  python tools/sanitize_templates.py --inplace <file.json> [...]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import replace
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP))
sys.path.insert(0, str(APP / "bot"))

from qfsky.long0167.actor_codec.actor_wire_0167 import inspect_actor_wire, reencode_actor_wire  # noqa: E402
from qfsky.long0167.actor_codec.bit_stream import BitWriter  # noqa: E402
from qfsky.join0167_profile import NATIVE_SHA256  # noqa: E402

FRAME_KEYS = ("idle", "candle", "before", "at", "template", "frame")


def identity_groups() -> list[str]:
    """身份组清单：synth_capture.IDENTITY（16）+ synth_identity.json identity（9）。"""
    groups: list[str] = []
    try:
        import synth_capture
        groups.extend(synth_capture.IDENTITY)
    except Exception:
        pass
    p = APP / "bot" / "synth_identity.json"
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
        groups.extend(doc.get("identity") or [])
    except (OSError, ValueError):
        pass
    seen, out = set(), []
    for g in groups:
        if g not in seen:
            seen.add(g)
            out.append(g)
    if not out:
        raise SystemExit("读不到身份组清单（synth_capture.IDENTITY / synth_identity.json）")
    return out


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


def _write(toks):
    w = BitWriter(max_bytes=0x2420)
    for t in toks:
        w.write_raw(bytes(t.value)) if t.kind == "raw" else w.write_bits(t.value, t.extent)
    return w.to_bytes()


def _parses(toks) -> bool:
    try:
        inspect_actor_wire(_write(toks), verified_profile_sha256=NATIVE_SHA256, check_zero_padding=False)
        return True
    except ValueError:
        return False


def sanitize_frame(raw: bytes, groups) -> tuple[bytes, int, list[str]]:
    """清零身份组里能安全清零的 token，返回 (新字节, 清零数, 保留的结构字段)。

    有些组里混着长度/计数字段（例如 0x21df444 是一张 6 条的记录表，里面有
    字符串长度 0x0c）。把这些也清零会让整帧解析不过（unexpected trailer），
    所以逐 token 试：清零后还能被 inspect_actor_wire 完整解析才保留改动，
    否则恢复原值并记进 kept。这样形状一定不变，也不会为了“全零”硬改结构。
    """
    o = inspect_actor_wire(raw, verified_profile_sha256=NATIVE_SHA256, check_zero_padding=False)
    idx = _index(o)
    toks = list(o._tokens)
    hit = 0
    kept: list[str] = []
    for key in groups:
        for i in idx.get(key, []):
            t = toks[i]
            toks[i] = (replace(t, value=b"\x00" * t.extent) if t.kind == "raw"
                       else replace(t, value=0))
            if _parses(toks):
                hit += 1
            else:
                toks[i] = t
                kept.append(f"{key}#{i}")
    out = _write(toks)
    chk = inspect_actor_wire(out, verified_profile_sha256=NATIVE_SHA256, check_zero_padding=False)
    if reencode_actor_wire(chk, verified_profile_sha256=NATIVE_SHA256) != out:
        raise ValueError("sanitized_roundtrip")
    # 形状必须一致（种类/宽度）
    if len(chk._tokens) != len(o._tokens):
        raise ValueError("sanitized_shape")
    for a, b in zip(o._tokens, chk._tokens):
        if (a.kind, a.extent) != (b.kind, b.extent):
            raise ValueError("sanitized_shape")
    return out, hit, kept


def sanitize_doc(doc: dict) -> tuple[dict, dict]:
    out = dict(doc)
    report = {}
    kept_all = {}
    for key, value in list(doc.items()):
        if key in FRAME_KEYS and isinstance(value, str) and len(value) >= 64:
            try:
                raw = bytes.fromhex(value)
            except ValueError:
                continue
            clean, hit, kept = sanitize_frame(raw, GROUPS)
            out[key] = clean.hex()
            report[key] = hit
            if kept:
                kept_all[key] = kept
        elif isinstance(value, dict):                       # social_templates 的 hold/carry 两层
            sub_out = dict(value)
            changed = False
            for sub, frame in list(value.items()):
                if sub in FRAME_KEYS and isinstance(frame, str) and len(frame) >= 64:
                    try:
                        raw = bytes.fromhex(frame)
                    except ValueError:
                        continue
                    clean, hit, kept = sanitize_frame(raw, GROUPS)
                    sub_out[sub] = clean.hex()
                    report[f"{key}.{sub}"] = hit
                    if kept:
                        kept_all[f"{key}.{sub}"] = kept
                    changed = True
            if changed:
                out[key] = sub_out
    if "source" in out:
        out["source"] = "<removed: capture path>"
    out["sanitized"] = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "identity_groups": len(GROUPS),
                        "zeroed_tokens": sum(report.values()),
                        "structural_tokens_kept": {k: len(v) for k, v in kept_all.items()},
                        "tool": "tools/sanitize_templates.py"}
    return out, report


GROUPS = identity_groups()


def main(argv=None):
    ap = argparse.ArgumentParser(description="动作模板身份字段清零")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true", help="只检查（会真的解析每一帧）")
    g.add_argument("--inplace", action="store_true", help="就地改写")
    g.add_argument("--out", metavar="DIR", help="写到目录")
    ap.add_argument("files", nargs="+")
    a = ap.parse_args(argv)
    rc = 0
    for name in a.files:
        p = Path(name)
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"FAIL {p}: {exc}")
            rc = 1
            continue
        try:
            clean, report = sanitize_doc(doc)
        except (ValueError, KeyError) as exc:
            print(f"FAIL {p}: {exc}")
            rc = 1
            continue
        if a.check:
            print(f"OK   {p}: 清零 {sum(report.values())} 个 token，帧 {sorted(report)}")
            continue
        dst = p if a.inplace else Path(a.out) / p.name
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(json.dumps(clean, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"WROTE {dst}: 清零 {sum(report.values())} 个 token，帧 {sorted(report)}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
