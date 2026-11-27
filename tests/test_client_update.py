"""Desktop updater tests: isolated data, fake GitHub and fake system browser."""
from __future__ import annotations

from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, Mock, patch
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from client_update import LATEST_URL, RELEASE_URL_PREFIX, UpdateChecker, _NoRedirect, current_version, parse_tag, valid_release_url


BUILD = {"release": "1.0.5-desktop-ui.10.2-terrainfix.18", "client_revision": "r10.2-terrainfix.18"}
TAG = "v1.0.5-r10.2-terrainfix.19"
URL = RELEASE_URL_PREFIX + "tag/" + TAG
DOC = {"tag_name": TAG, "html_url": URL, "name": "新版发布", "body": "\n".join(f"更新 {i}" for i in range(10))}


class VersionTests(unittest.TestCase):
    def test_uses_real_build_file_and_all_version_axes(self):
        current = current_version(json.loads((ROOT / "BUILD.json").read_text(encoding="utf-8")))
        self.assertEqual(current.display(), "v1.0.5-r10.2-terrainfix.19")
        for old in ("v1.0.4-r99-terrainfix.99", "v1.0.5-r10.1-terrainfix.99",
                    "v1.0.5-r10.2-terrainfix.9", "v1.0.5-r10.2-terrainfix.18"):
            self.assertLess(parse_tag(old), current)
        for new in ("v1.0.5-r10.2-terrainfix.20", "v1.0.5-r10.12-terrainfix.1",
                    "v1.0.5-r11", "v1.0.6-r1", "v1.1.0-r1"):
            self.assertGreater(parse_tag(new), current)
        self.assertEqual(parse_tag(TAG), current)
        self.assertEqual(parse_tag("v1.0.5-r10.02.0-terrainfix.019"), current)

    def test_current_release_fallbacks_and_invalid_tags(self):
        self.assertEqual(current_version({"release": BUILD["release"]}), current_version(BUILD))
        self.assertEqual(current_version({"release": "1.0.5-desktop", "client_revision": BUILD["client_revision"]}),
                         current_version(BUILD))
        self.assertEqual(current_version({"base_version": "1.0.5", "client_revision": BUILD["client_revision"]}),
                         current_version(BUILD))
        for invalid in ("nonsense", "v1.0.5-rx", "v1.0.5-r10.2-terrainfix.a", "v1.0.5-r10.2-beta", "v1.0.5", None, 2):
            with self.subTest(invalid=invalid):
                self.assertIsNone(parse_tag(invalid))
        self.assertIsNone(current_version({"release": "custom"}))

    def test_canonical_download_allowlist(self):
        self.assertTrue(valid_release_url(URL))
        self.assertTrue(valid_release_url(URL + "?x=1#release"))
        for invalid in (URL.replace("https:", "http:"), URL.replace("github.com", "github.com.evil"),
                        URL.replace("github.com", "github.com" + "@" + "evil.invalid"),
                        URL.replace("/releases/", "/release/"),
                        RELEASE_URL_PREFIX + "%2e%2e/settings", RELEASE_URL_PREFIX + "tag/../../settings",
                        RELEASE_URL_PREFIX + "tag/%2fsettings", RELEASE_URL_PREFIX + "tag/%5csettings",
                        RELEASE_URL_PREFIX + "tag/a\n", RELEASE_URL_PREFIX,
                        URL.replace("github.com", "GITHUB.COM"), "javascript:alert(1)", None):
            with self.subTest(invalid=invalid):
                self.assertFalse(valid_release_url(invalid))

    def test_endpoint_refuses_redirect(self):
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.test"))


class UpdateCheckerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="xg-update-")
        self.addCleanup(temp.cleanup)
        self.data = Path(temp.name) / "本机 设置"
        self.build_file = Path(temp.name) / "BUILD.json"
        self.build_file.write_text(json.dumps(BUILD), encoding="utf-8")
        self.opener = Mock(side_effect=lambda *_a, **_kw: self.payload(DOC))
        self.browser = Mock(return_value=True)
        self.log = Mock()

    @staticmethod
    def payload(doc):
        return io.BytesIO(json.dumps(doc, ensure_ascii=False).encode("utf-8"))

    def checker(self, **kw):
        return UpdateChecker(self.build_file, self.data, opener=self.opener, browser=self.browser, log=self.log, **kw)

    def status(self, checker):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            snap = checker.snapshot()
            if snap["status"] != "checking":
                return snap
            time.sleep(.005)
        self.fail("mock update worker never finished")

    def test_request_worker_and_release_readout(self):
        checker = self.checker()
        self.assertTrue(checker.start(startup=True)["auto_check"])
        self.assertEqual(checker.start(startup=True)["current"], "v1.0.5-r10.2-terrainfix.18")
        snap = self.status(checker)
        self.assertEqual(snap["status"], "available")
        self.assertEqual(snap["release"], {"tag": TAG, "title": "新版发布",
                                          "notes": "\n".join(f"更新 {i}" for i in range(8))})
        self.opener.assert_called_once()
        request, = self.opener.call_args.args
        self.assertEqual(request.full_url, LATEST_URL)
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.data)
        self.assertEqual(dict(request.header_items()), {"User-agent": "XiaoGuang-Updater",
                                                         "Accept": "application/vnd.github+json"})
        self.assertEqual(self.opener.call_args.kwargs, {"timeout": 8})

    def test_disabled_auto_persists_but_manual_runs(self):
        checker = self.checker()
        self.assertTrue(checker.set_auto(False)["ok"])
        self.assertFalse(checker.set_auto("true")["ok"])
        restarted = self.checker()
        self.assertEqual(restarted.start(startup=True)["status"], "idle")
        self.assertFalse(restarted.snapshot()["auto_check"])
        self.opener.assert_not_called()
        restarted.start()
        self.assertEqual(self.status(restarted)["status"], "available")

    def test_skip_persists_across_restarts_but_new_version_shows(self):
        checker = self.checker()
        checker.start(startup=True)
        self.assertEqual(self.status(checker)["status"], "available")
        self.assertTrue(checker.skip())
        pref = json.loads((self.data / "control/client-update.json").read_text(encoding="utf-8"))
        self.assertEqual(pref, {"auto_check": True, "skipped_versions": [TAG]})
        restarted = self.checker()
        self.opener.side_effect = lambda *_a, **_kw: self.payload({**DOC, "tag_name": "v1.0.5-r10.02-terrainfix.19"})
        restarted.start(startup=True)
        self.assertEqual(self.status(restarted)["status"], "no_update")
        self.opener.side_effect = lambda *_a, **_kw: self.payload({**DOC, "tag_name": "v1.0.5-r10.2-terrainfix.20"})
        restarted.start()
        self.assertEqual(self.status(restarted)["status"], "available")

    def test_later_only_dismisses_until_next_check_and_open_uses_verified_url(self):
        checker = self.checker()
        self.assertFalse(checker.open_release())
        checker.start()
        self.assertEqual(self.status(checker)["status"], "available")
        self.assertTrue(checker.dismiss())
        self.assertIsNone(checker.snapshot()["release"])
        self.assertFalse(self.data.exists())
        checker.start()
        self.assertEqual(self.status(checker)["status"], "available")
        self.assertTrue(checker.open_release())
        self.browser.assert_called_once_with(URL)
        self.assertIsNone(checker.snapshot()["release"])

    def test_invalid_current_is_no_update_without_network(self):
        self.build_file.write_text('{"release":"wrong","client_revision":"unknown"}', encoding="utf-8")
        self.assertEqual(self.checker().start()["status"], "no_update")
        self.opener.assert_not_called()

    def test_older_equal_invalid_tag_and_offsite_report_no_update(self):
        checker = self.checker()
        for fields in ({"tag_name": "v1.0.5-r10.2-terrainfix.18"},
                       {"tag_name": "v1.0.4-r99-terrainfix.99"},
                       {"tag_name": "not-a-version"}, {"html_url": "https://example.net/releases/tag/v2"}):
            with self.subTest(fields=fields):
                self.opener.side_effect = lambda *_a, **_kw: self.payload({**DOC, **fields})
                checker.start()
                self.assertEqual(self.status(checker)["status"], "no_update")
                self.assertFalse(checker.open_release())
        self.browser.assert_not_called()

    def test_rate_limit_proxy_and_unreachable_are_silent(self):
        checker = self.checker()
        for code in (403, 429):
            self.opener.side_effect = HTTPError(LATEST_URL, code, "fixture", {}, io.BytesIO(b""))
            checker.start()
            self.assertEqual(self.status(checker)["status"], "silent")
            self.log.assert_called_with("update_check_http_failure", status=code)
        self.opener.side_effect = URLError("sensitive proxy url")
        checker.start()
        self.assertEqual(self.status(checker)["status"], "silent")
        self.log.assert_called_with("update_check_network_failure", error="URLError")
        self.assertNotIn("sensitive", repr(self.log.call_args_list))

    def test_network_is_off_thread_and_has_absolute_prompt_deadline(self):
        caller = threading.get_ident()
        entered, release = threading.Event(), threading.Event()
        worker_thread = []

        def blocked(*_args, **_kw):
            worker_thread.append(threading.get_ident())
            entered.set()
            release.wait(3)
            return self.payload(DOC)

        self.opener.side_effect = blocked
        checker = self.checker(timeout=.15)
        try:
            self.assertEqual(checker.start(startup=True)["status"], "checking")
            self.assertTrue(entered.wait(1))
            self.assertNotEqual(worker_thread, [caller])
            self.assertEqual(self.status(checker)["status"], "silent")
            self.assertEqual(checker.start()["status"], "silent")
            self.opener.assert_called_once()
        finally:
            worker = checker._worker
            release.set()
            worker.join(2)
        self.assertEqual(checker.snapshot()["status"], "silent")
        self.assertFalse(checker.open_release())
        self.log.assert_any_call("update_check_timeout")

    def test_invalid_payload_and_write_error(self):
        checker = self.checker()
        self.opener.side_effect = lambda *_a, **_kw: io.BytesIO(b"invalid json")
        checker.start()
        self.assertEqual(self.status(checker)["status"], "no_update")
        self.log.assert_called_with("update_check_invalid_response")
        with patch("client_update.xg_paths.write_json", side_effect=PermissionError("sensitive path")):
            result = checker.set_auto(False)
            self.assertFalse(result["ok"])
            self.assertTrue(result["auto_check"])
            self.log.assert_called_with("update_preferences_write_failed", error="PermissionError")


