"""T33：native_lib 多候选 APK 的诊断文案与匹配优先。"""
from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
sys.path.insert(0, str(APP))

from capture import native_lib  # noqa: E402
from qfsky.version_profile import (CURRENT_NATIVE_SHA256, LEGACY_NATIVE_SHA256)  # noqa: E402

LIB = "lib/arm64-v8a/libBootloader.so"


def make_apk(path, body):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(LIB, body)
    return path


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class NativeLibraryCandidateTests(unittest.TestCase):
    def test_version_labels_recognize_both_profiles(self):
        self.assertEqual(native_lib._version_label(LEGACY_NATIVE_SHA256), "0.16.7 / 421589")
        self.assertEqual(native_lib._version_label(CURRENT_NATIVE_SHA256), "0.16.8 / 421629")
        self.assertEqual(native_lib._version_label("0" * 64), "未知版本")

    def test_error_lists_every_candidate_path(self):
        with tempfile.TemporaryDirectory(prefix="t33-native-") as folder:
            folder = Path(folder)
            first = make_apk(folder / "old_base.apk", b"old-native")
            second = make_apk(folder / "new_base.apk", b"new-native")
            with patch.object(native_lib, "_candidate_apks", return_value=[first, second]):
                with self.assertRaises(ValueError) as caught:
                    native_lib.ensure_native_library(dest=folder / "out.so")
            text = str(caught.exception)
            self.assertIn(str(first), text)
            self.assertIn(str(second), text)
            self.assertIn("未知版本", text)
            self.assertIn("0.16.8 / 421629", text)

    def test_error_shows_recognized_legacy_version(self):
        with tempfile.TemporaryDirectory(prefix="t33-native-") as folder:
            folder = Path(folder)
            old = make_apk(folder / "old_base.apk", b"old-native")
            new = make_apk(folder / "new_base.apk", b"new-native")
            labels = {sha(b"old-native"): "0.16.7 / 421589",
                      sha(b"new-native"): "0.16.8 / 421629"}

            def fake_label(value):
                return labels.get(value, "未知版本")

            with patch.object(native_lib, "_candidate_apks", return_value=[old, new]), \
                    patch.object(native_lib, "_version_label", side_effect=fake_label):
                with self.assertRaises(ValueError) as caught:
                    native_lib.ensure_native_library(expected_sha256="f" * 64,
                                                     dest=folder / "out.so")
            text = str(caught.exception)
            self.assertIn("0.16.7 / 421589", text)
            self.assertIn("0.16.8 / 421629", text)
            self.assertIn(str(old), text)
            self.assertIn(str(new), text)

    def test_matching_candidate_is_used_even_if_not_first(self):
        with tempfile.TemporaryDirectory(prefix="t33-native-") as folder:
            folder = Path(folder)
            bad = make_apk(folder / "old_base.apk", b"old-native")
            good = make_apk(folder / "new_base.apk", b"good-native")
            dest = folder / "out.so"
            with patch.object(native_lib, "_candidate_apks", return_value=[bad, good]):
                result = native_lib.ensure_native_library(
                    expected_sha256=sha(b"good-native"), dest=dest)
            self.assertEqual(result["source"], str(good))
            self.assertEqual(dest.read_bytes(), b"good-native")

    def test_apk_without_library_is_listed(self):
        with tempfile.TemporaryDirectory(prefix="t33-native-") as folder:
            folder = Path(folder)
            apk = folder / "broken.apk"
            with zipfile.ZipFile(apk, "w") as zf:
                zf.writestr("classes.dex", b"dex")
            with patch.object(native_lib, "_candidate_apks", return_value=[apk]):
                with self.assertRaises(ValueError) as caught:
                    native_lib.ensure_native_library(dest=folder / "out.so")
            self.assertIn(str(apk), str(caught.exception))
            self.assertIn("没有 lib/arm64-v8a/libBootloader.so", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
