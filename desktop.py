"""Windows EXE host for Xiaoguang. The game/login backend is not the UI process."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import quote
from urllib.request import Request, urlopen


def bundle_root():
    return Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent


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

    def start(self):
        app = self.root / "app"
        python = self.root / "runtime/python/python.exe"
        if not python.is_file() or not (app / "bot/control_api.py").is_file():
            raise RuntimeError("请解压完整客户端目录后运行，不要只复制 EXE。")
        self.data.mkdir(parents=True, exist_ok=True)
        (self.data / "logs").mkdir(exist_ok=True)
        env = dict(os.environ)
        env.update(SKY_AI_ROOT=str(app), XG_DATA_DIR=str(self.data), XG_PYTHON=str(python),
                   XG_NODE=str(self.root / "runtime/node/node.exe"), PYTHONDONTWRITEBYTECODE="1",
                   QFLY_DATA_DIR=str(self.data / "runtime/runtime-data"))
        self.log = open(self.data / "logs/desktop-backend.log", "ab")
        self.process = subprocess.Popen([str(python), "-B", str(app / "bot/control_api.py"), "--port", "0"],
                                        cwd=app, env=env, stdin=subprocess.DEVNULL,
                                        stdout=self.log, stderr=self.log,
                                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
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
            if self.web:
                try:
                    req = Request(f"http://127.0.0.1:{self.web['port']}/api/quit", data=b"{}", method="POST",
                                  headers={"Content-Type": "application/json", "Cookie": self.web["cookie_name"] + "=" + self.web["token"],
                                           "Origin": f"http://127.0.0.1:{self.web['port']}"})
                    with urlopen(req, timeout=5):
                        pass
                    self.process.wait(8)
                except Exception:
                    self.process.terminate()
                    try:
                        self.process.wait(5)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait(5)
            else:
                self.process.terminate()
                self.process.wait(5)
        if self.log:
            self.log.close()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--data-dir")
    p.add_argument("--backend-smoke", action="store_true", help="只启动/检查/停止本机后台，不登录游戏")
    p.add_argument("--smoke-report")
    a = p.parse_args()
    root = bundle_root()
    data = Path(a.data_dir or os.environ.get("XG_DESKTOP_DATA_DIR") or
                str(Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "XiaoGuangPrivate/data"))
    backend = Backend(root, data)
    try:
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
        controls._window = webview.create_window("小光 · Companion", backend.url(),
            js_api=controls, width=1240, height=820, min_size=(960, 640),
            background_color="#EDF3F6", frameless=True, easy_drag=False,
            text_select=True, confirm_close=True)
        controls._window.events.closed += backend.stop
        webview.start(gui="edgechromium", debug=False, private_mode=True)
    except Exception as exc:
        if a.backend_smoke:
            print(json.dumps({"backend_started": False, "error_type": type(exc).__name__}))
        elif os.name == "nt":
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, "小光桌面客户端启动未完成。\n" + str(exc)[:350], "小光", 0x10)
        return 1
    finally:
        backend.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
