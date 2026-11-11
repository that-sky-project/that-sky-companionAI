"""Offline feedback regressions: truthful completion, visible progress, no extra map captures."""
import ast
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ResourceFeedbackTests(unittest.TestCase):
    def test_frontend_models_and_live_refresh_rendering(self):
        node = os.environ.get("XG_TEST_NODE") or shutil.which("node")
        self.assertTrue(node, "Node is required for the desktop UI regression suite")
        command = [node, str(ROOT / "tests/resource_feedback_scenarios.cjs")]
        if os.environ.get("XG_RESOURCE_TEST_OUTPUT"):
            command.append(os.environ["XG_RESOURCE_TEST_OUTPUT"])
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr[-3500:])
        summary = json.loads(result.stdout)
        self.assertGreaterEqual(summary["passed"], 30)
        self.assertEqual(summary["failed"], 0)

    def hint(self, scene):
        path = ROOT / "app/capture/worker.py"
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "recorded_hint")
        namespace = {"HOME_SCENE": "CandleSpace", "scene_name": lambda value: value}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), "exec"), namespace)
        return namespace["recorded_hint"](scene, 1)

    def test_home_success_is_persistent_and_never_requests_other_maps(self):
        message = self.hint("CandleSpace")
        self.assertIn("采集成功", message)
        self.assertIn("自动登记", message)
        self.assertIn("不需要逐张跑图", message)
        self.assertIn("停止采集", message)
        self.assertNotIn("去下一张图", message)

    def test_other_map_hint_still_explains_home_only_installation(self):
        message = self.hint("fixture-other-map")
        self.assertIn("只需要遇境录制", message)
        self.assertNotIn("去下一张图", message)


if __name__ == "__main__":
    unittest.main()
