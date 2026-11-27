"""Offline MuMuManager/ADB fixtures; no real emulator is used."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


APP = Path(__file__).resolve().parents[1] / "app"
for directory in (APP, APP / "launcher"):
    sys.path.insert(0, str(directory))

import wizard
from capture import device
from capture.mumu_info import parse_instances


class AndroidImageVersionTests(unittest.TestCase):
    def test_json_guest_release_sdk_and_prefixed_inventory(self):
        output = "MuMuManager 12.0.0\n" + json.dumps({"instances": {
            "0": {"index": 0, "adbPort": 19700, "androidVersion": 9},
            "1": {"index": 1, "adbPort": 19702, "AndroidSDKVersion": 31},
            "2": {"network": {"adbPort": 19704},
                  "system": {"android": {"version": {"release": "Android 15"}}}},
        }})
        self.assertEqual([row.android_version for row in parse_instances(output)], ["9", "12", "15"])

    def test_csv_and_localized_text_versions(self):
        output = "index,adb_port,android_sdk\n0,19700,32\n1,19702,35\n"
        self.assertEqual([row.android_version for row in parse_instances(output)], ["12L", "15"])
        rows = parse_instances("[vm 2]\nadb.port=19704\n安卓版本：安卓 15.0.0\n")
        self.assertEqual(rows[0].android_version, "15.0.0")

    def test_client_version_name_and_unknown_sdk_are_not_os_versions(self):
        rows = parse_instances(json.dumps({"adb_port": 19704, "version": "12.2.15", "name": "Android 15",
                                           "android_release": "15 <unrecognized>", "android_sdk": 99}))
        self.assertIsNone(rows[0].android_version)

    def device_fixtures(self, rows, states, active_serial=None):
        manager, adb = Path("MuMuManager.exe"), Path("adb.exe")

        def manager_run(argv, **kwargs):
            self.assertEqual(argv[1:], ["info", "-v", "all"])
            return 0, json.dumps({"instances": rows}), ""

        def adb_run(path, *args, serial=None, **kwargs):
            if args == ("devices",):
                return 0, "List of devices attached\n" + "\n".join(f"{s}\t{state}" for s, state in states.items()), ""
            if args == ("shell", "pidof com.netease.sky"):
                self.assertEqual(serial, active_serial)
                return 0, "21111\n", ""
            if args == ("shell", "dumpsys activity activities"):
                return 0, "topResumedActivity=ActivityRecord{abcd u0 com.netease.sky/.Game t4}", ""
            if args == ("shell", "dumpsys package com.netease.sky"):
                return 0, "versionCode=421629 minSdk=23 targetSdk=35", ""
            if args == ("shell", "getconf PAGESIZE"):
                return 0, "16384", ""
            raise AssertionError((args, serial))

        for p in (patch.object(device, "_capture_config", return_value={}),
                  patch.object(device, "_managers_in", return_value=[manager]),
                  patch.object(device, "_manager_on_path", return_value=None),
                  patch.object(device, "find_adb", return_value=(adb, None)),
                  patch.object(device, "_run", side_effect=manager_run),
                  patch.object(device, "adb_run", side_effect=adb_run),
                  patch.dict(device.os.environ, {"XG_ADB_SERIAL": "", "XG_MUMU_MANAGER": ""})):
            p.start()
            self.addCleanup(p.stop)

    def test_check_associates_android_15_with_selected_instance_even_on_root_error(self):
        serial = "127.0.0.1:19704"
        rows = [{"adb_port": 19702, "androidVersion": "12", "isRunning": False},
                {"adb_port": 19704, "androidApiLevel": 35, "isRunning": True, "root.enabled": False}]
        self.device_fixtures(rows, {serial: "device"}, serial)
        result = device.check_device()
        self.assertEqual(result["serial"], serial)
        self.assertEqual(result["android_version"], "15")
        self.assertEqual(result["android_label"], "安卓 15")
        self.assertEqual(result["reason"], "root_disabled")

    def test_stopped_mixed_versions_do_not_get_a_random_label(self):
        rows = [{"adb_port": 19702, "androidVersion": "12", "isRunning": False},
                {"adb_port": 19704, "androidVersion": "15", "isRunning": False}]
        self.device_fixtures(rows, {})
        result = device.check_device()
        self.assertEqual(result["reason"], "mumu_not_started")
        self.assertIsNone(result["android_label"])
        with patch.dict(device.os.environ, {"XG_ADB_SERIAL": "127.0.0.1:19704"}):
            self.assertEqual(device.check_device()["android_label"], "安卓 15")

    def test_wizard_persists_android_image_metadata_without_serial_override(self):
        result = {"ok": True, "serial": "127.0.0.1:19704", "android_label": "安卓 15", "android_version": "15"}
        cfg = {}
        with patch.object(wizard.device, "check_device", return_value=result), \
             patch.object(wizard, "_cfg", return_value=cfg), patch.object(wizard, "mark") as mark, \
             patch.object(wizard.xg_paths, "save_config"):
            self.assertEqual(wizard.check_mumu(), result)
        self.assertEqual(mark.call_args.kwargs["detail"]["android_label"], "安卓 15")
        self.assertEqual(mark.call_args.kwargs["detail"]["android_version"], "15")
        self.assertNotIn("serial", cfg.get("capture", {}))


if __name__ == "__main__":
    unittest.main()
