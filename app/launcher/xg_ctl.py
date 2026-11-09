# -*- coding: utf-8 -*-
"""小光启停控制（替代 companion.sh / restart_home.sh / control_api.sh /
/tmp/xg_restart_chat.sh）。

子命令：
  start [--map ID] [--hours H] [--join UUID] [--wait SEC]
  stop
  status
  restart [--map ID]
  go <map_id>                 停当前窗口，按当前号的目录 manifest 在目标图重新开局
  restart-chat                只重启聊天进程

服务器上的 .sh 可以保留成薄壳：
  companion.sh   -> python launcher/xg_ctl.py start/stop/status/restart-chat
  control_api.sh -> python launcher/xg_ctl.py api-start/api-stop/api-status（见 launcher.py --stop）
  restart_home.sh-> python launcher/xg_ctl.py go "$1"

Linux 上行为与旧 bash 一致：runner 用 start_new_session 脱离，日志追加到
DATA/logs/long_runner.out；Windows 用 CREATE_NEW_PROCESS_GROUP|DETACHED_PROCESS，
不弹窗。进程查找一律按 argv 精确匹配（xg_proc），不会误杀 ssh/bash -c。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
# 直接命令行运行时也能找到正确的代码/数据根（启动器会显式设好同名环境变量）
os.environ.setdefault("SKY_AI_ROOT", str(APP))
if not os.environ.get("XG_DATA_DIR"):
    if os.name == "nt":
        _base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
        os.environ["XG_DATA_DIR"] = str(Path(_base) / "XiaoGuang" / "data")
    else:
        os.environ["XG_DATA_DIR"] = str(Path.home() / ".local/share/xiaoguang")
sys.path.insert(0, str(APP))
sys.path.insert(0, str(APP / "bot"))

import xg_account          # noqa: E402
import xg_paths            # noqa: E402
import xg_proc             # noqa: E402

RUN_SCRIPTS = ("bot/long_runner.py", "bot/experimental_ground_entry.py")
CHAT_SCRIPT = "bot/ai_companion.py"
SUP_SCRIPTS = ("launcher/supervisor_sixhour.py", "candidate_supervisor_sixhour_v22.py")
DEFAULT_MAP = 3526133726
CHAT_MAP = 817373972


# ------------------------------------------------------------------ 环境
def runner_env(deadline=None, map_id=None, extra=None):
    manifest = None
    try:
        manifest = xg_account.manifest_for()
    except Exception:
        manifest = None
    if manifest is None:
        # 兜底：安装包内全量目录（115 张图）。账号版生成失败时也不至于把
        # ASSET_CATALOG_TRIAL 关掉退回旧扫描路径。
        for _cand in (xg_paths.APP / "asset-catalog-v32c/manifest.json",
                      xg_paths.ASSET_RELEASE / "asset-catalog-v32c/manifest.json"):
            try:
                if _cand.is_file():
                    manifest = _cand
                    break
            except OSError:
                continue
    env = dict(os.environ)
    parts = [str(xg_paths.OVERLAY), str(xg_paths.QFSKY_CODE.parent), str(xg_paths.BOT_CODE),
             str(xg_paths.SRC), str(xg_paths.APP)]
    old = env.get("PYTHONPATH")
    if old:
        parts.append(old)
    env.update({
        "PYTHONPATH": os.pathsep.join(parts),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "SKY_ESCORT_SAME_PEER_V31": "1",
        "QFSKY_ASSET_BOOTSTRAP_TRIAL": "0",
        "QFSKY_ASSET_CATALOG_TRIAL": "1" if manifest is not None else "0",
        "QFSKY_ASSET_CATALOG_MANIFEST": str(manifest or
                                           (xg_paths.ASSET_RELEASE / "asset-catalog-v32c/manifest.json")),
        "QFSKY_ASSET_CATALOG_MANIFEST_SHA256": "",
        "QFLY_DATA_DIR": str(xg_paths.RUNTIME_DATA),
        "QFLY_JOIN_MODE": "current",
        "QFLY_JOIN_CONFIG": str(xg_paths.PROFILES_JSON),
        "QFLY_SINGLE_DEVICE_MODE": "1",
        "QFLY_MPAY_DEVICE_ATTRS_FILE": str(xg_paths.RUNTIME_DATA / "registered-device.local.json"),
        "QFLY_SCHEDULER_ENABLED": "0",
        "QFLY_LOCAL_NETWORK_MODE": "enabled",
        "SKY_AI_ROOT": str(xg_paths.ROOT),
        "XG_DATA_DIR": str(xg_paths.DATA),
    })
    if manifest is not None:
        try:
            env["QFSKY_ASSET_CATALOG_MANIFEST_SHA256"] = hashlib.sha256(manifest.read_bytes()).hexdigest()
        except OSError:
            pass
    if deadline:
        env["SKY_AERIAL_TRIAL_STOP_AT_UNIX"] = str(int(deadline))
    env.pop("CLAUDEWORK_JOIN_PLAYERS", None)
    if extra:
        env.update({k: str(v) for k, v in extra.items() if v is not None})
    return env


def _account(explicit=None):
    acc = explicit or xg_account.active_id()
    if not acc:
        raise SystemExit("还没有选账号：先在后台加账号/选号（DATA/control/active_account），"
                         "或用 XG_DEFAULT_ACCOUNT 指定")
    return acc


def runner_argv(account, map_id, hours):
    return [xg_paths.python_exe(), "-B", str(xg_paths.RUNTIME_ENTRY),
            "--pilot-config", str(xg_paths.PILOT_CONFIG), "--",
            "--account", account, "--map", str(int(map_id)), "--hours", str(hours),
            "--max-retries", "5", "--control", str(xg_paths.POSE_CONTROL), "--accept-offers"]


def chat_argv(account, map_id=None):
    return [xg_paths.python_exe(), "-B", str(xg_paths.BOT_CODE / "ai_companion.py"),
            "--account", account, "--map", str(int(map_id or CHAT_MAP))]


# ------------------------------------------------------------------ 探测
def runner_rows():
    return xg_proc.find_any_script(RUN_SCRIPTS)


def chat_rows():
    return xg_proc.find_script(CHAT_SCRIPT)


def append_jsonl(path, **row):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), **row}, ensure_ascii=False) + "\n")


def _phase_observing(path):
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-400:]
    except OSError:
        return False
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("phase") == "observing" or row.get("event") == "observing":
            return True
    return False


# ------------------------------------------------------------------ 子命令
def start(map_id=None, hours=3.0, join=None, wait=60.0, account=None, capture_only=None):
    xg_paths.ensure_dirs()
    acc = _account(account)
    map_id = int(map_id or DEFAULT_MAP)
    rows = runner_rows()
    if rows:
        return {"ok": False, "message": "长连接已在运行", "pid": rows[0]["pid"]}
    deadline = int(time.time() + float(hours) * 3600)
    xg_paths.write_json(xg_paths.CONTROL / "escort_travel.enabled.json",
                        {"enabled": True, "session_deadline": deadline, "version": 1})
    try:
        (xg_paths.CONTROL / "pose.json").unlink()
    except OSError:
        pass
    log = xg_paths.LOGS / "long_runner.jsonl"
    if log.exists():
        try:
            log.rename(log.with_name("long_runner." + time.strftime("%m%d-%H%M") + ".jsonl"))
        except OSError:
            pass
    extra = {"CLAUDEWORK_CAPTURE": str(xg_paths.CAPTURES / ("gamemsg-" + time.strftime("%m%d-%H%M") + ".jsonl")),
             "CLAUDEWORK_CAPTURE_ONLY": capture_only or "gamemsg"}
    if join:
        extra["CLAUDEWORK_JOIN_PLAYERS"] = join
    env = runner_env(deadline, map_id, extra)
    proc, ident = xg_proc.spawn_detached(runner_argv(acc, map_id, hours), cwd=xg_paths.APP,
                                         stdout=xg_paths.LOGS / "long_runner.out",
                                         stderr=xg_paths.LOGS / "long_runner.out", env=env)
    out = {"ok": True, "pid": proc.pid, "identity": ident, "account": acc, "map": map_id}
    if wait:
        end = time.monotonic() + float(wait)
        while time.monotonic() < end:
            if proc.poll() is not None:
                out.update(ok=False, message="长连接退出", returncode=proc.returncode)
                return out
            if _phase_observing(log):
                break
            time.sleep(0.5)
        else:
            out.update(ok=False, message="长连接未进入观察阶段（看 logs/long_runner.out）")
            return out
    if not chat_rows():
        time.sleep(3)                       # 入服会踢掉先连的 WS
        cproc, cident = xg_proc.spawn_detached(chat_argv(acc, CHAT_MAP), cwd=xg_paths.APP,
                                               stdout=xg_paths.LOGS / "companion.out",
                                               stderr=xg_paths.LOGS / "companion.out", env=env)
        out["chat"] = {"pid": cproc.pid, "identity": cident}
    append_jsonl(xg_paths.LOGS / "xg_ctl.jsonl", event="start", map=map_id, account=acc, pid=proc.pid)
    return out


def stop(grace=5.0):
    ids = {}
    for rows, name in ((chat_rows(), "chat"), (runner_rows(), "runner"),
                       (xg_proc.find_any_script(SUP_SCRIPTS), "supervisor")):
        ids[name] = [r["pid"] for r in rows]
        for r in rows:
            xg_proc.terminate(r["pid"], grace=grace)
    append_jsonl(xg_paths.LOGS / "xg_ctl.jsonl", event="stop", **ids)
    return {"ok": True, "stopped": ids}


def status():
    runners, chats = runner_rows(), chat_rows()
    map_id = None
    try:
        map_id = int((xg_paths.CONTROL / "current_map").read_text().strip())
    except (OSError, ValueError):
        pass
    if map_id is None and runners:
        map_id = xg_proc.argv_flag(runners[0]["argv"], "--map")
        try:
            map_id = int(map_id)
        except (TypeError, ValueError):
            map_id = None
    return {"ok": True,
            "runner": [{"pid": r["pid"], "create_time": r["create_time"], "argv": r["argv"]} for r in runners],
            "chat": [{"pid": r["pid"], "create_time": r["create_time"]} for r in chats],
            "supervisor": [r["pid"] for r in xg_proc.find_any_script(SUP_SCRIPTS)],
            "map": map_id, "observing": _phase_observing(xg_paths.LOGS / "long_runner.jsonl"),
            "account": xg_account.active_id(), "data": str(xg_paths.DATA)}


def restart(map_id=None, hours=3.0):
    stop()
    time.sleep(1)
    return start(map_id, hours=hours)


def restart_chat(account=None):
    acc = _account(account)
    for r in chat_rows():
        xg_proc.terminate(r["pid"], grace=3)
    time.sleep(1)
    env = runner_env()
    proc, ident = xg_proc.spawn_detached(chat_argv(acc, CHAT_MAP), cwd=xg_paths.APP,
                                         stdout=xg_paths.LOGS / "companion.out",
                                         stderr=xg_paths.LOGS / "companion.out", env=env)
    return {"ok": True, "chat": {"pid": proc.pid, "identity": ident}}


def go(map_id, wait=0.0):
    """restart_home.sh：停当前窗口，按号的 manifest 在目标图重新开局。"""
    stop()
    log = xg_paths.LOGS / "xg_go.log"
    proc, ident = xg_proc.spawn_detached(
        [xg_paths.python_exe(), "-B", str(xg_paths.LAUNCHER / "start_home_capture.py"), "auto", str(int(map_id))],
        cwd=xg_paths.APP, stdout=log, stderr=log)
    return {"ok": True, "pid": proc.pid, "identity": ident, "map": int(map_id)}


def main(argv=None):
    ap = argparse.ArgumentParser(description="小光启停")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("start")
    p.add_argument("--map", type=int)
    p.add_argument("--hours", type=float, default=3.0)
    p.add_argument("--join")
    p.add_argument("--wait", type=float, default=60.0)
    p.add_argument("--account")
    sub.add_parser("stop")
    sub.add_parser("status")
    p = sub.add_parser("restart")
    p.add_argument("--map", type=int)
    p.add_argument("--hours", type=float, default=3.0)
    p = sub.add_parser("go")
    p.add_argument("map", type=int)
    p = sub.add_parser("restart-chat")
    p.add_argument("--account")
    a = ap.parse_args(argv)
    if a.cmd == "start":
        out = start(a.map, a.hours, a.join, a.wait, a.account)
    elif a.cmd == "stop":
        out = stop()
    elif a.cmd == "status":
        out = status()
    elif a.cmd == "restart":
        out = restart(a.map, a.hours)
    elif a.cmd == "go":
        out = go(a.map)
    else:
        out = restart_chat(a.account)
    print(json.dumps(out, ensure_ascii=False), flush=True)
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
