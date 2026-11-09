# -*- coding: utf-8 -*-
"""找 MuMu / adb、确认 root、认当前地图。

自动查找顺序：环境变量/配置里的手填路径 → 注册表 → 常见安装位置 → PATH。
MuMu 12 的 adb 通常在 `shell\\adb.exe` 或 `nx_device\\12.0\\shell\\adb.exe`；
实例端口是 16384 + 32*n，另外试 7555。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import native_log
import xg_exec

PACKAGE = "com.netease.sky"
DEFAULT_PORTS = [16384 + 32 * i for i in range(16)] + [7555]
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _run(argv, timeout=20):
    try:
        p = xg_exec.run(argv, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return -1, "", str(exc)
    return (p.returncode,
            (p.stdout or b"").decode("utf-8", "replace").strip(),
            (p.stderr or b"").decode("utf-8", "replace").strip())


def adb_argv(path):
    """adb 可以是 exe，也可以是测试用的 .py/.cmd。"""
    p = str(path)
    low = p.lower()
    if low.endswith(".py"):
        return [sys.executable, p]
    if low.endswith((".cmd", ".bat")):
        return [os.environ.get("COMSPEC", "cmd.exe"), "/c", p]
    return [p]


def adb_run(adb, *args, serial=None, timeout=20):
    argv = [*adb_argv(adb), *(["-s", serial] if serial else []), *args]
    return _run(argv, timeout=timeout)


def _registry_roots():
    roots = []
    if os.name != "nt":
        return roots
    try:
        import winreg
    except Exception:
        return roots
    keys = (
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Netease\MuMuPlayer"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Netease\MuMuPlayer-12.0"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Netease\MuMuPlayer"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Netease\MuMuPlayer-12.0"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Netease\MuMuPlayer"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Netease\MuMuPlayer-12.0"),
    )
    for hive, key in keys:
        try:
            with winreg.OpenKey(hive, key) as k:
                for name in ("InstallPath", "InstallDir", "Path", "InstallLocation"):
                    try:
                        value, _ = winreg.QueryValueEx(k, name)
                    except OSError:
                        continue
                    if isinstance(value, str) and value.strip():
                        roots.append(Path(value.strip().strip('"')))
        except OSError:
            continue
    return roots


def candidate_roots():
    out = []
    cfg = {}
    try:
        import xg_paths
        cfg = (xg_paths.config() or {}).get("capture") or {}
    except Exception:
        cfg = {}
    for v in (os.environ.get("XG_MUMU_DIR"), cfg.get("mumu_dir")):
        if v:
            out.append(Path(str(v)).expanduser())
    out.extend(_registry_roots())
    envs = [os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
            os.environ.get("LOCALAPPDATA"), os.environ.get("ProgramData")]
    names = ["Netease/MuMuPlayer-12.0", "Netease/MuMuPlayer", "MuMuPlayer-12.0",
             "MuMuPlayer", "MuMu"]
    for base in envs:
        if not base:
            continue
        for name in names:
            out.append(Path(base) / name)
    for fixed in (r"C:\Program Files\Netease\MuMuPlayer-12.0",
                  r"C:\Program Files (x86)\Netease\MuMuPlayer-12.0",
                  r"D:\Program Files\Netease\MuMuPlayer-12.0",
                  r"D:\GAME\MuMuPlayer",
                  r"D:\MuMuPlayer-12.0"):
        out.append(Path(fixed))
    seen, uniq = set(), []
    for p in out:
        try:
            key = str(p).lower()
        except Exception:
            continue
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    return [p for p in uniq if p.is_dir()]


def _adb_under(root):
    direct = [root / "shell" / "adb.exe", root / "nx_device" / "12.0" / "shell" / "adb.exe",
              root / "nx_main" / "shell" / "adb.exe", root / "adb.exe",
              root / "emulator" / "nemu" / "shell" / "adb.exe"]
    for p in direct:
        if p.is_file():
            return p
    # 有界限的深搜：最多 5 层，找到第一个 adb.exe
    try:
        root = Path(root)
        for p in root.rglob("adb.exe"):
            try:
                if len(p.relative_to(root).parts) <= 5:
                    return p
            except ValueError:
                continue
    except OSError:
        pass
    return None


def find_adb():
    """返回 (adb 路径, MuMu 目录或 None)；找不到返回 (None, None)。"""
    cfg = {}
    try:
        import xg_paths
        cfg = (xg_paths.config() or {}).get("capture") or {}
    except Exception:
        cfg = {}
    for v in (os.environ.get("XG_ADB"), cfg.get("adb")):
        if v:
            p = Path(str(v)).expanduser()
            if p.is_file():
                return p, p.parent.parent if p.parent.name.lower() == "shell" else p.parent
    for root in candidate_roots():
        p = _adb_under(root)
        if p:
            return p, root
    found = shutil.which("adb")
    if found:
        return Path(found), None
    return None, None


def _connect(adb, serial):
    code, out, err = adb_run(adb, "connect", serial, timeout=4)
    text = (out + " " + err).lower()
    return ("connected" in text or "already" in text or code == 0 and "cannot" not in text)


def find_game(adb, serials=None):
    """返回 {"serial","pid"}；找不到返回 None。"""
    serials = list(serials or [])
    env_serial = os.environ.get("XG_ADB_SERIAL")
    if env_serial:
        serials.insert(0, env_serial)
    try:
        import xg_paths
        cfg_serial = ((xg_paths.config() or {}).get("capture") or {}).get("serial")
        if cfg_serial:
            serials.insert(0, str(cfg_serial))
    except Exception:
        pass
    serials.extend(f"127.0.0.1:{p}" for p in DEFAULT_PORTS)
    seen = set()
    for serial in serials:
        if not serial or serial in seen:
            continue
        seen.add(serial)
        if not _connect(adb, serial):
            continue
        code, out, _ = adb_run(adb, "shell", "pidof " + PACKAGE, serial=serial, timeout=8)
        if code == 0 and re.fullmatch(r"\d+", out or ""):
            return {"serial": serial, "pid": int(out)}
    return None


def ensure_root(adb, serial):
    code, out, _ = adb_run(adb, "shell", "id", serial=serial, timeout=8)
    if "uid=0" in (out or ""):
        return True
    adb_run(adb, "root", serial=serial, timeout=8)
    time.sleep(2)
    _connect(adb, serial)
    adb_run(adb, "wait-for-device", serial=serial, timeout=15)
    code, out, _ = adb_run(adb, "shell", "id", serial=serial, timeout=8)
    return "uid=0" in (out or "")


def game_version(adb, serial):
    code, out, _ = adb_run(adb, "shell", "dumpsys package " + PACKAGE, serial=serial, timeout=12)
    m = re.search(r"\bversionCode=(\d+)", out or "")
    return m.group(1) if m else None


def current_scene(adb, serial, pid, revision="V75885"):
    """返回 (scene, level_id) 或 (None, None)。"""
    code, out, _ = adb_run(adb, "logcat", "-d", "-v", "threadtime", "--pid=" + str(pid),
                           serial=serial, timeout=20)
    if code != 0:
        return None, None
    lines = [ln for ln in out.splitlines()
             if (m := re.match(r"^\d\d-\d\d \d\d:\d\d:\d\d\.\d{3}\s+(\d+)\s+", ln))
             and int(m.group(1)) == int(pid)]
    try:
        rows = native_log.bind(lines, revision, "entered-world")
        return (rows[-1]["scene"], rows[-1]["level_id"]) if rows else (None, None)
    except Exception:
        return None, None


def check_device(adb_path=None):
    """首次向导第 2 步：一次性检查 adb / MuMu / root / 游戏版本。"""
    adb, mumu = (Path(adb_path), None) if adb_path else find_adb()
    out = {"ok": False, "adb": str(adb) if adb else None, "mumu_dir": str(mumu) if mumu else None,
           "serial": None, "pid": None, "root": False, "version_code": None, "scene": None,
           "game_running": False, "message": ""}
    if not adb:
        out["message"] = "没找到 adb：MuMu 是不是没装？也可以在上面的输入框手填 adb.exe 路径。"
        return out
    game = find_game(adb)
    if not game:
        out["message"] = ("找到了 adb（%s），但没找到正在运行的光遇。"
                          "先在 MuMu 里装好并打开游戏、登进遇境，再点一次检查。" % adb)
        return out
    out.update(serial=game["serial"], pid=game["pid"], game_running=True)
    out["root"] = ensure_root(adb, game["serial"])
    if not out["root"]:
        out["message"] = ("找到了 MuMu（%s）和游戏，但拿不到 root。"
                          "在 MuMu 设置 → 其他设置里打开「手机 Root 权限」，重启模拟器后再试。"
                          % game["serial"])
        return out
    out["version_code"] = game_version(adb, game["serial"])
    out["scene"], _ = current_scene(adb, game["serial"], game["pid"])
    if out["version_code"] and out["version_code"] != "421589":
        out["message"] = ("MuMu 里装的光遇版本是 %s，小光只适配 0.16.7 / 421589。"
                          "换成这个版本的安装包再装一次。" % out["version_code"])
        return out
    out["ok"] = True
    out["message"] = "MuMu、adb、Root、游戏都正常。"
    return out
