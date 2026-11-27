"""The desktop host's read-only GitHub Release check and local preferences."""
from __future__ import annotations

import http.client
import json
import os
import re
import threading
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import unquote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

if __package__:
    from . import xg_paths
else:
    import xg_paths


REPOSITORY = "that-sky-project/that-sky-companionAI"
LATEST_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
RELEASE_URL_PREFIX = f"https://github.com/{REPOSITORY}/releases/"
USER_AGENT = "XiaoGuang-Updater"
TIMEOUT = 8
_RESPONSE_LIMIT = 256 * 1024
_PREFS_LIMIT = 64 * 1024
_BASE = r"(?P<base>[0-9]{1,9}\.[0-9]{1,9}\.[0-9]{1,9})"
_REV = r"(?P<rev>[0-9]{1,9}(?:\.[0-9]{1,9}){0,5})(?:-terrainfix\.(?P<fix>[0-9]{1,9}))?"
_TAG = re.compile(r"v?" + _BASE + "-r" + _REV)
_DISPLAY = re.compile(r"v?" + _BASE + r"-desktop-ui\." + _REV)
_REVISION = re.compile(r"r" + _REV)
_BASE_PREFIX = re.compile(r"v?" + _BASE + r"(?=-|$)")


@dataclass(frozen=True, order=True)
class Version:
    base: tuple[int, ...]
    revision: tuple[int, ...]
    terrainfix: int

    def display(self) -> str:
        base = ".".join(map(str, self.base))
        rev = ".".join(map(str, self.revision))
        fix = f"-terrainfix.{self.terrainfix}" if self.terrainfix else ""
        return f"v{base}-r{rev}{fix}"


def _match(pattern, value):
    return pattern.fullmatch(value.strip()) if isinstance(value, str) and len(value) <= 128 else None


def _version(base: str, revision: str, fix: str | None) -> Version:
    parts = list(map(int, revision.split(".")))
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return Version(tuple(map(int, base.split("."))), tuple(parts), int(fix or 0))


def parse_tag(tag) -> Version | None:
    match = _match(_TAG, tag)
    return _version(*match.groups()) if match else None


def current_version(build) -> Version | None:
    if not isinstance(build, dict):
        return None
    release = build.get("release")
    revision = _match(_REVISION, build.get("client_revision"))
    if revision:
        base = _BASE_PREFIX.match(release.strip()) if isinstance(release, str) and len(release) <= 128 else None
        base = base or _match(_BASE_PREFIX, build.get("base_version"))
        if base:
            return _version(base["base"], revision["rev"], revision["fix"])
    display = _match(_TAG, release) or _match(_DISPLAY, release)
    return _version(*display.groups()) if display else None


def valid_release_url(url) -> bool:
    if not (isinstance(url, str) and len(url) <= 2048 and url.startswith(RELEASE_URL_PREFIX)):
        return False
    if any(not (32 < ord(ch) < 127) or ch == "\\" for ch in url):
        return False
    try:
        path = urlsplit(url).path
        prefix = urlsplit(RELEASE_URL_PREFIX).path
        # Reject browser normalization/escape forms, including encoded dots,
        # slashes and backslashes in path segments. Query/fragment do not
        # participate in path validation.
        segments = path[len(prefix):].split("/")
        decoded = [unquote(segment, errors="strict") for segment in segments]
        return bool(decoded[0]) and all(
            segment not in (".", "..") and not any(ch in '/\\%' or ord(ch) < 32 for ch in segment)
            for segment in decoded
        )
    except ValueError:
        return False


