"""Public client for the private login broker. No NetEase login implementation.

Only game-runtime sessions are cached locally. SDK refresh roots, password
digests and device keys remain in the private service. HTTP is opt-in loopback
only for development; redirects and TLS certificate bypasses are not supported.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import copy
import ctypes
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler


class AuthError(Exception):
    def __init__(self, code, status=0):
        self.code, self.status = code, status
        super().__init__(code)


def data_root():
    if os.environ.get("XG_DATA_DIR"):
        return Path(os.environ["XG_DATA_DIR"])
    import xg_paths
    return Path(xg_paths.DATA)


def config_path():
    return data_root() / "control/auth_service.json"


def atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as f:
        name = f.name
        os.chmod(name, 0o600)
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(name, path)
    os.chmod(path, 0o600)


def _dpapi(raw, protect):
    if os.name != "nt":
        raise AuthError("windows_secret_backend_required")
    class Blob(ctypes.Structure):
        _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_char))]
    buf = ctypes.create_string_buffer(raw)
    source = Blob(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    target = Blob()
    fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise AuthError("local_secret_protection_failed")
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(target.pbData)


def validate_service_url(url, allow_loopback=False):
    u = urlsplit(url)
    if u.username or u.password or u.query or u.fragment or not u.hostname:
        raise AuthError("invalid_service_url")
    if u.scheme != "https":
        if not (allow_loopback and u.scheme == "http" and u.hostname in ("127.0.0.1", "::1")):
            raise AuthError("https_required")


def validate_config(url, token, allow_loopback=False):
    validate_service_url(url, allow_loopback)
    if not isinstance(token, str) or not token.startswith("xga_") or not 40 <= len(token) <= 150:
        raise AuthError("invalid_service_token")


def configure(url, token, allow_loopback=False, *, owner_id=None, previous_owner_id=None):
    validate_config(url, token, allow_loopback)
    try:
        previous = read_config()
    except AuthError:
        previous = None
    cfg = {"url": url.rstrip("/"), "allow_loopback": bool(allow_loopback)}
    if isinstance(owner_id, str) and owner_id:
        cfg["owner_id"] = owner_id
    if os.name == "nt":
        cfg.update(backend="dpapi", protected_token=base64.b64encode(_dpapi(token.encode(), True)).decode())
    else:
        cfg.update(backend="file-0600", token=token)
    atomic(config_path(), json.dumps(cfg).encode())
    same_owner = bool(previous and owner_id and
                      (previous.get("owner_id") or previous_owner_id) == owner_id and
                      previous["url"].rstrip("/") == url.rstrip("/"))
    if previous and not same_owner and (previous["url"].rstrip("/") != url.rstrip("/") or previous["token"] != token):
        with _disk_lock():
            atomic(data_root() / "runtime/runtime-data/accounts.json", b'{"accounts":[]}')
        atomic(data_root() / "control/active_account", b"\n")


def clear_local_service_session():
    """Forget local service/session secrets only; never remove assets or captures."""
    with _disk_lock():
        atomic(config_path(), b'{"signed_out":true}')
        atomic(data_root() / "runtime/runtime-data/accounts.json", b'{"accounts":[]}')
        atomic(data_root() / "control/active_account", b"\n")


def read_config():
    try:
        stored = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        stored = None
    # A deliberate local logout also suppresses a launch-environment fallback.
    if isinstance(stored, dict) and stored.get("signed_out") is True:
        raise AuthError("auth_service_not_configured")
    if os.environ.get("XG_AUTH_URL") and os.environ.get("XG_AUTH_TOKEN"):
        cfg = {"url": os.environ["XG_AUTH_URL"], "token": os.environ["XG_AUTH_TOKEN"],
               "allow_loopback": os.environ.get("XG_AUTH_ALLOW_LOOPBACK") == "1"}
    else:
        if not isinstance(stored, dict):
            raise AuthError("auth_service_not_configured")
        cfg = stored
        if cfg.get("backend") == "dpapi":
            cfg["token"] = _dpapi(base64.b64decode(cfg["protected_token"], validate=True), False).decode()
    validate_config(cfg.get("url", ""), cfg.get("token", ""), cfg.get("allow_loopback", False))
    return cfg


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise AuthError("service_redirect_rejected")


class Client:
    def __init__(self, config=None):
        self.config = config or read_config()
        validate_config(self.config.get("url", ""), self.config.get("token", ""), self.config.get("allow_loopback", False))
        self.opener = build_opener(_NoRedirect())

    def request(self, method, path, data=None, timeout=30):
        if not path.startswith("/v1/") or "?" in path or "#" in path:
            raise AuthError("invalid_api_path")
        body = json.dumps(data or {}, allow_nan=False).encode() if method == "POST" else None
        req = Request(self.config["url"].rstrip("/") + path, data=body, method=method,
                      headers={"Authorization": "Bearer " + self.config["token"], "Content-Type": "application/json"})
        try:
            with self.opener.open(req, timeout=timeout) as r:
                raw = r.read(256_001)
                if len(raw) > 256_000:
                    raise AuthError("service_response_too_large")
                out = json.loads(raw)
                if not isinstance(out, dict):
                    raise AuthError("invalid_service_response")
                return out
        except HTTPError as exc:
            try:
                code = json.loads(exc.read(4096)).get("error", "service_error")
                if not isinstance(code, str) or not code.replace("_", "").isalnum() or len(code) > 80:
                    code = "service_error"
            except (ValueError, AttributeError):
                code = "service_error"
            raise AuthError(code, exc.code) from None
        except (URLError, OSError, ValueError):
            raise AuthError("auth_service_unavailable") from None

    def wait_job(self, job, timeout=160):
        deadline = time.monotonic() + timeout
        while job["status"] == "running" and time.monotonic() < deadline:
            time.sleep(0.35)
            job = self.request("GET", "/v1/login-jobs/" + job["job_id"])
        return job

    def session(self, identity, *, timeout=30):
        reply = self.request("POST", "/v1/accounts/" + identity + "/session", timeout=timeout)
        account = sanitize_account(reply["account"])
        cache_update(identity, account)
        device = reply.get("runtime_device", {})
        # Public, non-secret identity attributes required by the existing runner.
        if device.get("device_id"):
            allowed = {"brand", "device_name", "device_type", "device_model", "system_name", "system_version",
                       "system_build", "resolution", "udid", "urs_udid", "mac", "oaid", "ext_ci", "ci_code",
                       "sv", "unique_id", "screen_width", "screen_height", "density"}
            profile = {"device_id": device["device_id"], "udid": device.get("udid", ""),
                       "urs_udid": device.get("urs_udid", device.get("udid", "")), "platform": device.get("platform", "android")}
            if isinstance(device.get("attrs"), dict):
                profile["attrs"] = {k: v for k, v in device["attrs"].items() if k in allowed and isinstance(v, (str, int, float, bool))}
            atomic(data_root() / "runtime/runtime-data/registered-device.local.json", json.dumps(profile).encode())
        return account

    def renew(self, identity):
        current = cache_get(identity) or {}
        job = self.request("POST", "/v1/accounts/" + identity + "/renew",
                           {"credential_version": current.get("credential_version")})
        deadline = time.monotonic() + 330
        while job["status"] == "running" and time.monotonic() < deadline:
            time.sleep(1)
            job = self.request("GET", "/v1/renew-jobs/" + job["job_id"])
        if job["status"] != "ready":
            raise AuthError(job.get("error", "renew_in_progress"), 409)
        return self.session(identity)


FIELDS = {"id", "alias", "server", "channel", "platform", "user_uuid", "session", "session2", "session3",
          "status", "status_note", "created_at", "last_renew_at", "credential_version", "data_version", "data", "data_at"}


def sanitize_account(account):
    # Discard unknown fields even if a future server accidentally sends them.
    out = {k: copy.deepcopy(v) for k, v in account.items() if k in FIELDS}
    out["renew"] = {"broker_account_id": str(account["id"])}
    return out


_cache_lock = threading.RLock()


@contextmanager
def _disk_lock():
    path = data_root() / "runtime/runtime-data/auth-cache.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with _cache_lock, open(path, "a+b") as f:
        if f.tell() == 0:
            f.write(b"0")
            f.flush()
        f.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            f.seek(0)
            if os.name == "nt":
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(f, fcntl.LOCK_UN)


def _cache_read():
    p = data_root() / "runtime/runtime-data/accounts.json"
    if not p.exists():
        return {"accounts": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("accounts"), list):
            raise ValueError()
        return data
    except (OSError, ValueError):
        raise AuthError("runtime_cache_corrupt") from None


def cache_get(identity=None):
    with _disk_lock():
        rows = _cache_read()["accounts"]
        return copy.deepcopy(rows if identity is None else next((r for r in rows if r["id"] == identity), None))


def cache_update(identity, fields):
    with _disk_lock():
        data = _cache_read()
        row = next((a for a in data["accounts"] if a["id"] == identity), None)
        if row is None:
            row = {"id": identity}
            data["accounts"].append(row)
        incoming = {k: copy.deepcopy(v) for k, v in fields.items() if k in FIELDS}
        if "session" in incoming and row.get("credential_version", 0) > incoming.get("credential_version", 0):
            return  # A slower HTTP response must not roll back rotated credentials.
        row.update(incoming)
        clean = {"accounts": [sanitize_account(a) for a in data["accounts"]]}
        atomic(data_root() / "runtime/runtime-data/accounts.json", json.dumps(clean, ensure_ascii=False).encode())


def _heartbeat_loop(identity, cancel, notify=None, *, refresh_session=True,
                    interval=60, lease_seconds=180, clock=None, client_factory=None):
    # Every caller must also obtain an online session before starting runtime work.
    # An outage gets only the remaining lease, never unlimited cached-session use.
    clock = clock or time.monotonic
    client_factory = client_factory or Client
    last_ok = clock()

    def report(code):
        if notify:
            try:
                notify("auth_service_status", error=code)
            except Exception:
                pass  # A UI/logging callback must not suppress cancellation.

    while not cancel.is_set():
        remaining = lease_seconds - (clock() - last_ok)
        if remaining <= 0:
            cancel.set()
            report("auth_service_lease_expired")
            return
        try:
            client = client_factory()
            if refresh_session:
                client.session(identity, timeout=min(30, remaining))
            else:
                # The chat process shares the runner's lease, not its cache writer.
                client.request("POST", "/v1/accounts/" + identity + "/heartbeat",
                               timeout=min(30, remaining))
            last_ok = clock()
        except AuthError as exc:
            terminal = exc.status in (401, 403) or exc.code in (
                "subscription_required", "subscription_expired", "subscription_paused",
                "subscription_unavailable", "unauthorized", "account_in_use",
                "reauth_required", "verification_required", "not_found")
            if terminal:
                cancel.set()
                report(exc.code)
                return
            report(exc.code)
        except Exception:
            report("auth_service_unavailable")
        remaining = lease_seconds - (clock() - last_ok)
        if remaining <= 0:
            cancel.set()
            report("auth_service_lease_expired")
            return
        if cancel.wait(min(interval, remaining)):
            return


def heartbeat(identity, cancel, notify=None, *, refresh_session=True):
    thread = threading.Thread(target=_heartbeat_loop,
                              args=(identity, cancel, notify),
                              kwargs={"refresh_session": refresh_session},
                              name="auth-session-sync", daemon=True)
    thread.start()
    return thread


def main():
    import argparse
    import getpass
    p = argparse.ArgumentParser(description="配置小光私有登录服务")
    p.add_argument("--url", required=True)
    p.add_argument("--token-file")
    p.add_argument("--allow-loopback", action="store_true")
    a = p.parse_args()
    token = json.loads(Path(a.token_file).read_text())["token"] if a.token_file else getpass.getpass("服务令牌（不回显）: ")
    configure(a.url, token, a.allow_loopback)
    Client().request("GET", "/v1/health")
    print("服务连接通过，配置已保存；没有发送游戏账号。")


if __name__ == "__main__":
    main()
