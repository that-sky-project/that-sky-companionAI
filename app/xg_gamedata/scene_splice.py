# -*- coding: utf-8 -*-
"""Scene MPVar 段拼接（好友传送必需）：把本号录制 scene 的 switches 段起接到目录 scene 上。

与服务器 ``fj_all.py`` 的 ``splice_scene`` 同一做法：用 scene_wire walker 的
boundary 标签定位 ``switches``，保留目录段之前的所有 token，之后整段用录制替换。
"""
from __future__ import annotations

import json
from pathlib import Path

from .common import GameDataError, canonical_json, sha256_bytes

_READY = False


def _install_walker():
    global _READY
    if _READY:
        return
    from qfsky.long0167.codec import scene_wire as sw
    W = sw._Walk
    original_init = W.__init__
    original_bound = W.boundary

    def _init(self, *args):
        original_init(self, *args)
        self.cur = "start"
        me = self

        class _Labelled(list):
            def append(self, token):
                list.append(self, token)
                me.labels.append(me.cur)

        self.labels = []
        self.tokens = _Labelled()

    def _bound(self, label):
        original_bound(self, label)
        self.cur = label

    W.__init__ = _init
    W.boundary = _bound
    _READY = True


def _labelled(data: bytes):
    _install_walker()
    from qfsky.long0167.codec import scene_wire as sw
    holder = {}
    original = sw._Walk.__init__

    def grab(self, *args):
        original(self, *args)
        holder["w"] = self

    sw._Walk.__init__ = grab
    try:
        obs = sw.inspect_scene_wire(data, verified_profile_sha256=sw.PUBLIC_ELF_SHA256_0167)
    finally:
        sw._Walk.__init__ = original
    walker = holder["w"]
    return obs, list(walker.tokens), list(walker.labels)


def splice(catalog_wire: bytes, capture_wire: bytes) -> tuple[bytes, object]:
    from qfsky.long0167.codec import scene_wire as sw
    from qfsky.long0167.codec.bit_stream import BitWriter
    _obs_a, tokens_a, labels_a = _labelled(catalog_wire)
    _obs_b, tokens_b, labels_b = _labelled(capture_wire)
    if "switches" not in labels_a or "switches" not in labels_b:
        raise GameDataError("scene 里没有 switches 段，不能拼 MPVar", "mpvar_no_switches")
    ia = labels_a.index("switches")
    ib = labels_b.index("switches")
    tokens = tokens_a[:ia] + tokens_b[ib:]
    writer = BitWriter(max_bytes=sw.MAX_SCENE_WIRE_BYTES)
    for token in tokens:
        if token.kind == "raw":
            writer.write_raw(token.value)
        else:
            writer.write_bits(token.value, token.extent)
    out = writer.to_bytes()
    obs = sw.inspect_scene_wire(out, verified_profile_sha256=sw.PUBLIC_ELF_SHA256_0167,
                                check_zero_padding=True)
    if sw.reencode_scene_wire(obs) != out:
        raise GameDataError("MPVar 拼接后不是 canonical scene wire", "mpvar_canonical")
    return out, obs


def apply_to_proof(proof: dict, wire: bytes, cache: bytes, obs, record_dir: str,
                   capture_wire_sha: str) -> tuple[dict, bytes, bytes]:
    """更新 proof 的 artifacts/counts，并记录 mpvar_splice 来源。"""
    proof = json.loads(json.dumps(proof))
    proof.setdefault("artifacts", {})
    proof["artifacts"]["wire"] = {"file": "scene.bin", "sha256": sha256_bytes(wire), "bytes": len(wire)}
    proof["artifacts"]["cache"] = {"file": "cache.bin", "sha256": sha256_bytes(cache), "bytes": len(cache)}
    proof["wire_variable_group_counts"] = dict(obs._counts)
    proof.setdefault("serializer", {})["serialized_length"] = len(wire)
    missing = [m for m in proof.get("missing_scene_inputs") or [] if "MPVar" not in m]
    missing.append("non-finite object initialization (MPVar/switches spliced from recorded capture)")
    proof["missing_scene_inputs"] = missing
    proof["mpvar_splice"] = {
        "source": "recorded-capture-scene-tail",
        "record_dir": record_dir,
        "from_boundary": "switches",
        "capture_scene_sha256": capture_wire_sha,
    }
    return proof, canonical_json(proof), cache
