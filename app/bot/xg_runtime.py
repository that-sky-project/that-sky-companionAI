"""新版小光（release-v20 + 地图目录）的状态与换图，给 control_api.py 用。

W1 移植：路径全部走 xg_paths（DATA 下的 control/、logs/、runtime/...），进程查找
全部走 xg_proc（按 argv 精确匹配，替代 pgrep -f），换图用 xg_ctl/start_home_capture
异步执行（Windows 上也能脱离启动）。对外接口与服务器版一致：
region_of / directory_rows / list_maps_payload / refresh / startable_maps /
live / go / stop_all / stable_info / pids / startable_maps。
"""
import json
import re
import time
from pathlib import Path

import xg_account
import xg_paths
import xg_proc

DEFAULT_MANIFEST = xg_paths.ASSET_RELEASE / "asset-catalog-v32c/manifest.json"
BOOTSTRAP_MANIFEST = xg_paths.ASSET_RELEASE / "asset-bootstrap-v32/NightArchive/manifest.json"
OUT = xg_paths.LEASE_DIR
POSE = xg_paths.POSE_STATE
GO = xg_paths.CONTROL / "xg_go.json"
GO_LOG = xg_paths.LOGS / "xg_go.log"
HOME = 3526133726
TARGET_RECORDS = xg_paths.destination_records()

# 进程识别：只认 “python …/bot/long_runner.py” 或 “python …/bot/experimental_ground_entry.py”
RUN_SCRIPTS = ("bot/experimental_ground_entry.py", "bot/long_runner.py")
GO_SCRIPTS = ("launcher/start_home_capture.py", "control/start_home_capture.py")

REGIONS = [("遇境", ("CandleSpace",)), ("晨岛", ("Dawn",)), ("云野", ("Prairie",)), ("雨林", ("Rain",)),
           ("霞谷", ("Sunset",)), ("暮土", ("Dusk",)), ("禁阁", ("Night",)), ("伊甸", ("Storm",)),
           ("重生之路", ("Orbit", "Credits")), ("小镇", ("MainStreet",)), ("云野", ("Day",))]
REGION_ORDER = ["遇境", "晨岛", "云野", "雨林", "霞谷", "暮土", "禁阁", "伊甸", "重生之路", "小镇", "季节与活动", "其他"]


def region_of(scene):
    if scene in ("CandleSpaceEnd", "Credits"):
        return "重生之路"
    if scene == "StormEvent_VoidSpace":
        return "季节与活动"
    for name, prefixes in REGIONS:
        if scene.startswith(prefixes):
            return name
    if scene.startswith(("Season", "Event", "AR_", "Void")):
        return "季节与活动"
    return "其他"


def _json(path):
    return xg_paths.read_json(path, None)


def _records():
    """{map_id: 中文名}（带路用的目的地表；用户机上生成，没有就是空表）。"""
    data = _json(TARGET_RECORDS) or {}
    out = {}
    for r in data.get("records", []):
        if r.get("kind") == "scene" and r.get("sceneHash"):
            out[int(r["sceneHash"])] = (r.get("displayName") or r.get("sceneName"), r.get("sceneName"))
    return out


def _records_path():
    return Path(TARGET_RECORDS)


def directory_rows(root=None, maps=None):
    """list_maps 的名称表：地图行 + 图内命名落点行。"""
    maps = maps or []
    data = _json(_records_path()) or {}
    recs = data.get("records", [])
    by_scene = {r.get("sceneName"): r for r in recs if r.get("kind") == "scene"}
    names = {m.get("scene"): m.get("name") for m in maps}
    rows = []
    for m in maps:
        r = by_scene.get(m["scene"], {})
        others = []
        for al in r.get("aliases", []):
            if (isinstance(al, str) and al != m["name"] and not al.isascii()
                    and not re.search(r"[()（）:：]", al) and al not in others):
                others.append(al)
        rows.append({"名称": m["name"], "其它叫法": others[:6], "区域": m["region"], "内部名": m["scene"]})
    for r in recs:
        if r.get("kind") != "landmark" or not r.get("spawnVariant"):
            continue
        scene = r.get("sceneName")
        if scene not in names:
            continue
        label = r.get("displayName") or r.get("spawnVariant")
        others = []
        for al in r.get("aliases", []):
            if (isinstance(al, str) and al != label and not al.isascii()
                    and not re.search(r"[()（）:：]", al) and al not in others):
                others.append(al)
        rows.append({"名称": f"{label}（在{names[scene]}里）", "其它叫法": others[:6],
                     "区域": region_of(scene), "内部名": scene, "落点": label})
    return rows


