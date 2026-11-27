from __future__ import annotations

import io
import random
import subprocess
import sys
import tempfile
import unittest
import warnings
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from capture import device
from xg_gamedata import android_package, apk
from xg_gamedata.common import GameDataError


PACKAGE = "com.netease.sky"
SERIAL = "emulator-5554"
INSTALL = f"/data/app/~~a5~A+b==/{PACKAGE}-cD_1+2=="
BASE = INSTALL + "/base.apk"
ARM_SPLIT = INSTALL + "/split_config.arm64_v8a.apk"


class Android15PackageTests(unittest.TestCase):
    def test_dumpsys_uses_the_installed_exact_package_entry(self):
        dump = f"""Packages:
  Package [{PACKAGE}.channel] (2381ec):
    versionCode=3 targetSdk=35
  Package [{PACKAGE}] (a41b1d):
    codePath={INSTALL}
    versionCode = 421629 minSdk=26 targetSdk=35
    longVersionCode=421629
    primaryCpuAbi=arm64-v8a
    secondaryCpuAbi=null
    legacyNativeLibraryDir={INSTALL}/lib
    User 0: installed=true hidden=false
Hidden system packages:
  Package [{PACKAGE}] (028f98):
    versionCode=99442
"""
        result = android_package.parse_package_dump(dump.replace("\n", "\r\n"), PACKAGE)
        self.assertEqual(result["version_code"], "421629")
        self.assertEqual(result["code_path"], INSTALL)
        self.assertIsNone(android_package.parse_package_dump(dump, PACKAGE + ".stub")["version_code"])

    def test_cmd_package_version_only_matches_exact_name(self):
        def fake_adb(adb, *args, serial, timeout):
            self.assertEqual(serial, SERIAL)
            self.assertGreater(timeout, 0)
            if args == ("shell", f"dumpsys package {PACKAGE}"):
                return 0, "", "Permission Denial: can't dump PackageManager"
            if args == ("shell", f"cmd package list packages --show-versioncode {PACKAGE}"):
                return 0, f"package:{PACKAGE}.other versionCode:11\npackage:{PACKAGE} versionCode:421629\n", ""
            self.fail(f"unexpected fake adb command: {args}")

        with patch.object(device, "adb_run", side_effect=fake_adb) as command:
            self.assertEqual(device.game_version("fake-adb", SERIAL), "421629")
            self.assertEqual(command.call_count, 2)
        self.assertIsNone(android_package.parse_package_list_version(f"package:{PACKAGE}.other versionCode:421629", PACKAGE))

    def test_arm64_on_translation_and_arm64_native_use_package_dirs_not_host_isa(self):
        for image, mode in (("x86_64", "arm64_on_x86_64"), ("arm64-v8a", "arm64_native")):
            with self.subTest(image=image):
                def fake_adb(adb, *args, serial, timeout):
                    self.assertEqual(serial, SERIAL)
                    if args == ("shell", f"dumpsys package {PACKAGE}"):
                        dump = (f"versionCode=421629\nprimaryCpuAbi=arm64-v8a\nsecondaryCpuAbi=null\n"
                                f"codePath={INSTALL}\nnativeLibraryDir={INSTALL}/lib/arm64\n")
                        return 0, dump, ""
                    if args == ("shell", "getprop ro.product.cpu.abi"):
                        return 0, image + "\r\n", ""
                    self.fail(f"unexpected fake adb command: {args}")

                diagnostics = {}
                with patch.object(device, "adb_run", side_effect=fake_adb):
                    self.assertEqual(device.game_version("fake-adb", SERIAL, diagnostics=diagnostics), "421629")
                abi = diagnostics["native_abi"]
                self.assertEqual((abi["image"], abi["primary"], abi["mode"]), (image, "arm64-v8a", mode))
                self.assertEqual(abi["native_library_dir"], INSTALL + "/lib/arm64")
                self.assertEqual(abi["candidate_dirs"], [INSTALL + "/lib/arm64"])
                self.assertEqual(abi["apk_lib_prefixes"], ["lib/arm64-v8a/"])

    def test_x86_64_native_report_is_distinct_from_arm64_translation(self):
        fields = android_package.parse_package_dump(f"primaryCpuAbi=x86_64\ncodePath={INSTALL}\n", PACKAGE)
        abi = android_package.describe_native_abi(fields, "x86_64")
        self.assertEqual(abi["mode"], "x86_64_native")
        self.assertEqual(abi["candidate_dirs"], [INSTALL + "/lib/x86_64"])
        self.assertEqual(abi["apk_lib_prefixes"], ["lib/x86_64/"])

    def test_pm_path_preserves_random_paths_and_does_not_accept_traversal(self):
        output = (f"Warning: ignoring stale overlay\r\n package: {ARM_SPLIT}\r\npackage:{BASE}\r\n"
                  f"package:{BASE}\npackage:/data/app/../game.apk\npackage:/data/app/app.apk;echo boom\n")
        self.assertEqual(apk._parse_pm_path(output), [ARM_SPLIT, BASE])


