# -*- coding: utf-8 -*-
"""录图进程的启停与状态（给 control_api / 向导用）。"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
for _p in (str(APP), str(APP / "bot")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import xg_paths   # noqa: E402
import xg_proc    # noqa: E402
import error_detail  # noqa: E402
import install_support  # noqa: E402


def _status_path():
    return xg_paths.CAPTURE_STATUS


def status():
    doc = xg_paths.read_json(_status_path(), {}) or {}
    if not isinstance(doc, dict):
        doc = {}
    pid = doc.get("worker_pid")
    if pid:
        ident = xg_proc.identity(pid)
        argv = xg_proc.argv_of(pid) if ident else []
        if not ident or not any("capture.worker" in str(a) or "capture\\worker.py" in str(a)
                                or "capture/worker.py" in str(a) for a in argv):
            doc["worker_pid"] = None
            if doc.get("state") not in (None, "stopped"):
                doc["state"] = "stopped"
                if not doc.get("last_error_code"):
                    doc["message"] = "录图进程已退出。请检查本机日志或点“开始录图”重试。"
    if not doc:
        doc = {"state": "idle", "message": "还没开始录图。", "worker_pid": None,
               "recorded": [], "catalog": {"needed": False}}
    return doc


def running():
    doc = status()
    return bool(doc.get("worker_pid")) and doc.get("state") not in ("stopped", "idle")


def start():
    if running():
        return {"ok": False, "message": "录图已经在跑了", "status": status()}
    xg_paths.ensure_dirs()
    try:
        xg_paths.CAPTURE_STOP.unlink()
    except OSError:
        pass
    env = dict(os.environ)
    parts = [str(xg_paths.APP)]
    old = env.get("PYTHONPATH")
    if old:
        parts.append(old)
    env.update({"PYTHONPATH": os.pathsep.join(parts), "PYTHONUTF8": "1",
                "PYTHONIOENCODING": "utf-8", "SKY_AI_ROOT": str(xg_paths.ROOT),
                "XG_DATA_DIR": str(xg_paths.DATA)})
    try:
        proc, ident = xg_proc.spawn_detached(
            [xg_paths.python_exe(), "-B", "-m", "capture.worker"],
            cwd=xg_paths.APP, stdout=xg_paths.LOGS / "capture.out",
            stderr=xg_paths.LOGS / "capture.out", env=env)
    except (OSError, MemoryError) as exc:
        issue = install_support.problem(exc, default="本机录图工具未启动。请核对随包 Python 和安全软件拦截记录，再重试。")
        return {"ok": False, **issue, "error_detail": error_detail.describe(exc, code=issue["code"])}
    # 给 worker 一点时间写第一份状态
    end = time.monotonic() + 5
    doc = {}
    while time.monotonic() < end:
        doc = status()
        if doc.get("worker_pid"):
            break
        if proc.poll() is not None:
            break
        time.sleep(0.15)
    if not doc.get("worker_pid"):
        detail = ""
        try:
            detail = (xg_paths.LOGS / "capture.out").read_text(encoding="utf-8", errors="replace")[-400:]
        except OSError:
            pass
        issue = install_support.problem(detail)
        code = (issue or {}).get("code", "worker_start")
        message = (issue or {}).get("message", "录图进程未启动。请确认完整解压，查看本机 capture.out 后重试。")
        failed = dict(doc or {})
        failed.update(state="error", message=message, last_error=message,
                      last_error_code=code,
                      last_error_detail=error_detail.describe(
                          None, code=code,
                          text=detail or "capture.worker exited before writing status"))
        try:
            xg_paths.write_json(xg_paths.CAPTURE_STATUS, failed, private_file=False)
        except OSError:
            pass
        return {"ok": False, "message": message, "code": code, "pid": proc.pid,
                "status": failed}
    return {"ok": True, "pid": proc.pid, "identity": ident, "status": doc}


def stop(grace=4.0):
    doc = status()
    try:
        xg_paths.CAPTURE_STOP.parent.mkdir(parents=True, exist_ok=True)
        xg_paths.CAPTURE_STOP.write_text("stop\n", encoding="utf-8")
    except OSError:
        pass
    pid = doc.get("worker_pid")
    if pid:
        end = time.monotonic() + max(1.0, grace)
        while time.monotonic() < end and xg_proc.identity(pid):
            time.sleep(0.2)
        if xg_proc.identity(pid):
            xg_proc.terminate(pid, grace=3)
    time.sleep(0.3)
    try:
        xg_paths.CAPTURE_STOP.unlink()
    except OSError:
        pass
    doc = status()
    doc.update(state="stopped", message="录图已停止。", worker_pid=None)
    xg_paths.write_json(xg_paths.CAPTURE_STATUS, doc, private_file=False)
    return {"ok": True, "status": doc}


def clear():
    try:
        xg_paths.CAPTURE_STATUS.unlink()
    except OSError:
        pass
    return {"ok": True}
