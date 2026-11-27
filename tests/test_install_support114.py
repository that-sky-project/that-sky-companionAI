"""T114 installation and device wiring: no external servers or actual emulator."""
import errno
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
for directory in (ROOT, APP, APP / "launcher", APP / "bot"):
    sys.path.insert(0, str(directory))

import desktop
import install_support
import capture_ctl
import tasks
import wizard
from capture import gamedata, worker
from xg_gamedata import apk
from xg_gamedata.common import GameDataError


def windows_error(code):
    exc = OSError("fixture")
    exc.winerror = code
    return exc


class InstallationTests(unittest.TestCase):
    def test_os_diagnostics_match_cause_and_have_action(self):
        cases = [
            (windows_error(225), "", "security_blocked", "保护历史记录"),
            (windows_error(206), "", "path_too_long", "短路径"),
            (windows_error(112), "", "disk_full", "空间"),
            (windows_error(1455), "", "memory_low", "虚拟内存"),
            (windows_error(193), "", "runtime_arch", "64 位 Windows 10/11"),
            (windows_error(126), "webview", "webview2_unavailable", "Evergreen"),
            (PermissionError(errno.EACCES, "local"), "data", "data_write_denied", "%LOCALAPPDATA%"),
            (FileNotFoundError(), "", "runtime_missing", "重新解压"),
        ]
        for error, stage, expected, hint in cases:
            with self.subTest(expected):
                result = install_support.problem(error, stage=stage)
                self.assertEqual(result["code"], expected)
                self.assertIn(hint, result["message"])
                self.assertNotIn("fixture", result["message"])

    def test_low_disk_advisory(self):
        with mock.patch.object(install_support.shutil, "disk_usage", return_value=SimpleNamespace(free=128)):
            self.assertTrue(any("空间" in warning for warning in install_support.wizard_warnings(ROOT, ROOT)))

    def test_native_startup_retry_occurs_after_failed_attempt_finished(self):
        with mock.patch.object(desktop, "_run_desktop", side_effect=[(1, "先修复"), (0, False)]) as attempt, \
             mock.patch.object(desktop, "_retry_message", return_value=True) as retry, \
             mock.patch.object(sys, "argv", ["desktop.py", "--data-dir", str(ROOT / "中文 用户")]):
            self.assertEqual(desktop.main(), 0)
        self.assertEqual(attempt.call_count, 2)
        self.assertEqual(attempt.call_args.args[2], ROOT / "中文 用户")
        retry.assert_called_once_with("先修复")

    def test_bundled_backend_paths_with_chinese_and_space(self):
        root = ROOT / "中文 客户端"
        data = ROOT / "另一个 中文 用户"
        fake_proc = SimpleNamespace(pid=123, poll=lambda: None)
        with mock.patch.object(Path, "is_file", return_value=True), mock.patch.object(Path, "mkdir"), \
             mock.patch.object(Path, "read_text", return_value='{"pid":123,"port":55555}'), \
             mock.patch("builtins.open", mock.mock_open()), mock.patch.object(desktop, "_cleanup_stale_backends"), \
             mock.patch.object(desktop, "_assign_kill_on_close_job"), mock.patch.object(desktop, "_lifecycle_log"), \
             mock.patch.object(desktop.subprocess, "Popen", return_value=fake_proc) as spawn:
            desktop.Backend(root, data).start()
        argv, = spawn.call_args.args
        self.assertEqual(Path(argv[0]), root / "runtime/python/python.exe")
        self.assertEqual(Path(argv[2]), root / "app/bot/control_api.py")
        self.assertEqual(spawn.call_args.kwargs["env"]["XG_NODE"], str(root / "runtime/node/node.exe"))
        self.assertEqual(spawn.call_args.kwargs["env"]["XG_DATA_DIR"], str(data))

    def test_resource_command_exits_without_json_and_has_human_hint(self):
        fake = SimpleNamespace(stdout="", stderr="MemoryError: synthetic details", returncode=1)
        with mock.patch.object(gamedata, "command", return_value=["fixture"]), \
             mock.patch.object(gamedata, "env", return_value={}), \
             mock.patch.object(gamedata.xg_exec, "run", return_value=fake):
            result = gamedata.run(["check-apk"])
        self.assertEqual(result["final"]["code"], "memory_low")
        self.assertIn("虚拟内存", result["final"]["error"])
        self.assertIn("synthetic details", result["diagnostic"])

    def test_no_disk_space_to_initialize_installation_task(self):
        with mock.patch.object(tasks, "current", return_value={"running": False}), \
             mock.patch.object(tasks.xg_paths, "ensure_dirs"), \
             mock.patch.object(tasks.xg_paths, "write_json", side_effect=OSError(errno.ENOSPC, "fixture")), \
             mock.patch.object(tasks.xg_proc, "spawn_detached") as spawn:
            result = tasks.start("pull-apk", [["pull-apk"]])
        self.assertEqual(result["code"], "disk_full")
        self.assertFalse(result["ok"])
        spawn.assert_not_called()

    def test_capture_start_security_error_does_not_look_like_root(self):
        with mock.patch.object(capture_ctl, "running", return_value=False), \
             mock.patch.object(capture_ctl.xg_paths, "ensure_dirs"), mock.patch.object(Path, "unlink"), \
             mock.patch.object(capture_ctl.xg_proc, "spawn_detached", side_effect=windows_error(225)):
            result = capture_ctl.start()
        self.assertEqual(result["code"], "security_blocked")
        self.assertIn("保护历史记录", result["message"])
        self.assertNotIn("Root", result["message"])

    def test_wrong_build_does_not_pass_via_correct_display_version(self):
        ok, message = wizard._version_ok({"version_code": 999999, "version": "0.16.8"})
        self.assertFalse(ok)
        self.assertIn("999999", message)
        self.assertIn("421629", message)

    def test_wizard_does_not_save_discovered_dhcp_address_or_adb_as_override(self):
        cfg = {}
        result = {"ok": True, "adb": "mock.exe", "serial": "192.168.3.42:5555", "mumu_dir": str(ROOT)}
        with mock.patch.object(wizard, "_cfg", return_value=cfg), mock.patch.object(wizard, "mark"), \
             mock.patch.object(wizard.xg_paths, "save_config"), mock.patch.object(wizard.device, "check_device", return_value=result):
            self.assertEqual(wizard.check_mumu(), result)
        self.assertNotIn("serial", cfg["capture"])
        self.assertNotIn("adb", cfg["capture"])

    def test_wizard_pull_preserves_game_discovery_reason(self):
        def no_device(adb, **kwargs):
            self.assertEqual(kwargs["mumu_dir"], ROOT)
            kwargs["diagnostics"].update(reason="game_in_background", message="请把光遇切到前台")
        with mock.patch.object(wizard.device, "find_adb", return_value=("fake.py", ROOT)), \
             mock.patch.object(wizard.device, "find_game", side_effect=no_device), \
             mock.patch.object(wizard.tasks, "start") as spawn:
            result = wizard.pull_apk()
        self.assertEqual(result, {"ok": False, "code": "game_in_background", "message": "请把光遇切到前台"})
        spawn.assert_not_called()

    def test_apk_pull_rejects_public_and_usb_before_any_adb(self):
        with mock.patch.object(apk, "_device_list") as devices:
            for serial in ("8.8.8.8:5555", "R5USB123"):
                with self.subTest(serial), self.assertRaises(GameDataError) as error:
                    apk.pull_apk("fake.exe", serial, ROOT)
                self.assertEqual(error.exception.code, "unsupported_device_scope")
        devices.assert_not_called()

    def test_worker_keeps_transport_failure_separate_from_root_switch(self):
        seen = []
        def root_failure(*args, **kwargs):
            kwargs["diagnostics"].update(reason="adb_disconnected", message="MuMu 已断开，请重新连接")
            return False
        with mock.patch.object(wizard.xg_paths, "ensure_dirs"), mock.patch.object(Path, "unlink"), \
             mock.patch.object(worker, "_cfg", return_value=({}, {}, {})), \
             mock.patch.object(worker, "_fake_record_dir", return_value=ROOT), \
             mock.patch.object(worker, "stop_requested", return_value=False), \
             mock.patch.object(worker, "read_status", return_value={}), \
             mock.patch.object(worker, "write_status", side_effect=lambda status: seen.append(dict(status))), \
             mock.patch.object(worker.device, "find_adb", return_value=("fake.py", ROOT)), \
             mock.patch.object(worker.device, "find_game", return_value={"serial": "192.168.3.42:5555", "pid": 190}), \
             mock.patch.object(worker.device, "ensure_root", side_effect=root_failure):
            self.assertEqual(worker.run(once=True), 1)
        self.assertEqual(seen[-1]["last_error_code"], "adb_disconnected")
        self.assertEqual(seen[-1]["message"], "MuMu 已断开，请重新连接")

    @unittest.skipUnless((ROOT / "runtime/node/node.exe").is_file(), "portable Node absent")
    def test_wizard_retry_offline_frontend(self):
        result = subprocess.run([str(ROOT / "runtime/node/node.exe"),
                                 str(ROOT / "tests/wizard_retry114_scenarios.cjs")], cwd=ROOT,
                                capture_output=True, encoding="utf-8", errors="replace", timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
