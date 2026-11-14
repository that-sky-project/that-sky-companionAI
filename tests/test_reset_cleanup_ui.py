"""Offline cleanup UI regressions; no account bootstrap or live network requests."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ResetCleanupUITests(unittest.TestCase):
    def test_cleanup_confirmation_and_resource_state_contract(self):
        node = os.environ.get('XG_TEST_NODE') or str(ROOT / 'runtime/node/node.exe')
        if not Path(node).is_file():
            node = shutil.which('node')
        self.assertTrue(node, 'Node is required for cleanup UI fixtures')
        result = subprocess.run([node, str(ROOT / 'tests/reset_cleanup_scenarios.cjs')],
                                cwd=ROOT, capture_output=True, text=True, encoding='utf-8', timeout=45)
        self.assertEqual(result.returncode, 0, result.stderr[-5000:])
        report = json.loads(result.stdout)
        self.assertGreaterEqual(report['passed'], 35)
        self.assertEqual(report['failed'], 0)


if __name__ == '__main__':
    unittest.main()
