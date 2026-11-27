"""Local WebView2 preflight; avoid an unusable silent MSHTML fallback."""
from __future__ import annotations

import os
from pathlib import Path
import re


_CLIENT = r"Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
_FIXED_ENV = "WEBVIEW2_BROWSER_EXECUTABLE_FOLDER"
_VERSION = re.compile(r"([0-9]{1,9}(?:\.[0-9]{1,9}){3})(?:\s+.+)?")


def _usable_version(value):
    match = _VERSION.fullmatch(value.strip()) if isinstance(value, str) else None
    return bool(match and any(int(part) for part in match[1].split(".")))


def _query_loader(path, browser_folder):
    """True/false for a definitive native result; None if unavailable."""
    import ctypes

    try:
        loader = ctypes.WinDLL(str(path), winmode=0x1100)
        get_version = loader.GetAvailableCoreWebView2BrowserVersionString
        get_version.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_wchar_p)]
        get_version.restype = ctypes.c_long
        free = ctypes.windll.ole32.CoTaskMemFree
        free.argtypes = [ctypes.c_void_p]
        free.restype = None
    except (OSError, AttributeError):
        # An architecture mismatch or absent DLL is not runtime absence.
        return None
    version = ctypes.c_wchar_p()
    try:
        hr = get_version(browser_folder, ctypes.byref(version))
        if hr == 0:
            return _usable_version(version.value)
        if (hr & 0xFFFFFFFF) in (0x80070002, 0x80070003):  # file / path not found
            return False
        return None
    finally:
        if version:
            free(ctypes.cast(version, ctypes.c_void_p))


def _registry_available(reg):
    uncertain = False
    for hive in (reg.HKEY_CURRENT_USER, reg.HKEY_LOCAL_MACHINE):
        for path, view in (("SOFTWARE\\" + _CLIENT, 0),
                           ("SOFTWARE\\" + _CLIENT, reg.KEY_WOW64_32KEY),
                           ("SOFTWARE\\" + _CLIENT, reg.KEY_WOW64_64KEY),
                           ("SOFTWARE\\WOW6432Node\\" + _CLIENT, 0)):
            try:
                with reg.OpenKey(hive, path, 0, reg.KEY_READ | view) as key:
                    value, _typ = reg.QueryValueEx(key, "pv")
                if _usable_version(value):
                    return True
            except FileNotFoundError:
                continue
            except OSError as exc:
                if getattr(exc, "winerror", None) not in (2, 3, 87):
                    uncertain = True
    return None if uncertain else False


def available(webview_file, *, environment=None, platform=None, query=None, registry=None):
    """Check installed Evergreen or Fixed Version; keep indeterminate local failures non-fatal."""
    platform = os.name if platform is None else platform
    if platform != "nt":
        return None
    environment = os.environ if environment is None else environment
    query = _query_loader if query is None else query
    folder = (environment.get(_FIXED_ENV) or "").strip().strip('"') or None
    if isinstance(webview_file, (str, os.PathLike)):
        lib = Path(webview_file).absolute().parent / "lib"
        try:
            for path in lib.rglob("WebView2Loader.dll"):
                if path.is_file():
                    result = query(path, folder)
                    if result is not None:
                        return result
        except OSError:
            pass
    if folder and (Path(folder) / "msedgewebview2.exe").is_file():
        return True
    if registry is None:
        try:
            import winreg as registry
        except ImportError:
            return None
    return _registry_available(registry)
