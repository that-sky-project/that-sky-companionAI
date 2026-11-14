# -*- coding: utf-8 -*-
"""后台长任务（names / terrain / catalog / pull-apk）的唯一任务槽。

同一时刻只跑一个；任务进程脱离 control_api，关页面不中断；状态和日志都落盘，
control_api 重启后还能接着看。进度按 xg_gamedata 的 JSON 行解析。
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import time
import threading
import copy
from functools import wraps
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
for _p in (str(APP), str(APP / "bot")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import xg_paths   # noqa: E402
import xg_proc    # noqa: E402
import error_detail  # noqa: E402
from capture import gamedata  # noqa: E402

CHAIN = Path(__file__).resolve().parent / "gamedata_chain.py"
LOCK_KINDS = {"data", "props", "catalog", "pull-apk", "single"}
TAIL_BYTES = 4 * 1024 * 1024
_TASK_LOCK = threading.RLock()


def _serialized(fn):
    @wraps(fn)
    def call(*args, **kwargs):
        with _TASK_LOCK:
            return fn(*args, **kwargs)
    return call


def _status_path():
    return xg_paths.WIZARD_TASK


def _tail(path: Path, limit=TAIL_BYTES):
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > limit:
                f.seek(size - limit)
                f.readline()
            raw = f.read()
        return raw.decode("utf-8", "replace")
    except OSError:
        return ""


def _parse(text):
    progress, final, rows = None, None, []
    ok_count = 0
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            doc = json.loads(line)
        except ValueError:
            continue
        if not isinstance(doc, dict):
            continue
        rows.append(doc)
        if "ok" in doc:
            final = doc
            if doc.get("ok"):
                ok_count += 1
        elif any(k in doc for k in ("stage", "done", "total", "eta_s", "msg")):
            progress = doc
    return progress, final, ok_count, rows[-20:]


def _raw_tail(text, limit=1600):
    """日志里最后几行非 JSON 原文（给“详情”排查用）。"""
    lines = [ln for ln in (text or "").splitlines()
             if ln.strip() and not ln.lstrip().startswith("{")]
    return "\n".join(lines[-20:])[-limit:]


def _plan_count(doc):
    try:
        plan = json.loads(Path(doc.get("plan") or "").read_text(encoding="utf-8"))
        return len(plan) if isinstance(plan, list) and plan else None
    except (OSError, ValueError):
        return None


def _alive(doc):
    pid = doc.get("pid")
    if not pid:
        return False
    ident = xg_proc.identity(pid)
    if not ident:
        try:
            return None if xg_proc.psutil.pid_exists(int(pid)) else False
        except (AttributeError, OSError, ValueError, TypeError):
            return None  # AccessDenied/unknown is busy, never a free task slot.
    argv = xg_proc.argv_of(pid)
    try:
        created = float(ident.get("create_time", 0))
        expected = float(doc.get("create_time") or 0)
        if not created > 0 or ident.get("state") == "Z":
            return False
        if expected > 0 and abs(created - expected) > 1e-3:
            return False
        if not argv:
            return None
        norm = lambda value: os.path.normcase(os.path.abspath(str(value)))
        if not any(norm(arg) == norm(CHAIN) for arg in argv):
            return False
        plan_at, kind_at = argv.index("--plan"), argv.index("--kind")
        if norm(argv[plan_at + 1]) != norm(doc.get("plan") or "") or argv[kind_at + 1] != doc.get("kind"):
            return False
        if expected <= 0:
            # Legacy rows lack a PID birth stamp. Require the exact nonce-bound
            # plan plus a creation time close to the persisted launch timestamp;
            # an unrelated/reused PID must never keep the slot busy or be killed.
            if not doc.get("id") or str(doc["id"]) not in Path(doc.get("plan") or "").name:
                return False
            started = float(doc.get("started_epoch") or time.mktime(
                time.strptime(doc.get("started") or "", "%Y-%m-%d %H:%M:%S")))
            if abs(created - started) > 5.0:
                return False
        return True
    except (ValueError, TypeError, IndexError, OverflowError, OSError):
        return False


def _persist_if_current(original, terminal):
    """CAS under the task slot lock: a late reader cannot replace a new task."""
    if original == terminal:
        return True
    fresh = xg_paths.read_json(_status_path(), {}) or {}
    if fresh != original:
        return False
    xg_paths.write_json(_status_path(), terminal, private_file=False)
    return True


@_serialized
def current():
    doc = xg_paths.read_json(_status_path(), {}) or {}
    if not isinstance(doc, dict) or not doc:
        return {"ok": True, "running": False, "task": None}
    original = copy.deepcopy(doc)
    log = Path(doc.get("log") or "")
    progress, final, ok_count, rows = _parse(_tail(log))
    identity_alive = _alive(doc)
    alive = identity_alive is not False
    plan_count = _plan_count(doc)
    # 一条链里每个子命令都会输出自己的 ok 行（names 完成时 terrain 还在跑），
    # 所以“看到 ok”不等于整条链结束：进程真的退出了，而且 ok 行数够 plan 的命令数，才算终态。
    running = bool(doc.get("running")) and alive
    partial = (final is not None and not alive and plan_count
               and ok_count < plan_count and final.get("ok"))
    if final is not None and not alive and original.get("running"):
        running = False
        doc["ok"] = bool(final.get("ok")) and not partial
        doc["result"] = final
        if partial:
            doc["error"] = "任务在子命令之间退出了（只完成了 %d/%d 条命令）" % (ok_count, plan_count)
            doc["error_code"] = "task_partial"
            doc["error_detail"] = error_detail.describe(
                None, code="task_partial", text=_raw_tail(_tail(log)))
        else:
            doc["error"] = None if final.get("ok") else (final.get("error") or final.get("message")
                                                         or "任务失败")
            if final.get("ok"):
                doc["error_code"] = ""
                doc["error_detail"] = None
            else:
                doc["error_code"] = str(final.get("code") or "task_failed")
                doc["error_detail"] = error_detail.describe(
                    None, code=doc["error_code"],
                    text=str(final.get("error") or final.get("message") or "任务失败"))
        doc["finished"] = doc.get("finished") or time.strftime("%Y-%m-%d %H:%M:%S")
        doc["running"] = False
    elif doc.get("running") and not alive:
        doc["running"] = False
        doc["ok"] = False
        doc["error"] = "任务进程已退出"
        doc["error_code"] = "task_process_gone"
        doc["error_detail"] = error_detail.describe(
            None, code="task_process_gone", text=_raw_tail(_tail(log)))
        doc["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
    if original.get("running") and not alive:
        # Persist the terminal transition before adding response-only progress
        # and nested task fields; later polls do not rewrite an unchanged file.
        if not _persist_if_current(original, doc):
            return current()
    if progress:
        for k in ("stage", "done", "total", "eta_s", "msg"):
            if k in progress:
                doc[k] = progress[k]
        doc["progress"] = {k: progress.get(k) for k in ("stage", "done", "total", "eta_s", "msg")}
    doc["running"] = running
    doc["process_state"] = "unknown" if identity_alive is None else "alive" if alive else "exited"
    doc["lines"] = rows
    doc["task"] = {
        "id": doc.get("id"), "kind": doc.get("kind"), "label": doc.get("label"),
        "running": running, "ok": doc.get("ok"), "stage": doc.get("stage"),
        "done": doc.get("done"), "total": doc.get("total"), "eta_s": doc.get("eta_s"),
        "msg": doc.get("msg"), "error": doc.get("error"), "result": doc.get("result"),
        "error_code": doc.get("error_code") or "", "error_detail": doc.get("error_detail"),
        "started": doc.get("started"), "finished": doc.get("finished"),
        "log": doc.get("log"),
        "process_state": doc["process_state"],
    }
    return doc


@_serialized
def start(kind, plan, label="", meta=None):
    if kind not in LOCK_KINDS:
        return {"ok": False, "message": "未知任务类型：" + str(kind)}
    cur = current()
    if cur.get("running"):
        return {"ok": False, "busy": True, "message": "已经有一个后台任务在跑，等它结束再开始",
                "task": cur.get("task")}
    xg_paths.ensure_dirs()
    task_id = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
    plan_path = xg_paths.CONTROL / ("task_" + task_id + ".plan.json")
    log_path = xg_paths.LOGS / ("task_" + task_id + ".jsonl")
    xg_paths.write_json(plan_path, plan, private_file=False)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("", encoding="utf-8")
    env = gamedata.env()
    try:
        proc, ident = xg_proc.spawn_detached(
            [xg_paths.python_exe(), "-B", str(CHAIN), "--kind", kind, "--plan", str(plan_path)],
            cwd=xg_paths.APP, stdout=log_path, stderr=log_path, env=env)
    except Exception as exc:
        return {"ok": False, "message": f"任务起不来：{type(exc).__name__}"}
    doc = {"id": task_id, "kind": kind, "label": label or kind, "pid": proc.pid,
           "create_time": (ident or {}).get("create_time"), "running": True, "ok": None,
           "stage": "starting", "done": 0, "total": None, "eta_s": None,
           "msg": "任务已开始", "started": time.strftime("%Y-%m-%d %H:%M:%S"),
           "started_epoch": time.time(),
           "finished": None, "error": None, "log": str(log_path), "plan": str(plan_path),
           "meta": meta or {}}
    xg_paths.write_json(_status_path(), doc, private_file=False)
    return {"ok": True, "task": current().get("task")}


@_serialized
def stop(grace=5.0):
    doc = current()
    if not doc.get("running"):
        return {"ok": True, "message": "没有在跑的任务", "task": doc.get("task")}
    pid = doc.get("pid")
    if pid:
        if not doc.get("create_time") or _alive(doc) is not True:
            return {"ok": False, "busy": True, "message": "任务进程身份暂时无法确认，请稍后重试", "task": doc.get("task")}
        xg_proc.terminate(pid, grace=grace)
    doc = current()
    doc.update(running=False, ok=False, error="已取消",
               error_code="cancelled",
               error_detail=error_detail.describe(None, code="cancelled", text="用户取消"),
               finished=time.strftime("%Y-%m-%d %H:%M:%S"))
    doc.pop("task", None)
    xg_paths.write_json(_status_path(), doc, private_file=False)
    return {"ok": True, "message": "任务已取消", "task": current().get("task")}


@_serialized
def clear():
    cur = current()
    if cur.get("running"):
        return {"ok": False, "busy": True, "message": "任务仍在运行或身份未确认，不能清空任务槽"}
    try:
        xg_paths.WIZARD_TASK.unlink()
    except OSError:
        pass
    return {"ok": True}


def command_for(kind, **kw):
    """构造常见任务的 plan（给 wizard / 测试共用）。"""
    if kind == "data":
        apk = kw["apk"]
        plan = [["names", "--apk", apk], ["terrain", "--apk", apk]]
        if kw.get("resume"):
            plan[-1].append("--resume")
        plan.append(["props", "--apk", apk, "--output", str(xg_paths.props_dir()), "--optional"])
        return plan
    if kind == "props":
        return [["props", "--apk", kw["apk"], "--output", str(xg_paths.props_dir())]]
    if kind == "catalog":
        return [["catalog", "--apk", kw["apk"], "--account", kw["account"]]]
    if kind == "pull-apk":
        return [["pull-apk", "--adb", kw["adb"], *(["--serial", kw["serial"]]
                                                   if kw.get("serial") else [])]]
    if kind == "single":
        return [list(kw["args"])]
    raise ValueError("unknown kind")
