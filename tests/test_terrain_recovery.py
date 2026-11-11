"""Offline terrain resume regressions; no accounts or game processes are accessed."""
from __future__ import annotations
import ast
import ctypes
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
sys.path.insert(0, str(APP))
from xg_gamedata import spawn_loader, status, terrain
from xg_gamedata.common import GameDataError, Progress, game_dir

ENABLED = sorted(spawn_loader.load()["placements"])
DISABLED = ["AR_TestLevel", "Credits", "WorldEmpty"]
ALL_SCENES = sorted(ENABLED + DISABLED)


def builder_helpers(logs):
    """Load the actual lightweight functions, not the APK/native build dependencies."""
    path = APP / "xg_gamedata/terrain/terrain_build.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = {"_abs", "_output_path", "atomic_bytes", "StageProgress"}
    nodes = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names]
    assert len(nodes) == len(names)
    namespace = {"os": os, "Path": Path, "time": time, "log": logs.append,
                 "fmt_seconds": str, "_long_path": os.fspath}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)
    return namespace


class EnabledMapProgressTests(unittest.TestCase):
    def setUp(self):
        self.logs = []
        self.StageProgress = builder_helpers(self.logs)["StageProgress"]

    def test_enabled_source_is_115_without_deprecated_scenes(self):
        self.assertEqual(len(ENABLED), 115)
        self.assertTrue(set(ENABLED).isdisjoint(DISABLED))

    def test_resource_progress_and_enabled_progress_are_distinct(self):
        progress = self.StageProgress("assemble", 118, ENABLED)
        for index, scene in enumerate(ALL_SCENES, 1):
            progress.update(scene + " ready", done=index, force=True)
        self.assertEqual(progress.total, 118)
        self.assertEqual(progress.done, 118)
        self.assertEqual(progress.completed_maps, set(ENABLED))
        self.assertTrue(self.logs[-1].startswith("[assemble 115/115]"))
        self.assertFalse(any("/118]" in line for line in self.logs))

    def test_excluded_scene_does_not_increment_enabled_count(self):
        progress = self.StageProgress("base", 118, ENABLED)
        progress.update("AR_TestLevel empty", done=1, force=True)
        self.assertTrue(self.logs[-1].startswith("[base 0/115]"))

    def test_out_of_order_or_duplicate_nav_events_count_once(self):
        progress = self.StageProgress("nav", 118, ENABLED)
        for index, name in enumerate((ENABLED[5], ENABLED[0], ENABLED[5]), 1):
            progress.update(name + " ready", done=index, force=True)
        self.assertEqual(len(progress.completed_maps), 2)
        self.assertTrue(self.logs[-1].startswith("[nav 2/115]"))

    def test_verified_stage_completion_accounts_for_resume_suppressed_events(self):
        progress = self.StageProgress("base", 118, ENABLED)
        progress.update("base 完成", done=118, force=True)
        self.assertTrue(self.logs[-1].startswith("[base 115/115]"))

    def test_internal_throttle_does_not_drop_completed_scene_counts(self):
        progress = self.StageProgress("world", 118, ENABLED)
        with patch.object(time, "monotonic", return_value=100):
            progress._last = 100
            progress.update(ENABLED[0] + " ready", done=1)
            progress.update(ENABLED[1] + " ready", done=2)
        self.assertEqual(progress.completed_maps, set(ENABLED[:2]))

    def test_direct_builder_without_filter_still_reports_resource_count(self):
        progress = self.StageProgress("assemble", 118)
        progress.update("all done", done=118, force=True)
        self.assertTrue(self.logs[-1].startswith("[assemble 118/118]"))


