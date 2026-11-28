"""terrainfix.20：更新检查限流回退、.NET 启动失败诊断、启动日志脱敏（离线）。"""
import email.message
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))
import client_update as cu  # noqa: E402
import install_support  # noqa: E402

TAG = "v1.0.5-r10.2-terrainfix.99"


def _err(url, code, location=None):
    headers = email.message.Message()
    if location:
        headers["Location"] = location
    return HTTPError(url, code, "x", headers, io.BytesIO(b""))


class FallbackTests(unittest.TestCase):
    def _run(self, opener):
        d = tempfile.mkdtemp()
        build = Path(d) / "BUILD.json"
        build.write_text(json.dumps({"release": "1.0.5-desktop-ui.10.2-terrainfix.19",
                                     "client_revision": "r10.2-terrainfix.19"}), encoding="utf-8")
        logs = []
        checker = cu.UpdateChecker(build, d, opener=opener, log=lambda e, **f: logs.append(e))
        checker.start()
        for _ in range(200):
            snap = checker.snapshot()
            if snap["status"] != "checking":
                break
            time.sleep(0.01)
        return snap, logs

    def test_rate_limited_api_uses_page_redirect(self):
        def opener(request, timeout):
            if request.full_url == cu.LATEST_URL:
                raise _err(request.full_url, 403)
            raise _err(request.full_url, 302, cu.RELEASE_URL_PREFIX + "tag/" + TAG)
        snap, logs = self._run(opener)
        self.assertEqual(snap["status"], "available")
        self.assertEqual(snap["release"]["tag"], TAG)
        self.assertIn("update_check_fallback_used", logs)

    def test_foreign_redirect_is_ignored(self):
        def opener(request, timeout):
            if request.full_url == cu.LATEST_URL:
                raise _err(request.full_url, 429)
            raise _err(request.full_url, 302, "https://example.com/releases/tag/" + TAG)
        snap, _ = self._run(opener)
        self.assertNotEqual(snap["status"], "available")

    def test_other_http_errors_do_not_fallback(self):
        calls = []

        def opener(request, timeout):
            calls.append(request.full_url)
            raise _err(request.full_url, 500)
        self._run(opener)
        self.assertEqual(calls, [cu.LATEST_URL])


class DiagnosisTests(unittest.TestCase):
    def test_pythonnet_runtime_error_maps_to_dotnet(self):
        try:
            try:
                raise OSError("load failed")
            except OSError as inner:
                raise RuntimeError('Failed to create a default .NET runtime, which would have been "netfx"') from inner
        except RuntimeError as exc:
            issue = install_support.problem(exc, stage="webview", default=install_support.BACKEND)
        self.assertEqual(issue["code"], "dotnet_unavailable")
        self.assertIn(".NET Framework", issue["message"])

    def test_resolve_loader_maps_to_dotnet(self):
        exc = RuntimeError("Failed to resolve Python.Runtime.Loader.Initialize from C:/x/Python.Runtime.dll")
        self.assertEqual(install_support.problem(exc, stage="webview", default="")["code"], "dotnet_unavailable")

    def test_unknown_runtime_error_unchanged(self):
        self.assertEqual(install_support.problem(RuntimeError("boom"), stage="webview", default="")["code"],
                         "desktop_view_failed")


class DetailTests(unittest.TestCase):
    def test_detail_masks_home_and_user(self):
        import desktop
        home = str(Path.home())
        user = os.environ.get("USERNAME") or "nobody"
        with mock.patch.dict(os.environ, {"USERNAME": user}):
            try:
                try:
                    raise OSError("cannot open " + home + "\\a.dll")
                except OSError as inner:
                    raise RuntimeError("outer " + user) from inner
            except RuntimeError as exc:
                text = desktop._error_detail(exc)
        self.assertIn("RuntimeError", text)
        self.assertIn("OSError", text)
        self.assertNotIn(home, text)
        if len(user) > 2:
            self.assertNotIn(user, text)


if __name__ == "__main__":
    unittest.main()


class TraceTests(unittest.TestCase):
    def test_trace_has_frames_and_is_masked(self):
        import desktop
        home = str(Path.home())
        try:
            raise RuntimeError("bad " + home)
        except RuntimeError as exc:
            text = desktop._error_trace(exc)
        self.assertIn("Traceback", text)
        self.assertIn("test_hotfix20.py", text)
        self.assertNotIn(home, text)


class BrowserFallbackTests(unittest.TestCase):
    class _Proc:
        def __init__(self, alive=True):
            self.alive = alive

        def poll(self):
            return None if self.alive else 1

    class _Backend:
        def __init__(self, alive=True, url="http://127.0.0.1:5000/desktop?t=x"):
            self.process = BrowserFallbackTests._Proc(alive)
            self._url = url

        def url(self):
            return self._url

    def _call(self, backend, opener):
        import desktop
        shown = []
        ok = desktop._browser_fallback(backend, tempfile.mkdtemp(), {"code": "dotnet_unavailable", "message": "装 .NET"},
                                       opener=opener, notify=shown.append)
        return ok, shown

    def test_opens_local_page_and_waits(self):
        opened = []
        ok, shown = self._call(self._Backend(), opened.append)
        self.assertTrue(ok)
        self.assertEqual(opened, ["http://127.0.0.1:5000/desktop?t=x"])
        self.assertIn("浏览器", shown[0])
        self.assertIn("装 .NET", shown[0])

    def test_dead_backend_or_foreign_url_or_open_error(self):
        self.assertFalse(self._call(self._Backend(alive=False), lambda u: None)[0])
        self.assertFalse(self._call(self._Backend(url="http://evil.example/"), lambda u: None)[0])

        def boom(_u):
            raise OSError("no browser")
        self.assertFalse(self._call(self._Backend(), boom)[0])
        self.assertFalse(self._call(None, lambda u: None)[0])
