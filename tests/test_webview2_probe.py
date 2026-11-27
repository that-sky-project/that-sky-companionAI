"""No GUI, native DLL load, or registry access: only WebView2 preflight fakes."""
from contextlib import nullcontext
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from webview2_probe import available


class FakeRegistry:
    HKEY_CURRENT_USER = "current"
    HKEY_LOCAL_MACHINE = "machine"
    KEY_READ = 1
    KEY_WOW64_32KEY = 2
    KEY_WOW64_64KEY = 4

    def __init__(self, versions=None, denied=False):
        self.versions = versions or {}
        self.denied = denied
        self.lookups = []

    def OpenKey(self, hive, path, reserved, access):
        if self.denied:
            raise PermissionError("fake permission")
        self.lookups.append((hive, path, access))
        if (hive, access) not in self.versions:
            raise FileNotFoundError("fake key")
        return nullcontext(self.versions[hive, access])

    def QueryValueEx(self, key, name):
        if name != "pv":
            raise FileNotFoundError("fake value")
        return key, "REG_SZ"


class WebViewProbeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="xg-webview2-")
        self.addCleanup(temp.cleanup)
        self.pkg = Path(temp.name) / "测试 安装目录" / "webview"
        self.module = self.pkg / "__init__.py"

    def probe(self, **kwargs):
        return available(self.module, environment={}, platform="nt", **kwargs)

    def test_per_user_and_32bit_machine_evergreen(self):
        for hive, access in (("current", 1), ("machine", 1 | FakeRegistry.KEY_WOW64_32KEY)):
            with self.subTest(hive=hive, access=access):
                registry = FakeRegistry({(hive, access): "131.0.2903.86"})
                self.assertIs(self.probe(registry=registry), True)
                self.assertTrue(registry.lookups[-1][1].endswith("{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"))

    def test_uninstalled_or_invalid_version_is_missing_but_access_denied_is_unknown(self):
        self.assertIs(self.probe(registry=FakeRegistry()), False)
        self.assertIs(self.probe(registry=FakeRegistry({("current", 1): "0.0.0.0"})), False)
        self.assertIs(self.probe(registry=FakeRegistry(denied=True)), None)
        self.assertIs(available(None, platform="posix", registry=FakeRegistry()), None)

    def test_native_result_beats_stale_registry_and_unknown_can_fallback(self):
        loader = self.pkg / "lib/runtimes/win-x64/native/WebView2Loader.dll"
        loader.parent.mkdir(parents=True)
        loader.touch()
        registry = FakeRegistry({("current", 1): "131.0.2903.86"})
        query = Mock(return_value=False)
        self.assertIs(self.probe(registry=registry, query=query), False)
        query.assert_called_once_with(loader, None)
        self.assertEqual(registry.lookups, [])
        query.return_value = None
        self.assertIs(self.probe(registry=registry, query=query), True)

    def test_fixed_version_location_with_space_and_unicode(self):
        fixed = self.pkg.parent / "固定 版本"
        fixed.mkdir(parents=True)
        (fixed / "msedgewebview2.exe").touch()
        self.assertIs(available(None, environment={"WEBVIEW2_BROWSER_EXECUTABLE_FOLDER": '"' + str(fixed) + '"'},
                                platform="nt", registry=FakeRegistry()), True)


if __name__ == "__main__":
    unittest.main()