def list_maps_payload(rows, query=""):
    from destination_resolver_v25 import _key
    q = _key(str(query or ""))
    if q:
        hit = [x for x in rows
               if q in _key(x["名称"] + "".join(x["其它叫法"]) + x["区域"] + x["内部名"])]
        if hit:
            return {"ok": True, "count": len(hit), "maps": hit,
                    "note": "这些是名字里含“%s”的图/落点；要去就发 /带我去+名称" % query}
        return {"ok": True, "count": len(rows), "maps": rows,
                "note": "名字里没有直接含“%s”的图，下面是全部，请按内部名/区域自己判断" % query}
    return {"ok": True, "count": len(rows), "maps": rows}


_maps_cache = {"key": None, "rows": []}
_manifest_cache = {}          # 账号 ID -> (取的时刻, manifest 路径或 None)


def refresh(account_id=None):
    _maps_cache.update(key=None, rows=[])
    if account_id:
        _manifest_cache.pop(account_id, None)
    else:
        _manifest_cache.clear()


def _manifest(account_id):
    """这个号该用的目录 manifest；没有专属目录返回 None。"""
    now = time.monotonic()
    hit = _manifest_cache.get(account_id)
    if hit and now - hit[0] < 60.0:
        return hit[1]
    path = None
    try:
        path = xg_account.manifest_for(account_id)
    except Exception:
        path = None
    if path is None and account_id and account_id == xg_account.DEFAULT_ACCOUNT and DEFAULT_MANIFEST.is_file():
        path = DEFAULT_MANIFEST
    _manifest_cache[account_id] = (now, path)
    return path


def _mine(cap, uuid):
    cap = cap or {}
    return bool(uuid) and (uuid in (cap.get("by_account") or {}) or cap.get("account") == uuid)


def startable_maps(account_id=None):
    """[{id, name, scene, region, kind}]，按区域排序。"""
    aid = account_id or xg_account.active_id()
    uuid = (xg_account.account_by_id(aid) or {}).get("user_uuid")
    man_path = _manifest(aid)
    prof_path = xg_paths.PROFILES_JSON
    boot_path = BOOTSTRAP_MANIFEST
    dest_path = _records_path()
    key = (aid, man_path,
           man_path.stat().st_mtime_ns if man_path is not None and man_path.exists() else 0,
           tuple(p.stat().st_mtime_ns if p.exists() else 0 for p in (prof_path, boot_path, dest_path)))
    if key == _maps_cache["key"]:
        return _maps_cache["rows"]
    names = _records()
    found = {}
    if man_path is not None:
        for mid, e in ((_json(man_path) or {}).get("entries") or {}).items():
            found[int(mid)] = (e.get("scene"), "catalog")
    for mid, e in ((_json(prof_path) or {}).get("maps") or {}).items():
        if not _mine((e or {}).get("current_capture"), uuid):
            continue
        found.setdefault(int(mid), (e.get("scene"), "capture"))
    if man_path == DEFAULT_MANIFEST:       # 禁阁档案只在默认目录这套里补
        boot = _json(boot_path) or {}
        for e in (boot.get("entries") or {}).values() if isinstance(boot.get("entries"), dict) else []:
            if e.get("scene"):
                found.setdefault(int(e.get("map_id") or 0) or _fnv(e["scene"]), (e["scene"], "bootstrap"))
        if not any(s == "NightArchive" for s, _ in found.values()) and boot_path.exists():
            found.setdefault(_fnv("NightArchive"), ("NightArchive", "bootstrap"))
    rows = []
    for mid, (scene, kind) in found.items():
        scene = scene or (names.get(mid) or (None, ""))[1] or str(mid)
        name = (names.get(mid) or (scene, scene))[0]
        rows.append({"id": mid, "name": name, "scene": scene, "region": region_of(scene), "kind": kind})
    rows.sort(key=lambda r: (REGION_ORDER.index(r["region"]), r["id"] != HOME, r["name"]))
    _maps_cache.update(key=key, rows=rows)
    return rows