def _text(value, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    # Remove lone surrogates, which are legal as JSON escapes but are not
    # representable as UTF-8 by the native JS bridge.
    return value[:limit].encode("utf-8", errors="replace").decode("utf-8")


@dataclass(frozen=True)
class Release:
    version: Version
    tag: str
    title: str
    notes: str
    url: str

    def ui(self) -> dict:
        return {"tag": self.tag, "title": self.title, "notes": self.notes}


def _release(doc) -> Release | None:
    if not isinstance(doc, dict):
        return None
    tag = doc.get("tag_name")
    url = doc.get("html_url")
    version = parse_tag(tag)
    if not version or not valid_release_url(url):
        return None
    notes = "\n".join(_text(doc.get("body"), 2400).strip().splitlines()[:8])
    return Release(version, tag.strip(), _text(doc.get("name"), 200).strip() or tag.strip(), notes, url)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # This public endpoint is fixed; no need to follow any redirect.
        return None


def _open_latest(request, *, timeout):
    # Keep urllib's default ProxyHandler (Windows system/user proxy and
    # environment settings). No cookies, auth header, or local build data.
    return build_opener(_NoRedirect()).open(request, timeout=timeout)


def _open_browser(url):
    if os.name == "nt":
        os.startfile(url)
        return True
    return webbrowser.open(url, new=2)


class UpdateChecker:
    """Thread-safe state owned by one desktop process, never by the WebView."""

    def __init__(self, build_file, data_dir=None, *, opener=None, browser=None, log=None,
                 timeout=TIMEOUT):
        self._lock = threading.Lock()
        self._path = Path(data_dir if data_dir is not None else xg_paths.DATA) / "control/client-update.json"
        try:
            self._current = current_version(xg_paths.read_json(build_file))
        except RecursionError:
            self._current = None
        self._opener = opener or _open_latest
        self._browser = browser or _open_browser
        self._log = log or (lambda _event, **_fields: None)
        self._timeout = timeout
        self._auto, self._skipped = self._prefs()
        self._state = "idle"
        self._release = None
        self._worker = None
        self._generation = 0
        self._startup_called = False

    def _prefs(self):
        try:
            with open(xg_paths.long_path(self._path), "rb") as fp:
                raw = fp.read(_PREFS_LIMIT + 1)
            doc = json.loads(raw) if len(raw) <= _PREFS_LIMIT else None
        except (OSError, ValueError, RecursionError):
            doc = None
        if not isinstance(doc, dict):
            return True, frozenset()
        skipped = doc.get("skipped_versions")
        versions = frozenset(filter(None, map(parse_tag, skipped))) if isinstance(skipped, list) else frozenset()
        return doc.get("auto_check") is not False, versions

    def _save(self, auto, skipped):
        try:
            xg_paths.write_json(self._path, {"auto_check": auto, "skipped_versions":
                                             [version.display() for version in sorted(skipped)]})
        except OSError as exc:
            self._log("update_preferences_write_failed", error=type(exc).__name__)
            return False
        self._auto, self._skipped = auto, skipped
        return True

    def _snapshot_locked(self) -> dict:
        return {"auto_check": self._auto, "status": self._state,
                "current": self._current.display() if self._current else None,
                "release": self._release.ui() if self._release else None}

    def snapshot(self) -> dict:
        with self._lock:
            return self._snapshot_locked()

    def start(self, *, startup=False) -> dict:
        with self._lock:
            if startup:
                if self._startup_called:
                    return self._snapshot_locked()
                self._startup_called = True
                if not self._auto:
                    return self._snapshot_locked()
            if self._worker is not None:
                # A timed-out DNS resolver can outlive the UI deadline. Do not
                # pile up another network worker while that check unwinds.
                return self._snapshot_locked()
            self._release = None
            if not self._current:
                self._state = "no_update"
                return self._snapshot_locked()
            self._state = "checking"
            self._generation += 1
            deadline = time.monotonic() + self._timeout
            timer = threading.Timer(self._timeout, self._expire, args=(self._generation,))
            timer.daemon = True
            self._worker = threading.Thread(target=self._run, args=(timer, deadline),
                                            name="XiaoGuang-Updater", daemon=True)
            try:
                self._worker.start()
                timer.start()
            except RuntimeError:
                if not self._worker.is_alive():
                    self._worker = None
                self._state = "silent"
                self._log("update_thread_start_failed")
            return self._snapshot_locked()

    def _expire(self, generation):
        with self._lock:
            if self._state != "checking" or generation != self._generation:
                return
            self._state = "silent"
        self._log("update_check_timeout")

    def _run(self, timer, deadline):
        release = None
        state = "no_update"
        try:
            request = Request(LATEST_URL, headers={"User-Agent": USER_AGENT,
                                                  "Accept": "application/vnd.github+json"}, method="GET")
            with self._opener(request, timeout=self._timeout) as response:
                raw = response.read(_RESPONSE_LIMIT + 1)
            if len(raw) <= _RESPONSE_LIMIT:
                release = _release(json.loads(raw))
        except HTTPError as exc:
            self._log("update_check_http_failure", status=exc.code)
            exc.close()
            state = "silent"
        except (OSError, http.client.HTTPException) as exc:
            self._log("update_check_network_failure", error=type(exc).__name__)
            state = "silent"
        except (ValueError, RecursionError):
            self._log("update_check_invalid_response")
        finally:
            with self._lock:
                timer.cancel()
                if self._state == "checking":
                    if time.monotonic() >= deadline:
                        self._state = "silent"
                        self._log("update_check_timeout")
                    elif release and release.version > self._current and release.version not in self._skipped:
                        self._release, self._state = release, "available"
                    else:
                        self._state = state
                self._worker = None

    def set_auto(self, enabled):
        with self._lock:
            ok = type(enabled) is bool and (enabled == self._auto or self._save(enabled, self._skipped))
            return {"ok": ok, **self._snapshot_locked()}

    def skip(self):
        with self._lock:
            if not self._release:
                return False
            if not self._save(self._auto, self._skipped | {self._release.version}):
                return False
            self._release, self._state = None, "no_update"
            return True

    def dismiss(self):
        with self._lock:
            if self._release:
                self._release, self._state = None, "idle"
            return True

    def open_release(self):
        with self._lock:
            release = self._release
            url = release.url if release else None
        if not valid_release_url(url):
            return False
        try:
            if not self._browser(url):
                return False
            with self._lock:
                if self._release is release:
                    self._release, self._state = None, "idle"
            return True
        except (OSError, ValueError, webbrowser.Error) as exc:
            self._log("update_browser_open_failed", error=type(exc).__name__)
            return False
