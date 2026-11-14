"""Read-only account catalogue certification, using the actual runtime loader.

The loader runs in a separate process: status polling never changes the server's
global QFLY/QFSKY environment. No task-success flag can certify an output folder.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
import types

APP = Path(__file__).resolve().parents[1]
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))
import xg_paths
import xg_exec

REBUILD_MESSAGE = "目录需要用当前版本重新录制遇境后重建"
_CACHE = {}
_LOCK = threading.RLock()


def _paths(data):
    data = Path(data).resolve()
    private = data / "runtime/private"
    return types.SimpleNamespace(DATA=data, APP=APP, OVERLAY=xg_paths.OVERLAY,
        QFSKY_CODE=xg_paths.QFSKY_CODE, BOT_CODE=xg_paths.BOT_CODE,
        RUNTIME_PRIVATE=private, CURRENT_PROFILES=private / "current_profiles",
        PROFILES_JSON=private / "connection0167_profiles.local.json",
        RUNTIME_DATA=data / "runtime/runtime-data",
        ASSET_RELEASE=data / "runtime/terrain-experimental-20261104/release-v20-hop-trial")


def _json(path, limit=2 * 1024 * 1024):
    with Path(path).open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("metadata_size")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("metadata_shape")
    return value


def _identity(path):
    try:
        st = path.stat()
        return (str(path), st.st_size, st.st_mtime_ns, st.st_ctime_ns)
    except OSError:
        return (str(path), None)


def _fingerprint(xg, folder):
    paths = [xg.PROFILES_JSON, xg.RUNTIME_PRIVATE / "catalog_sources.json",
             xg.RUNTIME_PRIVATE / "catalog_account_pins.json"]
    if folder.is_dir():
        paths.extend(p for p in folder.rglob("*") if p.is_file())
    if xg.CURRENT_PROFILES.is_dir():
        # Only runtime source parts, never inspect account/token/connection keys.
        for child in xg.CURRENT_PROFILES.iterdir():
            if child.is_dir():
                paths.extend(child / name for name in ("record.json", "actor.bin", "scene.bin", "cache.bin"))
    return tuple(_identity(p) for p in sorted(set(paths), key=str))


def _verify_request(request):
    """Child-process implementation; also injectable in offline fixture tests."""
    xg = _paths(request["data"])
    for item in reversed((str(xg.OVERLAY), str(xg.QFSKY_CODE.parent), str(xg.BOT_CODE))):
        if item not in sys.path:
            sys.path.insert(0, item)
    from xg_gamedata import catalog, spawn_loader
    from qfsky.version_profile import CURRENT_BUILD, profile_for_build
    catalog._install_paths(xg)
    from qfsky.join0167_profile import account_binding
    profile = profile_for_build(request["build"])
    if profile is None or profile.get("legacy") or request["build"] != CURRENT_BUILD:
        raise ValueError("catalog_current_build_required")
    path = xg.ASSET_RELEASE / "asset-catalog-acct" / request["account"] / "manifest.json"
    manifest = _json(path)
    if manifest.get("apk_build") != profile["build"] or manifest.get("native_sha256") != profile["native_sha256"]:
        raise ValueError("catalog_version_stale")
    profiles = _json(xg.PROFILES_JSON)
    if profiles.get("apk_build") != profile["build"] or profiles.get("resource_revision") != profile["resource_revision"]:
        raise ValueError("capture_version_stale")
    # Reuse the builder's strict same-account/current-version home validation,
    # including NetVersion[0], actor structure and captured record hash.
    donor = catalog._donor(xg, profiles, request["uuid"])
    if manifest.get("source_capture_record_sha256") != donor["record_sha256"]:
        raise ValueError("catalog_source_capture_changed")
    binding = account_binding(request["uuid"])
    if manifest.get("source_account_binding_hash") != binding:
        raise ValueError("catalog_account_mismatch")
    pack = spawn_loader.load(build=profile["build"])
    expected = {str(row["map_id"]): scene for scene, row in pack["placements"].items()}
    entries = manifest.get("entries") or {}
    if set(entries) != set(expected) or any(entries[k].get("scene") != scene for k, scene in expected.items()):
        raise ValueError("catalog_incomplete")
    actual_variants = {(mid, name) for mid, rows in (manifest.get("variants") or {}).items() for name in rows}
    expected_variants = {(str(pack["placements"][scene]["map_id"]), name)
                         for scene, rows in (pack.get("variants") or {}).items() for name in rows}
    if actual_variants != expected_variants:
        raise ValueError("catalog_variants_incomplete")
    # _verify_all installs QFLY_RESOURCE_REVISION, JOIN_CONFIG, DATA_DIR,
    # JOIN_MODE and exact manifest SHA, and calls load_catalog_trial for EVERY
    # map and variant. The process-local environment is restored in finally.
    return catalog._verify_all(xg, request["uuid"], binding, manifest, path,
                               profile["resource_revision"], None)


def _runtime_check(request):
    env = os.environ.copy()
    env.update(XG_DATA_DIR=request["data"], SKY_AI_ROOT=str(xg_paths.ROOT), PYTHONDONTWRITEBYTECODE="1")
    result = xg_exec.run([xg_paths.python_exe(), "-B", "-X", "utf8", str(Path(__file__)), "--verify"],
        input=json.dumps(request), text=True, encoding="utf-8", capture_output=True,
        timeout=120, cwd=str(APP), env=env)
    if result.returncode:
        raise ValueError("catalog_runtime_check_failed")
    response = json.loads(result.stdout)
    if not isinstance(response, dict) or response.get("ok") is not True:
        raise ValueError(str(response.get("code") or "catalog_runtime_invalid"))
    return response["checked"]


def certify(data, account, account_uuid, profile=None):
    from qfsky.version_profile import CURRENT_BUILD, profile_for_build
    profile = dict(profile or profile_for_build(CURRENT_BUILD))
    row = {"account": str(account or ""), "certified": False, "state": "missing",
           "code": "catalog_missing", "message": "还没有当前账号的有效地图目录",
           "maps": 0, "variants": 0, "current_build": profile.get("build"),
           "current_version": profile.get("version") or ""}
    if not isinstance(account, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", account) is None or not account_uuid:
        row.update(state="account_required", code="account_required", message="先选择当前游戏账号")
        return row
    xg = _paths(data)
    folder = xg.ASSET_RELEASE / "asset-catalog-acct" / account
    manifest_path = folder / "manifest.json"
    if not manifest_path.is_file():
        return row
    try:
        manifest = _json(manifest_path)
        if (manifest.get("apk_build") != profile.get("build")
                or manifest.get("native_sha256") != profile.get("native_sha256")):
            row.update(state="stale", code="catalog_version_stale", message=REBUILD_MESSAGE)
            return row
        request = {"data": str(xg.DATA), "account": account, "uuid": str(account_uuid), "build": profile["build"]}
        before = _fingerprint(xg, folder)
        key = (str(xg.DATA), account, str(account_uuid), profile["build"], profile.get("resource_revision"), before)
        with _LOCK:
            cached = _CACHE.get(key)
            if cached and time.monotonic() - cached[0] < 15:
                return dict(cached[1])
            checked = _runtime_check(request)
            if before != _fingerprint(xg, folder):
                raise ValueError("catalog_changed_during_validation")
            row.update(certified=True, state="ready", code="", message="当前账号目录已通过实际运行时校验",
                       maps=int(checked["maps"]), variants=int(checked["variants"]))
            if len(_CACHE) > 32:
                _CACHE.clear()
            _CACHE[key] = (time.monotonic(), dict(row))
    except Exception as exc:
        code = str(exc)
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", code):
            code = "catalog_runtime_invalid"
        row.update(state="invalid", code=code, message=REBUILD_MESSAGE)
    return row


if __name__ == "__main__":
    try:
        request = json.loads(sys.stdin.read(65537))
        checked = _verify_request(request)
        output = {"ok": True, "checked": checked}
    except Exception as exc:
        code = getattr(exc, "code", None) or str(exc)
        if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", code):
            code = "catalog_runtime_invalid"
        output = {"ok": False, "code": code}
    print(json.dumps(output, ensure_ascii=False))
