"""Distribution version consistency without importing account/runtime services."""
from pathlib import Path
import json
import unittest

ROOT = Path(__file__).resolve().parents[1]
VERSION = '1.0.5-desktop-ui.10.2-terrainfix.11'

class ReleaseVersionTests(unittest.TestCase):
    def test_build_version(self):
        doc = json.loads((ROOT/'BUILD.json').read_text(encoding='utf-8'))
        self.assertEqual(doc['release'], VERSION)
        self.assertEqual(doc['client_revision'], 'r10.2-terrainfix.11')

    def test_manifest_version(self):
        doc = json.loads((ROOT/'PACKAGE-MANIFEST.json').read_text(encoding='utf-8'))
        self.assertEqual(doc['release'], VERSION)

    def test_runtime_version_and_protocol_pin(self):
        doc = json.loads((ROOT/'app/config/experimental-hop-trial.runtime.json').read_text(encoding='utf-8'))
        self.assertEqual(doc['clientRelease'], VERSION)
        self.assertEqual(doc['hopV20']['codecSHA256'], '0b18ecc44e873f6caa181e6bc49f14b7e07058888523e47db52a14b32077a7ed')

    def test_desktop_visible_version(self):
        html = (ROOT/'app/web/desktop.html').read_text(encoding='utf-8')
        self.assertIn("id='client-release'", html)
        self.assertIn('>'+VERSION+'</p>', html)
        self.assertIn('小光 · 桌面 · '+VERSION+'</title>', html)

if __name__ == '__main__':
    unittest.main()
