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
                doc["message"] = "录图进程已经不在了（可能被关掉或出错退出）。"
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
    proc, ident = xg_proc.spawn_detached(
        [xg_paths.python_exe(), "-B", "-m", "capture.worker"],
        cwd=xg_paths.APP, stdout=xg_paths.LOGS / "capture.out",
        stderr=xg_paths.LOGS / "capture.out", env=env)
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
        failed = dict(doc or {})
        failed.update(state="error", message="录图进程没起来", last_error="录图进程没起来",
                      last_error_code="worker_start",
                      last_error_detail=error_detail.describe(
                          None, code="worker_start",
                          text=detail or "capture.worker exited before writing status"))
        try:
            xg_paths.write_json(xg_paths.CAPTURE_STATUS, failed, private_file=False)
        except OSError:
            pass
        return {"ok": False, "message": "录图进程没起来", "detail": detail, "pid": proc.pid,
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
