# -*- coding: utf-8 -*-
"""候选长连接监督器（W1 移植版，原 candidate_supervisor_sixhour_v22.py）。

只负责新开出来的这一局游戏会话：拉起 runner、等到房间真的连上（control/pose.state.json
里 connected + map_id 对上 + 时间新鲜）才开始计 6 小时窗口，到点/被停/runner
退出就把这一组进程全部收掉，并把状态写进 lease JSON。

移植点
======
* /proc/<pid>/stat、killpg、setsid → xg_proc（pid+创建时间身份、进程组/子进程树）。
* fcntl.flock → xg_lock.FileLock（msvcrt/fcntl 二选一）。
* 路径 → xg_paths：lease 在 DATA/runtime/...，状态文件 DATA/control/pose.state.json，
  cwd 必须是代码根 xg_paths.ROOT。
* 额外支持 DATA/control/supervisor.stop 文件：Windows 上 terminate() 没有优雅信号，
  启动器/后台写这个文件让它自己收尾。

私有 argv/env 仍从 stdin 读，不落盘、不打印。
"""
from __future__ import annotations

import argparse
import datetime
import json
import math
import os
import signal
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

import xg_lock       # noqa: E402
import xg_paths      # noqa: E402
import xg_proc       # noqa: E402

import psutil        # noqa: E402

ROOT = xg_paths.ROOT
RUNNER_NAMES = frozenset(("long_runner.py", "experimental_ground_entry.py"))
LOCK = xg_paths.CONTROL / ".stable-observe-start.lock"
STOP_FILE = xg_paths.CONTROL / "supervisor.stop"


def need(value, code):
    if not value:
        raise RuntimeError(code)


def expected_map_from_argv(argv):
    need(isinstance(argv, list) and all(type(arg) is str for arg in argv), "launch_argv_invalid")
    map_indices = [i for i, arg in enumerate(argv) if arg == "--map"]
    need(not any(arg.startswith("--map=") for arg in argv), "launch_map_equals_form")
    need(len(map_indices) == 1 and map_indices[0] + 1 < len(argv), "launch_map_missing_or_duplicate")
    text = argv[map_indices[0] + 1]
    need(text.isascii() and text.isdecimal(), "launch_map_not_decimal")
    mid = int(text)
    need(0 < mid <= 0xFFFFFFFF, "launch_map_out_of_range")
    return mid


def identity(pid):
    return xg_proc.identity(pid)


def same(row):
    return xg_proc.same_identity(row)


def members(pid):
    """这一组里还活着的进程（POSIX 按进程组/会话 pid；Windows 按子进程树）。"""
    out = []
    if os.name == "nt":
        try:
            proc = psutil.Process(int(pid))
            for child in proc.children(recursive=True):
                ident = xg_proc.identity(child.pid)
                if ident and ident.get("state") != "Z":
                    out.append(ident)
        except (psutil.Error, OSError, ValueError):
            return []
        return out
    try:
        for p in Path("/proc").iterdir():
            if not p.name.isdigit():
                continue
            ident = xg_proc.identity(int(p.name))
            if ident and ident.get("pgid") == pid and ident.get("session") == pid and ident.get("state") != "Z":
                out.append(ident)
    except OSError:
        pass
    return out


def active_games():
    """当前代码根下已经在跑的长连接进程（防止同时开两局）。"""
    return xg_proc.find_any_script(("bot/long_runner.py", "bot/experimental_ground_entry.py"), within=ROOT)


def atomic(path, value):
    temporary = Path(str(path) + ".pending")
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def utc(value):
    return datetime.datetime.fromtimestamp(value, datetime.timezone.utc).isoformat()


def connection_ready(value, modified, launch_wall, now, expected_map):
    if type(expected_map) is not int or not 0 < expected_map <= 0xFFFFFFFF:
        return False
    if not isinstance(value, dict):
        return False
    room = value.get("room")
    if not isinstance(room, dict) or room.get("connected") is not True or type(room.get("map_id")) is not int:
        return False
    if room.get("map_id") != expected_map:
        return False
    generated = room.get("generated_at")
    return all(type(x) in (int, float) and math.isfinite(x) for x in (modified, launch_wall, now, generated)) and \
        launch_wall <= modified <= now + .5 and launch_wall <= generated <= now + .5 and \
        0 <= now - generated <= 2 and 0 <= now - modified <= 2


