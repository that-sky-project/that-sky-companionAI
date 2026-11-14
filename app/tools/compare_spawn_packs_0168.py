# -*- coding: utf-8 -*-
"""比对 0.16.7 / 0.16.8 两份落点包，证明坐标与标注逐项一致。

用法（app 根目录）：
  python tools/compare_spawn_packs_0168.py
  XG_T33_CATALOG_INPUTS=<0.16.8 catalog-inputs> python tools/compare_spawn_packs_0168.py

只读本地 JSON；不联网、不写业务数据。退出码 0 = 通过。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

from xg_gamedata import spawn_loader  # noqa: E402

COORD_KEYS = ("scene", "map_id", "wire_position", "wire_position_float32",
              "feet_position", "heading")
EVIDENCE_KEYS = {
    "native_sha256", "apk_archive_directory_sha256", "apk_build", "source_member",
    "source_member_sha256", "apk_member_sha256", "apk_member_matches_scene_provenance",
    "entry_object_index", "transform_object_index",
}
NESTED_EVIDENCE = {
    ("native_spawn", "object_index"), ("native_spawn", "marker_index"),
    ("placementBoundary", "object_index"), ("portal_clearance", "nearest_portal_object_index"),
}


def strip_evidence(value, under_provenance=False, parent=None):
    if isinstance(value, dict):
        out = {}
        for key, child in value.items():
            if under_provenance and key in EVIDENCE_KEYS:
                continue
            if under_provenance and (parent, key) in NESTED_EVIDENCE:
                continue
            out[key] = strip_evidence(child, under_provenance or key == "provenance", key)
        return out
    if isinstance(value, list):
        return [strip_evidence(child, under_provenance, parent) for child in value]
    return value


def main():
    old = spawn_loader.load(build=421589)
    new = spawn_loader.load(build=421629)
    report = {
        "old_pack": str(spawn_loader.pack_path(421589)),
        "new_pack": str(spawn_loader.pack_path(421629)),
        "scenes": 0, "variants": 0,
        "coordinate_mismatches": [], "annotation_mismatches": [],
        "non_evidence_mismatches": [],
        "source_member_sha_changed": [], "object_index_changed": [],
        "catalog_inputs_member_mismatches": [],
    }
    for scene, a in sorted(old["placements"].items()):
        b = new["placements"].get(scene)
        if b is None:
            report["non_evidence_mismatches"].append([scene, "missing_in_new"])
            continue
        for key in COORD_KEYS:
            if a.get(key) != b.get(key):
                report["coordinate_mismatches"].append([scene, key])
        aprov, bprov = a.get("provenance") or {}, b.get("provenance") or {}
        if aprov.get("annotation") != bprov.get("annotation"):
            report["annotation_mismatches"].append(scene)
        if strip_evidence(a) != strip_evidence(b):
            report["non_evidence_mismatches"].append([scene, "non_evidence"])
        if aprov.get("source_member_sha256") != bprov.get("source_member_sha256"):
            report["source_member_sha_changed"].append(scene)
        old_obj = (aprov.get("entry_object_index"), aprov.get("transform_object_index"))
        new_obj = (bprov.get("entry_object_index"), bprov.get("transform_object_index"))
        if old_obj != new_obj:
            report["object_index_changed"].append([scene, old_obj, new_obj])
        report["scenes"] += 1
    for scene, rows in old.get("variants", {}).items():
        for name, a in sorted(rows.items()):
            b = ((new.get("variants") or {}).get(scene) or {}).get(name)
            if b is None:
                report["non_evidence_mismatches"].append([scene + "/" + name, "missing_in_new"])
                continue
            for key in COORD_KEYS:
                if a.get(key) != b.get(key):
                    report["coordinate_mismatches"].append([scene + "/" + name, key])
            if (a.get("provenance") or {}).get("annotation") != (b.get("provenance") or {}).get("annotation"):
                report["annotation_mismatches"].append(scene + "/" + name)
            if strip_evidence(a) != strip_evidence(b):
                report["non_evidence_mismatches"].append([scene + "/" + name, "non_evidence"])
            report["variants"] += 1
    folder = os.environ.get("XG_T33_CATALOG_INPUTS")
    if folder:
        inputs = Path(folder)
        for scene, b in sorted(new["placements"].items()):
            proof = json.loads((inputs / (scene + ".json")).read_text(encoding="utf-8"))
            actual = (proof.get("provenance") or {}).get("scene_member_sha256")
            if (b.get("provenance") or {}).get("source_member_sha256") != actual:
                report["catalog_inputs_member_mismatches"].append(scene)
    passed = not any(report[key] for key in (
        "coordinate_mismatches", "annotation_mismatches", "non_evidence_mismatches",
        "catalog_inputs_member_mismatches"))
    report["passed"] = passed
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
