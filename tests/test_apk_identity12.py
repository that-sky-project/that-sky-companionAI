"""Offline synthetic APK identity, consumer subset and pulled-error regression."""
import copy
import hashlib
import io
import json
from contextlib import redirect_stdout
import struct
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import warnings
import zipfile

APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))
from xg_gamedata import apk, apk_identity as identity, spawn_loader
from xg_gamedata.common import GameDataError
from qfsky import version_profile

NATIVE = identity.NATIVE_MEMBER
SCENE = "assets/Data/Levels/CandleSpace/Objects.level.bin"
MESH = "assets/Data/Meshes/Synthetic.mesh"
DEFS = "assets/Data/Resources/PlaceableDefsSynthetic.json"
DICTIONARY = "assets/Data/Resources/CompressDictionary.zst"


class QuietProgress:
    def __init__(self, *args, **kwargs):
        self.done = 0
    def emit(self, *args, **kwargs):
        self.done = kwargs.get("done", self.done)
    def finish(self, *args, **kwargs):
        pass


class ApkIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="apkidentity12-", dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.payloads = {
            "AndroidManifest.xml": b"synthetic Android manifest",
            "resources.arsc": b"synthetic string resources",
            NATIVE: b"synthetic native identity 421629",
            SCENE: b"synthetic scene payload",
            MESH: b"synthetic geometry marker; no APK geometry",
            DEFS: b'{"synthetic":true}',
            DICTIONARY: b"synthetic dictionary",
            "assets/unrelated-channel.cfg": b"initial",
            "padding": b"P" * (1024 * 1024),
        }
        self.base = self.write_apk("base.apk")
        with zipfile.ZipFile(self.base) as zf:
            metadata = {i.filename: {"size": i.file_size, "crc32": i.CRC} for i in zf.infolist()}
            baseline_directory = identity.archive_directory_sha(zf)
        self.manifest = {
            "schema": identity.SCHEMA, "build": 421629,
            "native_sha256": hashlib.sha256(self.payloads[NATIVE]).hexdigest(),
            "reference_apk_archive_directory_sha256": baseline_directory,
            "members": metadata,
            "consumers": {
                "names": {"members": [NATIVE, "resources.arsc", DICTIONARY], "inventories": [
                    {"label": "scene_names", "pattern": r"assets/Data/Levels/[A-Za-z0-9_]+/Objects\.level\.bin",
                     "members": [SCENE]}]},
                "catalog": {"members": [NATIVE, SCENE], "inventories": []},
                "terrain": {"members": [NATIVE, MESH], "inventories": []},
                "props": {"members": [NATIVE, DEFS, MESH], "inventories": [
                    {"label": "defs", "pattern": r"assets/Data/Resources/PlaceableDefs[^/]*\.json",
                     "members": [DEFS]}]},
            },
        }
        self.profile = dict(version_profile.PROFILES[421629])
        self.profile.update(native_sha256=self.manifest["native_sha256"],
                            apk_directory_sha256=baseline_directory,
                            resource_identity_schema=identity.SCHEMA,
                            resource_identity_sha256={c: identity.consumer_digest(self.manifest, c)
                                                      for c in identity.CONSUMERS})
        self.profile_patch = patch.dict(version_profile.PROFILES, {421629: self.profile})
        self.profile_patch.start()
        self.addCleanup(self.profile_patch.stop)
        self.loader_patch = patch.object(identity, "_load_manifest", return_value=self.manifest)
        self.loader_patch.start()
        self.addCleanup(self.loader_patch.stop)

    def write_apk(self, name, changed=None, removed=(), reverse=False, extra=(), compressed=False):
        path = self.root / name
        items = dict(self.payloads)
        items.update(changed or {})
        for member in removed:
            items.pop(member, None)
        rows = list(items.items()) + list(extra)
        if reverse:
            rows.reverse()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED if compressed else zipfile.ZIP_STORED) as zf:
                for member, data in rows:
                    zf.writestr(member, data)
        return path

    def validate(self, path=None, scopes=identity.CONSUMERS, **kwargs):
        return identity.validate_apk_members(path or self.base, scopes, **kwargs)

    def assert_error(self, path, code, scopes=identity.CONSUMERS):
        with self.assertRaises(GameDataError) as cm:
            self.validate(path, scopes)
        self.assertEqual(cm.exception.code, code)
        return cm.exception

    def test_known_native_and_exact_consumed_members(self):
        result = self.validate()
        self.assertEqual(result["build"], 421629)
        self.assertFalse(result["legacy"])
        self.assertEqual(set(result["consumer_sha256"]), set(identity.CONSUMERS))

    def test_signature_addition_is_ignored(self):
        path = self.write_apk("signature.apk", extra=[("META-INF/CHANNEL.RSA", b"new signature")])
        self.assertEqual(self.validate(path)["identity_digest"], self.validate()["identity_digest"])

    def test_channel_file_change_is_ignored(self):
        path = self.write_apk("channel.apk", {"assets/unrelated-channel.cfg": b"another channel"})
        self.assertEqual(self.validate(path)["consumer_sha256"], self.validate()["consumer_sha256"])

    def test_unused_assets_data_member_is_not_globally_locked(self):
        path = self.write_apk("unused.apk", extra=[("assets/Data/Textures/UnusedSynthetic.png", b"opaque")])
        self.assertEqual(self.validate(path)["identity_digest"], self.validate()["identity_digest"])

    def test_zip_member_order_and_compression_are_ignored(self):
        path = self.write_apk("reordered.apk", reverse=True, compressed=True)
        self.assertEqual(self.validate(path)["identity_digest"], self.validate()["identity_digest"])

    def test_whole_hash_can_differ_while_identity_is_same(self):
        path = self.write_apk("changed-signature.apk", extra=[("META-INF/CERT.SF", b"channel signing info")])
        a, b = apk.inspect_apk(self.base), apk.inspect_apk(path)
        self.assertNotEqual(a["sha256"], b["sha256"])
        self.assertNotEqual(a["apk_archive_directory_sha256"], b["apk_archive_directory_sha256"])
        self.assertEqual(a["resource_identity_sha256"], b["resource_identity_sha256"])

    def test_actual_directory_is_not_forged_to_reference(self):
        path = self.write_apk("actual.apk", extra=[("channel.bin", b"different")])
        result = self.validate(path, ["catalog"])
        self.assertNotEqual(result["apk_archive_directory_sha256"], result["reference_apk_archive_directory_sha256"])

    def test_native_change_rejected(self):
        self.assert_error(self.write_apk("native.apk", {NATIVE: b"unknown native"}), "apk_identity_mismatch")

    def test_native_missing_rejected(self):
        self.assert_error(self.write_apk("native-missing.apk", removed=[NATIVE]), "apk_no_native")

    def test_duplicate_native_rejected(self):
        self.assert_error(self.write_apk("duplicate-native.apk", extra=[(NATIVE, self.payloads[NATIVE])]),
                          "apk_duplicate_member")

    def test_consumed_crc_change_names_member(self):
        error = self.assert_error(self.write_apk("arsc.apk", {"resources.arsc": b"changed strings"}),
                                  "apk_resource_mismatch", ["names"])
        self.assertIn("resources.arsc", error.message)
        self.assertEqual(error.members[0]["name"], "resources.arsc")

    def test_consumed_missing_catalog_scene(self):
        self.assert_error(self.write_apk("scene-missing.apk", removed=[SCENE]),
                          "apk_resource_mismatch", ["catalog"])

    def test_consumed_size_change_mesh(self):
        self.assert_error(self.write_apk("mesh.apk", {MESH: b"short"}), "apk_resource_mismatch", ["terrain"])

    def test_consumed_dictionary_change(self):
        self.assert_error(self.write_apk("dictionary.apk", {DICTIONARY: b"wrong"}),
                          "apk_resource_mismatch", ["names"])

    def test_duplicate_consumed_member(self):
        self.assert_error(self.write_apk("duplicate-mesh.apk", extra=[(MESH, self.payloads[MESH])]),
                          "apk_resource_mismatch", ["props"])

    def test_unrelated_duplicate_does_not_become_global_identity_pin(self):
        path = self.write_apk("duplicate-signature.apk", extra=[("META-INF/CERT.SF", b"one"),
                                                                ("META-INF/CERT.SF", b"two")])
        self.assertEqual(self.validate(path)["identity_digest"], self.validate()["identity_digest"])

    def test_consumer_scope_does_not_pin_other_consumers_payload(self):
        path = self.write_apk("names-only.apk", {MESH: b"terrain changed"})
        self.assertEqual(self.validate(path, ["names"])["identity_digest"],
                         self.validate(scopes=["names"])["identity_digest"])
        self.assert_error(path, "apk_resource_mismatch", ["terrain"])

    def test_names_scene_inventory_does_not_pin_unused_scene_payload(self):
        path = self.write_apk("scene-payload.apk", {SCENE: b"changed scene payload"})
        self.validate(path, ["names"])
        self.assert_error(path, "apk_resource_mismatch", ["catalog"])

    def test_names_scene_inventory_addition_rejected(self):
        path = self.write_apk("new-scene.apk", extra=[("assets/Data/Levels/NewSynthetic/Objects.level.bin", b"x")])
        error = self.assert_error(path, "apk_resource_mismatch", ["names"])
        self.assertIn("NewSynthetic", error.message)

    def test_new_definition_in_discovery_namespace_rejected(self):
        path = self.write_apk("new-defs.apk", extra=[("assets/Data/Resources/PlaceableDefsExtra.json", b"{}")])
        self.assert_error(path, "apk_resource_mismatch", ["props"])

    def test_error_lists_at_most_eight_concrete_members(self):
        rows = [("assets/Data/Levels/New%d/Objects.level.bin" % n, b"x") for n in range(20)]
        error = self.assert_error(self.write_apk("many.apk", extra=rows), "apk_resource_mismatch", ["names"])
        self.assertEqual(len(error.members), 8)
        self.assertEqual(error.difference_count, 20)

    def test_unknown_consumer_rejected(self):
        self.assert_error(self.base, "apk_consumer", ["unrecognized"])

    def test_empty_consumer_rejected(self):
        self.assert_error(self.base, "apk_consumer", [])

    def test_wrong_expected_build_rejected(self):
        with self.assertRaises(GameDataError) as cm:
            self.validate(expected_build=421589)
        self.assertEqual(cm.exception.code, "apk_build_mismatch")

    def test_bad_zip_has_business_code(self):
        path = self.root / "bad.apk"
        path.write_bytes(b"not ZIP")
        self.assert_error(path, "apk_bad_zip")

    def test_open_zip_supported_and_not_closed(self):
        with zipfile.ZipFile(self.base) as zf:
            self.validate(zf, ["catalog"])
            self.assertIsNotNone(zf.fp)

    def test_resource_provenance_is_complete_and_explicit(self):
        path = self.write_apk("proof.apk", extra=[("channel.bin", b"different")])
        fields = identity.resource_provenance(self.validate(path, ["catalog"]), "catalog")
        self.assertEqual(fields["resource_identity_scope"], "catalog")
        self.assertEqual(fields["resource_identity_schema"], identity.SCHEMA)
        self.assertEqual(fields["resource_identity_sha256"], self.profile["resource_identity_sha256"]["catalog"])
        self.assertNotEqual(fields["apk_archive_directory_sha256"], fields["reference_apk_archive_directory_sha256"])

    def test_resource_provenance_rejects_unvalidated_scope(self):
        with self.assertRaises(GameDataError):
            identity.resource_provenance(self.validate(scopes=["names"]), "catalog")

    def test_legacy_exact_native_and_directory_recognized(self):
        old_native = self.payloads[NATIVE] + b"-legacy"
        path = self.write_apk("legacy.apk", {NATIVE: old_native})
        with zipfile.ZipFile(path) as zf:
            directory = identity.archive_directory_sha(zf)
        legacy = dict(version_profile.PROFILES[421589])
        legacy.update(native_sha256=hashlib.sha256(old_native).hexdigest(), apk_directory_sha256=directory)
        with patch.dict(version_profile.PROFILES, {421589: legacy}):
            result = self.validate(path)
            self.assertTrue(result["legacy"])
            self.assertEqual(result["consumer_sha256"], {})
            self.assertEqual(result["identity_mode"], "legacy-native-and-exact-directory")

    def test_legacy_channel_change_stays_exact_until_verified_baseline_exists(self):
        old_native = self.payloads[NATIVE] + b"-legacy"
        base = self.write_apk("legacy-base.apk", {NATIVE: old_native})
        with zipfile.ZipFile(base) as zf:
            directory = identity.archive_directory_sha(zf)
        legacy = dict(version_profile.PROFILES[421589])
        legacy.update(native_sha256=hashlib.sha256(old_native).hexdigest(), apk_directory_sha256=directory)
        changed = self.write_apk("legacy-channel.apk", {NATIVE: old_native}, extra=[("channel.bin", b"new")])
        with patch.dict(version_profile.PROFILES, {421589: legacy}):
            self.assert_error(changed, "apk_identity_mismatch")

    def test_spawn_reference_accepts_validated_subset_not_actual_archive_equality(self):
        result = self.validate(self.write_apk("spawn-channel.apk", extra=[("channel.bin", b"x")]), ["catalog"])
        pack = {"apk_build": 421629, "native_sha256": self.profile["native_sha256"],
                "apk_archive_directory_sha256": self.profile["apk_directory_sha256"]}
        spawn_loader.require_apk_evidence(pack, build=421629, native_sha256=result["native_sha256"],
                                         apk_archive_directory_sha256=result["apk_archive_directory_sha256"],
                                         resource_identity_sha256=result["consumer_sha256"]["catalog"])

    def test_spawn_wrong_subset_does_not_fall_back_to_exact_directory(self):
        pack = {"apk_build": 421629, "native_sha256": self.profile["native_sha256"],
                "apk_archive_directory_sha256": self.profile["apk_directory_sha256"]}
        with self.assertRaises(GameDataError):
            spawn_loader.require_apk_evidence(pack, build=421629, native_sha256=self.profile["native_sha256"],
                                             apk_archive_directory_sha256=self.profile["apk_directory_sha256"],
                                             resource_identity_sha256="0" * 64)

    def test_original_spawn_call_without_subset_remains_strict(self):
        pack = {"apk_build": 421629, "native_sha256": self.profile["native_sha256"],
                "apk_archive_directory_sha256": self.profile["apk_directory_sha256"]}
        with self.assertRaises(GameDataError):
            spawn_loader.require_apk_evidence(pack, build=421629, native_sha256=self.profile["native_sha256"],
                                             apk_archive_directory_sha256="1" * 64)

    def test_inspect_missing_manifest_keeps_original_code(self):
        with self.assertRaises(GameDataError) as cm:
            apk.inspect_apk(self.write_apk("not-android.apk", removed=["AndroidManifest.xml"]))
        self.assertEqual(cm.exception.code, "apk_not_android")

    def test_manifest_pins_are_validated_not_just_self_declared(self):
        self.loader_patch.stop()
        tampered = copy.deepcopy(self.manifest)
        tampered["members"][SCENE]["crc32"] ^= 1
        with patch.object(identity.Path, "read_text", return_value=json.dumps(tampered)):
            with self.assertRaises(GameDataError) as cm:
                identity._load_manifest(421629)
        self.assertEqual(cm.exception.code, "apk_resource_manifest")
        self.loader_patch.start()

    def test_manifest_valid_synthetic_baseline_loads(self):
        self.loader_patch.stop()
        with patch.object(identity.Path, "read_text", return_value=json.dumps(self.manifest)):
            self.assertEqual(identity._load_manifest(421629)["build"], 421629)
        self.loader_patch.start()

    def _pull_error(self, splits=False):
        adb = self.root / "synthetic-adb.exe"
        adb.write_bytes(b"not executable")
        remote = ["package:/data/app/synthetic/base.apk"]
        if splits:
            remote.append("package:/data/app/synthetic/split.apk")
        def run(_adb, _serial, args, **kwargs):
            if args[:3] == ["shell", "pm", "path"]:
                return "\n".join(remote)
            if args[0] == "pull":
                Path(args[-1]).write_bytes(b"synthetic pulled bytes")
                return ""
            raise AssertionError(args)
        serial = "192.168.3.42:5555"  # synthetic fixture, using a valid bridge serial
        with patch.object(apk, "Progress", QuietProgress), patch.object(apk, "_device_list", return_value=[serial]), \
             patch.object(apk, "_run_adb", side_effect=run), \
             patch.object(apk, "_merge_apk_view", side_effect=lambda base, splits, out, progress: out), \
             patch.object(apk, "inspect_apk", side_effect=GameDataError("synthetic critical member", "apk_resource_mismatch")):
            with self.assertRaises(GameDataError) as cm:
                apk.pull_apk(str(adb), serial, self.root / "data")
        return cm.exception

    def test_pulled_base_validation_failure_keeps_code_and_stage(self):
        error = self._pull_error()
        self.assertEqual(error.code, "apk_resource_mismatch")
        self.assertEqual(error.stage, "validate_pulled_apk")
        self.assertEqual(error.child_code, error.code)
        self.assertTrue(error.message.startswith("拉取成功，但安装包校验未通过："))

    def test_pulled_merged_validation_failure_keeps_original_code(self):
        error = self._pull_error(splits=True)
        self.assertEqual(error.code, "apk_resource_mismatch")
        self.assertEqual(error.stage, "validate_pulled_apk")

    def test_cmd_pull_emits_stage_and_child_code(self):
        error = apk._pulled_validation_error(GameDataError("detail", "apk_identity_mismatch"))
        with patch.object(apk, "pull_apk", side_effect=error), patch.object(apk, "final") as emit:
            code = apk.cmd_pull(SimpleNamespace(data=str(self.root), adb="unused", serial=None))
        self.assertEqual(code, 1)
        self.assertEqual(emit.call_args.kwargs["code"], "apk_identity_mismatch")
        self.assertEqual(emit.call_args.kwargs["stage"], "validate_pulled_apk")

    def test_inspect_expected_build_rejects_legacy_before_generation(self):
        old_native = self.payloads[NATIVE] + b"-legacy"
        path = self.write_apk("legacy-inspect.apk", {NATIVE: old_native})
        with zipfile.ZipFile(path) as zf:
            directory = identity.archive_directory_sha(zf)
        legacy = dict(version_profile.PROFILES[421589])
        legacy.update(native_sha256=hashlib.sha256(old_native).hexdigest(), apk_directory_sha256=directory)
        with patch.dict(version_profile.PROFILES, {421589: legacy}):
            with self.assertRaises(GameDataError) as cm:
                apk.inspect_apk(path, consumers=("names",), expected_build=421629)
        self.assertEqual(cm.exception.code, "apk_build_mismatch")

    def _cache_fixture(self):
        from xg_gamedata import scene_inputs_build as builder
        scene, native, wire = "Synthetic", b"N", b"W"
        cache = bytearray(builder.CACHE_SIZE)
        cache[:14] = builder.CACHE_HEADER
        cache[14:15] = wire
        struct.pack_into("<H", cache, 0x200E, len(wire))
        raw = {"native": native, "cache": bytes(cache), "wire": wire}
        paths = {kind: self.root / ("cache-" + kind) for kind in raw}
        for key, data in raw.items():
            paths[key].write_bytes(data)
        paths["json"] = self.root / "cache-proof.json"
        pins = {"test-generator": "same"}
        row = {"map_id": 7, "member_sha256": "b" * 64, "netversion_u16": [1, 2, 3, 4, 5, 6]}
        provenance = identity.resource_provenance(self.validate(scopes=["catalog"]), "catalog")
        provenance.update(scene_member_sha256=row["member_sha256"],
                          apk_archive_directory_sha256="1" * 64)
        report = {"schema": builder.SCHEMA, "scene": scene, "map_id": 7,
                  "native_sha256": self.profile["native_sha256"], "source_pins": pins,
                  "netversion_u16": row["netversion_u16"], "wire_variable_group_counts": {},
                  "provenance": provenance,
                  "artifacts": {key: {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
                                for key, data in raw.items()}}
        paths["json"].write_text(json.dumps(report), encoding="utf-8")
        evidence = {"native_sha256": self.profile["native_sha256"],
                    "scenes": {scene: row}, "apk_archive_directory_sha256": self.profile["apk_directory_sha256"]}
        return builder, paths, scene, evidence, pins, wire

    def test_scene_cache_reuses_same_native_and_member_sha_across_channel_archives(self):
        builder, paths, scene, evidence, pins, wire = self._cache_fixture()
        with patch.object(builder, "_scene_wire", return_value=(
                lambda *a, **k: SimpleNamespace(_counts={}), lambda observation: wire)):
            self.assertTrue(builder._reuse(paths, scene, evidence, pins))

    def test_scene_cache_still_rejects_changed_member_sha(self):
        builder, paths, scene, evidence, pins, wire = self._cache_fixture()
        evidence["scenes"][scene]["member_sha256"] = "c" * 64
        with patch.object(builder, "_scene_wire", return_value=(
                lambda *a, **k: SimpleNamespace(_counts={}), lambda observation: wire)):
            self.assertFalse(builder._reuse(paths, scene, evidence, pins))

    def test_scene_cache_still_rejects_changed_artifact(self):
        builder, paths, scene, evidence, pins, wire = self._cache_fixture()
        paths["wire"].write_bytes(b"corrupt")
        self.assertFalse(builder._reuse(paths, scene, evidence, pins))

    def test_scene_cache_rejects_damaged_subset_digest(self):
        builder, paths, scene, evidence, pins, wire = self._cache_fixture()
        report = json.loads(paths["json"].read_text(encoding="utf-8"))
        report["provenance"]["resource_identity_sha256"] = "f" * 64
        paths["json"].write_text(json.dumps(report), encoding="utf-8")
        self.assertFalse(builder._reuse(paths, scene, evidence, pins))

    def test_scene_cache_rejects_wrong_scope(self):
        builder, paths, scene, evidence, pins, wire = self._cache_fixture()
        report = json.loads(paths["json"].read_text(encoding="utf-8"))
        report["provenance"]["resource_identity_scope"] = "props"
        paths["json"].write_text(json.dumps(report), encoding="utf-8")
        self.assertFalse(builder._reuse(paths, scene, evidence, pins))

    def test_scene_cache_accepts_legacy_exact_reference_proof(self):
        builder, paths, scene, evidence, pins, wire = self._cache_fixture()
        report = json.loads(paths["json"].read_text(encoding="utf-8"))
        for key in ("resource_identity_schema", "resource_identity_scope", "resource_identity_sha256",
                    "reference_apk_archive_directory_sha256"):
            report["provenance"].pop(key)
        report["provenance"]["apk_archive_directory_sha256"] = self.profile["apk_directory_sha256"]
        paths["json"].write_text(json.dumps(report), encoding="utf-8")
        with patch.object(builder, "_scene_wire", return_value=(
                lambda *a, **k: SimpleNamespace(_counts={}), lambda observation: wire)):
            self.assertTrue(builder._reuse(paths, scene, evidence, pins))

    def test_cmd_check_final_json_keeps_bounded_member_details(self):
        path = self.write_apk("check-cli-diffs.apk", extra=[
            ("assets/Data/Levels/Extra%d/Objects.level.bin" % n, b"x") for n in range(12)])
        output = io.StringIO()
        with patch.object(apk, "Progress", QuietProgress), redirect_stdout(output):
            result = apk.cmd_check(SimpleNamespace(apk=str(path)))
        row = json.loads(output.getvalue().splitlines()[-1])
        self.assertEqual(result, 1)
        self.assertEqual(row["code"], "apk_resource_mismatch")
        self.assertEqual(row["difference_count"], 12)
        self.assertEqual(len(row["members"]), 8)
        self.assertIn("Extra", row["members"][0]["name"])
        self.assertIn("关键资源", row["error"])

    def test_cmd_pull_final_json_keeps_stage_code_and_bounded_member_details(self):
        error = GameDataError("synthetic resource difference", "apk_resource_mismatch")
        error.members = [{"name": "synthetic/member%d" % n, "reason": "missing"} for n in range(8)]
        error.difference_count = 15
        pulled = apk._pulled_validation_error(error)
        output = io.StringIO()
        with patch.object(apk, "pull_apk", side_effect=pulled), redirect_stdout(output):
            result = apk.cmd_pull(SimpleNamespace(data=str(self.root), adb="unused", serial=None))
        row = json.loads(output.getvalue().splitlines()[-1])
        self.assertEqual(result, 1)
        self.assertEqual(row["code"], error.code)
        self.assertEqual(row["child_code"], error.code)
        self.assertEqual(row["stage"], "validate_pulled_apk")
        self.assertEqual(row["difference_count"], 15)
        self.assertEqual(row["members"], error.members)

    def test_generic_cli_business_exception_keeps_only_allowed_metadata(self):
        from xg_gamedata import __main__ as entry
        error = GameDataError("original diagnostic", "apk_resource_mismatch")
        error.members = [{"name": "synthetic/member", "reason": "missing"}]
        error.difference_count = 1
        error.stage = "validate_pulled_apk"
        error.child_code = error.code
        error.unrelated_detail = "not forwarded"
        output = io.StringIO()
        with patch.object(apk, "cmd_check", side_effect=error), redirect_stdout(output):
            result = entry.main(["check-apk", "--apk", "unused.apk"])
        row = json.loads(output.getvalue().splitlines()[-1])
        self.assertEqual(result, 1)
        self.assertEqual(row["error"], error.message)
        self.assertEqual(row["code"], error.code)
        for key in ("members", "difference_count", "stage", "child_code"):
            self.assertEqual(row[key], getattr(error, key))
        self.assertNotIn("unrelated_detail", row)

    def test_generic_cli_business_exception_without_metadata_keeps_old_shape(self):
        from xg_gamedata import __main__ as entry
        error = GameDataError("old message", "original_code")
        output = io.StringIO()
        with patch.object(apk, "cmd_check", side_effect=error), redirect_stdout(output):
            result = entry.main(["check-apk", "--apk", "unused.apk"])
        row = json.loads(output.getvalue().splitlines()[-1])
        self.assertEqual(result, 1)
        self.assertEqual(row, {"ok": False, "error": error.message, "code": error.code})

    def _props_cli_failure(self, error, optional=False):
        from xg_gamedata import __main__ as entry, props, common
        output = io.StringIO()
        args = SimpleNamespace(apk="unused.apk", output=str(self.root / "props"),
                               data=str(self.root), optional=optional)
        with patch.object(props, "generate", side_effect=error), \
             patch.object(common, "Progress", QuietProgress), redirect_stdout(output):
            result = entry.cmd_props(args)
        return result, json.loads(output.getvalue().splitlines()[-1])

    def test_props_cli_business_reason_keeps_member_crc_and_original_code(self):
        from xg_gamedata.props import PropGenerationError
        result, row = self._props_cli_failure(PropGenerationError(
            "apk_resource_mismatch", "assets/Data/Synthetic.mesh [size/CRC changed]"))
        self.assertEqual(result, 1)
        self.assertFalse(row["ok"])
        self.assertEqual(row["code"], "apk_resource_mismatch")
        self.assertIn("道具形状未生成", row["error"])
        self.assertIn("Synthetic.mesh", row["error"])
        self.assertIn("size/CRC changed", row["error"])

    def test_props_cli_optional_failure_remains_success_warning_with_reason(self):
        from xg_gamedata.props import PropGenerationError
        result, row = self._props_cli_failure(PropGenerationError(
            "apk_resource_mismatch", "synthetic member [CRC changed]"), optional=True)
        self.assertEqual(result, 0)
        self.assertTrue(row["ok"])
        self.assertFalse(row["props"]["ready"])
        self.assertEqual(row["warning_code"], "apk_resource_mismatch")
        self.assertIn("CRC changed", row["warning"])

    def test_props_cli_gamedata_business_error_reason_is_preserved(self):
        result, row = self._props_cli_failure(GameDataError("synthetic member missing", "apk_resource_mismatch"))
        self.assertEqual(result, 1)
        self.assertIn("synthetic member missing", row["error"])
        self.assertEqual(row["code"], "apk_resource_mismatch")

    def test_props_cli_unknown_internal_exception_does_not_leak_details(self):
        result, row = self._props_cli_failure(RuntimeError("synthetic-internal-detail-not-for-display"))
        self.assertEqual(result, 1)
        self.assertEqual(row["code"], "props_generation_failed")
        self.assertNotIn("synthetic-internal-detail-not-for-display", row["error"])
        self.assertNotIn("traceback", row)


if __name__ == "__main__":
    unittest.main()