def _fnv(s):
    h = 0x811C9DC5
    for b in s.encode():
        h = ((h ^ b) * 0x01000193) & 0xFFFFFFFF
    return h


def pids(script=None):
    """兼容旧接口：传脚本名（默认两个运行入口）返回 pid 列表。"""
    scripts = (script,) if script else RUN_SCRIPTS
    return [r["pid"] for r in xg_proc.find_any_script(scripts)]


def _lease_for(runner_pid):
    """当前窗口的 lease（找 runner pid 对得上的那个）。"""
    try:
        files = sorted(OUT.glob("online-*.lease.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:6]
    except OSError:
        return None
    for f in files:
        d = _json(f) or {}
        if runner_pid is None or (d.get("runner") or {}).get("pid") == runner_pid:
            return d
    return None


def going():
    """正在换图：{map, name, since}；没有则 None。"""
    g = _json(GO)
    if not g or not xg_proc.find_any_script(GO_SCRIPTS):
        return None
    return g


def startup_failure():
    """Read only this account's latest attempt, then classify its last traceback."""
    attempt = _json(GO) or {}
    if attempt.get("account") != xg_account.active_id() or not attempt.get("started_at"):
        return None

    def tail(path, offset, limit):
        with path.open("rb") as stream:
            stream.seek(0, 2)
            end = stream.tell()
            # A log may have been rotated/truncated since the attempt was submitted.
            start = int(offset or 0)
            if start > end:
                start = 0
            stream.seek(max(start, end - limit))
            return stream.read(limit).decode("utf-8", errors="replace")

    try:
        text = tail(GO_LOG, attempt.get("log_offset"), 16384)
    except (OSError, ValueError, TypeError):
        return None
    failed = "Traceback (most recent call last)" in text
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("step") == "lease" and row.get("status") in ("failed", "stopped", None):
            failed = True
    if not failed:
        return None

    runner_log = OUT / "candidate-runner.out"
    try:
        if runner_log.stat().st_mtime >= float(attempt["started_at"]):
            runner_text = tail(runner_log, attempt.get("runner_log_offset"), 16384)
            if runner_text.strip():
                text = runner_text
    except (OSError, ValueError, TypeError):
        pass
    # Legacy attempts have no byte offset. The final traceback wins, never an
    # earlier failure that merely shares the same append-only log file.
    marker = "Traceback (most recent call last):"
    if marker in text:
        text = text.rsplit(marker, 1)[1]
    code = "startup_failed"
    message = "上线未成功，游戏连接进程已退出。请查看本次启动的诊断日志。"
    if "仅支持当前 0.16.7 / 421589 模式" in text:
        code = "protocol_version_mismatch"
        message = "上线失败：运行协议与选择的游戏版本不一致。请更新匹配版本的客户端；反复续期或重建地图不会修复这个错误。"
    elif "当前已验证入口为网易安卓渠道" in text or "当前连接入口仅支持网易国服 Android / iOS 账号" in text:
        code = "unsupported_account_platform"
        message = "上线失败：账号渠道或登录平台未被当前连接器支持。请核对账号平台；这不是地图生成或游戏版本错误。"
    elif "401 Unauthorized" in text or "session_expired" in text:
        code = "login_expired"
        message = "上线失败：游戏登录状态已失效。请先退出 MuMu 中的同一账号，再到账号页点“续期登录”；若仍失败，请重新登录。"
    elif "map_capture_not_recorded_for_account" in text or "map_capture_not_configured" in text:
        code = "account_capture_missing"
        message = "上线失败：本账号的地图开局数据尚未就绪，请检查遇境采集和本账号地图目录的校验结果。"
    return {"code": code, "message": message, "attempt": attempt["started_at"]}


def live(owner):
    runner = pids()
    st = _json(POSE) or {}
    room = st.get("room") or {}
    age = time.time() - float(st.get("generated_at") or 0)
    fresh = bool(runner) and age < 20 and room.get("connected")
    slots = room.get("slots") or {}
    map_id = room.get("map_id") if runner else None
    pending = room.get("pending_map_id")
    lease = _lease_for(runner[0]) if runner else None
    stop_at = (lease or {}).get("stop_at_unix")
    names = {m["id"]: m["name"] for m in startable_maps()}
    rec = _records()

    def name_of(mid):
        return names.get(mid) or (rec.get(mid) or ("未知地图",))[0] if mid else "—"

    pending_go = going()
    error = startup_failure() if not runner and not pending_go else None
    return {
        "startupError": error,
        "runner": bool(runner), "online": bool(fresh), "map": map_id, "mapName": name_of(map_id),
        "changing": bool(runner and pending and pending != map_id), "pendingMap": pending, "pendingName": name_of(pending),
        "players": len(slots) if runner else None, "ownerHere": bool(owner) and owner in slots.values(),
        "pose": st.get("pose"), "stateAge": round(age, 1) if st else None,
        "since": (lease or {}).get("launch_utc") and time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime((lease or {}).get("launch_unix") or 0)),
        "windowEnd": time.strftime("%m-%d %H:%M", time.localtime(stop_at)) if stop_at else None,
        "going": pending_go,
    }


def go(map_id):
    """让小光去 map_id（后台执行，立即返回）。"""
    maps = {m["id"]: m for m in startable_maps()}
    if int(map_id) not in maps:
        raise ValueError("这张图现在还去不了")
    if going():
        raise RuntimeError("上一次换图还在进行中，稍等一下")
    from xg_auth_client import Client, AuthError
    try:
        Client().session(xg_account.active_id())
    except AuthError as exc:
        raise RuntimeError(exc.code) from None
    xg_paths.write_json(GO, {"map": int(map_id), "name": maps[int(map_id)]["name"],
                             "since": time.strftime("%H:%M:%S"), "started_at": time.time(),
                             "account": xg_account.active_id(),
                             "log_offset": GO_LOG.stat().st_size if GO_LOG.exists() else 0,
                             "runner_log_offset": (OUT / "candidate-runner.out").stat().st_size
                             if (OUT / "candidate-runner.out").exists() else 0})
    GO_LOG.parent.mkdir(parents=True, exist_ok=True)
    with GO_LOG.open("ab") as log:
        log.write(f"\n== {time.strftime('%F %T')} go {map_id} {maps[int(map_id)]['scene']}\n".encode())
    xg_proc.spawn_detached([xg_paths.python_exe(), "-B", str(xg_paths.LAUNCHER / "start_home_capture.py"),
                            "auto", str(map_id)],
                           cwd=xg_paths.APP, stdout=GO_LOG, stderr=GO_LOG)
    return maps[int(map_id)]


def stop_all():
    """停掉监督器和 runner（后台换图时不允许）。"""
    if going():
        raise RuntimeError("正在换图，等它完成再下线")
    ids = [r["pid"] for r in xg_proc.find_any_script(
        ("launcher/supervisor_sixhour.py", "candidate_supervisor_sixhour_v22.py"))] + pids()
    for p in ids:
        xg_proc.terminate(p, grace=3.0)
    return bool(ids)


# ---------- 稳定版信息 ----------
STABLE = xg_paths.STABLE_DATA
_stable_cache = {"at": 0.0, "value": None}


def stable_info(max_age=300):
    now = time.time()
    if _stable_cache["value"] is not None and now - _stable_cache["at"] < max_age:
        return _stable_cache["value"]
    import hashlib
    try:
        label = (STABLE / "CURRENT").read_text().strip()
        sums = (STABLE / label / "SHA256SUMS.release").read_text().splitlines()
    except OSError:
        value = {"label": None, "matches": None, "changed": []}
        _stable_cache.update(at=now, value=value)
        return value
    live_of = {"./release-v20-hop-trial/": xg_paths.APP / "runtime/terrain-experimental-20261104/release-v20-hop-trial",
               "./root-bot/": xg_paths.BOT_CODE, "./control/": xg_paths.CONTROL,
               "./runtime-private/": xg_paths.RUNTIME_PRIVATE}
    changed = []
    for line in sums:
        digest, _, rel = line.partition("  ")
        if "/seats_" in rel or "/walk_" in rel:
            continue
        live = next((base / rel[len(pre):] for pre, base in live_of.items() if rel.startswith(pre)), None)
        if live is None:
            continue
        try:
            if hashlib.sha256(live.read_bytes()).hexdigest() != digest:
                changed.append(rel)
        except OSError:
            changed.append(rel)
    value = {"label": label, "matches": not changed, "changed": changed[:20], "changedCount": len(changed)}
    _stable_cache.update(at=now, value=value)
    return value
