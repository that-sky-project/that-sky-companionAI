# -*- coding: utf-8 -*-
"""开一个新的受监督窗口（W1 移植版，原 control/start_home_capture.py）。

按 control/active_account 选号：
  * 第一个参数 auto：按这个号的 account_catalog manifest-for 选目录并算 sha；
  * 传显式 sha：manifest 仍按号选，sha 用传进来的；
  * 这个号没有专属目录（manifest-for 退出码 3）→ QFSKY_ASSET_CATALOG_TRIAL=0。
  * 聊天进程只复用 --account 与当前号一致的；不一致的优雅停掉再起。
  * --dry-run：只打印将要执行的 argv/env 摘要，不启动/不杀任何进程。

与服务器版的差异：不再读 backups/ 里的 launch-template.private.json（那是私人物料），
argv 直接在这里拼；进程操作走 xg_proc，锁走 xg_lock，路径走 xg_paths。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
os.environ.setdefault("SKY_AI_ROOT", str(APP))
if not os.environ.get("XG_DATA_DIR"):
    if os.name == "nt":
        _base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
        os.environ["XG_DATA_DIR"] = str(Path(_base) / "XiaoGuang" / "data")
    else:
        os.environ["XG_DATA_DIR"] = str(Path.home() / ".local/share/xiaoguang")
sys.path.insert(0, str(APP))
sys.path.insert(0, str(APP / "bot"))

import xg_account     # noqa: E402
import xg_exec        # noqa: E402
import xg_paths       # noqa: E402
import xg_proc        # noqa: E402

DEFAULT_MANIFEST = xg_paths.ASSET_RELEASE / "asset-catalog-v32c/manifest.json"
CHAT_SCRIPT = "bot/ai_companion.py"
CHAT_MAP = 817373972
START_MAP_DEFAULT = 3526133726
D2 = float(int(time.time()) + 6 * 3600)


def _account():
    acc = xg_account.active_id()
    if not acc:
        raise SystemExit("没有当前账号：先在后台选号（DATA/control/active_account）")
    return acc


def resolve_manifest(account):
    """(manifest 路径或 None, 说明)。没有专属目录时只有默认号用老目录。"""
    try:
        p = xg_account.manifest_for(account)
    except Exception as exc:
        return None, "account_catalog 不可用：" + str(exc)[:80]
    if p is not None:
        return Path(p), "account_catalog"
    if account and account == xg_account.DEFAULT_ACCOUNT and DEFAULT_MANIFEST.is_file():
        return DEFAULT_MANIFEST, "默认号用默认目录"
    return None, "没有专属目录"


def _mine(row, account):
    return xg_proc.argv_flag(row.get("argv") or [], "--account") == account


def build_run_argv(account, start_map):
    return [xg_paths.python_exe(), "-B", str(xg_paths.RUNTIME_ENTRY),
            "--pilot-config", str(xg_paths.PILOT_CONFIG), "--",
            "--account", str(account), "--map", str(int(start_map)), "--hours", "6.25",
            "--max-retries", "5", "--control", str(xg_paths.POSE_CONTROL), "--accept-offers"]


def build_env(manifest, sha):
    import xg_ctl
    return xg_ctl.runner_env(D2, None, {
        "QFSKY_ASSET_CATALOG_TRIAL": "1" if manifest is not None else "0",
        "QFSKY_ASSET_CATALOG_MANIFEST": str(manifest or DEFAULT_MANIFEST),
        "QFSKY_ASSET_CATALOG_MANIFEST_SHA256": sha or "",
        "CLAUDEWORK_CAPTURE": str(xg_paths.CAPTURES / ("gamemsg-exp-" + time.strftime("%m%d-%H%M") + ".jsonl")),
        "CLAUDEWORK_CAPTURE_ONLY": "gamemsg",
    })


def select_map(start_map, account, manifest):
    if manifest is not None:
        return start_map, False
    uuid = (xg_account.account_by_id(account) or {}).get("user_uuid")
    mine = xg_account.recorded_maps(uuid)
    if not mine or start_map in mine:
        return start_map, False
    return (START_MAP_DEFAULT if START_MAP_DEFAULT in mine else mine[0]), True


def _spawn_supervisor(lease, payload):
    xg_paths.LEASE_DIR.mkdir(parents=True, exist_ok=True)
    log = xg_paths.LEASE_DIR / "supervisor.out"
    logf = open(log, "ab", buffering=0)
    kwargs = dict(stdin=subprocess.PIPE, stdout=logf, stderr=logf, cwd=str(xg_paths.APP), close_fds=True)
    if os.name == "nt":
        kwargs["creationflags"] = xg_proc.CREATE_NEW_PROCESS_GROUP | xg_proc.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    try:
        proc = xg_exec.popen([xg_paths.python_exe(), "-B", str(xg_paths.SUPERVISOR),
                              "--lease", str(lease), "--duration", "21600", "--grace", "5"], **kwargs)
        proc.stdin.write(json.dumps(payload).encode())
        proc.stdin.close()
    finally:
        logf.close()
    return proc


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("sha", nargs="?", default="auto")
    ap.add_argument("map", nargs="?", default="")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--wait", type=float, default=240.0)
    a = ap.parse_args(argv)

    account = _account()
    manifest, why = resolve_manifest(account)
    if manifest is not None:
        sha = hashlib.sha256(manifest.read_bytes()).hexdigest() if a.sha in ("", "auto") else a.sha
    else:
        sha = "" if a.sha in ("", "auto") else a.sha
    start_map = int(a.map) if str(a.map).isdigit() else START_MAP_DEFAULT
    start_map, adjusted = select_map(start_map, account, manifest)
    argv_run = build_run_argv(account, start_map)
    env = build_env(manifest, sha)
    rows = xg_proc.find_script(CHAT_SCRIPT)
    mine = [r for r in rows if _mine(r, account)]
    stale = [r for r in rows if not _mine(r, account)]
    out = {"account": account, "map": start_map, "mapAdjusted": adjusted,
           "catalog": {"manifest": None if manifest is None else str(manifest), "sha256": sha,
                       "trial": "1" if manifest is not None else "0", "why": why},
           "argv": argv_run}
    if a.dry_run:
        out.update(dryRun=True, chat={"reuse": [r["pid"] for r in mine], "stop": [r["pid"] for r in stale]},
                   env={k: env[k] for k in ("QFSKY_ASSET_CATALOG_TRIAL", "QFSKY_ASSET_CATALOG_MANIFEST",
                                            "QFSKY_ASSET_CATALOG_MANIFEST_SHA256", "PYTHONPATH",
                                            "QFLY_DATA_DIR", "QFLY_SINGLE_DEVICE_MODE",
                                            "QFLY_MPAY_DEVICE_ATTRS_FILE")})
        print(json.dumps(out, ensure_ascii=False), flush=True)
        return 0

    xg_paths.ensure_dirs()
    xg_paths.write_json(xg_paths.CONTROL / "escort_travel.enabled.json",
                        {"enabled": True, "session_deadline": D2, "version": 1})
    if stale:
        print(json.dumps({"step": "chat_stopped",
                          "signals": [r["pid"] for r in stale]}, ensure_ascii=False), flush=True)
        for r in stale:
            xg_proc.terminate(r["pid"], grace=5)
    if mine:
        chat_id = mine[0]
        print(json.dumps({"step": "chat_reused", "identity": chat_id}, ensure_ascii=False), flush=True)
    else:
        proc, ident = xg_proc.spawn_detached(
            [xg_paths.python_exe(), "-B", str(xg_paths.BOT_CODE / "ai_companion.py"),
             "--account", account, "--map", str(CHAT_MAP)],
            cwd=xg_paths.APP, stdout=xg_paths.LOGS / "companion.out",
            stderr=xg_paths.LOGS / "companion.out", env=env)
        time.sleep(5)
        chat_id = xg_proc.identity(proc.pid) or ident
        print(json.dumps({"step": "chat_started", "identity": chat_id}, ensure_ascii=False), flush=True)

    lease = xg_paths.LEASE_DIR / ("online-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + ".lease.json")
    xg_paths.LEASE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"stop_at_unix": D2, "argv": argv_run, "environment": env, "cwd": str(xg_paths.ROOT),
               "chat_identity": chat_id, "state_file": str(xg_paths.POSE_STATE)}
    try:
        lease.unlink()
    except OSError:
        pass
    sup = _spawn_supervisor(lease, payload)
    print(json.dumps({"step": "supervisor_started", "pid": sup.pid, "lease": str(lease)},
                     ensure_ascii=False), flush=True)
    state = None
    end = time.monotonic() + max(0.0, a.wait)
    while time.monotonic() < end:
        state = xg_paths.read_json(lease, None)
        if state and state.get("status") in ("running", "failed", "stopped"):
            break
        time.sleep(0.1)
    print(json.dumps({"step": "lease", "status": (state or {}).get("status"),
                      "connected": (state or {}).get("connected_timer_started"),
                      "runner": (state or {}).get("runner"),
                      "error": (state or {}).get("error_code"),
                      "reason": (state or {}).get("stop_reason")}, ensure_ascii=False), flush=True)
    return 0 if (state and state.get("status") == "running") else 1


if __name__ == "__main__":
    raise SystemExit(main())
