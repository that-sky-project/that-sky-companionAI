"""T33：0.16.8 落点包选择 / 坐标标注一致性 / v32c placement 校验。

这些用例只读本地落点包，不连游戏、不读真实账号目录。
可选：设置 XG_T33_CATALOG_INPUTS 指向一份 0.16.8 catalog-inputs，
额外核对落点包的 source_member_sha256 与现场 APK 成员证据一致。
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
sys.path[:0] = [str(APP / "escort-v31-server"), str(APP)]

from xg_gamedata import catalog, spawn_loader  # noqa: E402
from xg_gamedata.common import GameDataError  # noqa: E402
import qfsky  # noqa: E402
import qfsky.map_long0167  # noqa: E402

# unittest discover 可能已先导入 app/qfsky（没有 overlay 的 extend_path 结果）；
# 这里显式把 escort-v31-server 的 overlay 子包路径补进去，测试进程内也能找到
# asset_bootstrap_v32 / asset_catalog_v32c / catalog_account_pins。
for _pkg, _path in ((qfsky, APP / "escort-v31-server" / "qfsky"),
                    (qfsky.map_long0167, APP / "escort-v31-server" / "qfsky" / "map_long0167")):
    if str(_path) not in _pkg.__path__:
        _pkg.__path__.append(str(_path))

from qfsky.map_long0167 import asset_bootstrap_v32 as B  # noqa: E402
from qfsky.map_long0167 import asset_catalog_v32c as C  # noqa: E402
from qfsky.version_profile import (LEGACY_APK_DIRECTORY_SHA256, LEGACY_BUILD,  # noqa: E402
                                   LEGACY_NATIVE_SHA256, PROFILES)

# 版本/源证据字段：坐标与标注不在这里面。新旧包只允许这些字段不同。
EVIDENCE_KEYS = {
    "native_sha256",
    "apk_archive_directory_sha256",
    "apk_build",
    "source_member",
    "source_member_sha256",
    "apk_member_sha256",
    "apk_member_matches_scene_provenance",
    "entry_object_index",
    "transform_object_index",
}
NESTED_EVIDENCE = {
    ("native_spawn", "object_index"),
    ("native_spawn", "marker_index"),
    ("placementBoundary", "object_index"),
    ("portal_clearance", "nearest_portal_object_index"),
}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


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


def placement_entry(placement):
    return {
        "scene": placement["scene"],
        "level_id": placement["map_id"],
        "placement_sha256": hashlib.sha256(canonical(placement)).hexdigest(),
        "netversion_u16": [0, 0, 0, 0, 0, 0],
    }


class SpawnPackSelectionTests(unittest.TestCase):
    def test_0168_and_0167_choose_different_packs(self):
        current = spawn_loader.load(build=421629)
        legacy = spawn_loader.load(build=421589)
        self.assertEqual(current["apk_build"], 421629)
        self.assertEqual(current["native_sha256"], PROFILES[421629]["native_sha256"])
        self.assertIsNone(legacy.get("apk_build"))
        self.assertEqual(spawn_loader.load()["apk_build"], 421629)
        self.assertEqual(spawn_loader.pack_path(421629).name, "spawn_pack_0168.json")
        self.assertEqual(spawn_loader.pack_path(421589).name, "spawn_pack.json")
        with self.assertRaises(GameDataError):
            spawn_loader.pack_path(999999)

    def test_apk_evidence_mismatch_is_chinese_fail_closed(self):
        pack = spawn_loader.load(build=421629)
        with self.assertRaises(GameDataError) as caught:
            spawn_loader.require_apk_evidence(
                pack, native_sha256="0" * 64,
                apk_archive_directory_sha256=pack["apk_archive_directory_sha256"],
                build=421629)
        self.assertIn("不一致", caught.exception.message)
        self.assertEqual(caught.exception.code, "spawn_pack_apk_mismatch")


class SpawnPackEquivalenceTests(unittest.TestCase):
    def test_coordinates_and_annotations_are_identical(self):
        old = spawn_loader.load(build=421589)
        new = spawn_loader.load(build=421629)
        self.assertEqual(sorted(old["placements"]), sorted(new["placements"]))
        for scene in sorted(old["placements"]):
            with self.subTest(scene=scene):
                a, b = old["placements"][scene], new["placements"][scene]
                for key in ("scene", "map_id", "wire_position", "wire_position_float32",
                            "feet_position", "heading"):
                    self.assertEqual(a.get(key), b.get(key), key)
                self.assertEqual((a.get("provenance") or {}).get("annotation"),
                                 (b.get("provenance") or {}).get("annotation"))
                self.assertEqual(strip_evidence(a), strip_evidence(b))
        for scene, rows in old["variants"].items():
            for name in rows:
                with self.subTest(scene=scene + "/" + name):
                    self.assertEqual(strip_evidence(rows[name]),
                                     strip_evidence(new["variants"][scene][name]))


class PlacementValidationTests(unittest.TestCase):
    def test_0168_pack_all_placements_pass_v32c(self):
        pack = spawn_loader.load(build=421629)
        count = 0
        for scene, placement in sorted(pack["placements"].items()):
            with self.subTest(scene=scene):
                C._placement(placement, placement_entry(placement))
                count += 1
        for scene, rows in pack["variants"].items():
            for name, placement in rows.items():
                with self.subTest(scene=scene + "/" + name):
                    C._placement(placement, placement_entry(placement))
                    count += 1
        self.assertEqual(count, 116)

    def test_0168_pack_rejected_with_legacy_constants(self):
        pack = spawn_loader.load(build=421629)
        saved = (C.NATIVE_SHA256, B.APK_DIRECTORY_SHA)
        C.NATIVE_SHA256 = LEGACY_NATIVE_SHA256
        B.APK_DIRECTORY_SHA = LEGACY_APK_DIRECTORY_SHA256
        try:
            with self.assertRaises(ValueError):
                C._placement(pack["placements"]["AR_Bedroom"],
                             placement_entry(pack["placements"]["AR_Bedroom"]))
        finally:
            C.NATIVE_SHA256, B.APK_DIRECTORY_SHA = saved

    def test_0167_pack_all_placements_pass_with_legacy_profile(self):
        pack = spawn_loader.load(build=LEGACY_BUILD)
        saved = (C.NATIVE_SHA256, B.APK_DIRECTORY_SHA)
        C.NATIVE_SHA256 = LEGACY_NATIVE_SHA256
        B.APK_DIRECTORY_SHA = LEGACY_APK_DIRECTORY_SHA256
        try:
            for scene, placement in sorted(pack["placements"].items()):
                with self.subTest(scene=scene):
                    C._placement(placement, placement_entry(placement))
            self.assertEqual(len(pack["placements"]), 115)
        finally:
            C.NATIVE_SHA256, B.APK_DIRECTORY_SHA = saved


class SceneMemberEvidenceTests(unittest.TestCase):
    def test_source_member_sha_matches_scene_proof_when_fixture_available(self):
        folder = os.environ.get("XG_T33_CATALOG_INPUTS")
        if not folder:
            self.skipTest("XG_T33_CATALOG_INPUTS 未设置")
        inputs = Path(folder)
        pack = spawn_loader.load(build=421629)
        checked = 0
        for scene, placement in sorted(pack["placements"].items()):
            with self.subTest(scene=scene):
                proof = json.loads((inputs / (scene + ".json")).read_text(encoding="utf-8"))
                catalog._placement_evidence(proof, placement, PROFILES[421629])
                checked += 1
        for scene, rows in pack["variants"].items():
            proof = json.loads((inputs / (scene + ".json")).read_text(encoding="utf-8"))
            for name, placement in rows.items():
                with self.subTest(scene=scene + "/" + name):
                    catalog._placement_evidence(proof, placement, PROFILES[421629])
                    checked += 1
        self.assertEqual(checked, 116)


if __name__ == "__main__":
    unittest.main()
