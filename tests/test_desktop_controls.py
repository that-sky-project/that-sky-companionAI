"""Run desktop control regressions in an isolated child, never the live app."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "desktop_backend_scenarios.py"


class DesktopControlTests(unittest.TestCase):
    def test_isolated_control_api_and_native_rule_engine(self):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
        result = subprocess.run(
            [sys.executable, "-X", "utf8", "-B", str(FIXTURE)], cwd=ROOT, env=env,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90,
        )
        detail = (result.stdout + "\n" + result.stderr)[-16000:]
        self.assertEqual(result.returncode, 0, detail)
        records = [line.removeprefix("SCENARIO_RESULT=")
                   for line in result.stdout.splitlines() if line.startswith("SCENARIO_RESULT=")]
        self.assertEqual(len(records), 1, detail)
        summary = json.loads(records[0])
        self.assertTrue(summary["successful"], detail)
        self.assertEqual(summary["testsRun"], 28, detail)
        self.assertEqual(summary["failures"], 0, detail)
        self.assertEqual(summary["errors"], 0, detail)
        self.assertEqual(summary["ruleCompilationFailures"], [], detail)
        self.assertEqual(sum(summary["rules"].values()), 780, detail)
        self.assertTrue(summary["realPcre2"], detail)
        self.assertEqual(summary["upstream"], "blocked", detail)
        self.assertEqual(summary["gameProcesses"], "blocked", detail)
        if os.name == "nt":
            self.assertEqual(summary["skipped"], 0, detail)


if __name__ == "__main__":
    unittest.main()
