# -*- coding: utf-8 -*-
"""开机自启动：只在当前用户的“启动”文件夹放/删快捷方式，不改注册表。

Windows 用 PowerShell 的 WScript.Shell 建 .lnk（不引入 pywin32）。
测试用 XG_STARTUP_DIR 覆盖启动文件夹，避免动真实用户目录。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import xg_exec

APP = Path(__file__).resolve().parent.parent
NAME = "小光.lnk"


def startup_dir():
    env = os.environ.get("XG_STARTUP_DIR")
    if env:
        return Path(env).expanduser()
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData/Roaming")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def shortcut_path():
    return startup_dir() / NAME


def _target():
    """返回 (target, arguments, workdir)。安装版优先跑 小光.vbs（pythonw，无控制台）。"""
    root = APP.parent
    exe = os.environ.get("XG_PYTHONW")
    if not exe:
        for cand in (root / "runtime" / "python" / "pythonw.exe",
                     Path(sys.executable).with_name("pythonw.exe")):
            if cand.is_file():
                exe = str(cand)
                break
    if not exe:
        exe = sys.executable
    return exe, f'"{APP / "launcher" / "launcher.py"}"', str(root)


def _powershell(script):
    exe = os.environ.get("XG_POWERSHELL") or "powershell.exe"
    try:
        p = xg_exec.run([exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                         "-Command", script], capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=60)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return -1, str(exc)


def _ps_quote(s):
    return "'" + str(s).replace("'", "''") + "'"


def enable():
    if os.name != "nt":
        return {"ok": False, "message": "只有 Windows 支持开机自启动"}
    d = startup_dir()
    d.mkdir(parents=True, exist_ok=True)
    root = APP.parent
    vbs = root / "小光.vbs"
    if vbs.is_file():
        target = os.environ.get("WSCRIPT_EXE")
        if not target and os.name == "nt":
            target = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "wscript.exe")
        target = target or "wscript.exe"
        args = f'"{vbs}"'
    else:
        target, args, _ = _target()
    lnk = shortcut_path()
    script = (
        "$ws = New-Object -ComObject WScript.Shell;"
        f"$s = $ws.CreateShortcut({_ps_quote(lnk)});"
        f"$s.TargetPath = {_ps_quote(target)};"
        f"$s.Arguments = {_ps_quote(args)};"
        f"$s.WorkingDirectory = {_ps_quote(root)};"
        "$s.WindowStyle = 7;"
        "$s.Description = '小光';"
        "$s.Save();"
    )
    code, out = _powershell(script)
    if code != 0 or not lnk.is_file():
        return {"ok": False, "message": "建快捷方式失败：" + out[-300:]}
    return {"ok": True, "enabled": True, "path": str(lnk), "target": target, "args": args}


def disable():
    removed = []
    for p in (shortcut_path(), startup_dir() / "小光.cmd"):
        try:
            if p.is_file():
                p.unlink()
                removed.append(str(p))
        except OSError:
            pass
    return {"ok": True, "enabled": False, "removed": removed}


def status():
    return {"ok": True, "enabled": shortcut_path().is_file(), "path": str(shortcut_path()),
            "supported": os.name == "nt"}
