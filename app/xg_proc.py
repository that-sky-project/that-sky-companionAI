# -*- coding: utf-8 -*-
"""跨平台进程管理（W1）：替代 pgrep -f、/proc/<pid>/{cmdline,stat,environ}、
kill -TERM/-9、setsid 这一套。

要点
====
* **按 argv 精确找人**：只有“python 解释器后面的第一个参数”正好是目标脚本
  （或 `-m 模块`）才算命中；命令行里只是含这串字符的进程（ssh、bash -c、
  grep、编辑器）不会被误认。
* **进程身份**：pid + 创建时间（Windows 上 pid 会复用，必须带创建时间）。
  POSIX 额外带 pgid/session/start_ticks/state，服务器监督器逻辑不用改。
* **优雅停止**：Windows 先 CTRL_BREAK_EVENT，再 terminate，等待后 kill（含子进程树，
  否则 Node 引擎会留在后台）；POSIX SIGTERM → 等待 → SIGKILL（按进程组）。
* **脱离启动**：POSIX start_new_session=True；Windows
  CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS，不弹控制台窗口。

依赖：psutil（requirements.txt 里列了）。
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import psutil

import xg_exec

IS_WINDOWS = os.name == "nt"

# Windows 进程创建标志（winbase.h）
CREATE_NEW_PROCESS_GROUP = 0x00000200
DETACHED_PROCESS = 0x00000008

_PY_NAME_HINTS = ("python", "pythonw", "pypy")


def _is_python(name: str) -> bool:
    n = (name or "").lower()
    return any(n.startswith(h) for h in _PY_NAME_HINTS)


def argv_of(pid) -> list[str]:
    try:
        return list(psutil.Process(int(pid)).cmdline())
    except (psutil.Error, ValueError, TypeError, OSError):
        return []


def _posix_stat(pid):
    if IS_WINDOWS:
        return {}
    try:
        raw = Path(f"/proc/{int(pid)}/stat").read_text()
        fields = raw.rsplit(") ", 1)[1].split()
        return {"start_ticks": int(fields[19]), "pgid": int(fields[2]),
                "session": int(fields[3]), "state": fields[0]}
    except (OSError, IndexError, ValueError):
        return {}


def identity(pid):
    """{pid, create_time, name, ...}；进程不在返回 None。"""
    try:
        p = psutil.Process(int(pid))
        with p.oneshot():
            row = {"pid": int(pid), "create_time": round(float(p.create_time()), 6),
                   "name": p.name()}
    except (psutil.Error, ValueError, TypeError, OSError):
        return None
    extra = _posix_stat(pid)
    if extra:
        row.update(extra)
    else:
        try:
            st = p.status()
        except (psutil.Error, OSError):
            st = "?"
        row.setdefault("state", "Z" if st == psutil.STATUS_ZOMBIE else "S")
    return row


def same_identity(row) -> bool:
    """row 里记录的进程还是不是同一个（pid + 创建时间，POSIX 再比 pgid/session）。"""
    if not isinstance(row, dict) or not row.get("pid"):
        return False
    cur = identity(row["pid"])
    if cur is None or cur.get("state") == "Z":
        return False
    if "create_time" in row and abs(float(cur["create_time"]) - float(row["create_time"])) > 1e-3:
        return False
    for key in ("pgid", "session", "start_ticks"):
        if key in row and key in cur and row[key] != cur[key]:
            return False
    return True


def alive(pid) -> bool:
    try:
        p = psutil.Process(int(pid))
        return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
    except (psutil.Error, ValueError, TypeError, OSError):
        return False


# ------------------------------------------------------------------ 找人
def matches_script(argv, script: str) -> bool:
    """argv 是不是“python <script> …”。

    script 可带目录（bot/ai_companion.py）也可只是文件名。解释器后第一个非选项
    参数必须正好是它（绝对/相对路径都行，按 / 归一化），或者 `-m 模块`。
    argv 里别处出现这串字符不算。
    """
    if not argv or not script:
        return False
    exe = str(argv[0]).replace("\\", "/")
    if not _is_python(Path(exe).name):
        return False
    want = script.replace("\\", "/").lstrip("./")
    want_base = want.rsplit("/", 1)[-1]
    j = 1
    while j < len(argv):
        arg = str(argv[j])
        if arg == "-m":
            if j + 1 >= len(argv):
                return False
            mod = argv[j + 1].replace("\\", "/").lstrip("./").replace("/", ".").removesuffix(".py")
            return mod == want_base.removesuffix(".py").replace("/", ".") or mod in (
                want.replace("/", ".").removesuffix(".py"), want_base.removesuffix(".py"))
        if arg in ("-c", "-"):
            return False        # -c 后面是代码字符串，不是脚本
        if not arg.startswith("-"):
            break
        j += 1
    if j >= len(argv):
        return False
    got = str(argv[j]).replace("\\", "/").lstrip("./")
    if got == want or got.endswith("/" + want):
        return True
    # 只按文件名匹配：用于安装版/测试里脚本在临时目录的情况。要求这个参数
    # 真的存在成一个文件，避免把 `python -c "... ai_companion.py"` 当进程。
    if got.rsplit("/", 1)[-1] != want_base:
        return False
    try:
        return Path(str(argv[j])).is_file()
    except OSError:
        return False


def _parent_pid(pid):
    try:
        return psutil.Process(int(pid)).ppid()
    except (psutil.Error, ValueError, OSError):
        return None


def _dedupe_parents(rows):
    """去掉“转发壳 + 真身”里的壳：父子两个都匹配、argv 又一样时只留子进程。

    Windows 的 venv python.exe 有时是转发壳（Popen 的 pid 和真正跑脚本的
    python 不是同一个）；不去重会让 status/stop 看到两条。
    """
    by_pid = {r["pid"]: r for r in rows}
    drop = set()
    for row in rows:
        tail = row.get("argv")[1:] if isinstance(row.get("argv"), list) else None
        parent = _parent_pid(row["pid"])
        while parent:
            other = by_pid.get(parent)
            if other is not None and isinstance(other.get("argv"), list) and other["argv"][1:] == tail:
                drop.add(parent)
                break
            parent = _parent_pid(parent)
    return [r for r in rows if r["pid"] not in drop]


def _rows(match, within=None, need_env=False):
    out = []
    me = os.getpid()
    for p in psutil.process_iter():
        try:
            pid = p.pid
            if pid == me:
                continue
            argv = p.cmdline()
            if not match(argv):
                continue
            ident = identity(pid)
            if ident is None or ident.get("state") == "Z":
                continue
            row = dict(ident)
            row["argv"] = argv
            try:
                row["cwd"] = p.cwd()
            except (psutil.Error, OSError):
                row["cwd"] = None
            if within is not None and row["cwd"]:
                try:
                    Path(row["cwd"]).resolve().relative_to(Path(within).resolve())
                except (ValueError, OSError):
                    continue
            if need_env:
                row["environ"] = process_environ(pid)
            out.append(row)
        except (psutil.Error, OSError, ValueError):
            continue
    out.sort(key=lambda r: r["pid"])
    return _dedupe_parents(out)


def find_script(script: str, within=None, need_env: bool = False):
    """所有“python <script>”进程：[{pid, create_time, argv, cwd, ...}]。"""
    return _rows(lambda argv: matches_script(argv, script), within=within, need_env=need_env)


def find_any_script(scripts, within=None, need_env: bool = False):
    scripts = list(scripts)
    return _rows(lambda argv: any(matches_script(argv, s) for s in scripts),
                 within=within, need_env=need_env)


def find_argv_contains(text: str, within=None):
    """命令行里含某段文字的进程（只给诊断用；不要拿它认业务进程）。"""
    return _rows(lambda argv: any(text in a for a in argv), within=within)


def argv_flag(argv, name, default=None):
    try:
        return argv[argv.index(name) + 1]
    except (ValueError, IndexError):
        return default


def process_environ(pid) -> dict:
    """进程环境变量；Windows 上没权限/跨位数读不到就返回 {}（不要抛）。"""
    try:
        return {str(k): str(v) for k, v in psutil.Process(int(pid)).environ().items()}
    except (psutil.Error, OSError, ValueError, TypeError):
        return {}


# ------------------------------------------------------------------ 停止
def _children_tree(pid):
    try:
        return psutil.Process(int(pid)).children(recursive=True)
    except (psutil.Error, ValueError, OSError):
        return []


def _signal(pid, sig, group=False):
    try:
        if group and not IS_WINDOWS:
            os.killpg(int(pid), sig)
        else:
            os.kill(int(pid), sig)
        return True
    except (OSError, ValueError):
        return False


def _wait_gone(pid, timeout: float) -> bool:
    end = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < end:
        if not alive(pid):
            return True
        time.sleep(0.05)
    return not alive(pid)


def terminate(pid, grace: float = 5.0, tree: bool = True, ctrl_break: bool = False) -> bool:
    """优雅停止：先礼貌信号 → 等 grace 秒 → 还在就强杀。返回是否已经不在。

    Windows 的 CTRL_BREAK_EVENT 是发给“进程组”的：目标不是自己新建的进程组时，
    会打到调用方所在的控制台组（实测会把启动器/测试一起 Ctrl-C 掉）。所以默认
    不发，只有明确知道自己用 CREATE_NEW_PROCESS_GROUP 起了它（例如同一进程里
    spawn_detached 出来的）才传 ctrl_break=True。
    """
    pid = int(pid)
    if not alive(pid):
        return True
    kids = _children_tree(pid) if tree else []
    if IS_WINDOWS:
        if ctrl_break:
            try:
                os.kill(pid, signal.CTRL_BREAK_EVENT)
            except (OSError, ValueError, AttributeError, SystemError):
                pass
            _wait_gone(pid, min(1.0, grace))
            if not alive(pid):
                return True
        try:
            psutil.Process(pid).terminate()
        except (psutil.Error, OSError):
            pass
        if _wait_gone(pid, max(0.0, grace - (1.0 if ctrl_break else 0.0))):
            return True
        try:
            psutil.Process(pid).kill()
        except (psutil.Error, OSError):
            pass
        _wait_gone(pid, 2.0)
    else:
        _signal(pid, signal.SIGTERM, group=True)
        if _wait_gone(pid, grace):
            return True
        _signal(pid, signal.SIGKILL, group=True)
        _wait_gone(pid, 2.0)
    for kid in kids:
        try:
            if kid.is_running():
                kid.kill()
        except (psutil.Error, OSError):
            pass
    return not alive(pid)


def stop_all(scripts, grace: float = 5.0, within=None) -> list[int]:
    """按脚本名停掉所有匹配进程，返回 pid 列表。"""
    rows = find_any_script(scripts, within=within)
    for row in rows:
        terminate(row["pid"], grace=grace)
    return [r["pid"] for r in rows]


# ------------------------------------------------------------------ 启动
def spawn_detached(argv, cwd=None, stdout=None, stderr=None, env=None, stdin=subprocess.DEVNULL):
    """启动脱离的后台进程，返回 (Popen, identity)。stdout/stderr 可以是路径或文件对象。"""
    created = []
    out_f = err_f = None
    try:
        if stdout is None:
            out_f = subprocess.DEVNULL
        elif isinstance(stdout, (str, os.PathLike)):
            Path(stdout).parent.mkdir(parents=True, exist_ok=True)
            out_f = open(stdout, "ab")
            created.append(out_f)
        else:
            out_f = stdout
        if stderr is None:
            err_f = subprocess.STDOUT if out_f is not subprocess.DEVNULL else subprocess.DEVNULL
        elif isinstance(stderr, (str, os.PathLike)):
            Path(stderr).parent.mkdir(parents=True, exist_ok=True)
            err_f = open(stderr, "ab")
            created.append(err_f)
        else:
            err_f = stderr
        kwargs = dict(cwd=str(cwd) if cwd else None, env=env, stdin=stdin,
                      stdout=out_f, stderr=err_f, close_fds=True)
        if IS_WINDOWS:
            kwargs["creationflags"] = CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS
        else:
            kwargs["start_new_session"] = True
        proc = xg_exec.popen([str(a) for a in argv], **kwargs)
    finally:
        for f in created:
            try:
                f.close()
            except OSError:
                pass
    ident = identity(proc.pid) or {"pid": proc.pid, "create_time": 0.0,
                                   "name": Path(str(argv[0])).name, "state": "?"}
    return proc, ident


def describe(row) -> str:
    return f"pid={row.get('pid')} ct={row.get('create_time')} argv={' '.join(row.get('argv') or [])}"


def self_check() -> dict:
    return {"platform": "windows" if IS_WINDOWS else os.name,
            "self": identity(os.getpid()), "python": sys.executable}


if __name__ == "__main__":
    import json
    print(json.dumps(self_check(), ensure_ascii=False, indent=1))
