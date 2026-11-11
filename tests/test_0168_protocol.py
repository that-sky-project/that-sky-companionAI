# -*- coding: utf-8 -*-
"""0.16.8 / 421629 protocol gates, import chain and Actor round-trip (offline)."""
from __future__ import annotations
import os
from pathlib import Path
import socket
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
sys.path.insert(0, str(APP))


class VersionProfileTests(unittest.TestCase):
    def test_current_profile_is_0168(self):
        from qfsky.version_profile import (CURRENT_BUILD, CURRENT_VERSION, CURRENT_RESOURCE_REVISION,
                                           PROFILES, profile_for_build, profile_for_version)
        self.assertEqual((CURRENT_VERSION, CURRENT_BUILD), ("0.16.8", 421629))
        self.assertEqual(CURRENT_RESOURCE_REVISION, "V76318")
        self.assertEqual(profile_for_build(421629)["native_sha256"][:8], "0b18ecc4")
        self.assertEqual(profile_for_version("0.16.7.421589")["version"], "0.16.7")
        self.assertIn(421589, PROFILES)
        self.assertIn(421629, PROFILES)

    def test_account_binding_domain_stays_421589(self):
        from qfsky import join0167_profile
        self.assertEqual(join0167_profile.BUILD, 421629)
        self.assertEqual(join0167_profile.ACCOUNT_DOMAIN, b"sky-cn-421589-level-cache-account-v1\x00")
        self.assertEqual(join0167_profile.BINDING_ALGORITHM,
                         "sha256-domain-sky-cn-421589-level-cache-account-v1")


class ImportChainTests(unittest.TestCase):
    def test_protocol_modules_import_without_network(self):
        def denied(*args, **kwargs):
            raise AssertionError("network during import")
        with patch.object(socket.socket, "connect", denied), patch.object(socket, "create_connection", denied):
            from qfsky import (connection0167, current_join, join0167_codec, join0167_profile,
                               receive0167, register0167_codec, scene_control0167,
                               type2_dictionary0167, handoff0167, handshake0167_fields)
            from qfsky.long0167.actor_codec import actor_migration_0167, actor_wire_0167
            from qfsky.long0167.codec import scene_wire
            from qfsky.map_long0167 import current_inputs, map_long_engine, prepare
        self.assertEqual(connection0167.BUILD, 421629)
        self.assertEqual(current_join.BUILD, 421629)
        self.assertEqual(join0167_codec.BUILD, 421629)
        self.assertEqual(receive0167.APK_BUILD, 421629)
        self.assertEqual(register0167_codec.BUILD, 421629)
        self.assertEqual(handoff0167.BUILD, 421629)
        self.assertEqual(handoff0167.HOME_RESOURCE, "V76318")
        self.assertEqual(handshake0167_fields.TICKET_DOMAIN, b"sky-cn-421629-fresh-initial-ticket-v1\x00")
        self.assertEqual(actor_wire_0167.PUBLIC_ELF_SHA256, join0167_profile.NATIVE_SHA256)
        self.assertTrue(hasattr(scene_wire, "PUBLIC_ELF_SHA256_0168"))

    def test_old_build_is_rejected_by_current_gates(self):
        from qfsky import join0167_codec, join0167_profile, current_join
        with self.assertRaises(ValueError):
            join0167_codec.encode({"build": 421589})
        with self.assertRaises(Exception):
            join0167_profile.validate_record({"apk_build": 421589}, b"", scene="CandleSpace",
                                             resource_revision="V76318", account_uuid="0" * 32)
        with self.assertRaises(Exception):
            current_join.load_profile(3526133726, "0" * 32, 421589)


class ActorRoundTripTests(unittest.TestCase):
    def test_0_16_8_actor_decode_encode_byte_identical(self):
        from qfsky.join0167_profile import NATIVE_SHA256
        from qfsky.long0167.actor_codec.actor_wire_0167 import inspect_actor_wire, reencode_actor_wire
        body = (ROOT / "tests/fixtures/actor_0168_candle.bin").read_bytes()
        observation = inspect_actor_wire(body, verified_profile_sha256=NATIVE_SHA256)
        self.assertEqual(observation.metadata()["profile"], "netease-android-0.16.8-421629")
        self.assertEqual(reencode_actor_wire(observation, verified_profile_sha256=NATIVE_SHA256), body)

    def test_new_level_state_boundary_exists(self):
        from qfsky.long0167.actor_codec import actor_wire_schema_0167 as schema
        labels = {row[0] for row in schema.MAIN}
        for plan in schema.FIXED_HELPERS.values():
            labels.update(row[0] for row in plan)
        self.assertIn("0x21df648", labels)
        self.assertIn("0x21dd0e8", labels)
        self.assertIn("0x21dd054", labels)
        self.assertNotIn("0x21dee4c", labels)


class BuildKeyTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory(prefix="xg-build-key-")
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {"XG_DATA_DIR": self.tmp.name}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop("SKY_BUILD_ACCESS_KEY_NETEASE", None)

    def test_missing_key_has_clear_chinese_message_and_no_value(self):
        from qfsky.build_key import BuildAccessKeyMissing, resolve
        with self.assertRaises(BuildAccessKeyMissing) as caught:
            resolve(allow_service=False)
        text = str(caught.exception)
        self.assertIn("x-sky-build-access-key", text)
        self.assertIn("SKY_BUILD_ACCESS_KEY_NETEASE", text)
        self.assertIn("runtime/private/build_access_key", text)

    def test_env_private_file_and_cache_order(self):
        from qfsky import build_key
        with patch.dict(os.environ, {"SKY_BUILD_ACCESS_KEY_NETEASE": "env-key-012345"}, clear=False):
            self.assertEqual(build_key.resolve(allow_service=False), "env-key-012345")
        build_key.private_path().parent.mkdir(parents=True, exist_ok=True)
        build_key.private_path().write_text("file-key-012345\n", encoding="utf-8")
        os.environ.pop("SKY_BUILD_ACCESS_KEY_NETEASE", None)
        self.assertEqual(build_key.resolve(allow_service=False), "file-key-012345")
        build_key.store_downloaded("cache-key-012345")
        self.assertEqual(build_key.resolve(allow_service=False), "file-key-012345")
        build_key.private_path().unlink()
        self.assertEqual(build_key.resolve(allow_service=False), "cache-key-012345")
        self.assertEqual(build_key.status(), {"configured": True, "source": "service_cache"})

    def test_placeholder_is_never_accepted(self):
        from qfsky import build_key
        with patch.dict(os.environ, {"SKY_BUILD_ACCESS_KEY_NETEASE": "<REDACTED-0.16.7>"}, clear=False):
            with self.assertRaises(build_key.BuildAccessKeyMissing):
                build_key.resolve(allow_service=False)


if __name__ == "__main__":
    unittest.main()
