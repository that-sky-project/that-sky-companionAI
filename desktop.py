"""Windows EXE host for Xiaoguang. The game/login backend is not the UI process."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import quote
from urllib.request import ProxyHandler, Request, build_opener

import psutil


APP_TITLE = "小光 · Companion"
BACKEND_SCRIPT = "control_api.py"
PARENT_PID_ENV = "XG_PARENT_PID"
PARENT_CREATE_ENV = "XG_PARENT_CREATE_TIME"
MUTEX_PREFIX = "Local\\XiaoGuangDesktop-"
STALE_WAIT = 10.0


def bundle_root():
    return Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent


class StaleBackendError(RuntimeError):
    """An old backend is still alive, so starting another one is unsafe."""


def _lifecycle_log(data, event, **fields):
    """Append one JSON line. Never include web tokens or account data."""
    row = {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event}
    row.update(fields)
    try:
        logs = Path(data) / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        with (logs / "desktop-lifecycle.log").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _read_json(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _same_dir(left, right):
    try:
        a = os.path.normcase(os.path.realpath(os.fspath(left)))
        b = os.path.normcase(os.path.realpath(os.fspath(right)))
    except (OSError, ValueError, TypeError):
        return False
    return a == b


def _is_python_name(name):
    n = (name or "").lower()
    return n.startswith(("python", "pythonw", "pypy"))


def _looks_like_backend(argv):
    """Require Python + a control_api.py script argument, not a substring elsewhere."""
    if not isinstance(argv, (list, tuple)) or not argv:
        return False
    try:
        if not _is_python_name(Path(str(argv[0])).name):
            return False
    except (OSError, ValueError):
        return False
    for arg in argv[1:]:
        text = str(arg)
        if text == "-c":
            return False
        try:
            if Path(text).name.lower() == BACKEND_SCRIPT:
                return True
        except (OSError, ValueError):
            continue
    return False


def _process_info(pid, need_env=False):
    try:
        proc = psutil.Process(int(pid))
        with proc.oneshot():
            info = {"pid": int(pid), "create_time": round(float(proc.create_time()), 6),
                    "name": proc.name()}
    except (psutil.Error, ValueError, TypeError, OSError):
        return None
    try:
        info["argv"] = list(proc.cmdline())
    except (psutil.Error, OSError):
        info["argv"] = []
    try:
        info["cwd"] = proc.cwd()
    except (psutil.Error, OSError):
        info["cwd"] = None
    if need_env:
        try:
            info["environ"] = {str(k): str(v) for k, v in proc.environ().items()}
        except (psutil.Error, OSError, ValueError, TypeError):
            info["environ"] = {}
    return info


def _same_identity(record, info):
    if not isinstance(record, dict) or not info:
        return False
    try:
        if int(record.get("pid")) != int(info.get("pid")):
            return False
    except (TypeError, ValueError):
        return False
    if record.get("create_time") is None:
        return None
    try:
        return abs(float(record["create_time"]) - float(info["create_time"])) <= 1e-3
    except (KeyError, TypeError, ValueError):
        return False


def _script_path(argv):
    for arg in (argv or [])[1:]:
        try:
            if Path(str(arg)).name.lower() == BACKEND_SCRIPT:
                return str(arg)
        except (OSError, ValueError):
            continue
    return ""


def _candidate(info, web=None):
    argv = info.get("argv") or []
    script = _script_path(argv)
    install = info.get("cwd")
    if script:
        try:
            install = str(Path(script).resolve().parent.parent)
        except (OSError, ValueError):
            pass
    return {"pid": info["pid"], "create_time": info.get("create_time"), "argv": argv,
            "cwd": info.get("cwd"), "dir": install or "未知目录", "web": web}


def _alive_same(candidate):
    info = _process_info(candidate["pid"])
    if info is None:
        return False
    if candidate.get("create_time") is None:
        return True
    try:
        return abs(float(info["create_time"]) - float(candidate["create_time"])) <= 1e-3
    except (TypeError, ValueError):
        return False


def _wait_gone(candidate, timeout):
    end = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < end:
        if not _alive_same(candidate):
            return True
        time.sleep(0.1)
    return not _alive_same(candidate)


def _ask_quit(web):
    """POST the authenticated /api/quit endpoint on loopback only."""
    try:
        port = int(web.get("port"))
    except (TypeError, ValueError):
        return False, "bad_port"
    if not 1 <= port <= 65535:
        return False, "bad_port"
    token = str(web.get("token") or "")
    if not token:
        return False, "no_token"
    cookie = str(web.get("cookie_name") or "xg_token")
    req = Request(f"http://127.0.0.1:{port}/api/quit", data=b"{}", method="POST",
                  headers={"Content-Type": "application/json", "Cookie": cookie + "=" + token,
                           "Origin": f"http://127.0.0.1:{port}"})
    try:
        opener = build_opener(ProxyHandler({}))
        with opener.open(req, timeout=4) as response:
            response.read(256)
            ok = 200 <= int(response.status) < 300
        return ok, "http_ok" if ok else "http_status"
    except Exception as exc:  # noqa: BLE001 - cleanup must continue to the kill path
        return False, type(exc).__name__


def _terminate_candidate(candidate):
    if not _alive_same(candidate):
        return True
    try:
        proc = psutil.Process(int(candidate["pid"]))
        proc.terminate()
    except psutil.NoSuchProcess:
        return True
    except (psutil.Error, OSError, ValueError):
        return False
    try:
        proc.wait(timeout=5)
        return True
    except psutil.TimeoutExpired:
        pass
    except (psutil.Error, OSError):
        return not _alive_same(candidate)
    try:
        proc.kill()
        proc.wait(timeout=2)
    except (psutil.Error, OSError):
        pass
    return not _alive_same(candidate)


def _find_stale(data):
    """Find control_api.py processes that use this exact data directory.

    The web.json PID is checked against its creation time and command line. A full
    process scan catches leftovers started by an older app directory: their
    command line still says control_api.py and their XG_DATA_DIR points here.
    """
    data = Path(data).resolve()
    record = _read_json(data / "control" / "web.json")
    found = {}
    ignored = []
    if record and record.get("pid"):
        info = _process_info(record["pid"], need_env=True)
        if info is None:
            ignored.append({"pid": record.get("pid"), "reason": "not_running"})
        else:
            same = _same_identity(record, info)
            env_ok = _same_dir((info.get("environ") or {}).get("XG_DATA_DIR"), data)
            if same is False and not env_ok:
                ignored.append({"pid": record.get("pid"), "reason": "identity_mismatch"})
            elif not _looks_like_backend(info.get("argv")):
                ignored.append({"pid": record.get("pid"), "reason": "not_control_api"})
            else:
                found[info["pid"]] = _candidate(info, record)

    me = os.getpid()
    for proc in psutil.process_iter():
        try:
            pid = int(proc.pid)
            if pid == me or pid in found:
                continue
            argv = list(proc.cmdline())
        except (psutil.Error, OSError, ValueError, TypeError):
            continue
        if not _looks_like_backend(argv):
            continue
        info = _process_info(pid, need_env=True)
        if info is None:
            continue
        target = (info.get("environ") or {}).get("XG_DATA_DIR")
        if not target or not _same_dir(target, data):
            continue
        found[pid] = _candidate(info)
    return list(found.values()), ignored, record


def _cleanup_stale_backends(data):
    """Stop only verified Xiaoguang backends that share this data directory."""
    data = Path(data).resolve()
    found, ignored, record = _find_stale(data)
    for row in ignored:
        _lifecycle_log(data, "stale_backend_ignored", **row)
    _lifecycle_log(data, "startup_cleanup_scan", candidates=len(found),
                   web_record=bool(record), ignored=len(ignored))
    cleared, failed = [], []
    for candidate in found:
        _lifecycle_log(data, "stale_backend_found", pid=candidate["pid"],
                       install_dir=candidate["dir"], has_web=bool(candidate["web"]))
        gone = False
        if candidate["web"]:
            ok, why = _ask_quit(candidate["web"])
            _lifecycle_log(data, "stale_backend_http_quit", pid=candidate["pid"],
                           ok=ok, detail=why)
            if ok:
                gone = _wait_gone(candidate, STALE_WAIT)
                if gone:
                    _lifecycle_log(data, "stale_backend_exited", pid=candidate["pid"])
        if not gone:
            _lifecycle_log(data, "stale_backend_terminate", pid=candidate["pid"])
            gone = _terminate_candidate(candidate)
        if gone:
            cleared.append(candidate["pid"])
        else:
            failed.append(candidate)

    if failed:
        dirs = sorted({row.get("dir") or "未知目录" for row in failed})
        _lifecycle_log(data, "startup_cleanup_done", cleared=cleared,
                       failed=[row["pid"] for row in failed])
        raise StaleBackendError(
            "发现旧的小光后台在运行（来自 " + "、".join(dirs) +
            " 目录），请在任务管理器结束 python.exe 后重试。")

    # The old record is either gone, invalid, or owned by a non-backend PID.
    try:
        (data / "control" / "web.json").unlink()
    except OSError:
        pass
    _lifecycle_log(data, "startup_cleanup_done", cleared=cleared, failed=[])
    return cleared


def _assign_kill_on_close_job(pid):
    """Best-effort Windows Job Object: closing the host handle kills the backend."""
    if os.name != "nt":
        return None
    try:
        from ctypes import wintypes

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [("ReadOperationCount", ctypes.c_ulonglong),
                        ("WriteOperationCount", ctypes.c_ulonglong),
                        ("OtherOperationCount", ctypes.c_ulonglong),
                        ("ReadTransferCount", ctypes.c_ulonglong),
                        ("WriteTransferCount", ctypes.c_ulonglong),
                        ("OtherTransferCount", ctypes.c_ulonglong)]

        class BASIC_LIMIT(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                        ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class EXTENDED_LIMIT(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC_LIMIT),
                        ("IoInfo", IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                     ctypes.c_void_p, wintypes.DWORD]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = EXTENDED_LIMIT()
        info.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            kernel32.CloseHandle(job)
            return None
        # PROCESS_SET_QUOTA | PROCESS_TERMINATE
        handle = kernel32.OpenProcess(0x0100 | 0x0001, False, int(pid))
        if not handle:
            kernel32.CloseHandle(job)
            return None
        assigned = kernel32.AssignProcessToJobObject(job, handle)
        kernel32.CloseHandle(handle)
        if not assigned:
            kernel32.CloseHandle(job)
            return None
        return job
    except Exception:  # noqa: BLE001 - watchdog is the portable fallback
        return None


def _close_handle(handle):
    if not handle or os.name != "nt":
        return
    try:
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001
        pass


def _close_job(job):
    _close_handle(job)


class DesktopInstance:
    """One UI host per data directory. Named mutex on Windows, lock file elsewhere."""

    def __init__(self, data):
        data = Path(data).resolve()
        key = os.path.normcase(os.path.realpath(str(data))).encode("utf-8", "surrogatepass")
        self.name = MUTEX_PREFIX + hashlib.sha256(key).hexdigest()[:20]
        self.data = data
        self._handle = None
        self._file = None

    def acquire(self):
        if os.name == "nt":
            from ctypes import wintypes
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateMutexW.restype = wintypes.HANDLE
            kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel32.CreateMutexW(None, False, self.name)
            if not handle:
                raise OSError(ctypes.get_last_error(), "CreateMutexW failed")
            if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
                kernel32.CloseHandle(handle)
                return False
            self._handle = handle
            return True
        try:
            import fcntl
            control = self.data / "control"
            control.mkdir(parents=True, exist_ok=True)
            lock = (control / "desktop.lock").open("a+b")
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                lock.close()
                return False
            lock.seek(0)
            lock.truncate()
            lock.write(str(os.getpid()).encode("ascii"))
            lock.flush()
            self._file = lock
            return True
        except OSError as exc:
            if self._file is not None:
                self._file.close()
            raise exc

    def release(self):
        if self._handle is not None and os.name == "nt":
            _close_handle(self._handle)
            self._handle = None
        if self._file is not None:
            try:
                self._file.close()
            except OSError:
                pass
            self._file = None


def _focus_existing_window():
    if os.name != "nt":
        return False
    try:
        from ctypes import wintypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        found = []

        def visit(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            size = int(user32.GetWindowTextLengthW(hwnd)) + 1
            if size <= 1:
                return True
            buf = ctypes.create_unicode_buffer(size)
            user32.GetWindowTextW(hwnd, buf, size)
            title = buf.value
            if title == APP_TITLE or title.startswith("小光"):
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                user32.BringWindowToTop(hwnd)
                user32.SetForegroundWindow(hwnd)
                found.append(hwnd)
                return False
            return True

        user32.EnumWindows(EnumProc(visit), 0)
        return bool(found)
    except Exception:  # noqa: BLE001 - focus is best effort
        return False


def _message(text, error=True):
    if os.name == "nt":
        try:
            ctypes.windll.user32.MessageBoxW(None, text, "小光", 0x10 if error else 0x40)
            return
        except Exception:  # noqa: BLE001
            pass
    print(text, file=sys.stderr)


class WindowControls:
    """Only window controls are exposed to JS. No filesystem, token or shell API."""
    def __init__(self):
        self._window = None
        self._maximized = False

    def minimize(self):
        self._window.minimize()

    def toggle_maximize(self):
        if self._maximized:
            self._window.restore()
        else:
            self._window.maximize()
        self._maximized = not self._maximized

    def close(self):
        self._window.destroy()


class Backend:
    def __init__(self, root, data):
        self.root, self.data = Path(root).resolve(), Path(data).resolve()
        self.process = None
        self.web = None
        self.log = None
        self.job = None

    def start(self):
        app = self.root / "app"
        python = self.root / "runtime/python/python.exe"
        if not python.is_file() or not (app / "bot/control_api.py").is_file():
            raise RuntimeError("请解压完整客户端目录后运行，不要只复制 EXE。")
        self.data.mkdir(parents=True, exist_ok=True)
        (self.data / "logs").mkdir(exist_ok=True)
        _cleanup_stale_backends(self.data)
        try:
            parent = psutil.Process(os.getpid())
            parent_ct = round(float(parent.create_time()), 6)
        except (psutil.Error, OSError, ValueError):
            parent_ct = 0.0
        env = dict(os.environ)
        env.update(SKY_AI_ROOT=str(app), XG_DATA_DIR=str(self.data), XG_PYTHON=str(python),
                   XG_NODE=str(self.root / "runtime/node/node.exe"), PYTHONDONTWRITEBYTECODE="1",
                   QFLY_DATA_DIR=str(self.data / "runtime/runtime-data"),
                   **{PARENT_PID_ENV: str(os.getpid()),
                      PARENT_CREATE_ENV: ("%.6f" % parent_ct) if parent_ct else ""})
        self.log = open(self.data / "logs/desktop-backend.log", "ab")
        self.process = subprocess.Popen([str(python), "-B", str(app / "bot/control_api.py"), "--port", "0"],
                                        cwd=app, env=env, stdin=subprocess.DEVNULL,
                                        stdout=self.log, stderr=self.log,
                                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        self.job = _assign_kill_on_close_job(self.process.pid)
        _lifecycle_log(self.data, "backend_started", pid=self.process.pid,
                       parent_pid=os.getpid(), parent_create_time=parent_ct,
                       job_object=bool(self.job))
        record = self.data / "control/web.json"
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("本机后台启动失败。详情保存在本机 desktop-backend.log。")
            try:
                web = json.loads(record.read_text(encoding="utf-8"))
                if web.get("pid") == self.process.pid and isinstance(web.get("port"), int):
                    self.web = web
                    return
            except (OSError, ValueError):
                pass
            time.sleep(.15)
        raise RuntimeError("本机后台启动超时。")

    def url(self, path="/desktop"):
        # This is the existing local one-use UI bootstrap token, NOT a game
        # credential or broker token. The backend removes it with a redirect
        # and never logs query strings. Cookies remain HttpOnly/SameSite.
        return f"http://127.0.0.1:{self.web['port']}{path}?t=" + quote(self.web["token"], safe="")

    def stop(self):
        if self.process and self.process.poll() is None:
            quit_asked = False
            if self.web:
                quit_asked, _why = _ask_quit(self.web)
            stopped = False
            if quit_asked:
                try:
                    self.process.wait(8)
                    stopped = True
                except subprocess.TimeoutExpired:
                    pass
            if not stopped:
                try:
                    self.process.terminate()
                    self.process.wait(5)
                except (subprocess.TimeoutExpired, OSError):
                    try:
                        self.process.kill()
                        self.process.wait(5)
                    except (subprocess.TimeoutExpired, OSError):
                        pass
        if self.log:
            try:
                self.log.close()
            except OSError:
                pass
            self.log = None
        _close_job(self.job)
        self.job = None
        pid = self.process.pid if self.process else None
        if pid:
            record = _read_json(self.data / "control/web.json")
            if record and record.get("pid") == pid:
                try:
                    (self.data / "control/web.json").unlink()
                except OSError:
                    pass
        self.process = None
        self.web = None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--data-dir")
    p.add_argument("--backend-smoke", action="store_true", help="只启动/检查/停止本机后台，不登录游戏")
    p.add_argument("--smoke-report")
    a = p.parse_args()
    root = bundle_root()
    data = Path(a.data_dir or os.environ.get("XG_DESKTOP_DATA_DIR") or
                str(Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "XiaoGuangPrivate/data"))
    instance = DesktopInstance(data)
    backend = Backend(root, data)
    try:
        if not instance.acquire():
            if a.backend_smoke:
                report = {"backend_started": False, "already_running": True, "data": str(data)}
                if a.smoke_report:
                    Path(a.smoke_report).write_text(json.dumps(report), encoding="utf-8")
                else:
                    print(json.dumps(report, ensure_ascii=False))
                return 0
            focused = _focus_existing_window()
            _message("小光已经在运行。\n" +
                     ("已切换到已有窗口。" if focused else "请在任务栏切换到已有窗口，或先关闭它。"), False)
            return 0
        backend.start()
        if a.backend_smoke:
            report = {"backend_started": True, "loopback_only": True, "game_login_executed": False}
            if a.smoke_report:
                Path(a.smoke_report).write_text(json.dumps(report), encoding="utf-8")
            else:
                print(json.dumps(report))
            return 0
        import webview
        controls = WindowControls()
        controls._window = webview.create_window(APP_TITLE, backend.url(),
            js_api=controls, width=1240, height=820, min_size=(960, 640),
            background_color="#EDF3F6", frameless=True, easy_drag=False,
            text_select=True, confirm_close=True)
        controls._window.events.closed += backend.stop
        webview.start(gui="edgechromium", debug=False, private_mode=True)
    except Exception as exc:
        if a.backend_smoke:
            print(json.dumps({"backend_started": False, "error_type": type(exc).__name__,
                              "message": str(exc)[:350]}, ensure_ascii=False))
        elif isinstance(exc, StaleBackendError):
            _message(str(exc))
        elif os.name == "nt":
            _message("小光桌面客户端启动未完成。\n" + str(exc)[:350])
        return 1
    finally:
        backend.stop()
        instance.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
