# -*- coding: utf-8 -*-
"""游戏更新后旧版本遇境录制必须失效：状态、文案、目录构建、界面。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"


class StaleCaptureBackendTests(unittest.TestCase):
    def scenario(self, mode):
        with tempfile.TemporaryDirectory(prefix="xg-stale-capture-") as tmp:
            env = dict(os.environ, XG_DATA_DIR=tmp, PYTHONUTF8="1",
                       PYTHONDONTWRITEBYTECODE="1")
            result = subprocess.run(
                [sys.executable, "-X", "utf8", "-B",
                 str(ROOT / "tests/stale_capture_scenarios.py"), mode],
                cwd=APP, env=env, capture_output=True, text=True,
                encoding="utf-8", timeout=45)
            self.assertEqual(result.returncode, 0, result.stderr[-3000:])
            return json.loads(result.stdout.strip().splitlines()[-1])

    def test_old_version_recording_is_stale_and_never_counted_as_recorded(self):
        row = self.scenario("stale")
        self.assertEqual(row["recorded"], [])
        self.assertEqual(len(row["stale"]), 1)
        self.assertEqual(row["stale"][0]["version"], "0.16.7")
        self.assertEqual(row["stale"][0]["apk_build"], 421589)
        self.assertIsNone(row["last_registered"])
        self.assertFalse(row["capture_done"])
        self.assertIn("游戏已更新到 0.16.8", row["stale_notice"])
        self.assertIn("之前的遇境录制是 0.16.7 的", row["stale_notice"])
        self.assertIn("需要用 MuMu 重新录一次遇境（只需遇境一张）", row["stale_notice"])
        self.assertEqual(row["capture_hint"], row["stale_notice"])
        self.assertEqual(row["capture_message"], row["stale_notice"])
        self.assertNotIn("采集成功", row["capture_message"])

    def test_old_version_still_blocks_catalog_build(self):
        row = self.scenario("stale")
        self.assertFalse(row["catalog_start"]["ok"])
        self.assertEqual(row["catalog_start"]["code"], "capture_stale")
        self.assertEqual(row["catalog_start"]["message"], row["stale_notice"])

    def test_data_is_rebuilt_but_login_session_is_version_independent(self):
        row = self.scenario("stale")
        self.assertTrue(row["data_needs_rebuild"])
        self.assertIn("新版本需要重新生成游戏数据", row["data_hint"])
        self.assertEqual([item["version"] for item in row["game_data"]["stale"]], ["0.16.7"])
        # 账号登录会话与游戏版本无关：版本变化后仍是已登录。
        self.assertTrue(row["login_done"])

    def test_current_version_recording_is_accepted(self):
        row = self.scenario("current")
        self.assertEqual(row["recorded"], ["CandleSpace"])
        self.assertEqual(row["stale"], [])
        self.assertEqual(row["stale_notice"], "")
        self.assertIsNotNone(row["last_registered"])
        self.assertEqual(row["last_registered"]["record_dir"], "CandleSpace_auto_new")
        self.assertTrue(row["catalog_start"]["ok"])
        self.assertFalse(row["data_needs_rebuild"])

    def test_legacy_frontends_show_the_stale_notice(self):
        for name in ("wizard.js", "w4b.js"):
            text = (APP / "web" / name).read_text(encoding="utf-8")
            self.assertIn("需要重新录制遇境", text, name)
            self.assertIn("stale_notice", text, name)


class StaleCaptureUiTests(unittest.TestCase):
    def test_desktop_capture_and_catalog_views(self):
        node = os.environ.get("XG_TEST_NODE") or shutil.which("node")
        self.assertTrue(node, "Node is required for the desktop UI regression suite")
        result = subprocess.run(
            [node, str(ROOT / "tests/stale_capture_ui_scenarios.cjs")],
            capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr[-3000:])
        summary = json.loads(result.stdout)
        self.assertEqual(summary["failed"], 0)
        self.assertGreaterEqual(summary["passed"], 6)


if __name__ == "__main__":
    unittest.main()
