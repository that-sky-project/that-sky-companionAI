"""Read-only account catalogue certification, using the actual runtime loader.

The loader runs in a separate process: status polling never changes the server's
global QFLY/QFSKY environment. No task-success flag can certify an output folder.
"""
from __future__ import annotations
from collections import OrderedDict
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
QIANNIAOCHENG_STALE_CODE = "catalog_qianniaocheng_stale"
QIANNIAOCHENG_REBUILD_MESSAGE = "千鸟城落点已更新，需要重新构建地图目录"
VARIANTS_STALE_CODE = "catalog_variants_incomplete"
VARIANTS_REBUILD_MESSAGE = "地图落点有更新（如八人门彩虹日/奥利奥），需要重新构建地图目录"
CHECKING_CODE = "catalog_checking"
CHECKING_MESSAGE = "正在后台校验当前账号的地图目录，请稍候"
MAX_CACHED_RESULTS = 32
MAX_ACTIVE_CHECKS = 2
_CACHE = OrderedDict()  # Terminal results live until fingerprint change/eviction/restart.
_INFLIGHT = {}          # One task per account scope, bounded globally; no queued threads.
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
    with open(xg_paths.win_path(path), "rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("metadata_size")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("metadata_shape")
    return value


def _identity(path):
    try:
        st = os.stat(xg_paths.win_path(path))
        return (str(path), st.st_size, st.st_mtime_ns, st.st_ctime_ns)
    except OSError:
        return (str(path), None)


def _fingerprint(xg, folder):
    paths = [xg.PROFILES_JSON, xg.RUNTIME_PRIVATE / "catalog_sources.json",
             xg.RUNTIME_PRIVATE / "catalog_account_pins.json"]
    folder_fs = Path(xg_paths.long_path(folder, force=True))
    if folder_fs.is_dir():
        paths.extend(folder / p.relative_to(folder_fs) for p in folder_fs.rglob("*") if p.is_file())
    profiles_fs = Path(xg_paths.long_path(xg.CURRENT_PROFILES, force=True))
    if profiles_fs.is_dir():
        # Only runtime source parts, never inspect account/token/connection keys.
        for child in profiles_fs.iterdir():
            if child.is_dir():
                paths.extend(xg.CURRENT_PROFILES / child.name / name
                             for name in ("record.json", "actor.bin", "scene.bin", "cache.bin"))
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


def _failure(row, exc):
    result = dict(row)
    if isinstance(exc, ImportError):
        # 程序文件缺失/导入路径不对，不是目录本身坏了：别让用户反复重录重建。
        name = getattr(exc, "name", None) or ""
        name = name if re.fullmatch(r"[A-Za-z0-9_.]{1,80}", name) else ""
        result.update(state="invalid", code="catalog_import_failed",
                      message="校验程序缺少模块%s，请重新解压完整客户端（源码运行请检查安装目录完整）"
                      % ("（%s）" % name if name else ""))
        return result
    code = getattr(exc, "code", None) or str(exc)
    if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", code):
        code = "catalog_runtime_invalid"
    if code == "catalog_version_stale":
        result.update(state="stale", code=code, message=REBUILD_MESSAGE)
    elif code == QIANNIAOCHENG_STALE_CODE:
        result.update(state="stale", code=code, message=QIANNIAOCHENG_REBUILD_MESSAGE)
    elif code == VARIANTS_STALE_CODE:
        result.update(state="stale", code=code, message=VARIANTS_REBUILD_MESSAGE)
    elif code == "catalog_import_failed":
        result.update(state="invalid", code=code,
                      message="校验程序缺少模块，请重新解压完整客户端（源码运行请检查安装目录完整）")
    else:
        result.update(state="invalid", code=code, message=REBUILD_MESSAGE)
    return result


def _ensure_qfsky_paths(xg=None):
    """Self-contained import setup, independent of how the calling process was started.

    qfsky / qfsky.map_long0167 merge the overlay with pkgutil.extend_path, which only
    sees directories present at first import. If qfsky was imported earlier without the
    overlay, add the overlay directories to the live package __path__ as well."""
    xg = xg or xg_paths
    overlay = Path(xg.OVERLAY)
    for item in reversed((str(overlay), str(xg.QFSKY_CODE.parent), str(xg.BOT_CODE))):
        if item not in sys.path:
            sys.path.insert(0, item)
    src = getattr(xg, "SRC", None)
    if src is not None and str(src) not in sys.path and Path(src).is_dir():
        sys.path.append(str(src))
    for name, sub in (("qfsky", overlay / "qfsky"), ("qfsky.map_long0167", overlay / "qfsky/map_long0167")):
        module = sys.modules.get(name)
        path = getattr(module, "__path__", None)
        if path is not None and sub.is_dir() and str(sub) not in list(path):
            path.append(str(sub))


def _qianniao_precheck(manifest, folder):
    """Only reject stale named placement; never certify account/capture readiness."""
    _ensure_qfsky_paths()
    variants = manifest.get("variants") or {}
    if not isinstance(variants, dict):
        raise ValueError("catalog_variants_invalid")
    named = variants.get("507487826") or {}
    if not isinstance(named, dict):
        raise ValueError("catalog_variants_invalid")
    if "qianniaocheng" not in named:
        return  # The full loader still enforces exact completeness.
    row = named["qianniaocheng"]
    from qfsky.map_long0167 import asset_catalog_v32c as policy_module
    policy_table = json.loads(policy_module._EXPECTED_VARIANTS_JSON)
    if hashlib.sha256(policy_module.canonical_json(policy_table)).hexdigest() != policy_module._EXPECTED_VARIANTS_SHA:
        raise ValueError("catalog_variant_policy_invalid")
    policy = (policy_table.get("507487826") or {}).get("qianniaocheng")
    if not isinstance(policy, dict) or any(k not in policy for k in ("scene", "placement_sha256", "annotation_sha256")):
        raise ValueError("catalog_variant_policy_invalid")
    if not isinstance(row, dict) or any(row.get(k) != policy[k] for k in ("scene", "placement_sha256", "annotation_sha256")):
        raise ValueError(QIANNIAOCHENG_STALE_CODE)
    # Fixed relative path, not a path supplied by untrusted manifest metadata.
    with open(xg_paths.win_path(folder / "SunsetEnd2/variants/qianniaocheng/placement.json"), "rb") as stream:
        raw = stream.read(128 * 1024 + 1)
    if len(raw) > 128 * 1024:
        raise ValueError("metadata_size")
    placement = json.loads(raw)
    if not isinstance(placement, dict):
        raise ValueError("metadata_shape")
    annotation = (placement.get("provenance") or {}).get("annotation")
    if (hashlib.sha256(raw).hexdigest() != policy["placement_sha256"]
            or hashlib.sha256(policy_module.canonical_json(annotation)).hexdigest() != policy["annotation_sha256"]):
        raise ValueError(QIANNIAOCHENG_STALE_CODE)


def _cache_result(key, row):
    """Caller holds _LOCK; metadata only, no I/O or process work in this lock."""
    _CACHE[key] = dict(row)
    _CACHE.move_to_end(key)
    while len(_CACHE) > MAX_CACHED_RESULTS:
        _CACHE.popitem(last=False)


def _background_check(scope, key, job, request, xg, folder, before, row):
    try:
        checked = _runtime_check(request)
        if before != _fingerprint(xg, folder):
            raise ValueError("catalog_changed_during_validation")
        result = dict(row, certified=True, state="ready", code="",
                      message="当前账号目录已通过实际运行时校验",
                      maps=int(checked["maps"]), variants=int(checked["variants"]))
    except Exception as exc:
        result = _failure(row, exc)
    finally:
        # Even an unexpected BaseException must release the reserved slot.
        with _LOCK:
            if _INFLIGHT.get(scope) is job:
                if job["latest"] == key and "result" in locals():
                    _cache_result(key, result)
                del _INFLIGHT[scope]


def certify(data, account, account_uuid, profile=None):
    _ensure_qfsky_paths()
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
    if not Path(xg_paths.win_path(manifest_path)).is_file():
        return row
    try:
        # Fingerprint before reading metadata: a concurrent atomic replacement must
        # never bind an old rejection to the new file's long-lived cache key.
        before = _fingerprint(xg, folder)
        scope = (str(xg.DATA), account, str(account_uuid), profile["build"],
                 profile.get("resource_revision"), profile.get("native_sha256"))
        key = (*scope, before)
        checking = dict(row, state="checking", code=CHECKING_CODE, message=CHECKING_MESSAGE)
        with _LOCK:
            pending = _INFLIGHT.get(scope)
            if pending is not None:
                pending["latest"] = key
            cached = _CACHE.get(key)
            if cached is not None:
                _CACHE.move_to_end(key)
                return dict(cached)
        failure = None
        try:
            manifest = _json(manifest_path)
            if (manifest.get("apk_build") != profile.get("build")
                    or manifest.get("native_sha256") != profile.get("native_sha256")):
                raise ValueError("catalog_version_stale")
            _qianniao_precheck(manifest, folder)
        except Exception as exc:
            failure = _failure(row, exc)
        # File I/O is outside _LOCK, including every failure-path recheck.
        if before != _fingerprint(xg, folder):
            return checking
        if failure is not None:
            with _LOCK:
                _cache_result(key, failure)
            return failure
        request = {"data": str(xg.DATA), "account": account, "uuid": str(account_uuid), "build": profile["build"]}
        with _LOCK:
            # Another caller/worker may have completed while the cheap check ran.
            cached = _CACHE.get(key)
            if cached is not None:
                _CACHE.move_to_end(key)
                return dict(cached)
            pending = _INFLIGHT.get(scope)
            if pending is not None:
                pending["latest"] = key
                return checking
            if len(_INFLIGHT) >= MAX_ACTIVE_CHECKS:
                return checking
            job = {"latest": key}
            _INFLIGHT[scope] = job
        try:
            # Starting and running a worker both happen outside the metadata lock.
            threading.Thread(target=_background_check,
                args=(scope, key, job, request, xg, folder, before, row),
                name="catalog-certification", daemon=True).start()
        except Exception:
            result = _failure(row, ValueError("catalog_check_start_failed"))
            with _LOCK:
                if _INFLIGHT.get(scope) is job:
                    if job["latest"] == key:
                        _cache_result(key, result)
                    del _INFLIGHT[scope]
            return result
        return checking
    except Exception as exc:
        return _failure(row, exc)


if __name__ == "__main__":
    try:
        request = json.loads(sys.stdin.read(65537))
        checked = _verify_request(request)
        output = {"ok": True, "checked": checked}
    except Exception as exc:
        code = "catalog_import_failed" if isinstance(exc, ImportError) else (getattr(exc, "code", None) or str(exc))
        if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", code):
            code = "catalog_runtime_invalid"
        output = {"ok": False, "code": code}
    print(json.dumps(output, ensure_ascii=False))