def read_ready(path, launch_wall, expected_map):
    try:
        stat = path.stat()
        if not 0 < stat.st_size <= 2 * 1024 * 1024:
            return False
        raw = path.read_bytes()
        return len(raw) <= 2 * 1024 * 1024 and connection_ready(
            json.loads(raw), stat.st_mtime, launch_wall, time.time(), expected_map)
    except (OSError, ValueError, TypeError):
        return False


def _stop_requested():
    return STOP_FILE.exists()


def supervise(payload, lease, duration=21600, grace=5):
    process = child = None
    lock = None
    lease_allowed = False
    stop_at = hard_stop_monotonic = effective_deadline = None
    stop = [False]
    state = dict(schema="experimental-ground-supervisor-resume-v1", mode="experimental_ground",
                 status="initializing", supervisor=identity(os.getpid()), duration_seconds=duration,
                 grace_seconds=grace, terrain_requested=True, terrain_activation_verified=False,
                 new_code_deployed=True, nativeCalibrationRequired=False, source_writes=False)
    for signame in ("SIGTERM", "SIGINT", "SIGBREAK"):
        try:
            signal.signal(getattr(signal, signame), lambda *_: stop.__setitem__(0, True))
        except (OSError, ValueError, AttributeError):
            pass

    def save():
        if not lease_allowed:
            return False
        try:
            atomic(lease, state)
            return True
        except Exception as error:
            state["lease_write_error"] = type(error).__name__
            return False

    def stop_group():
        nonlocal process, child
        if process is None:
            return
        pid = process.pid
        if child is None:
            xg_proc.terminate(pid, grace=grace)
            process.wait(timeout=5)
            state["emergency_unverified_child_cleanup"] = True
            return
        leader = identity(child["pid"])
        need(leader is None or leader.get("create_time") == child.get("create_time"), "owned_pid_reused")
        # W6：官方 embeddable 的 python.exe 是真身，不像 venv 的转发壳会再起一个
        # 子进程；Windows 上 members(pid) 因此可能为空。runner 本身仍必须停，
        # 否则 supervisor 退出后长连接会留在后台。
        state["term_sent_utc"] = utc(time.time())
        save()
        xg_proc.terminate(pid, grace=grace)
        state["kill_sent_utc"] = utc(time.time())
        save()
        try:
            process.wait(timeout=5)
        except Exception:
            pass
        state["remaining_live_group_members"] = len(members(pid))
        need(state["remaining_live_group_members"] == 0, "owned_group_still_live")

    failed = False
    try:
        need(duration == 21600 and grace == 5, "lease_duration_invalid")
        need(Path(lease).is_relative_to(xg_paths.RUNTIME) and Path(lease).name.endswith(".lease.json")
             and not Path(lease).exists(), "lease_path")
        lease_allowed = True
        stop_at = payload.get("stop_at_unix")
        if stop_at is not None:
            need(type(stop_at) in (int, float) and math.isfinite(stop_at), "stop_at_unix_invalid")
            anchor_wall, anchor_monotonic = time.time(), time.monotonic()
            state.update(stop_at_unix=stop_at, stop_at_utc=utc(stop_at), original_window_preserved=True)
            if stop_at <= anchor_wall:
                state["stop_reason"] = "expired_before_start"
                raise RuntimeError("original_deadline_expired")
            hard_stop_monotonic = anchor_monotonic + (stop_at - anchor_wall)
            state["hard_stop_monotonic"] = hard_stop_monotonic
        else:
            state["original_window_preserved"] = False
        need(isinstance(payload.get("argv"), list) and 0 < len(payload["argv"]) <= 256
             and all(type(x) is str for x in payload["argv"]), "private_argv_invalid")
        need(any(Path(arg).name == "experimental_ground_entry.py" for arg in payload["argv"]),
             "not_candidate_entry")
        expected_map = expected_map_from_argv(payload["argv"])
        need(isinstance(payload.get("environment"), dict)
             and all(type(k) is str and type(v) is str for k, v in payload["environment"].items()),
             "private_environment_invalid")
        need(Path(payload["cwd"]).resolve() == ROOT, "private_cwd_invalid")
        need(Path(payload["state_file"]) == xg_paths.POSE_STATE, "ready_state_path")
        chat = payload["chat_identity"]
        need(isinstance(chat, dict) and same(chat), "chat_identity_changed")

        LOCK.parent.mkdir(parents=True, exist_ok=True)
        lock = xg_lock.FileLock(LOCK)
        lock.acquire()
        need(not active_games(), "another_game_process_present")
        need(save(), "initial_lease_write_failed")
        Path(lease).parent.mkdir(parents=True, exist_ok=True)
        runner_out = Path(lease).parent / "candidate-runner.out"
        logf = open(runner_out, "ab")
        launch_start, launch_wall = time.monotonic(), time.time()
        try:
            if hard_stop_monotonic is not None and (launch_start >= hard_stop_monotonic or launch_wall >= stop_at):
                state["stop_reason"] = "expired_before_start"
                raise RuntimeError("original_deadline_expired")
            process = xg_proc.spawn_detached(payload["argv"], cwd=payload["cwd"],
                                             stdout=logf, stderr=logf,
                                             env=payload["environment"])[0]
        finally:
            logf.close()
        child = identity(process.pid)
        need(child is not None, "owned_group_identity_invalid")
        need(chat.get("pid") != child.get("pid") and same(chat), "chat_group_or_identity_changed")
        state.update(status="starting", runner=child, chat_identity=chat, launch_unix=launch_wall,
                     launch_utc=utc(launch_wall), startup_timeout_seconds=120,
                     connected_timer_started=False, expected_map_id=expected_map)
        need(save(), "starting_lease_write_failed")
        start = None
        while not stop[0] and process.poll() is None:
            now = time.monotonic()
            if _stop_requested():
                state["stop_reason"] = "external_stop"
                break
            if hard_stop_monotonic is not None and (now >= hard_stop_monotonic or time.time() >= stop_at):
                break
            if start is None:
                if read_ready(xg_paths.POSE_STATE, launch_wall, expected_map):
                    start, wall = now, time.time()
                    effective_deadline = (min(start + duration, hard_stop_monotonic)
                                          if hard_stop_monotonic is not None else start + duration)
                    deadline_wall = min(wall + duration, stop_at) if stop_at is not None else wall + duration
                    state.update(status="running", connected_timer_started=True, start_unix=wall,
                                 start_utc=utc(wall), deadline_unix=deadline_wall,
                                 deadline_utc=utc(deadline_wall), start_monotonic=start,
                                 deadline_monotonic=effective_deadline,
                                 effective_duration_seconds=max(0., effective_deadline - start))
                    need(save(), "running_lease_write_failed")
                elif now >= launch_start + 120:
                    break
            elif now >= effective_deadline:
                break
            next_boundary = effective_deadline if effective_deadline is not None else launch_start + 120
            if hard_stop_monotonic is not None:
                next_boundary = min(next_boundary, hard_stop_monotonic)
            time.sleep(min(.1, max(0., next_boundary - time.monotonic())))
        state.setdefault("stop_reason", (
            "original_deadline" if hard_stop_monotonic is not None and (
                time.monotonic() >= hard_stop_monotonic or time.time() >= stop_at) else
            "deadline" if start is not None and time.monotonic() >= effective_deadline else
            "startup_timeout" if start is None and time.monotonic() >= launch_start + 120 else
            "external_stop" if stop[0] else "runner_exit"))
    except Exception as error:
        failed = True
        state["error_code"] = error.args[0] if isinstance(error, RuntimeError) else type(error).__name__
    finally:
        state["status"] = "stopping"
        save()
        try:
            stop_group()
        except Exception as error:
            failed = True
            state["cleanup_error"] = error.args[0] if isinstance(error, RuntimeError) else type(error).__name__
        state.update(status="failed" if failed else "stopped", stopped_utc=utc(time.time()))
        if process is not None:
            state["runner_returncode"] = process.poll()
        if not save():
            failed = True
        if lock is not None:
            lock.release()
        try:
            if STOP_FILE.exists():
                STOP_FILE.unlink()
        except OSError:
            pass
    return 1 if failed else 0


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--lease", type=Path, required=True)
    parser.add_argument("--duration", type=float, default=21600)
    parser.add_argument("--grace", type=float, default=5)
    a = parser.parse_args(argv)
    raw = sys.stdin.buffer.read(65537)
    if len(raw) > 65536:
        raise SystemExit(2)
    payload = json.loads(raw)
    xg_paths.ensure_dirs()
    return supervise(payload, a.lease.resolve(), a.duration, a.grace)


if __name__ == "__main__":
    raise SystemExit(main())