class UpdateUITests(unittest.TestCase):
    def test_desktop_settings_and_plain_text_release(self):
        result = subprocess.run([str(ROOT / "runtime/node/node.exe"), str(ROOT / "tests/client_update_ui_scenarios.cjs")],
                                cwd=ROOT, text=True, encoding="utf-8", capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_host_starts_checker_only_for_desktop_and_exposes_scoped_actions(self):
        spec = importlib.util.spec_from_file_location("update_host_under_test", ROOT / "desktop.py")
        host = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(host)
        updater, backend, wx = Mock(), Mock(), MagicMock()
        backend.data = ROOT
        updater.snapshot.return_value = {"status": "checking"}
        updater.open_release.return_value = True
        with patch.object(host, "DesktopInstance"), patch.object(host, "Backend", return_value=backend), \
                patch.object(host.webview2_probe, "available", return_value=True), \
                patch.object(host, "_make_updater", return_value=updater) as make, patch.dict(sys.modules, webview=wx):
            self.assertEqual(host._run_desktop(Mock(backend_smoke=False), ROOT, ROOT), (0, False))
        make.assert_called_once_with(ROOT, ROOT)
        updater.start.assert_called_once_with(startup=True)
        controls = wx.create_window.call_args.kwargs["js_api"]
        self.assertEqual(controls.update_get(), {"status": "checking"})
        self.assertTrue(controls.update_open_release())
        updater.open_release.assert_called_once_with()
        updater.reset_mock()
        with patch.object(host, "DesktopInstance"), patch.object(host, "Backend", return_value=backend), \
                patch.object(host, "_make_updater", return_value=updater) as make, redirect_stdout(io.StringIO()):
            self.assertEqual(host._run_desktop(Mock(backend_smoke=True, smoke_report=None), ROOT, ROOT), (0, False))
        make.assert_not_called()
        updater.start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