class Android15SplitPullTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.adb = self.root / "fake-adb.exe"
        self.adb.touch()

    def test_cmd_path_fallback_pulls_split_and_runs_existing_identity_validation(self):
        calls = []

        def fake_adb(prefix, args, **options):
            self.assertEqual(prefix, [str(self.adb)])
            command = tuple(args)
            calls.append(command)
            if command == ("devices",):
                return subprocess.CompletedProcess(args, 0, f"List of devices attached\n{SERIAL}\tdevice\n", "")
            self.assertEqual(command[:2], ("-s", SERIAL))
            if command[2:] == ("shell", "pm", "path", PACKAGE):
                return subprocess.CompletedProcess(args, 1, "", "Unknown command: path")
            if command[2:] == ("shell", "cmd", "package", "path", PACKAGE):
                return subprocess.CompletedProcess(args, 0, f"package:{ARM_SPLIT}\r\npackage:{BASE}\r\n", "")
            if command[2] == "pull":
                remote, local = command[3:]
                with zipfile.ZipFile(local, "w") as archive:
                    if remote == BASE:
                        archive.writestr("AndroidManifest.xml", b"base manifest")
                        archive.writestr("resources.arsc", b"base resources")
                        archive.writestr(apk.SCENE_MEMBER, b"scene")
                        archive.writestr("assets/padding.bin", random.Random(15).randbytes(1_100_000))
                    elif remote == ARM_SPLIT:
                        archive.writestr("AndroidManifest.xml", b"split manifest")
                        archive.writestr(apk.NATIVE_MEMBER, b"64-bit split member")
                    else:
                        self.fail(f"unexpected remote APK: {remote}")
                return subprocess.CompletedProcess(args, 0, "1 file pulled", "")
            self.fail(f"unexpected fake adb command: {command}")

        def identity(archive, **options):
            self.assertEqual(archive.read("AndroidManifest.xml"), b"base manifest")
            self.assertEqual(archive.read(apk.NATIVE_MEMBER), b"64-bit split member")
            self.assertEqual(archive.read(apk.SCENE_MEMBER), b"scene")
            return {"legacy": False, "version": "0.16.8", "build": 421629}

        with patch.object(apk.xg_adb, "run", side_effect=fake_adb), \
             patch.object(apk, "validate_apk_members", side_effect=identity) as validate, redirect_stdout(io.StringIO()):
            result = apk.pull_apk(str(self.adb), SERIAL, self.root / "data")
        self.assertTrue(result["merged"])
        self.assertEqual(result["remote_paths"], [BASE, ARM_SPLIT])
        self.assertEqual(result["split_count"], 1)
        self.assertEqual(result["check"]["build"], 421629)
        self.assertEqual([call[3] for call in calls if len(call) > 3 and call[2] == "pull"], [BASE, ARM_SPLIT])
        validate.assert_called_once()

    def test_permission_denial_does_not_become_game_not_installed(self):
        with patch.object(apk, "_run_adb", side_effect=["", "SecurityException: permission denied"]) as run:
            with self.assertRaises(GameDataError) as caught:
                apk._query_apk_paths(self.adb, SERIAL)
        self.assertEqual(caught.exception.code, "pm_path_failed")
        self.assertEqual(run.call_count, 2)

    def test_split_only_output_is_not_accepted_as_base(self):
        with patch.object(apk, "_device_list", return_value=[SERIAL]), \
             patch.object(apk, "_run_adb", return_value=f"package:{ARM_SPLIT}") as run, redirect_stdout(io.StringIO()):
            with self.assertRaises(GameDataError) as caught:
                apk.pull_apk(str(self.adb), SERIAL, self.root / "data")
        self.assertEqual(caught.exception.code, "apk_base_path_ambiguous")
        run.assert_called_once()

    def test_duplicate_member_in_one_split_is_not_hidden_by_merging(self):
        base, split, merged = (self.root / f"{name}.apk" for name in ("base", "split", "merged"))
        with zipfile.ZipFile(base, "w") as archive:
            archive.writestr("AndroidManifest.xml", b"base manifest")
        with warnings.catch_warnings(), zipfile.ZipFile(split, "w") as archive:
            warnings.simplefilter("ignore", UserWarning)
            archive.writestr(apk.NATIVE_MEMBER, b"first")
            archive.writestr(apk.NATIVE_MEMBER, b"second")
        with self.assertRaises(GameDataError) as caught:
            apk._merge_apk_view(base, [split], merged)
        self.assertEqual(caught.exception.code, "apk_duplicate_member")
        self.assertFalse(merged.exists())


if __name__ == "__main__":
    unittest.main()
