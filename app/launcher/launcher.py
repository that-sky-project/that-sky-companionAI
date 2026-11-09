# -*- coding: utf-8 -*-
"""小光启动器（W1）：起本机 control_api（后台）+ 打开浏览器；--stop 停掉。

  python launcher.py                 启动并打开后台页面
  python launcher.py --no-browser    只启动（测试/远程用）
  python launcher.py --stop          停掉后台接口（--all 连小光一起停）
  python launcher.py --status        看后台接口状态

首次运行（DATA 下没有 config.json）：打开的是风险告知页，勾选同意后写 config.json
（记下同意时间）才进面板。端口随机、只监听 127.0.0.1，端口和令牌写在
DATA/control/web.json（0600）；浏览器第一次带 ?t=<一次性令牌>，服务端换成
HttpOnly cookie。
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
import webbrowser
from pathlib import Path

APP = Path(__file__).resolve().parent.parent


def default_data_dir() -> Path:
    env = os.environ.get("XG_DATA_DIR")
    if env:
        return Path(env).expanduser()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base) / "XiaoGuang" / "data"
        return Path.home() / "XiaoGuang" / "data"
    return Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local/share")) / "xiaoguang"


def bootstrap(data_dir=None):
    """写环境变量并返回 xg_paths（必须在 import xg_paths 之前调用）。"""
    data = Path(data_dir).expanduser().resolve() if data_dir else default_data_dir().resolve()
    os.environ["SKY_AI_ROOT"] = str(APP)
    os.environ["XG_DATA_DIR"] = str(data)
    os.environ.setdefault("XG_PYTHON", sys.executable)
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    parts = [str(APP), str(APP / "bot"), str(APP / "src")]
    old = os.environ.get("PYTHONPATH")
    if old:
        parts.append(old)
    os.environ["PYTHONPATH"] = os.pathsep.join(parts)
    sys.path.insert(0, str(APP / "bot"))
    sys.path.insert(0, str(APP))
    import xg_paths
    xg_paths.ensure_dirs()
    return xg_paths


def _read_web(xg_paths):
    doc = xg_paths.read_json(xg_paths.WEB_JSON, None)
    return doc if isinstance(doc, dict) else None


def running(xg_paths):
    doc = _read_web(xg_paths)
    if not doc:
        return None
    import xg_proc
    ident = xg_proc.identity(doc.get("pid"))
    if ident is None:
        return None
    if doc.get("create_time") and abs(float(ident["create_time"]) - float(doc["create_time"])) > 1e-3:
        return None
    argv = ident and xg_proc.argv_of(ident["pid"])
    if argv and not any("control_api.py" in str(a) for a in argv):
        return None
    return {**doc, "identity": ident}


def start(xg_paths, no_browser=False, wait=20.0):
    import xg_proc
    cur = running(xg_paths)
    if cur:
        # 后台已经在跑：换一个新令牌也要重启，保证“一次性 token”语义
        xg_proc.terminate(cur["pid"], grace=5)
        try:
            xg_paths.WEB_JSON.unlink()
        except OSError:
            pass
    token = secrets.token_urlsafe(24)
    # 令牌不放命令行（进程列表可见）：用环境变量传给子进程
    child = dict(os.environ, XG_WEB_TOKEN=token)
    proc, ident = xg_proc.spawn_detached(
        [sys.executable, "-B", str(APP / "bot" / "control_api.py"), "--port", "0"],
        cwd=APP, stdout=xg_paths.LOGS / "control_api.out", stderr=xg_paths.LOGS / "control_api.out",
        env=child)
    doc = None
    end = time.monotonic() + max(1.0, wait)
    while time.monotonic() < end:
        doc = _read_web(xg_paths)
        # 注意：venv 的 python.exe 在 Windows 上可能是个转发壳，Popen 拿到的 pid
        # 和真正跑脚本的 python 不是同一个。这里以 web.json 里的 pid + argv 为准。
        if doc and doc.get("pid") and doc.get("port"):
            argv = xg_proc.argv_of(doc["pid"])
            if any("control_api.py" in str(a) for a in argv):
                break
        if proc.poll() is not None and not doc:
            break
        time.sleep(0.1)
    if not doc or not doc.get("port") or not any("control_api.py" in str(a)
                                                 for a in xg_proc.argv_of(doc.get("pid"))):
        detail = ""
        try:
            detail = (xg_paths.LOGS / "control_api.out").read_text(encoding="utf-8", errors="replace")[-500:]
        except OSError:
            pass
        return {"ok": False, "message": "控制接口没起来", "detail": detail, "pid": proc.pid}
    url = f"http://127.0.0.1:{doc['port']}/?t={token}"
    if not no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    return {"ok": True, "url": url, "port": doc["port"], "pid": proc.pid,
            "web": str(xg_paths.WEB_JSON), "data": str(xg_paths.DATA),
            "consent": bool((xg_paths.config() or {}).get("consent"))}


def stop(xg_paths, all_proc=False):
    import xg_proc
    out = {"ok": True}
    if all_proc:
        # 先停录图/长任务/小光，最后才停 control_api：从 /api/quit 里停自己时，
        # terminate 会立刻结束本进程，后面的步骤就没机会执行。
        try:
            import capture_ctl
            out["capture"] = capture_ctl.stop()
        except Exception as exc:
            out["capture"] = {"ok": False, "message": type(exc).__name__}
        try:
            import tasks
            out["tasks"] = tasks.stop()
        except Exception as exc:
            out["tasks"] = {"ok": False, "message": type(exc).__name__}
        import xg_ctl
        out["light"] = xg_ctl.stop()
    doc = _read_web(xg_paths)
    if doc and doc.get("pid"):
        # 先删 web.json 再停进程：从 /api/quit 里停自己时，terminate 会立刻
        # 结束本进程，后面的 unlink 就没机会执行。
        try:
            xg_paths.WEB_JSON.unlink()
        except OSError:
            pass
        if xg_proc.same_identity({"pid": doc.get("pid"), "create_time": doc.get("create_time", 0.0)}):
            out["api"] = {"pid": doc["pid"], "stopped": xg_proc.terminate(doc["pid"], grace=5)}
        else:
            out["api"] = {"pid": doc["pid"], "stopped": True, "note": "进程已经不在了"}
    else:
        out["api"] = None
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="小光启动器（W1）")
    ap.add_argument("--data", help="用户数据目录（默认 %%LOCALAPPDATA%%\\XiaoGuang\\data）")
    ap.add_argument("--no-browser", action="store_true", help="不打开浏览器（测试用）")
    ap.add_argument("--stop", action="store_true", help="停掉后台接口")
    ap.add_argument("--all", action="store_true", help="--stop 时连小光（runner/聊天）一起停")
    ap.add_argument("--status", action="store_true", help="显示后台接口状态")
    ap.add_argument("--wait", type=float, default=20.0, help="等后台接口起来的最长秒数")
    a = ap.parse_args(argv)
    xg_paths = bootstrap(a.data)
    if a.stop:
        print(json.dumps(stop(xg_paths, a.all), ensure_ascii=False), flush=True)
        return 0
    if a.status:
        cur = running(xg_paths)
        print(json.dumps({"running": bool(cur), "web": cur and {k: cur[k] for k in ("port", "pid", "started")}},
                         ensure_ascii=False), flush=True)
        return 0 if cur else 1
    out = start(xg_paths, a.no_browser, a.wait)
    print(json.dumps(out, ensure_ascii=False), flush=True)
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
