# -*- coding: utf-8 -*-
"""0.16.8 遇境 resource tag / 登记录制回归（全部离线、临时目录）。

覆盖：
  * 421589→36851、421629→63789 只从版本档案取；
  * 0.16.8 新录制（63789）通过 scene_record / scene_candidate / engine；
  * 0.16.7 录制在 0.16.7 档案下仍通过；
  * 旧配置/旧 catalog_sources 里的 36851 条目 + 新录制 63789 → 登记成功，
    配置与目录源更新为新值，旧文件留备份。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
sys.path.insert(0, str(APP))

HOME = 3526133726
ACTOR_FIXTURE = ROOT / "tests/fixtures/actor_0168_candle.bin"


def scene_fixture(tag, profile_sha):
    """生成该 tag 下最短的零值规范 Scene wire（结构真实、内容全零）。"""
    from qfsky.long0167.codec.scene_wire import inspect_scene_wire
    lo, hi = 4, 0x2000
    while lo <= hi:
        size = (lo + hi) // 2
        blob = struct.pack("<I", tag) + bytes(size - 4)
        try:
            inspect_scene_wire(blob, verified_profile_sha256=profile_sha,
                               check_zero_padding=True)
            return blob
        except ValueError as error:
            if str(error) == "scene_unexpected_wire_trailer":
                hi = size - 1
            else:
                lo = size + 1
    raise AssertionError("synthetic scene wire length not found")


def legacy_actor_fixture(map_id=HOME):
    """按 0.16.7 layout 生成可规范回编码、可迁移的 Home Actor。"""
    from qfsky.long0167.actor_codec.legacy_actor import actor_wire as old
    from qfsky.long0167.actor_codec.legacy_actor.actor_wire_schema import (
        PUBLIC_ELF_SHA256 as OLD_SHA)
    from qfsky.long0167.actor_codec.bit_stream import BitWriter

    raw = bytearray(1320)
    struct.pack_into("<I", raw, 47, map_id)
    walk = old._Walk(bytes(raw), True, False)
    walk.main()
    tokens = list(walk.tokens)
    empty = [i for i, token in enumerate(tokens)
             if token.kind == "raw" and token.extent == 0]
    if len(empty) != 10:
        raise AssertionError("legacy actor zero fixture shape changed")
    for index in empty:
        tokens[index - 1] = replace(tokens[index - 1], value=struct.pack("<I", 1))
        tokens[index] = replace(tokens[index], extent=1, value=b"\0")
    writer = BitWriter(max_bytes=1320)
    for token in tokens:
        if token.kind == "raw":
            writer.write_raw(token.value)
        else:
            writer.write_bits(token.value, token.extent)
    body = writer.to_bytes()
    observed = old.inspect_actor_wire(body, verified_profile_sha256=OLD_SHA,
                                      check_zero_padding=True,
                                      require_canonical_writer=True)
    assert observed.matches_map_association(map_id)
    assert old.reencode_actor_wire(observed) == body
    return body


def write_json(path, doc):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, sort_keys=True), encoding="utf-8")


OWNER = "00000000-0000-4000-8000-000000000031"


def make_capture(folder, uuid=OWNER, tag=None):
    """造一份完整可加载的 0.16.8 遇境录制（Scene 结构真实、Actor 用仓库夹具）。"""
    import hashlib
    from qfsky.join0167_profile import BINDING_ALGORITHM, account_binding, NATIVE_SHA256
    from qfsky.version_profile import (CURRENT_BUILD, CURRENT_HOME_RESOURCE_TAG,
                                       CURRENT_NATIVE_SHA256, CURRENT_RESOURCE_REVISION)
    from qfsky.long0167.actor_codec.actor_wire_0167 import inspect_actor_wire

    tag = CURRENT_HOME_RESOURCE_TAG if tag is None else tag
    scene = scene_fixture(tag, CURRENT_NATIVE_SHA256)
    actor = ACTOR_FIXTURE.read_bytes()
    cache = bytearray(0x2010)
    struct.pack_into("<H", cache, 0, 25536)
    cache[2] = 3
    struct.pack_into("<I", cache, 4, 9)
    cache[8] = 4
    struct.pack_into("<HH", cache, 10, 7, 125)
    cache[14:14 + len(scene)] = scene
    struct.pack_into("<H", cache, 0x200E, len(scene))
    cache = bytes(cache)
    nv = [tag, 49307, 23536, 0, 0, 25536]
    data = {"actor.bin": actor, "scene.bin": scene, "cache.bin": cache}
    observed = inspect_actor_wire(actor, verified_profile_sha256=NATIVE_SHA256,
                                  check_zero_padding=True,
                                  require_canonical_writer=True)
    checks = {
        "scene_current_schema_checked": True,
        "actor_current_schema_checked": True,
        "scene_actor_same_account_map_revision": True,
        "paired_equal_reads": True,
        "actor_map_prefix_matches_native_level_id": True,
        "actual_current_wire_preserved": True,
        "scene_bytes": len(scene),
        "actor_bytes": len(actor),
        "actor_timed_record_count": dict(observed._counts).get("timed_raw9_records", 0),
        "join_player_fields_same_capture": True,
        "join_player_fields_paired_equal_reads": True,
    }
    record = {
        "schema": 1,
        "kind": "sky-current-scene-actor-pair",
        "classification": "private-current-account-session-state",
        "apk_build": CURRENT_BUILD,
        "native_sha256": CURRENT_NATIVE_SHA256,
        "scene": "CandleSpace",
        "resource_revision": CURRENT_RESOURCE_REVISION,
        "level_id": HOME,
        "netversion_u16": nv,
        "captured_utc": "2026-11-12T00:00:00+00:00",
        "account_binding": {"algorithm": BINDING_ALGORITHM,
                            "source": "native-join-builder-uid16",
                            "value": account_binding(uuid)},
        "files": {name: {"size": len(body), "sha256": hashlib.sha256(body).hexdigest()}
                  for name, body in data.items()},
        "checks": checks,
        "native_build_and_root_validated": True,
        "runtime_full_wire_captured": False,
        "atomic_snapshot_proven": False,
        "capture_variant": "current-profile-player-pair-v1",
        "player_fields": {"spawn_hex": bytes(16).hex(),
                          "matrix_hex": struct.pack("<16f", *([0.0] * 16)).hex(),
                          "level_flag": 0},
        "scene_observation": {
            "binding": "adjacent-native-init-lines-exact-timestamp-pid-tid",
            "scene": "CandleSpace",
            "level_id": HOME,
            "netversion_u16": nv,
        },
    }
    write_json(folder / "record.json", record)
    for name, body in data.items():
        (folder / name).write_bytes(body)
    (folder / "capture_evidence.json").write_text('{"synthetic_fixture":true}',
                                                   encoding="utf-8")
    return record


class VersionProfileTests(unittest.TestCase):
    def test_home_tag_and_netversion_are_per_build(self):
        from qfsky.version_profile import (CURRENT_BUILD, LEGACY_BUILD,
                                           home_netversion, home_resource_tag,
                                           profile_for_native_sha256)
        self.assertEqual((LEGACY_BUILD, CURRENT_BUILD), (421589, 421629))
        self.assertEqual(home_resource_tag(LEGACY_BUILD), 36851)
        self.assertEqual(home_resource_tag(421629), 63789)
        self.assertEqual(home_netversion(421589)[0], 36851)
        self.assertEqual(home_netversion(421629), (63789, 49307, 23536, 0, 0, 25536))
        self.assertEqual(profile_for_native_sha256(
            "0b81f1da54d9c6f28c08746b2ec19519d8eb78c4f1b3c5e4e420ff726dc25321")
            ["home_resource_tag"], 36851)
        self.assertEqual(profile_for_native_sha256(
            "0b18ecc44e873f6caa181e6bc49f14b7e07058888523e47db52a14b32077a7ed")
            ["home_resource_tag"], 63789)

    def test_home_tag_not_scattered_outside_version_profile(self):
        for path in (APP / "qfsky").rglob("*.py"):
            if path.name == "version_profile.py":
                continue
            text = path.read_text(encoding="utf-8-sig")
            self.assertNotIn("36851", text, path)
            self.assertNotIn("63789", text, path)

    def test_handoff_constants_come_from_profile(self):
        from qfsky import handoff0167
        from qfsky.version_profile import home_netversion, profile_for_build
        self.assertEqual(handoff0167.HOME_NETVERSION,
                         home_netversion(handoff0167.BUILD))
        self.assertEqual(handoff0167.HOME_RESOURCE,
                         profile_for_build(handoff0167.BUILD)["resource_revision"])


class SceneContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from qfsky.join0167_profile import NATIVE_SHA256
        from qfsky.long0167.codec.scene_wire import PUBLIC_ELF_SHA256_0167
        from qfsky.version_profile import CURRENT_HOME_RESOURCE_TAG, LEGACY_HOME_RESOURCE_TAG
        cls.current_scene = scene_fixture(CURRENT_HOME_RESOURCE_TAG, NATIVE_SHA256)
        cls.legacy_scene = scene_fixture(LEGACY_HOME_RESOURCE_TAG, PUBLIC_ELF_SHA256_0167)

    def record(self, scene):
        from qfsky.long0167.level_authority import LevelRecord
        return LevelRecord(0x63c0, 0, 0, 0, 5, 125, scene)

    def test_scene_record_accepts_current_tag_only_for_current_profile(self):
        from qfsky.join0167_profile import NATIVE_SHA256
        from qfsky.long0167.scene_record import _check_record
        _check_record(self.record(self.current_scene),
                      verified_profile_sha256=NATIVE_SHA256)
        with self.assertRaises(ValueError):
            _check_record(self.record(self.current_scene),
                          expected_resource_tag=36851,
                          verified_profile_sha256=NATIVE_SHA256)

    def test_scene_record_keeps_legacy_profile_working(self):
        from qfsky.long0167.codec.scene_wire import PUBLIC_ELF_SHA256_0167
        from qfsky.long0167.scene_record import _check_record
        _check_record(self.record(self.legacy_scene),
                      verified_profile_sha256=PUBLIC_ELF_SHA256_0167)
        with self.assertRaises(ValueError):
            _check_record(self.record(self.legacy_scene),
                          expected_resource_tag=63789,
                          verified_profile_sha256=PUBLIC_ELF_SHA256_0167)

    def test_scene_candidate_accepts_0_16_8_scene(self):
        from qfsky.long0167.scene_candidate import ColdSceneCandidate
        from qfsky.long0167.scope import ConnectionScope
        scope = ConnectionScope(bytes([1]) * 16, object(), object(), object(),
                                1, 1, HOME, True, lambda: True)
        candidate = ColdSceneCandidate(scope=scope, scene_bytes=self.current_scene)
        self.assertTrue(candidate.metadata()["canonical_resource_checked"])
        self.assertTrue(candidate.metadata()["historical_seed"])

    def test_engine_accepts_0_16_8_home_tag(self):
        from qfsky.long0167.engine import validate_templates
        validate_templates(self.current_scene, legacy_actor_fixture())


class CatalogDonorTests(unittest.TestCase):
    def test_stale_catalog_source_falls_back_to_current_home_capture(self):
        from types import SimpleNamespace
        from xg_gamedata import catalog

        with tempfile.TemporaryDirectory(prefix="xg-home-catalog-") as tmp:
            root = Path(tmp)
            private = root / "runtime/private"
            profiles_root = private / "current_profiles"
            old = profiles_root / "CandleSpace_old"
            new = profiles_root / "CandleSpace_new"
            old.mkdir(parents=True)
            new.mkdir(parents=True)
            old_nv = [36851, 49307, 23536, 0, 0, 25536]
            write_json(old / "record.json", {
                "apk_build": 421589,
                "native_sha256": "0b81f1da54d9c6f28c08746b2ec19519d8eb78c4f1b3c5e4e420ff726dc25321",
                "resource_revision": "V75885",
                "scene": "CandleSpace", "level_id": HOME,
                "netversion_u16": old_nv,
            })
            record = make_capture(new)
            write_json(private / "catalog_sources.json",
                       {OWNER: {"record_dir": "CandleSpace_old",
                                "netversion_u16": old_nv}})
            profiles = {"maps": {str(HOME): {
                "scene": "CandleSpace", "name": "遇境",
                "current_capture": {
                    "record_dir": "CandleSpace_new",
                    "netversion_u16": record["netversion_u16"],
                    "account": OWNER,
                    "by_account": {OWNER: {
                        "record_dir": "CandleSpace_new",
                        "netversion_u16": record["netversion_u16"]}},
                },
            }}}
            xg = SimpleNamespace(CURRENT_PROFILES=profiles_root,
                                 RUNTIME_PRIVATE=private)
            donor = catalog._donor(xg, profiles, OWNER)
            self.assertEqual(donor["record_dir"], "CandleSpace_new")
            self.assertEqual(donor["netversion_u16"][0], 63789)
            with self.assertRaises(catalog.GameDataError) as caught:
                catalog._read_donor(xg, "CandleSpace_old", expected_uuid=OWNER)
            self.assertEqual(caught.exception.code, "donor_version")


class CaptureRegisterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="xg-home-tag-")
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name)
        self.profiles = self.data / "runtime/private/current_profiles"
        self.cfg = self.data / "runtime/private/connection0167_profiles.local.json"
        self.sources = self.data / "runtime/private/catalog_sources.json"
        self.accounts = self.data / "runtime/runtime-data/accounts.json"
        self.profiles.mkdir(parents=True)
        write_json(self.accounts, {"accounts": [{
            "id": "fixture-a", "user_uuid": OWNER, "alias": "测试号", "created_at": 1}]})
        old_nv = [36851, 49307, 23536, 0, 0, 25536]
        write_json(self.cfg, {
            "apk_build": 421589,
            "resource_revision": "V75885",
            "private_root": "current_profiles",
            "dictionary_path": "../protocol_results/handshake/resources/CompressDictionary.zst",
            "home_long_templates": {},
            "maps": {
                str(HOME): {
                    "scene": "CandleSpace", "name": "遇境",
                    "current_capture": {
                        "record_dir": "CandleSpace_old",
                        "netversion_u16": old_nv,
                        "account": OWNER,
                        "by_account": {OWNER: {"record_dir": "CandleSpace_old",
                                               "netversion_u16": old_nv}},
                    },
                },
            },
        })
        write_json(self.sources, {OWNER: {"record_dir": "CandleSpace_old",
                                          "netversion_u16": old_nv}})
        old = self.profiles / "CandleSpace_old"
        old.mkdir()
        write_json(old / "record.json", {"apk_build": 421589, "scene": "CandleSpace",
                                         "level_id": HOME,
                                         "netversion_u16": old_nv})
        self.incoming = self.data / "incoming/CandleSpace_new"
        self.incoming.mkdir(parents=True)
        self.record = make_capture(self.incoming)

    def test_old_36851_entries_are_upgraded_to_new_capture(self):
        from xg_gamedata import captures, dictionary
        import xg_paths

        with patch.multiple(
                xg_paths,
                DATA=self.data,
                RUNTIME_PRIVATE=self.data / "runtime/private",
                RUNTIME_DATA=self.data / "runtime/runtime-data",
                CURRENT_PROFILES=self.profiles,
                PROFILES_JSON=self.cfg,
                ACCOUNTS_JSON=self.accounts,
                NAMES=self.data / "game/0.16.8-421629/names",
                APK_DIR=self.data / "game/0.16.8-421629/apk",
                OVERLAY=APP / "escort-v31-server",
                QFSKY_CODE=APP / "qfsky",
                BOT_CODE=APP / "bot",
        ), patch.object(captures, "_runtime_source",
                        lambda uuid: type("Source", (), {"scene": "CandleSpace"})()), \
                patch.object(dictionary, "ensure_for_data", lambda *args, **kwargs: {}):
            result = captures.register(self.incoming, self.data, _QuietProgress())

        self.assertTrue(result["ok"], result)
        self.assertTrue(result["loaded"])
        self.assertEqual(result["netversion_u16"][0], 63789)
        self.assertEqual(result["resource_revision"], "V76318")
        self.assertTrue(result["backups"])
        new_cfg = json.loads(self.cfg.read_text(encoding="utf-8"))
        self.assertEqual(new_cfg["apk_build"], 421629)
        self.assertEqual(new_cfg["resource_revision"], "V76318")
        entry = new_cfg["maps"][str(HOME)]
        self.assertEqual(entry["current_capture"]["netversion_u16"][0], 63789)
        self.assertEqual(entry["current_capture"]["by_account"][OWNER]["netversion_u16"][0],
                         63789)
        new_source = json.loads(self.sources.read_text(encoding="utf-8"))[OWNER]
        self.assertEqual(new_source["netversion_u16"][0], 63789)
        self.assertEqual(new_source["record_dir"], result["record_dir"])
        backups = [Path(item) for item in result["backups"]]
        self.assertTrue(any(path.name.startswith(self.cfg.name + ".bak-") for path in backups))
        self.assertTrue(any(path.name.startswith(self.sources.name + ".bak-") for path in backups))
        backup_cfg = next(path for path in backups if path.name.startswith(self.cfg.name + ".bak-"))
        old_cfg = json.loads(backup_cfg.read_text(encoding="utf-8"))
        self.assertEqual(old_cfg["apk_build"], 421589)
        self.assertEqual(old_cfg["maps"][str(HOME)]["current_capture"]["netversion_u16"][0],
                         36851)
        backup_sources = next(
            path for path in backups if path.name.startswith(self.sources.name + ".bak-"))
        old_sources = json.loads(backup_sources.read_text(encoding="utf-8"))
        self.assertEqual(old_sources[OWNER]["netversion_u16"][0], 36851)

    def test_new_capture_expectation_is_its_own_netversion(self):
        from xg_gamedata import captures
        self.assertEqual(tuple(self.record["netversion_u16"]),
                         (63789, 49307, 23536, 0, 0, 25536))
        # 旧配置仍是 36851；登记加载只把录制自己的 netversion 当期望值。
        old_cfg = json.loads(self.cfg.read_text(encoding="utf-8"))
        self.assertEqual(old_cfg["maps"][str(HOME)]["current_capture"]["netversion_u16"][0],
                         36851)
        with patch("qfsky.map_long0167.prepare.prepare_current_capture") as prepare:
            prepare.return_value = object()
            captures._load_capture(self.profiles, "CandleSpace_new", uuid=OWNER,
                                   record=self.record, revision="V76318")
            self.assertEqual(prepare.call_args.kwargs["expected_netversion"],
                             (63789, 49307, 23536, 0, 0, 25536))


class _QuietProgress:
    def emit(self, *args, **kwargs):
        pass

    def finish(self, *args, **kwargs):
        pass


if __name__ == "__main__":
    unittest.main()
