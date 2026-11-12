"""Offline lifecycle regressions: parent watchdog, orphan cleanup and single instance.

Every process created here is a disposable fixture. Nothing scans for or stops a
real XiaoGuang/game process: the cleanup tests use a unique temporary data dir.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

import psutil

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import desktop  # noqa: E402


def wait_for(predicate, timeout=20.0, interval=0.1):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def process_gone(pid):
    try:
        proc = psutil.Process(int(pid))
        return not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True
    except (psutil.Error, OSError, ValueError):
        return True


def kill_own(pid):
    """Best-effort cleanup for a fixture process created by this test file."""
    if not pid:
        return
    try:
        proc = psutil.Process(int(pid))
        if proc.is_running():
            proc.kill()
            try:
                proc.wait(timeout=3)
            except psutil.TimeoutExpired:
                pass
    except (psutil.Error, OSError, ValueError):
        pass


class FixtureProcess:
    def __init__(self, path, proc):
        self.path = Path(path)
        self.proc = proc


class StaleBackendCleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="xg-lifecycle-stale-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data = self.tmp / "shared-data"
        self.pids = []
        self.procs = []

    def tearDown(self):
        for proc in self.procs:
            kill_own(proc.pid)
            try:
                proc.wait(timeout=3)
            except (subprocess.TimeoutExpired, OSError):
                pass

    def _start_http_backend(self, version="old-version"):
        base = self.tmp / version
        script = base / "app" / "bot" / "control_api.py"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(textwrap.dedent(r'''
            import http.server, json, os, threading, time
            from pathlib import Path
            import psutil
            DATA = Path(os.environ["XG_DATA_DIR"])
            CONTROL = DATA / "control"
            CONTROL.mkdir(parents=True, exist_ok=True)
            MARK = Path(os.environ["XG_FAKE_QUIT_MARKER"])

            class Handler(http.server.BaseHTTPRequestHandler):
                def log_message(self, *args):
                    return
                def do_POST(self):
                    if self.path != "/api/quit":
                        self.send_response(404); self.end_headers(); return
                    MARK.write_text("http_quit", encoding="utf-8")
                    body = b'{"ok":true}'
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    self.wfile.flush()
                    threading.Thread(target=self._later, daemon=True).start()
                def _later(self):
                    time.sleep(0.4)
                    os._exit(0)

            srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            doc = {"port": int(srv.server_address[1]), "token": "fixture-token",
                   "cookie_name": "xg_token", "pid": os.getpid(),
                   "create_time": round(float(psutil.Process().create_time()), 6),
                   "started": "fixture", "version": 1}
            pending = CONTROL / "web.json.pending"
            pending.write_text(json.dumps(doc), encoding="utf-8")
            os.replace(pending, CONTROL / "web.json")
            srv.serve_forever()
        ''').lstrip(), encoding="utf-8")
        env = dict(os.environ, XG_DATA_DIR=str(self.data), PYTHONDONTWRITEBYTECODE="1",
                   XG_FAKE_QUIT_MARKER=str(self.tmp / "quit.marker"))
        log = open(self.tmp / (version + ".log"), "ab")
        self.addCleanup(log.close)
        proc = subprocess.Popen([sys.executable, "-B", str(script)], cwd=str(script.parent),
                                env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        self.pids.append(proc.pid)
        self.procs.append(proc)
        web = self.data / "control" / "web.json"
        self.assertTrue(wait_for(web.is_file), "fake backend did not write web.json")
        return FixtureProcess(script, proc)

    def _start_silent_backend(self, version):
        base = self.tmp / version
        script = base / "app" / "bot" / "control_api.py"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("import time\nwhile True:\n    time.sleep(1)\n", encoding="utf-8")
        env = dict(os.environ, XG_DATA_DIR=str(self.data), PYTHONDONTWRITEBYTECODE="1")
        proc = subprocess.Popen([sys.executable, "-B", str(script)], cwd=str(script.parent),
                                env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.pids.append(proc.pid)
        self.procs.append(proc)
        self.assertTrue(wait_for(lambda: psutil.pid_exists(proc.pid)))
        return FixtureProcess(script, proc)

    def test_01_http_quit_then_terminate_other_version_orphan(self):
        http_backend = self._start_http_backend("version-a")
        silent_backend = self._start_silent_backend("version-b")
        cleared = desktop._cleanup_stale_backends(self.data)
        self.assertIn(http_backend.proc.pid, cleared)
        self.assertIn(silent_backend.proc.pid, cleared)
        self.assertTrue((self.tmp / "quit.marker").is_file())
        self.assertTrue(wait_for(lambda: process_gone(http_backend.proc.pid), 5))
        self.assertTrue(wait_for(lambda: process_gone(silent_backend.proc.pid), 5))
        log = (self.data / "logs" / "desktop-lifecycle.log").read_text(encoding="utf-8")
        self.assertIn("stale_backend_http_quit", log)
        self.assertIn("stale_backend_terminate", log)
        self.assertIn('"failed": []', log)
        self.assertFalse((self.data / "control" / "web.json").exists())

    def test_02_non_backend_pid_is_never_killed(self):
        script = self.tmp / "harmless.py"
        script.write_text("import time\nwhile True:\n    time.sleep(1)\n", encoding="utf-8")
        proc = subprocess.Popen([sys.executable, "-B", str(script)], cwd=str(self.tmp),
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        self.pids.append(proc.pid)
        self.procs.append(proc)
        control = self.data / "control"
        control.mkdir(parents=True, exist_ok=True)
        doc = {"port": 1, "token": "fixture-token", "pid": proc.pid,
               "create_time": round(float(psutil.Process(proc.pid).create_time()), 6)}
        (control / "web.json").write_text(json.dumps(doc), encoding="utf-8")
        desktop._cleanup_stale_backends(self.data)
        self.assertIsNone(proc.poll(), "non-control_api process was killed")
        self.assertTrue(psutil.pid_exists(proc.pid))
        log = (self.data / "logs" / "desktop-lifecycle.log").read_text(encoding="utf-8")
        self.assertIn('"reason": "not_control_api"', log)

    def test_03_unremovable_backend_has_chinese_prompt(self):
        self._start_silent_backend("stuck-version")
        with mock.patch.object(desktop, "_terminate_candidate", return_value=False), \
                mock.patch.object(desktop, "_ask_quit", return_value=(False, "fixture")):
            with self.assertRaises(desktop.StaleBackendError) as caught:
                desktop._cleanup_stale_backends(self.data)
        message = str(caught.exception)
        self.assertIn("发现旧的小光后台在运行", message)
        self.assertIn("任务管理器结束 python.exe", message)
        self.assertIn("stuck-version", message)
        log = (self.data / "logs" / "desktop-lifecycle.log").read_text(encoding="utf-8")
        self.assertIn('"failed": [', log)


class SingleInstanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="xg-lifecycle-instance-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_01_same_data_dir_has_one_owner(self):
        first = desktop.DesktopInstance(self.tmp / "data-a")
        second = desktop.DesktopInstance(self.tmp / "data-a")
        other = desktop.DesktopInstance(self.tmp / "data-b")
        try:
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire(), "second host acquired the same data dir")
            self.assertTrue(other.acquire(), "different data dirs must not conflict")
            other.release()
        finally:
            first.release()

    def test_02_mutex_is_released_on_close(self):
        first = desktop.DesktopInstance(self.tmp / "data-a")
        self.assertTrue(first.acquire())
        first.release()
        again = desktop.DesktopInstance(self.tmp / "data-a")
        try:
            self.assertTrue(again.acquire(), "released mutex stayed locked")
        finally:
            again.release()


class ParentWatchdogTests(unittest.TestCase):
    """A real control_api process survives its launcher only long enough to exit."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="xg-lifecycle-watchdog-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data = self.tmp / "data"
        self.pid = None

    def tearDown(self):
        kill_own(self.pid)

    def test_01_backend_exits_after_parent_process_exits(self):
        marker = self.tmp / "cleanup.marker"
        pidfile = self.tmp / "backend.pid"
        backend = self.tmp / "backend_runner.py"
        backend.write_text(textwrap.dedent(f'''
            import os, sys
            from pathlib import Path
            sys.path[:0] = [r"{ROOT / 'app'}", r"{ROOT / 'app' / 'bot'}", r"{ROOT / 'app' / 'launcher'}"]
            import launcher
            def fake_stop(*args, **kwargs):
                Path(os.environ["XG_TEST_MARKER"]).write_text("cleanup", encoding="utf-8")
                return {{"ok": True}}
            launcher.stop = fake_stop
            import control_api
            raise SystemExit(control_api.main(["--port", "0"]))
        ''').lstrip(), encoding="utf-8")
        parent = self.tmp / "parent_runner.py"
        parent.write_text(textwrap.dedent(f'''
            import os, subprocess, sys
            from pathlib import Path
            import psutil
            env = dict(os.environ)
            env["XG_DATA_DIR"] = r"{self.data}"
            env["XG_PARENT_PID"] = str(os.getpid())
            env["XG_PARENT_CREATE_TIME"] = "%.6f" % psutil.Process().create_time()
            env["XG_TEST_MARKER"] = r"{marker}"
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            log = open(r"{self.tmp / 'backend.out'}", "ab")
            p = subprocess.Popen([sys.executable, "-B", r"{backend}"], env=env,
                                 cwd=r"{self.tmp}", stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=log)
            Path(r"{pidfile}").write_text(str(p.pid), encoding="utf-8")
            sys.exit(0)
        ''').lstrip(), encoding="utf-8")
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run([sys.executable, "-B", str(parent)], env=env,
                                cwd=str(self.tmp), capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(wait_for(pidfile.is_file, 10), "parent did not report backend pid")
        self.pid = int(pidfile.read_text(encoding="utf-8").strip())
        self.assertTrue(wait_for(lambda: process_gone(self.pid), 20),
                        "backend stayed alive after its parent exited")
        self.assertTrue(wait_for(marker.is_file, 3), "cleanup path was not called")
        log = (self.data / "logs" / "control_api.log").read_text(encoding="utf-8")
        self.assertIn("parent_exited", log)
        self.assertIn("shutdown_requested", log)
        web = self.data / "control" / "web.json"
        self.assertTrue(wait_for(lambda: not web.exists(), 5))


class JobObjectTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows Job Objects only")
    def test_01_closing_job_handle_kills_child(self):
        proc = subprocess.Popen([sys.executable, "-B", "-c", "import time; time.sleep(30)"],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        self.addCleanup(kill_own, proc.pid)
        job = desktop._assign_kill_on_close_job(proc.pid)
        if not job:
            self.skipTest("this session does not allow nested Job Objects")
        desktop._close_job(job)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.fail("Job Object handle close did not kill the child")


if __name__ == "__main__":
    unittest.main()
