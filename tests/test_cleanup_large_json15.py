"""Cleanup allowlist covers large generated terrain/hop JSON (tester hit large_text_unverified)."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT / "app/launcher", ROOT / "app", ROOT / "app/bot"):
    sys.path.insert(0, str(p))
import data_cleanup as dc  # noqa: E402

ok = dc.CleanupService._large_json_allowed


class LargeJsonAllowlistTests(unittest.TestCase):
    def test_generated_large_files_allowed(self):
        for rel in ("game/0.16.8-421629/terrain/assets.work/beamo-world-all-v1/DuskMid_Past/beamo/manifest.json",
                    "game/0.16.7-421589/terrain/assets/Dawn/static-world/manifest.json",
                    "game/0.16.8-421629/terrain/assets/Dawn/static-world/runtime-manifest.json",
                    "hop-proofs/hop-00000000-0000-4000-8000-000000000000.json",
                    "game/0.16.8-421629/terrain/assets.work/beamo-world-all-v1/Dawn/beamo/manifest.json.pending"):
            self.assertTrue(ok(rel), rel)

    def test_unknown_large_text_still_refused(self):
        for rel in ("game/0.16.8-421629/terrain/assets.work/beamo-world-all-v1/Dawn/beamo/secret.json",
                    "game/0.16.8-421629/terrain/assets/Dawn/notes/manifest.json",
                    "hop-proofs/notes.json", "hop-proofs/sub/hop-00000000-0000-4000-8000-000000000000.json",
                    "game/0.16.8-421629/names/catalog.json", "logs/companion.jsonl"):
            self.assertFalse(ok(rel), rel)


if __name__ == "__main__":
    unittest.main()
