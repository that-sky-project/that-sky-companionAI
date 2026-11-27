"""terrainfix.15: atomic JSON write survives a target that refuses rename (AV / other instance)."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
import xg_paths  # noqa: E402


class WriteJsonTests(unittest.TestCase):
    def test_normal_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "c.json"
            xg_paths.write_json(p, {"a": 1})
            self.assertEqual(json.loads(p.read_text(encoding="utf-8")), {"a": 1})

    def test_transient_denial_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "c.json"
            xg_paths.write_json(p, {"a": 1})
            real = os.replace
            calls = {"n": 0}

            def flaky(src, dst):
                calls["n"] += 1
                if calls["n"] < 3:
                    raise PermissionError(5, "denied")
                return real(src, dst)
            with mock.patch("os.replace", side_effect=flaky), mock.patch("time.sleep"):
                xg_paths.write_json(p, {"a": 2})
            self.assertEqual(json.loads(p.read_text(encoding="utf-8")), {"a": 2})
            self.assertEqual(calls["n"], 3)

    def test_persistent_denial_preserves_old_document(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "c.json"
            xg_paths.write_json(p, {"a": 1, "long": "x" * 200})
            with mock.patch("os.replace", side_effect=PermissionError(5, "denied")), mock.patch("time.sleep"):
                with self.assertRaises(PermissionError):
                    xg_paths.write_json(p, {"a": 2})
            self.assertEqual(json.loads(p.read_text(encoding="utf-8")), {"a": 1, "long": "x" * 200})
            self.assertFalse(list(p.parent.glob("*.pending")))


if __name__ == "__main__":
    unittest.main()