class OutputPathTests(unittest.TestCase):
    def setUp(self):
        self.helpers = builder_helpers([])

    def test_new_output_preserves_long_filename(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "multimap-catalog.json"
            self.helpers["atomic_bytes"](path, b"first")
            self.helpers["atomic_bytes"](path, b"second")
            self.assertEqual(path.read_bytes(), b"second")
            self.assertFalse(path.with_name(path.name + ".t").exists())

    @unittest.skipUnless(os.name == "nt", "Windows 8.3 alias regression")
    def test_replace_via_real_windows_short_alias_keeps_long_filename(self):
        with tempfile.TemporaryDirectory(prefix="xg-catalog-alias-") as folder:
            path = Path(folder) / "multimap-catalog.json"
            path.write_bytes(b"old")
            short_path = ctypes.windll.kernel32.GetShortPathNameW
            short_path.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
            short_path.restype = ctypes.c_uint32
            for value in (b"new-1", b"new-2"):
                buf = ctypes.create_unicode_buffer(32768)
                count = short_path(str(path), buf, len(buf))
                self.assertTrue(count)
                alias = Path(buf.value)
                if alias.name.casefold() == path.name.casefold():
                    self.skipTest("8.3 aliases disabled on this test volume")
                self.assertEqual(self.helpers["_output_path"](alias).name, path.name)
                self.helpers["atomic_bytes"](alias, value)
                self.assertTrue(path.is_file(), "atomic replace must preserve canonical filename")
                self.assertEqual(path.read_bytes(), value)


class TerrainWrapperTests(unittest.TestCase):
    def run_fixture(self, *, maps=None, existing=False, exit_code=0, missing_catalog=False, omit_scene=None):
        with tempfile.TemporaryDirectory(prefix="xg-terrain-fixture-") as folder:
            data = Path(folder)
            root = game_dir(data) / "terrain"
            root.mkdir(parents=True)
            target = root / "multimap-catalog.json"
            if existing:
                target.write_text("{}", encoding="utf-8")
            requested = maps or ALL_SCENES
            entries = [{"scene_name": scene} for scene in requested if scene != omit_scene]
            seen = {}
            short_calls = []
            def short_path(path):
                short_calls.append(Path(path))
                if Path(path).is_file():
                    return str(Path(path).with_name("MULTIM~1.JSO"))
                return str(path)
            def popen(command, **kwargs):
                seen["command"] = command
                out = Path(command[command.index("--out") + 1])
                (out / "fixture.bin").write_bytes(b"fixture")
                if not missing_catalog:
                    Path(command[command.index("--catalog") + 1]).write_text(
                        json.dumps({"profile": "experimental_multimap_v13", "entries": entries}), encoding="utf-8")
                total = len(maps or ENABLED)
                return types.SimpleNamespace(stdout=io.StringIO(f"[assemble {total}/{total}] fixture ready\n"),
                                             wait=lambda: exit_code)
            stream = io.StringIO()
            progress = Progress("terrain", 0, stream=stream)
            with patch.object(terrain, "ensure_pipeline"), patch.object(terrain, "inspect_apk"), \
                 patch.object(terrain, "xg_paths", return_value=types.SimpleNamespace(short_path=short_path)), \
                 patch.object(terrain.xg_exec, "popen", side_effect=popen):
                reply = terrain.run(data / "fixture.apk", data, maps, existing, 1, progress)
            return reply, seen["command"], short_calls, [json.loads(line) for line in stream.getvalue().splitlines()]

    def test_resume_keeps_long_output_filename_with_existing_catalog(self):
        result, command, calls, events = self.run_fixture(existing=True)
        catalog_arg = command[command.index("--catalog") + 1]
        self.assertEqual(Path(catalog_arg).name, "multimap-catalog.json")
        self.assertTrue(all(path.name != "multimap-catalog.json" for path in calls))
        self.assertIn("--resume", command)
        self.assertEqual(result["maps"], 115)

    def test_default_generation_keeps_all_resources_and_only_filters_progress(self):
        result, command, calls, events = self.run_fixture()
        self.assertNotIn("--maps", command)
        self.assertEqual(set(command[command.index("--progress-maps") + 1].split(",")), set(ENABLED))
        self.assertEqual((result["maps"], result["resource_maps"], result["excluded_maps"]), (115, 118, 3))
        self.assertTrue(all(event["total"] == 115 for event in events))
        self.assertEqual(events[-1]["done"], 115)

    def test_explicit_scene_build_still_supported(self):
        result, command, calls, events = self.run_fixture(maps=["CandleSpace"])
        self.assertEqual(command[command.index("--maps") + 1], "CandleSpace")
        self.assertEqual(result["maps"], 1)
        self.assertEqual(events[-1]["total"], 1)

    def test_child_failure_is_not_masked_by_existing_catalog(self):
        with self.assertRaises(GameDataError) as caught:
            self.run_fixture(existing=True, exit_code=1)
        self.assertEqual(caught.exception.code, "terrain_failed")

    def test_missing_catalog_still_fails_closed(self):
        with self.assertRaises(GameDataError) as caught:
            self.run_fixture(missing_catalog=True)
        self.assertEqual(caught.exception.code, "terrain_no_catalog")

    def test_missing_enabled_scene_is_not_reported_as_complete(self):
        with self.assertRaises(GameDataError) as caught:
            self.run_fixture(omit_scene=ENABLED[0])
        self.assertEqual(caught.exception.code, "terrain_catalog_incomplete")

    def test_status_reports_enabled_count_and_preserves_resource_count(self):
        with tempfile.TemporaryDirectory(prefix="xg-terrain-status-") as folder:
            data = Path(folder)
            root = game_dir(data) / "terrain"
            root.mkdir(parents=True)
            (root / "multimap-catalog.json").write_text(json.dumps({
                "profile": "experimental_multimap_v13", "entries": [{"scene_name": name} for name in ALL_SCENES]
            }), encoding="utf-8")
            value = status.collect(data)["terrain"]
            self.assertEqual((value["maps"], value["resource_maps"], value["excluded_maps"]), (115, 118, 3))


if __name__ == "__main__":
    unittest.main()
