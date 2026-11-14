# -*- coding: utf-8 -*-
"""首次安装向导：9 步状态、MuMu 检查、APK 校验、后台任务串联。

状态存 DATA/config.json 的 `wizard` 段；每一步都能回来看、失败可重试。
不连服务器/游戏服务器；APK 和游戏数据都在用户本机生成。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
for _p in (str(APP), str(APP / "bot")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import xg_account        # noqa: E402
import xg_exec           # noqa: E402
import xg_paths          # noqa: E402
import error_detail      # noqa: E402
from capture import device, gamedata, native_lib  # noqa: E402
import capture_ctl       # noqa: E402
import tasks             # noqa: E402

WIZARD_VERSION = 1
STEPS = [
    ("risk", "风险告知"),
    ("mumu", "检查 MuMu"),
    ("apk", "游戏安装包"),
    ("data", "生成游戏数据"),
    ("capture", "录制遇境"),
    ("login", "登录游戏号"),
    ("ai", "AI 接口"),
    ("owner", "选哥哥"),
    ("finish", "完成上线"),
]
STEP_IDS = [s[0] for s in STEPS]
LOCK = threading.RLock()


def _reset_path():
    return xg_paths.CONTROL / "resource_reset.json"


def _reset_doc():
    value = xg_paths.read_json(_reset_path(), {}) or {}
    return value if isinstance(value, dict) else {}


def _reset_steps(doc, account=None):
    account = str(account or active_account() or "")
    return [step for step in doc.get("steps", [])
            if step not in ("capture", "catalog")
            or account not in doc.get(step + "_approved_accounts", [])]


def resource_reset_status():
    doc = _reset_doc()
    result = {key: doc.get(key) for key in ("action", "selection", "at", "auto_suppressed")}
    result["steps"] = _reset_steps(doc)
    result["active"] = bool(result["steps"])
    return result


def _save_reset(doc):
    doc["active"] = bool(doc.get("steps"))
    xg_paths.write_json(_reset_path(), doc, private_file=False)


def after_data_reset(action, selection, batch_id=None):
    """Maintenance state only: never alter account/AI/owner settings."""
    with LOCK:
        selected = {key: bool((selection or {}).get(key)) for key in ("terrain", "catalog", "names", "captures", "props")}
        current = tasks.current()
        previous = _reset_doc()
        history = list(previous.get("history") or [])
        if action == "restore":
            # A restore only undoes the maintenance gates belonging to its
            # own latest batch. Earlier resets must survive later operations.
            match = bool(history and (not batch_id or history[-1].get("batch_id") == str(batch_id)))
            if match:
                doc = dict(history[-1].get("prior") or {})
                doc["history"] = history[:-1]
            else:
                doc = dict(previous)
                doc["restore_scope_unknown"] = bool(previous)
            doc.setdefault("steps", [])
            doc.update(action="restore", selection=selected, auto_suppressed=True,
                       at=time.strftime("%Y-%m-%d %H:%M:%S"),
                       stale_task_id=(current.get("task") or {}).get("id"), allowed_task_ids=[])
        else:
            prior = {key: value for key, value in previous.items() if key != "history"}
            history.append({"batch_id": str(batch_id or ""), "prior": prior})
            steps = list(previous.get("steps") or [])
            if selected["terrain"] or selected["names"]:
                if "data" not in steps:
                    steps.append("data")
            if selected["captures"]:
                if "capture" not in steps:
                    steps.append("capture")
            if selected["props"] and "props" not in steps:
                steps.append("props")
            if any(selected[key] for key in ("terrain", "catalog", "names", "captures")):
                if "catalog" not in steps:
                    steps.append("catalog")
            doc = {**previous, "version": 2, "action": str(action), "selection": selected,
                   "steps": [step for step in ("data", "props", "capture", "catalog") if step in steps],
                   "history": history, "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "reset_epoch": time.time(), "auto_suppressed": True,
                   "stale_task_id": (current.get("task") or {}).get("id"), "allowed_task_ids": []}
            if any(selected[key] for key in ("terrain", "catalog", "names", "captures")):
                doc["catalog_approved_accounts"] = []
            if selected["captures"]:
                inv = _capture_inventory()
                doc.update(capture_requested_accounts=[], capture_approved_accounts=[],
                           capture_baseline=[[r.get("record_dir"), r.get("record_sha256")]
                                             for r in inv["current"] + inv["stale"]])
        _save_reset(doc)
    return resource_reset_status()


def _reset_started(kind, output=None, account=None):
    with LOCK:
        doc = _reset_doc()
        if not doc:
            return
        if kind != "props":
            doc["auto_suppressed"] = False
        if kind == "capture":
            account = str(account or active_account() or "")
            if account and account not in doc.setdefault("capture_requested_accounts", []):
                doc["capture_requested_accounts"].append(account)
        task_id = ((output or {}).get("task") or {}).get("id")
        if task_id and task_id not in doc.setdefault("allowed_task_ids", []):
            doc["allowed_task_ids"].append(task_id)
        _save_reset(doc)


def _reset_complete(step, account=None):
    with LOCK:
        doc = _reset_doc()
        if step not in doc.get("steps", []):
            return
        if step in ("capture", "catalog"):
            account = str(account or active_account() or "")
            if not account or account in doc.setdefault(step + "_approved_accounts", []):
                return
            doc[step + "_approved_accounts"].append(account)
        else:
            doc["steps"] = [s for s in doc["steps"] if s != step]
        _save_reset(doc)


def _capture_after_reset(inventory, cap=None, account=None):
    """Retained raw recordings do not silently undo a registration reset."""
    reset = _reset_doc()
    if "capture" not in reset.get("steps", []):
        return inventory
    account = str(account or active_account() or "")
    binding = _account_binding(account)
    # Retained recordings of a different account must never reappear as this
    # account's newly approved capture, including after another account finishes.
    inventory = {**inventory, "current": [r for r in inventory["current"]
                 if binding and r.get("account_binding") == binding]}
    if "capture" not in _reset_steps(reset, account):
        return inventory
    cap = cap or capture_ctl.status() or {}
    registered = cap.get("last_registered") or {}
    name = Path(str(registered.get("record_dir") or "")).name
    baseline = {tuple(row) for row in reset.get("capture_baseline", []) if isinstance(row, list) and len(row) == 2}
    accepted = [r for r in inventory["current"]
                if account in reset.get("capture_requested_accounts", [])
                and str(registered.get("account") or "") == account
                and r.get("record_dir") == name
                and (r.get("record_dir"), r.get("record_sha256")) not in baseline]
    if accepted:
        _reset_complete("capture", account)
        return inventory
    return {**inventory, "current": [], "reset_blocked": True}


def catalog_status(account=None, doc=None, profile=None):
    from xg_gamedata.catalog_state import certify, REBUILD_MESSAGE
    account = str(account or active_account() or "")
    profile = profile or _current_game_profile()
    reset = _reset_doc()
    task_doc = tasks.current() if doc is None else doc
    task = task_doc.get("task") or {}
    task_account = str((task_doc.get("meta") or {}).get("account") or "")
    if task.get("kind") == "catalog" and task.get("running") and task_account in ("", account):
        return {"account": account, "certified": False, "state": "building", "code": "catalog_building",
                "message": "正在按当前录制重新生成目录，完成后再作运行时校验", "maps": 0, "variants": 0,
                "current_build": profile.get("build"), "current_version": profile.get("version") or ""}
    allowed = (task.get("id") in reset.get("allowed_task_ids", [])
               and task_account == account and bool(account)
               and task.get("kind") == "catalog" and task.get("ok") is True and not task.get("running"))
    reset_steps = _reset_steps(reset, account)
    blocked = ("capture" in reset_steps or "catalog" in reset_steps and not allowed)
    if blocked:
        return {"account": account, "certified": False, "state": "reset", "code": "resource_reset",
                "message": REBUILD_MESSAGE, "maps": 0, "variants": 0,
                "current_build": profile.get("build"), "current_version": profile.get("version") or ""}
    inventory = _capture_inventory(profile)
    if inventory["stale"] and not inventory["current"]:
        return {"account": account, "certified": False, "state": "stale", "code": "capture_stale",
                "message": REBUILD_MESSAGE, "maps": 0, "variants": 0,
                "current_build": profile.get("build"), "current_version": profile.get("version") or ""}
    owner = next((r for r in _accounts() if str(r.get("id")) == account), {})
    return certify(xg_paths.DATA, account, owner.get("user_uuid"), profile)


# ------------------------------------------------------------------ 状态读写
def _cfg():
    return xg_paths.config() or {}


def _wizard():
    w = _cfg().get("wizard")
    if not isinstance(w, dict):
        w = {}
    w.setdefault("version", WIZARD_VERSION)
    w.setdefault("steps", {})
    return w


def _save(w):
    with LOCK:
        cfg = _cfg()
        cfg["wizard"] = w
        xg_paths.save_config(cfg)


def _saved(step):
    v = (_wizard().get("steps") or {}).get(step)
    return v if isinstance(v, dict) else {}


def mark(step, done=True, detail=None, message=""):
    if step not in STEP_IDS:
        raise ValueError("未知向导步骤：" + str(step))
    with LOCK:
        w = _wizard()
        steps = w.setdefault("steps", {})
        cur = steps.get(step) if isinstance(steps.get(step), dict) else {}
        cur.update(done=bool(done), at=time.strftime("%Y-%m-%d %H:%M:%S"))
        if detail is not None:
            cur["detail"] = detail
        if message:
            cur["message"] = message
        steps[step] = cur
        _save(w)
    return cur


# ------------------------------------------------------------------ 计算状态
def _accounts():
    doc = xg_paths.read_json(xg_paths.ACCOUNTS_JSON, None)
    if isinstance(doc, dict):
        items = doc.get("accounts", doc)
    else:
        items = doc
    if isinstance(items, dict):
        items = list(items.values())
    return [x for x in (items or []) if isinstance(x, dict)]


def _catalog_dir(account):
    return xg_paths.ASSET_RELEASE / "asset-catalog-acct" / str(account or "_none")


def _catalog_result(account, doc=None):
    """Only a same-account, post-self-check success can certify publication."""
    doc = tasks.current() if doc is None else doc
    task = doc.get("task") or {}
    if task.get("kind") == "catalog":
        result = task.get("result") or {}
        owner = str((doc.get("meta") or {}).get("account") or result.get("account") or "")
        if not owner or owner == account:
            if task.get("running") or task.get("ok") is not True:
                return None
            return result if result.get("ok") is True and result.get("account") == account else None
    saved = _saved("capture")
    detail = saved.get("detail") or {}
    result = detail.get("result") or {}
    if (saved.get("done") is True and detail.get("account") == account
            and result.get("ok") is True and result.get("account") == account):
        return result
    return None


_CATALOG_HASH_CACHE = {}


def _catalog_sha(path):
    """Rehash changed files only; task state is checked before consulting this cache."""
    stat = path.stat()
    identity = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    cached = _CATALOG_HASH_CACHE.get(path)
    if cached and cached[0] == identity:
        return cached[1]
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    after = path.stat()
    if identity != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise ValueError("catalog changed during validation")
    if len(_CATALOG_HASH_CACHE) >= 2048:
        _CATALOG_HASH_CACHE.clear()
    value = digest.hexdigest()
    _CATALOG_HASH_CACHE[path] = (identity, value)
    return value


def _catalog_ready(account, doc=None, profile=None):
    return catalog_status(account, doc=doc, profile=profile).get('certified') is True


def active_account():
    try:
        return xg_account.active_id() or ""
    except Exception:
        return ""


def _apk_path():
    w = _wizard()
    apk = ((w.get("apk") or {}).get("path")) or ((_cfg().get("gamedata") or {}).get("apk"))
    if apk and Path(str(apk)).is_file():
        return str(apk)
    try:
        found = sorted(xg_paths.APK_DIR.glob("*.apk"), key=lambda p: p.stat().st_mtime, reverse=True)
        return str(found[0]) if found else ""
    except OSError:
        return ""


# ---------------------------------------------------------------- 版本与录制
def _profile_candidate(value):
    """把 APK build / 版本字符串映射到现有版本档案（不写死版本号）。"""
    from qfsky.version_profile import PROFILES, profile_for_build, profile_for_version
    profile = None
    if type(value) is int:
        profile = profile_for_build(value)
    elif isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            profile = profile_for_build(int(text))
        if profile is None:
            profile = profile_for_version(text)
        if profile is None and "-" in text:
            tail = text.rsplit("-", 1)[-1]
            if tail.isdigit():
                profile = profile_for_build(int(tail))
    if profile is None:
        return None
    if "build" not in profile:
        for build, row in PROFILES.items():
            if (row.get("version") == profile.get("version")
                    and row.get("game_version") == profile.get("game_version")):
                profile = dict(profile)
                profile["build"] = build
                break
    return dict(profile)


def _current_game_profile():
    """当前选择的游戏版本档案：优先 APK 校验结果，其次 MuMu 识别，最后现有默认档。"""
    from qfsky.version_profile import CURRENT_BUILD
    w = _wizard()
    apk = w.get("apk") if isinstance(w.get("apk"), dict) else {}
    check = apk.get("check") if isinstance(apk.get("check"), dict) else {}
    saved = w.get("steps") if isinstance(w.get("steps"), dict) else {}
    mumu = saved.get("mumu") if isinstance(saved.get("mumu"), dict) else {}
    mumu_detail = mumu.get("detail") if isinstance(mumu.get("detail"), dict) else {}
    cfg = _cfg()
    gd = cfg.get("gamedata") if isinstance(cfg.get("gamedata"), dict) else {}
    capture = cfg.get("capture") if isinstance(cfg.get("capture"), dict) else {}
    candidates = [
        check.get("version_code"), check.get("build"), check.get("version"),
        apk.get("version_code"), apk.get("build"), apk.get("version"),
        mumu_detail.get("version_code"), mumu_detail.get("build"),
        gd.get("version_code"), gd.get("build"), gd.get("version"),
        capture.get("version_code"), capture.get("build"),
    ]
    for value in candidates:
        profile = _profile_candidate(value)
        if profile and not profile.get("legacy"):
            return profile
    return _profile_candidate(CURRENT_BUILD)


def _record_profile(record):
    return _profile_candidate(record.get("apk_build")) if isinstance(record, dict) else None


def _record_version(record, profile=None):
    if profile:
        return profile.get("version") or profile.get("game_version") or ""
    for key in ("game_version", "version", "apk_version"):
        value = record.get(key) if isinstance(record, dict) else None
        if isinstance(value, str) and value.strip():
            return value.strip()
    build = record.get("apk_build") if isinstance(record, dict) else None
    return ("未知版本（build=%s）" % build) if build is not None else "未知版本"


def _record_is_current(record, profile):
    if not isinstance(record, dict) or not profile:
        return False
    nv = record.get("netversion_u16")
    if (record.get("scene") == profile.get("home_scene") and nv is not None
            and (not isinstance(nv, list) or len(nv) != 6
                 or any(type(v) is not int or not 0 <= v <= 65535 for v in nv)
                 or nv[0] != profile.get("home_resource_tag"))):
        return False
    return (type(record.get("apk_build")) is int
            and record["apk_build"] == profile.get("build")
            and record.get("native_sha256") == profile.get("native_sha256")
            and record.get("resource_revision") == profile.get("resource_revision"))


def _account_binding(account):
    if not account:
        return ""
    row = next((x for x in _accounts() if str(x.get("id")) == str(account)), None)
    uuid = (row or {}).get("user_uuid")
    if not uuid:
        return ""
    try:
        from qfsky.join0167_profile import account_binding
        return account_binding(uuid)
    except Exception:
        return ""


def _record_entry(path, record, raw, profile):
    binding = record.get("account_binding")
    binding = binding.get("value") if isinstance(binding, dict) else ""
    rp = _record_profile(record)
    return {
        "record_dir": path.name,
        "scene": record.get("scene") if isinstance(record.get("scene"), str) else "",
        "scene_name": record.get("scene_name") if isinstance(record.get("scene_name"), str) else "",
        "apk_build": record.get("apk_build"),
        "resource_revision": record.get("resource_revision"),
        "netversion_u16": record.get("netversion_u16"),
        "native_sha256": record.get("native_sha256"),
        "version": _record_version(record, rp),
        "game_version": (rp or {}).get("game_version") or "",
        "captured_utc": str(record.get("captured_utc") or ""),
        "account_binding": binding if isinstance(binding, str) else "",
        "record_sha256": hashlib.sha256(raw).hexdigest(),
        "current": _record_is_current(record, profile),
    }


def _capture_inventory(profile=None):
    """扫 current_profiles：当前版本录制与旧版本录制分开列，旧的不算已录过。"""
    profile = profile or _current_game_profile()
    current, stale = [], []
    root = xg_paths.CURRENT_PROFILES
    try:
        children = sorted(root.iterdir())
    except OSError:
        children = []
    for child in children:
        record_path = child / "record.json"
        if not child.is_dir() or not record_path.is_file():
            continue
        try:
            raw = record_path.read_bytes()
            record = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        entry = _record_entry(child, record, raw, profile)
        (current if entry["current"] else stale).append(entry)
    order = lambda row: (str(row.get("captured_utc") or ""), str(row.get("record_dir") or ""))
    current.sort(key=order, reverse=True)
    stale.sort(key=order, reverse=True)
    return {"current": current, "stale": stale, "profile": profile,
            "current_version": profile.get("version") or "",
            "current_build": profile.get("build")}


def _stale_capture_message(profile, stale):
    current = (profile or {}).get("version") or "新版本"
    versions = []
    for row in stale or []:
        version = str(row.get("version") or "").strip()
        if version and version not in versions:
            versions.append(version)
    old = "、".join(versions) if versions else "旧版本"
    return ("游戏已更新到 %s，之前的遇境录制是 %s 的，"
            "需要用 MuMu 重新录一次遇境（只需遇境一张）。" % (current, old))


def _active_capture_state(cap):
    state = str((cap or {}).get("state") or "")
    return (bool((cap or {}).get("worker_pid")) and state not in ("", "idle", "stopped")
            or state in ("starting", "searching", "waiting", "recording", "registering"))


def capture_status(profile=None, inventory=None):
    """给前端的采集状态：recorded / last_registered 只认当前版本录制。"""
    cap = dict(capture_ctl.status() or {})
    profile = profile or _current_game_profile()
    inv = _capture_after_reset(inventory or _capture_inventory(profile), cap)
    current, stale = inv["current"], inv["stale"]
    cap["recorded"] = sorted({str(row["scene"]) for row in current if row.get("scene")})
    cap["stale"] = stale
    cap["current_version"] = profile.get("version") or ""
    cap["current_build"] = profile.get("build")
    cap["current_game_version"] = profile.get("game_version") or ""
    current_names = {str(row.get("record_dir") or "") for row in current}
    reg = cap.get("last_registered")
    if isinstance(reg, dict):
        name = Path(str(reg.get("record_dir") or "")).name
        if name not in current_names:
            cap["last_registered_stale"] = reg
            cap["last_registered"] = None
    if stale and not current:
        notice = _stale_capture_message(profile, stale)
        cap["stale_notice"] = notice
        cap["stale_version"] = stale[0].get("version") or ""
        if not _active_capture_state(cap) and cap.get("state") != "error":
            cap["state"] = "stale"
            cap["message"] = notice
    else:
        cap.pop("stale_notice", None)
    if inv.get("reset_blocked"):
        cap.update(state="reset" if not _active_capture_state(cap) else cap.get("state"),
                   message="录制登记已清理，请重新录制遇境或明确重新登记现有录制",
                   last_registered=None)
    cert = catalog_status(profile=profile)
    cap["catalog"] = {**(cap.get("catalog") or {}), **cert,
                      "done": cert["certified"], "needed": not cert["certified"]}
    return cap


def _game_data_versions():
    """data\\game 下其它版本的旧游戏数据目录，只为提示“需要重新生成”。"""
    root = xg_paths.DATA / "game"
    current = str(xg_paths.GAME_VERSION)
    rows = []
    try:
        children = sorted(root.iterdir())
    except OSError:
        children = []
    for child in children:
        if child.name == current or not child.is_dir():
            continue
        names = (child / "names").is_dir()
        terrain = (child / "terrain").is_dir()
        if not (names or terrain):
            continue
        profile = _profile_candidate(child.name)
        rows.append({"dir": child.name, "path": str(child),
                     "version": (profile or {}).get("version") or child.name,
                     "has_names": names, "has_terrain": terrain})
    return rows


def _props_status(profile=None):
    from xg_gamedata.status import props_status
    result = props_status(xg_paths.props_dir(), profile or _current_game_profile())
    if "props" in _reset_doc().get("steps", []):
        result.update(ready=False, state="reset", code="props_reset",
                      message="道具形状已清理，请单独补生成；无需重做名称表与地形。")
    return result


def _game_data_state(profile, complete):
    stale = _game_data_versions()
    return {"current_version": profile.get("version") or "",
            "current_dir": str(xg_paths.GAME_DIR),
            "current_dir_version": str(xg_paths.GAME_VERSION),
            "complete": bool(complete), "stale": stale,
            "needs_rebuild": bool(stale) and not complete}


def _ai_configured():
    try:
        import llm_providers
        return bool(llm_providers.settings_status(llm_providers.load_settings()).get("configured"))
    except Exception:
        return False


def _data_complete(doc=None):
    """Require the builder's committed full manifest and its extant outputs.

    A saved checkbox, a nonempty directory or a stopped process is not proof.
    Check metadata and file sizes, not GBs of geometry, on each status poll.
    """
    try:
        task = ((tasks.current() if doc is None else doc).get("task") or {})
        if task.get("kind") == "data" and (task.get("running") or task.get("ok") is not True):
            return False
        for name in ("emotes.json", "outfit_names_zh.json", "gift_names.json",
                     "catalog.json", "destination-records-v25.json", "scene_names.json"):
            table = json.loads((xg_paths.NAMES / name).read_text(encoding="utf-8"))
            if not isinstance(table, (dict, list)) or not table:
                return False
        root = xg_paths.TERRAIN
        summary = json.loads((root / "build-summary.json").read_text(encoding="utf-8"))
        raw = (root / "multimap-catalog.json").read_bytes()
        catalog = json.loads(raw)
        proof = summary["catalog"]
        requested = summary["maps_requested"]
        entries = catalog["entries"]
        count = summary["apk"]["scene_count"]
        totals = summary["totals"]
        if (summary.get("schema") != "skyterrain-user-terrain-build-v1"
                or catalog.get("format") != "skyterrain-catalog-v1"
                or summary.get("status") != "complete" or not summary.get("finished_at")
                or proof.get("partial") is not False
                or hashlib.sha256(raw).hexdigest() != proof["sha256"]
                or not isinstance(count, int) or isinstance(count, bool) or count <= 0
                or not isinstance(requested, list) or not isinstance(entries, list)
                or len(requested) != count or len(set(requested)) != count
                or len(entries) != count or proof["scene_count"] != count
                or totals["maps_requested"] != count or totals["maps_failed"] != 0
                or set(summary["maps"]) != set(requested)
                or set(row["scene_name"] for row in entries) != set(requested)
                or any(summary["maps"][name]["status"] not in ("ready", "on_demand", "empty")
                       for name in requested)):
            return False
        assets = (root / "assets").resolve()

        def asset_file(relative, parent=assets):
            path = (parent / relative).resolve()
            path.relative_to(assets)  # Never follow a stale/escaping manifest path.
            if not path.is_file() or path.stat().st_size <= 0:
                raise ValueError("missing asset")
            return path

        for row in entries:
            if row.get("collision_status") == "empty_scene":
                continue
            manifest_path = asset_file(row["asset_relpath"])
            manifest_raw = manifest_path.read_bytes()
            if hashlib.sha256(manifest_raw).hexdigest() != row["asset_manifest_sha256"]:
                return False
            manifest = json.loads(manifest_raw)
            if (manifest.get("format") != "skyterrain-static-v1"
                    or manifest.get("mapName") != row["scene_name"]):
                return False
            parent = manifest_path.parent
            for field, count_field in (("positions", "vertexCount"), ("indices", "triangleCount")):
                if asset_file(manifest[field], parent).stat().st_size != manifest[count_field] * 12:
                    return False
            asset_file("manifest.json", parent)
            asset_file(manifest["evidenceManifest"]["file"], parent)
            asset_file(row["staged_navigation"]["report_relpath"])
            if row.get("navigation_relpath"):
                asset_file(row["navigation_relpath"])
        files = size = 0
        for directory, _, names in os.walk(xg_paths.long_path(assets)):
            for name in names:
                size += os.stat(os.path.join(directory, name)).st_size
                files += 1
        output = totals["output"]
        return (files > 0 and files == output["files"] == summary["assemble"]["expected_files"]
                and size == output["bytes"])
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return False


def step_status():
    """每一步现在的真实状态（已完成 / 未完成 / 提示）。"""
    w = _wizard()
    saved = w.get("steps") or {}
    consent = bool((_cfg().get("consent") or {}).get("accepted"))
    acc = active_account()
    cap = capture_ctl.status()
    apk = _apk_path()
    profile = _current_game_profile()
    inventory = _capture_after_reset(_capture_inventory(profile), cap)
    data_done = _data_complete()
    if "data" in _reset_doc().get("steps", []):
        data_done = False
    data_state = _game_data_state(profile, data_done)
    capture_account = str(acc or (cap.get("catalog") or {}).get("account")
                          or (cap.get("last_registered") or {}).get("account") or "")
    capture_ok = _catalog_ready(capture_account, profile=profile)
    data_hint = "生成名称表、地形与道具形状（道具失败可单独补生成）"
    if data_state["needs_rebuild"]:
        data_hint = ("游戏已更新到 %s，新版本需要重新生成游戏数据"
                     "（名称表 + 地形，约 25 分钟）。" % (profile.get("version") or "新版本"))
    capture_hint = "用这个号在 MuMu 里走进遇境，录一次，再生成这个号的地图目录"
    stale_capture = bool(inventory["stale"] and not inventory["current"])
    if stale_capture:
        capture_hint = _stale_capture_message(profile, inventory["stale"])
    if inventory.get("reset_blocked"):
        capture_hint = "录制登记已清理，请重新录制遇境或明确重新登记现有录制"
    checks = {
        "risk": (consent, "先读完风险告知并同意"),
        "mumu": (bool((saved.get("mumu") or {}).get("done")) or bool((cap.get("mumu") or {}).get("root")),
                 "检查 MuMu、adb、Root，并在 MuMu 里装好游戏"),
        "apk": (bool((saved.get("apk") or {}).get("done")) and bool(apk), "从 MuMu 拉 APK，或手动选安装包"),
        "data": (data_done, data_hint),
        # 账号登录会话与游戏版本无关：换版本不要求重新登录。
        "login": (bool(acc and _accounts()), "登录要给小光用的游戏号（不得使用主号）"),
        "capture": (capture_ok, capture_hint),
        "ai": (_ai_configured(), "填 AI 接口的 Key / 地址 / 模型并测试"),
        "owner": (xg_paths.OWNER_UUID.is_file(), "从小光号好友里选“哥哥”，起称呼、选人设"),
        "finish": (False, "全部完成后点“完成，上线”"),
    }
    checks["finish"] = (bool(w.get("completed_at"))
                        and all(value[0] for key, value in checks.items() if key != "finish"),
                        checks["finish"][1])
    rows = []
    for sid, title in STEPS:
        done, hint = checks[sid]
        item = {"id": sid, "title": title, "done": bool(done), "hint": "" if done else hint}
        if isinstance(saved.get(sid), dict):
            item["at"] = saved[sid].get("at")
            item["detail"] = saved[sid].get("detail")
            item["message"] = saved[sid].get("message")
        if sid == "data" and data_state["needs_rebuild"]:
            item["needs_rebuild"] = True
            item["game_data_stale"] = data_state["stale"]
        if sid == "capture" and inventory["stale"]:
            item["stale"] = inventory["stale"]
            item["stale_version"] = inventory["stale"][0].get("version") or ""
            if stale_capture:
                notice = _stale_capture_message(profile, inventory["stale"])
                item["stale_notice"] = notice
                item["message"] = notice
                if isinstance(item.get("detail"), dict):
                    item["detail"] = {k: v for k, v in item["detail"].items() if k != "result"}
        rows.append(item)
    current = next((r["id"] for r in rows if not r["done"]), None)
    return rows, current


def is_complete(rows=None):
    """Historical completion is valid only while every prerequisite is real."""
    if rows is None:
        rows, _ = step_status()
    return (bool(_wizard().get("completed_at"))
            and all(row.get("done") is True for row in rows if row.get("id") != "finish"))


def _device_status():
    """本机设备摘要（W7）；失败只返回中文说明，不影响向导其它步骤。"""
    try:
        import local_device
        return local_device.status_all()
    except Exception as exc:
        return {"ok": False, "current": {"registered": False,
                                         "message": "本机设备状态读取失败：" + str(exc)[:120]},
                "android": {"registered": False}, "ios": {"registered": False}, "devices": []}


def summary():
    rows, current = step_status()
    task = tasks.current()
    profile = _current_game_profile()
    inventory = _capture_inventory(profile)
    cap = capture_status(profile, inventory)
    certificate = catalog_status(profile=profile, doc=task)
    props = _props_status(profile)
    game_data = _game_data_state(profile, next(r["done"] for r in rows if r["id"] == "data"))
    game_data["props"] = props
    return {"ok": True, "version": WIZARD_VERSION, "steps": rows, "current": current,
            "device": _device_status(),
            "completed": is_complete(rows),
            "completed_at": _wizard().get("completed_at"),
            "account": active_account(), "apk": _apk_path(),
            "aiConfigured": _ai_configured(),
            "gameVersion": profile.get("version") or "",
            "gameBuild": profile.get("build"),
            "gameVersionDir": str(xg_paths.GAME_VERSION),
            "gameData": game_data,
            "task": task.get("task"), "taskRunning": bool(task.get("running")),
            "catalog": certificate, "props": props, "resourceReset": resource_reset_status(),
            "capture": {"state": cap.get("state"), "message": cap.get("message"),
                        "scene": cap.get("scene"), "scene_name": cap.get("scene_name"),
                        "recorded": cap.get("recorded") or [], "stale": cap.get("stale") or [],
                        "stale_notice": cap.get("stale_notice") or "",
                        "stale_version": cap.get("stale_version") or "",
                        "current_version": cap.get("current_version") or "",
                        "current_game_version": cap.get("current_game_version") or "",
                        "last_registered": cap.get("last_registered"),
                        "last_registered_stale": cap.get("last_registered_stale"),
                        "mumu": cap.get("mumu") or {},
                        "last_error": cap.get("last_error") or "",
                        "catalog": cap.get("catalog") or {}},
            "autostart": _autostart_status()}


def _autostart_status():
    try:
        import autostart
        return autostart.status()
    except Exception:
        return {"enabled": False, "supported": os.name == "nt"}


# ------------------------------------------------------------------ 第 2 步
def check_mumu(adb_path=None):
    r = device.check_device(adb_path)
    if adb_path and r.get("adb"):
        cfg = _cfg()
        cfg.setdefault("capture", {})["adb"] = str(adb_path)
        if r.get("mumu_dir"):
            cfg["capture"]["mumu_dir"] = str(r["mumu_dir"])
        xg_paths.save_config(cfg)
    if r.get("ok"):
        mark("mumu", True, detail={k: r.get(k) for k in
                                   ("adb", "mumu_dir", "serial", "pid", "root", "version_code", "scene")},
             message=r.get("message", ""))
        cfg = _cfg()
        cfg.setdefault("capture", {}).update({"adb": r.get("adb"), "serial": r.get("serial")})
        xg_paths.save_config(cfg)
    return r


# ------------------------------------------------------------------ 第 3 步
def pull_apk():
    adb, mumu = device.find_adb()
    if not adb:
        return {"ok": False, "message": "没找到 MuMu/adb：先做第 2 步，或手填 adb.exe 路径"}
    game = device.find_game(adb)
    if not game:
        return {"ok": False, "message": "没找到正在跑光遇的 MuMu：先打开游戏、登进遇境"}
    plan = tasks.command_for("pull-apk", adb=str(adb), serial=game["serial"])
    out = tasks.start("pull-apk", plan, label="从 MuMu 拉游戏安装包",
                      meta={"adb": str(adb), "serial": game["serial"]})
    if out.get("ok"):
        w = _wizard()
        w.setdefault("apk", {})["from"] = "mumu"
        _save(w)
    return out


def _version_ok(final):
    code = final.get("version_code") or final.get("versionCode") or final.get("build")
    ver = final.get("version") or final.get("app_version") or ""
    from qfsky.version_profile import profile_for_version, CURRENT_BUILD
    profile = profile_for_version(code) or profile_for_version(ver)
    if profile is None:
        return False, f"版本不对：装的是 {code or ver or '未知'}，小光只适配 0.16.8 / 421629"
    if profile["legacy"]:
        return False, (f"识别为旧版 {profile['version']} / {profile['version']}；"
                       "0.16.7 只保留识别，不再用于生成数据，请升级到 0.16.8 / 421629")
    return True, ""


def check_apk(path):
    path = str(path or "").strip()
    if not path:
        return {"ok": False, "message": "先选安装包文件"}
    p = Path(path)
    if not p.is_file():
        return {"ok": False, "message": f"文件不存在：{path}"}
    r = gamedata.run(["check-apk", "--apk", str(p)], timeout=1800)
    final = r.get("final") or {}
    if not r.get("ok"):
        return {"ok": False, "message": final.get("message") or final.get("error") or "APK 校验没通过"}
    okv, why = _version_ok(final)
    if not okv:
        return {"ok": False, "message": why, "detail": final}
    apk_final = final.get("apk") or final.get("path") or str(p)
    fake = bool(os.environ.get("XG_CAPTURE_FAKE_DIR"))
    native = None
    if not fake:
        try:
            native = native_lib.ensure_native_library(apk_final)
        except Exception as exc:
            return {"ok": False, "message": str(exc), "detail": final}
    cfg = _cfg()
    cfg.setdefault("capture", {})["apk"] = str(apk_final)
    cfg.setdefault("gamedata", {})["apk"] = str(apk_final)
    w = cfg.setdefault("wizard", {"version": WIZARD_VERSION, "steps": {}})
    w.setdefault("apk", {}).update({"path": str(apk_final), "from": "manual" if not
                                    (w.get("apk") or {}).get("from") else w["apk"].get("from"),
                                    "checked": True, "version": final.get("version") or "0.16.8",
                                    "version_code": final.get("version_code") or 421629})
    w.setdefault("steps", {}).setdefault("apk", {})["done"] = True
    w["steps"]["apk"]["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    w["steps"]["apk"]["detail"] = {"path": str(apk_final), "native": native, "check": final}
    xg_paths.save_config(cfg)
    return {"ok": True, "apk": str(apk_final), "native": native, "detail": final,
            "message": "安装包版本正确（0.16.8 / 421629）"}


# ------------------------------------------------------------------ 第 4 步
def start_data(resume=False):
    apk = _apk_path()
    if not apk:
        return {"ok": False, "message": "还没校验安装包：先做第 3 步"}
    plan = tasks.command_for("data", apk=apk, resume=bool(resume))
    out = tasks.start("data", plan, label="生成游戏数据（名称表 + 地形 + 道具形状）",
                      meta={"apk": apk, "resume": bool(resume)})
    if out.get("ok"):
        _reset_started("data", out)
    return out


# ------------------------------------------------------------------ 第 5 步
def start_props():
    apk = _apk_path()
    if not apk:
        return {"ok": False, "message": "请先校验本机 APK，再补生成道具形状"}
    out = tasks.start("props", tasks.command_for("props", apk=apk),
                      label="补生成道具形状（不重做地形）", meta={"apk": apk})
    if out.get("ok"):
        _reset_started("props", out)
    return out


def login_done():
    accounts = _accounts()
    if not accounts:
        return {"ok": False, "message": "还没登录游戏号：先在下面用手机号/邮箱登录一个号"}
    acc = active_account()
    ids = [str(a.get("id") or "") for a in accounts]
    if not acc or acc not in ids:
        # 2026-11-08 Claude：登录成功只把号写进账号库，不会设成“当前使用的号”
        # （服务器后台是登录后再点“切换到这个号”）。向导里自动选刚登录的那个（最新加入的）。
        newest = max(accounts, key=lambda a: float(a.get("created_at") or 0))
        acc = str(newest.get("id") or "")
        if not acc:
            return {"ok": False, "message": "账号库里的号没有编号，请重新登录一次"}
        f = xg_paths.CONTROL / "active_account"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(acc + "\n", encoding="utf-8")
    mark("login", True, detail={"account": acc}, message="已登录：" + acc)
    return {"ok": True, "account": acc}


# ------------------------------------------------------------------ 第 6 步
def start_capture():
    out = capture_ctl.start()
    if out.get("ok"):
        _reset_started("capture")
        cfg = _cfg()
        cfg.setdefault("wizard", {"version": WIZARD_VERSION, "steps": {}})
        w = cfg["wizard"]
        w["capture_started_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        xg_paths.save_config(cfg)
    return out


def stop_capture():
    return capture_ctl.stop()


def start_catalog(account=None, apk=None):
    account = str(account or active_account() or "")
    apk = apk or _apk_path()
    profile = _current_game_profile()
    inventory = _capture_after_reset(_capture_inventory(profile), account=account)
    if inventory["stale"] and not inventory["current"]:
        notice = _stale_capture_message(profile, inventory["stale"])
        return {"ok": False, "code": "capture_stale", "message": notice,
                "stale": inventory["stale"], "current_version": profile.get("version") or ""}
    if not account:
        return {"ok": False, "message": "还没登录游戏号：先做第 5 步"}
    if not apk:
        return {"ok": False, "message": "还没校验安装包：先做第 3 步"}
    binding = _account_binding(account)
    current_home = [r for r in inventory["current"]
                    if r.get("scene") == profile.get("home_scene")
                    and r.get("account_binding") == binding and binding
                    and isinstance(r.get("netversion_u16"), list)
                    and len(r["netversion_u16"]) == 6
                    and r["netversion_u16"][0] == profile.get("home_resource_tag")]
    if not current_home:
        return {"ok": False, "code": "capture_required",
                "message": "目录需要用当前版本重新录制遇境后重建；请先重新录制或明确重新登记这个号的遇境"}
    plan = tasks.command_for("catalog", apk=apk, account=account)
    out = tasks.start("catalog", plan, label="生成这个号的地图目录",
                      meta={"account": account, "apk": apk})
    if out.get("ok"):
        _reset_started("catalog", out)
        cap = capture_ctl.status()
        cap["catalog"] = {"needed": False, "started": True, "done": False,
                          "account": account, "apk": apk}
        xg_paths.write_json(xg_paths.CAPTURE_STATUS, cap, private_file=False)
    return out


def register_capture(path=""):
    """对已经录好的目录补做一次 capture-register（W9：登记失败不再只能重录）。"""
    cap = capture_ctl.status()
    target = str(path or cap.get("last_record_dir") or "").strip()
    if not target:
        return {"ok": False, "message": "没有可重新登记的录制目录：先录一次，或传入 --dir"}
    p = Path(target)
    if not p.is_absolute():
        p = xg_paths.CURRENT_PROFILES / p
    try:
        p = p.resolve()
        root = xg_paths.CURRENT_PROFILES.resolve()
    except OSError as exc:
        return {"ok": False, "message": "录制目录不可读：%s" % exc}
    if not p.is_dir() or not p.is_relative_to(root):
        return {"ok": False, "message": "只能重新登记本机 current_profiles 里的录制：%s" % p}
    try:
        raw = (p / "record.json").read_bytes()
        record = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, ValueError):
        record = None
    profile = _current_game_profile()
    if isinstance(record, dict) and not _record_is_current(record, profile):
        entry = _record_entry(p, record, raw, profile)
        notice = _stale_capture_message(profile, [entry])
        return {"ok": False, "code": "capture_stale", "message": notice, "stale": [entry]}
    r = gamedata.run(["capture-register", "--dir", str(p)], timeout=900)
    final = r.get("final") or {}
    if not r.get("ok"):
        why = final.get("message") or final.get("error") or "capture-register 失败"
        detail = error_detail.from_result(r)
        detail["record_dir"] = str(p)
        cap.update(state="error", last_error=str(why),
                   last_error_code=detail.get("code") or "capture_register",
                   last_error_detail=detail, last_record_dir=str(p),
                   message="重新登记失败：%s（录制还在 %s）" % (why, p.name))
        xg_paths.write_json(xg_paths.CAPTURE_STATUS, cap, private_file=False)
        return {"ok": False, "message": str(why), "detail": detail, "final": final}
    # This explicit action may re-register a retained current-version recording.
    account = str(final.get("account") or active_account() or "")
    _reset_complete("capture", account)
    _reset_started("capture", account=account)
    rec = final.get("recorded") or {}
    recorded = sorted(set(rec.get("scenes") or []) | set(cap.get("recorded") or []))
    scene = str(final.get("scene") or cap.get("scene") or "")
    apk = _apk_path()
    already = _catalog_ready(account, profile=profile)
    need = bool(scene == "CandleSpace" and account and apk and not already)
    cap.update(state="ready", last_error="", last_error_code="", last_error_detail=None,
               last_record_dir=str(p), recorded=recorded,
               last_registered={"record_dir": str(p), "scene": scene,
                                "map": final.get("map") or scene,
                                "account": account, "at": time.strftime("%Y-%m-%d %H:%M:%S")},
               message="「%s」已重新登记到 %s 名下。" % (
                   final.get("map") or scene, final.get("alias") or account or "本机"))
    cap["catalog"] = {"needed": need, "account": account, "apk": apk,
                      "started": False, "done": not need}
    xg_paths.write_json(xg_paths.CAPTURE_STATUS, cap, private_file=False)
    if need:
        ensure_catalog()
    elif already:
        mark("capture", True, detail={"account": account, "catalog": str(_catalog_dir(account)),
                                      "result": _catalog_result(account)},
             message="这个号的地图目录已经生成")
    return {"ok": True, "record_dir": str(p), "account": account, "scene": scene,
            "catalog_needed": need, "message": cap["message"]}


def ensure_catalog():
    """后台监视：录图进程说需要 catalog 时，自动起任务（同一时刻只跑一个）。"""
    reset = _reset_doc()
    if reset.get("auto_suppressed"):
        return None
    cap = capture_ctl.status()
    cat = cap.get("catalog") or {}
    if not cat.get("needed") or cat.get("started"):
        return None
    account, apk = cat.get("account"), cat.get("apk") or _apk_path()
    if not account or not apk:
        return None
    if any(s in _reset_steps(reset, account) for s in ("data", "capture")):
        return None
    if _catalog_ready(account):
        mark("capture", True, detail={"account": account, "result": _catalog_result(account)})
        return None
    cur = tasks.current()
    if cur.get("running"):
        return None
    out = start_catalog(account, apk)
    return out


# ------------------------------------------------------------------ 第 7 步
def ai_done():
    if not _ai_configured():
        return {"ok": False, "message": "AI 接口还没配齐：填服务商/地址/模型/Key，点测试通过再保存"}
    mark("ai", True, message="AI 接口已配置")
    return {"ok": True}


# ------------------------------------------------------------------ 第 8 步
def owner_done(uuid, title="", persona=""):
    uuid = str(uuid or "").strip()
    if not xg_account.UUID_RE.fullmatch(uuid):
        return {"ok": False, "message": "哥哥的 uuid 形状不对"}
    if title:
        import owner_title
        ok, why = owner_title.set_title(str(title))
        if not ok:
            return {"ok": False, "message": why}
    xg_paths.OWNER_UUID.parent.mkdir(parents=True, exist_ok=True)
    xg_paths.OWNER_UUID.write_text(uuid + "\n", encoding="utf-8")
    if persona:
        try:
            xg_exec.run([xg_paths.python_exe(), "-B", str(xg_paths.BOT_CODE / "personas.py"),
                         "select", str(persona)], cwd=str(xg_paths.APP),
                        capture_output=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            pass
    mark("owner", True, detail={"uuid": uuid, "title": title, "persona": persona},
         message="已选好哥哥")
    return {"ok": True, "uuid": uuid}


# ------------------------------------------------------------------ 第 9 步
def finish():
    rows, _ = step_status()
    missing = [r["title"] for r in rows if r["id"] != "finish" and not r["done"]]
    if missing:
        return {"ok": False, "message": "还没完成：" + "、".join(missing)}
    with LOCK:
        w = _wizard()
        w["completed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        w.setdefault("steps", {})["finish"] = {
            "done": True, "at": w["completed_at"], "message": "向导完成"}
        _save(w)
    return {"ok": True, "completed": True, "message": "完成，可以上线了"}


# ------------------------------------------------------------------ 任务回调
def after_task(doc=None):
    doc = doc or tasks.current()
    task = doc.get("task") or {}
    tid = task.get("id")
    if not tid or task.get("running"):
        return None
    reset = _reset_doc()
    if tid == reset.get("stale_task_id") and tid not in reset.get("allowed_task_ids", []):
        return None
    w = _wizard()
    if w.get("last_task_id") == tid:
        return None
    kind, ok = task.get("kind"), bool(task.get("ok"))
    result = task.get("result") or {}
    changed = False
    if kind == "data":
        complete = ok and _data_complete(doc)
        if complete:
            _reset_complete("data")
        from xg_gamedata.status import props_status
        props_ready = props_status(xg_paths.props_dir(), _current_game_profile())["ready"]
        if props_ready:
            _reset_complete("props")
        mark("data", complete, detail={"task": tid, "result": result},
             message=("游戏数据已生成" if props_ready else "名称表与地形已生成；道具形状可单独补生成")
                     if complete else "游戏数据未完整生成，请重试或续跑")
        changed = True
    elif kind == "props":
        from xg_gamedata.status import props_status
        if ok and props_status(xg_paths.props_dir(), _current_game_profile())["ready"]:
            _reset_complete("props")
        changed = True
    elif kind == "catalog":
        acc = str((doc.get("meta") or {}).get("account") or result.get("account") or "")
        complete = ok and _catalog_ready(acc, doc)
        if complete:
            _reset_complete("catalog", acc)
        mark("capture", complete, detail={"task": tid, "account": acc, "result": result},
             message="这个号的地图目录已生成" if complete else "地图目录未完整生成，请重试")
        cap = capture_ctl.status()
        cap["catalog"] = {"needed": not complete, "started": True, "done": complete,
                          "account": acc, "apk": (doc.get("meta") or {}).get("apk")}
        xg_paths.write_json(xg_paths.CAPTURE_STATUS, cap, private_file=False)
        changed = True
    elif kind == "pull-apk" and ok:
        apk = result.get("apk") or result.get("path")
        if not apk:
            try:
                found = sorted(xg_paths.APK_DIR.glob("*.apk"),
                               key=lambda p: p.stat().st_mtime, reverse=True)
                apk = str(found[0]) if found else ""
            except OSError:
                apk = ""
        if apk:
            cfg = _cfg()
            w = cfg.setdefault("wizard", {"version": WIZARD_VERSION, "steps": {}})
            w.setdefault("apk", {}).update({"path": str(apk), "from": "mumu", "pulled": True})
            xg_paths.save_config(cfg)
            changed = True
    with LOCK:
        w = _wizard()
        w["last_task_id"] = tid
        _save(w)
    return {"changed": changed, "kind": kind, "ok": ok}
