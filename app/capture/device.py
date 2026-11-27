# -*- coding: utf-8 -*-
"""找 MuMu / adb、确认 root、认当前地图。

显式 XG_ADB / 配置手填 adb 路径始终优先，不因自动探测而替换。
自动发现运行中的 MuMu / Nemu / nx_ 安装目录、注册表、常见位置和 PATH；
支持 nx_device/<任意版本>/shell/adb.exe、nx_main/adb.exe 及旧 shell 布局。
自动候选按可见的 device 状态、运行安装目录、运行版本亲和及数字版本排序；
目录枚举和 devices 探测都有上限，不遍历整盘、不重启已有 adb server。
每次检查重新读取 MuMuManager，旧版再回退到设备列表及传统本机端口。
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
from .device_scope import is_supported_emulator_serial, is_supported_emulator_tcp_serial
from .mumu_info import parse_instances
import xg_adb
import xg_exec
import xg_paths

PACKAGE = "com.netease.sky"
DEFAULT_PORTS = [16384 + 32 * i for i in range(16)] + [7555]
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _decode_cli(value):
    if not value:
        return ""
    for encoding in (("utf-16",) if value.startswith((b"\xff\xfe", b"\xfe\xff")) else
                     ("utf-8-sig", "gb18030")):
        try:
            return value.decode(encoding).strip()
        except UnicodeDecodeError:
            continue
    return value.decode("utf-8", "replace").strip()


def _run(argv, timeout=20, *, adb_prefix=None, cwd=None):
    try:
        if adb_prefix is None:
            p = xg_exec.run(argv, capture_output=True, timeout=timeout, **({"cwd": cwd} if cwd else {}))
        else:
            p = xg_adb.run(adb_prefix, argv, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return -1, "", str(exc)
    return p.returncode, _decode_cli(p.stdout), _decode_cli(p.stderr)


def adb_argv(path):
    """adb 可以是 exe，也可以是测试用的 .py/.cmd。"""
    p = str(path)
    low = p.lower()
    if low.endswith(".py"):
        return [sys.executable, p]
    if low.endswith((".cmd", ".bat")):
        return [os.environ.get("COMSPEC", "cmd.exe"), "/c", p]
    return [xg_paths.long_path(p)]


def adb_run(adb, *args, serial=None, timeout=20):
    argv = [*(["-s", serial] if serial else []), *args]
    return _run(argv, timeout=timeout, adb_prefix=adb_argv(adb))


# 自动发现的边界：不递归搜索磁盘，也不逐个无限等待 ADB。
MAX_PROCESS_ENTRIES = 4096
MAX_ROOTS = 96
MAX_DIRECTORY_ENTRIES = 128
MAX_FALLBACK_DIRECTORIES = 64
MAX_FALLBACK_ENTRIES = 1024
MAX_ADB_CANDIDATES = 96
MAX_ADB_PROBES = 8
ADB_PROBE_BUDGET = 8.0
GAME_DISCOVERY_BUDGET = 45.0


def _capture_config():
    try:
        import xg_paths
        value = (xg_paths.config() or {}).get("capture") or {}
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _path_key(path):
    return os.path.normcase(os.path.abspath(str(path))).casefold()


def _unique_paths(paths, limit=MAX_ROOTS):
    seen, result = set(), []
    for value in paths:
        try:
            path = Path(value)
            key = _path_key(path)
        except (TypeError, ValueError, OSError):
            continue
        if key not in seen:
            seen.add(key)
            result.append(path)
            if len(result) >= limit:
                break
    return result


def _process_executable_paths(api=None):
    """只用 Windows 系统 API；权限不足的进程跳过，所有句柄及时关闭。

    api 参数用于离线测试；正常调用不会依赖 psutil、PowerShell 或第三方模块。
    """
    if api is None and os.name != "nt":
        return []
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]

    if api is None:
        try:
            api = ctypes.WinDLL("kernel32", use_last_error=True)
            declarations = (
                ("CreateToolhelp32Snapshot", [wintypes.DWORD, wintypes.DWORD], wintypes.HANDLE),
                ("Process32FirstW", [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)], wintypes.BOOL),
                ("Process32NextW", [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)], wintypes.BOOL),
                ("OpenProcess", [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
                ("QueryFullProcessImageNameW", [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                               ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
                ("CloseHandle", [wintypes.HANDLE], wintypes.BOOL),
            )
            for name, args, result in declarations:
                function = getattr(api, name)
                function.argtypes, function.restype = args, result
        except (AttributeError, OSError):
            return []

    def close(handle):
        try:
            api.CloseHandle(handle)
        except (OSError, ValueError, AttributeError):
            pass

    try:
        snapshot = api.CreateToolhelp32Snapshot(0x00000002, 0)
    except (OSError, ValueError, AttributeError):
        return []
    invalid = ctypes.c_void_p(-1).value
    handle_value = getattr(snapshot, "value", snapshot)
    if handle_value in (None, 0, -1, invalid):
        return []
    paths = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        more = api.Process32FirstW(snapshot, ctypes.byref(entry))
        count = 0
        while more and count < MAX_PROCESS_ENTRIES:
            count += 1
            handle = None
            try:
                handle = api.OpenProcess(0x1000, False, entry.th32ProcessID)
                if getattr(handle, "value", handle):
                    length = wintypes.DWORD(32768)
                    buffer = ctypes.create_unicode_buffer(length.value)
                    if api.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
                        path = Path(buffer.value)
                        if path.is_absolute() and (path.name.casefold() == "adb.exe" or
                                                  any(token in str(path).casefold() for token in ("mumu", "nemu", "nx_"))):
                            paths.append(path)
            except (OSError, ValueError, AttributeError):
                pass
            finally:
                if getattr(handle, "value", handle):
                    close(handle)
            more = api.Process32NextW(snapshot, ctypes.byref(entry))
    except (OSError, ValueError, AttributeError):
        pass
    finally:
        close(snapshot)
    return _unique_paths(paths, limit=64)


def _running_mumu_roots(process_paths=None):
    """从完整进程路径最多向上六级找明确布局，优先安装根而非内部 shell。"""
    if process_paths is None:
        process_paths = _process_executable_paths()
    roots = []
    for executable in list(process_paths)[:64]:
        path = Path(executable)
        if not any(token in str(path).casefold() for token in ("mumu", "nemu", "nx_")):
            continue
        selected, fallback = None, None
        for ancestor in list(path.parents)[:6]:
            if ancestor == ancestor.parent:
                break
            try:
                if _is_dir(ancestor / "nx_device") or _is_dir(ancestor / "nx_main"):
                    selected = ancestor
                    break
                if fallback is None and _is_dir(ancestor / "shell"):
                    fallback = ancestor
            except OSError:
                continue
        if selected is not None or fallback is not None:
            roots.append(selected if selected is not None else fallback)
    return _unique_paths(roots, limit=32)


def _registry_roots():
    roots = []
    if os.name != "nt":
        return roots
    try:
        import winreg
    except ImportError:
        return roots
    # 枚举厂商的直接子项以兼容未知版本，不扫描整个注册表。
    keys = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for vendor in (r"SOFTWARE\Netease", r"SOFTWARE\WOW6432Node\Netease"):
            keys.extend((hive, vendor + "\\" + name) for name in ("MuMuPlayer", "MuMuPlayer-12.0", "MuMuPlayerX", "MuMuPlayer-6.0", "Nemu"))
            try:
                with winreg.OpenKey(hive, vendor) as key:
                    for index in range(MAX_DIRECTORY_ENTRIES):
                        try:
                            name = winreg.EnumKey(key, index)
                        except OSError:
                            break
                        if any(token in name.casefold() for token in ("mumu", "nemu", "nx_")):
                            keys.append((hive, vendor + "\\" + name))
            except OSError:
                continue
    seen = set()
    for hive, key_name in keys:
        if (hive, key_name.casefold()) in seen:
            continue
        seen.add((hive, key_name.casefold()))
        try:
            with winreg.OpenKey(hive, key_name) as key:
                for name in ("InstallPath", "InstallDir", "Path", "InstallLocation"):
                    try:
                        value, _ = winreg.QueryValueEx(key, name)
                    except OSError:
                        continue
                    if isinstance(value, str) and value.strip():
                        roots.append(Path(os.path.expandvars(value.strip().strip('"'))))
        except OSError:
            continue
    return _unique_paths(roots)


def _registry_uninstall_roots():
    """Find custom-drive 6/X/12 installations via the installer index, not a disk scan."""
    if os.name != "nt":
        return []
    import winreg
    roots = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for subkey in (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
                       r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"):
            try:
                with winreg.OpenKey(hive, subkey) as parent:
                    for index in range(512):
                        try:
                            name = winreg.EnumKey(parent, index)
                        except OSError:
                            break
                        try:
                            with winreg.OpenKey(parent, name) as key:
                                display, _ = winreg.QueryValueEx(key, "DisplayName")
                                if not re.search(r"mumu|nemu|网易模拟器", str(display), re.I):
                                    continue
                                try:
                                    location, _ = winreg.QueryValueEx(key, "InstallLocation")
                                except OSError:
                                    location = ""
                                if isinstance(location, str) and location.strip().strip('"'):
                                    roots.append(Path(os.path.expandvars(location.strip().strip('"'))))
                                    continue
                                icon, _ = winreg.QueryValueEx(key, "DisplayIcon")
                                match = re.fullmatch(r'\s*"?(.*?\.exe)"?(?:,[-+]?\d+)?\s*', str(icon), re.I)
                                if match:
                                    roots.append(Path(os.path.expandvars(match[1])).parent)
                        except OSError:
                            continue
            except OSError:
                continue
    return _unique_paths(roots)


def _hint_roots(adb_or_manager):
    path = Path(adb_or_manager)
    roots = [path.parent]
    for parent in list(path.parents)[:6]:
        if parent == parent.parent:
            break
        name = parent.name.casefold()
        if (name in ("nx_main", "nx_device", "shell", "nemu", "emulator", "bin", "platform-tools")
                or any(token in name for token in ("mumu", "nemu", "netease"))):
            roots.append(parent.parent)
    return [p for p in _unique_paths(roots, limit=8) if p != p.parent]


def _manager_on_path():
    found = shutil.which("MuMuManager.exe")
    return Path(found) if found and Path(found).name.casefold() == "mumumanager.exe" else None


def _child_directories(root):
    result = []
    try:
        with os.scandir(xg_paths.long_path(root)) as entries:
            for index, entry in enumerate(entries):
                if index >= MAX_DIRECTORY_ENTRIES:
                    break
                try:
                    if entry.is_dir(follow_symlinks=False):
                        result.append(Path(root) / entry.name)
                except OSError:
                    continue
    except OSError:
        pass
    return result


def candidate_roots(*, running_roots=None):
    cfg = _capture_config()
    out = [p for v in (os.environ.get("XG_MUMU_DIR"), cfg.get("mumu_dir")) if (p := _configured_path(v))]
    out.extend(_running_mumu_roots() if running_roots is None else running_roots)
    out.extend(_registry_roots())
    out.extend(_registry_uninstall_roots())
    for manager in (_configured_path(os.environ.get("XG_MUMU_MANAGER") or cfg.get("mumu_manager")),
                    _manager_on_path()):
        if manager and _is_file(manager):
            out.extend(_hint_roots(manager))
    envs = [os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
            os.environ.get("LOCALAPPDATA"), os.environ.get("ProgramData")]
    names = ["Netease/MuMuPlayer-12.0", "Netease/MuMuPlayer", "Netease/MuMuPlayerX", "Netease/MuMuPlayer-6.0",
             "Netease/Nemu", "MuMuPlayer-12.0", "MuMuPlayerX", "MuMuPlayer", "MuMu", "Nemu"]
    for base in envs:
        if not base:
            continue
        out.extend(Path(base) / name for name in names)
        for parent in (Path(base), Path(base) / "Netease"):
            out.extend(path for path in _child_directories(parent)
                       if any(token in path.name.casefold() for token in ("mumu", "nemu", "nx_")))
    out.extend(Path(fixed) for fixed in (
        r"C:\Program Files\Netease\MuMuPlayer-12.0",
        r"C:\Program Files (x86)\Netease\MuMuPlayer-12.0",
        r"D:\Program Files\Netease\MuMuPlayer-12.0", r"D:\GAME\MuMuPlayer", r"D:\MuMuPlayer-12.0"))
    result = []
    for path in _unique_paths(out):
        try:
            if _is_dir(path):
                result.append(path)
        except OSError:
            continue
    return result


def _version_key(path):
    """数字而非字符串比较：15.10 > 15.9 > 12.0。"""
    parts = Path(path).parts
    version = ""
    for index, part in enumerate(parts[:-1]):
        if part.casefold() == "nx_device":
            version = parts[index + 1]
            break
    if not version:
        for part in reversed(parts):
            match = re.search(r"(?:mumuplayer|nemu)[-_](\d[\w.-]*)", part, re.I)
            if match:
                version = match.group(1)
                break
    numbers = [int(value) for value in re.findall(r"\d+", version)[:8]]
    return tuple((numbers + [0] * 8)[:8])


def _bounded_adb_fallback(root):
    """旧布局兼容：路径深度 <= 5，总目录/条目限额，不跟随链接或重解析点。"""
    root = Path(root)
    pending, found, visited, entries_seen = [(root, 0)], [], 0, 0
    while pending and visited < MAX_FALLBACK_DIRECTORIES and entries_seen < MAX_FALLBACK_ENTRIES:
        directory, depth = pending.pop(0)
        try:
            info = Path(xg_paths.long_path(directory)).lstat()
            if Path(xg_paths.long_path(directory)).is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                continue
            visited += 1
            with os.scandir(xg_paths.long_path(directory)) as entries:
                for entry in entries:
                    entries_seen += 1
                    if entries_seen > MAX_FALLBACK_ENTRIES:
                        break
                    try:
                        info = entry.stat(follow_symlinks=False)
                        if entry.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                            continue
                        if entry.name.casefold() == "adb.exe" and depth + 1 <= 5 and entry.is_file(follow_symlinks=False):
                            found.append(directory / entry.name)
                            if len(found) >= MAX_ADB_CANDIDATES:
                                return found
                        elif depth + 1 < 5 and entry.is_dir(follow_symlinks=False):
                            if len(pending) + visited < MAX_FALLBACK_DIRECTORIES:
                                pending.append((directory / entry.name, depth + 1))
                    except OSError:
                        continue
        except OSError:
            continue
    return found


def _adb_candidates_under(root):
    """先查已知布局和 nx_device 版本；未命中才进行有界旧布局搜索。"""
    root = Path(root)
    versions = sorted(_child_directories(root / "nx_device"),
                      key=lambda path: (_version_key(path), path.name.casefold()), reverse=True)
    direct = [path / "shell" / "adb.exe" for path in versions]
    direct.extend([root / "shell" / "adb.exe", root / "nx_main" / "adb.exe",
                   root / "nx_main" / "shell" / "adb.exe", root / "adb.exe",
                   root / "emulator" / "nemu" / "shell" / "adb.exe",
                   root / "nemu" / "shell" / "adb.exe", root / "sdk" / "platform-tools" / "adb.exe"])
    result = []
    for path in _unique_paths(direct, limit=MAX_ADB_CANDIDATES):
        try:
            if _is_file(path):
                result.append(path)
        except OSError:
            continue
    return sorted(result or _bounded_adb_fallback(root), key=_version_key, reverse=True)


def _adb_under(root):
    """兼容旧调用：返回数字版本最高的已发现 adb 或 None。"""
    paths = _adb_candidates_under(root)
    return paths[0] if paths else None


def _list_adb_devices(adb, timeout=2.0):
    code, out, err = adb_run(adb, "devices", timeout=timeout)
    states, header = {}, False
    if code == 0:
        for line in out.splitlines():
            if line.strip() == "List of devices attached":
                header = True
                continue
            columns = line.split()
            if (header and len(columns) >= 2 and not columns[0].endswith(":")
                    and columns[1] in ("device", "offline", "unauthorized", "bootloader", "recovery", "unknown")):
                states.setdefault(columns[0], columns[1])
                if len(states) >= 32:
                    break
    return code, states, err


def _probe_adb_devices(adb, timeout=2.0):
    _, states, _ = _list_adb_devices(adb, timeout=timeout)
    return [serial for serial, state in states.items() if state == "device"]


def _bundled_adb_paths():
    install = Path(__file__).resolve().parents[2]
    choices = [install / layout for layout in ("runtime/platform-tools/adb.exe", "runtime/adb/adb.exe",
                                                "runtime/adb.exe", "app/bin/adb.exe")]
    return [path for path in choices if _is_file(path)]


def _is_file(path):
    try:
        return Path(xg_paths.long_path(path)).is_file()
    except (OSError, ValueError):
        return False


def _is_dir(path):
    try:
        return Path(xg_paths.long_path(path)).is_dir()
    except (OSError, ValueError):
        return False


def _configured_path(value):
    if not isinstance(value, (str, Path)):
        return None
    value = str(value).strip().strip('"')
    if not value or "\x00" in value:
        return None
    try:
        return Path(os.path.expandvars(value)).expanduser()
    except (TypeError, ValueError, OSError, RuntimeError):
        return None


def _mumu_root_for_adb(path):
    roots = _hint_roots(path)
    return next((root for root in roots if _is_dir(root / "nx_main") or _is_dir(root / "nx_device")),
                path.parent.parent if path.parent.name.casefold() == "shell" else path.parent)


def find_adb():
    """返回 (adb 路径, MuMu 目录或 None)；手填优先，自动候选有界探测。"""
    cfg = _capture_config()
    for value in (os.environ.get("XG_ADB"), cfg.get("adb")):
        path = _configured_path(value)
        if path and _is_file(path):
            return path, _mumu_root_for_adb(path)
    processes = _process_executable_paths()
    running = _running_mumu_roots(processes)
    running_keys = {_path_key(path) for path in running}
    records, seen = [], set()
    for root in candidate_roots(running_roots=running):
        for path in _adb_candidates_under(root):
            key = _path_key(path)
            if key in seen:
                continue
            seen.add(key)
            is_running = _path_key(root) in running_keys
            affinity = is_running and any(
                _path_key(process).startswith(_path_key(root) + os.sep)
                and _version_key(process) == _version_key(path) and any(_version_key(path))
                for process in processes)
            records.append((path, root, is_running, affinity))
            if len(records) >= MAX_ADB_CANDIDATES:
                break
        if len(records) >= MAX_ADB_CANDIDATES:
            break
    records.sort(key=lambda row: (row[2], row[3], _version_key(row[0])), reverse=True)
    # Installed MuMu packages are preferred; a runtime-bundled ADB, the existing
    # local ADB server executable, or PATH is a fallback rather than an upgrade.
    vendor_count = len(records)
    fallback = [path for path in processes if path.name.casefold() == "adb.exe"] + _bundled_adb_paths()
    found = shutil.which("adb")
    if found:
        fallback.append(Path(found))
    for path in _unique_paths(fallback, limit=16):
        if _path_key(path) not in seen:
            seen.add(_path_key(path))
            records.append((path, None, False, False))
    if not records:
        return None, None
    deadline = time.monotonic() + ADB_PROBE_BUDGET
    best = records[0]
    first = records[:MAX_ADB_PROBES]
    if vendor_count >= MAX_ADB_PROBES and len(records) > vendor_count:
        # Reserve at least one probe for the running compatible server/client.
        first = records[:MAX_ADB_PROBES - 1] + records[vendor_count:vendor_count + 1]
    for row in first:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if any(is_supported_emulator_serial(serial)
               for serial in _probe_adb_devices(row[0], timeout=min(2.0, remaining))):
            # records 已按运行目录/版本排序，首个就绪项即最优项。
            best = row
            break
    return best[0], best[1]


def _connect(adb, serial, timeout=4):
    if not is_supported_emulator_tcp_serial(serial):
        return False
    code, out, err = adb_run(adb, "connect", serial, timeout=timeout)
    reply = (out + " " + err).lower()
    return code == 0 and not any(word in reply for word in ("cannot", "failed", "unable", "refused"))


def _managers_in(roots):
    candidates = []
    for root in _unique_paths(roots, limit=24):
        choices = [root / folder / "MuMuManager.exe" for folder in ("", "nx_main", "shell", "bin")]
        choices.extend(p / "shell" / "MuMuManager.exe" for p in
                       sorted(_child_directories(root / "nx_device"), key=_version_key, reverse=True)[:4])
        candidates.extend(path for path in choices if _is_file(path))
    return candidates


def discover_mumu_instances(adb, mumu_dir=None, *, timeout=6.0):
    """Inventory is never cached: a bridge DHCP lease can change on every guest boot."""
    cfg = _capture_config()
    hints = [mumu_dir, os.environ.get("XG_MUMU_DIR"), cfg.get("mumu_dir"), *_hint_roots(adb)]
    explicit = _configured_path(os.environ.get("XG_MUMU_MANAGER") or cfg.get("mumu_manager"))
    managers = [explicit] if explicit and _is_file(explicit) else []
    managers += _managers_in(path for path in hints if path)
    on_path = _manager_on_path()
    if on_path:
        managers.append(on_path)
    if not managers:
        managers += _managers_in(candidate_roots())
    deadline = time.monotonic() + timeout
    for manager in _unique_paths(managers, limit=4):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        code, out, err = _run([xg_paths.long_path(manager), "info", "-v", "all"], timeout=min(3.0, remaining),
                              cwd=xg_paths.long_path(manager.parent))
        if code == 0:
            instances = parse_instances(out) or parse_instances(err)
            if instances:
                return instances
    return ()


DEVICE_MESSAGES = {
    "adb_unavailable": "adb 无法运行或响应超时。请在 MuMu 安装目录重新选取自带的 adb.exe；若系统拦截，请查看 Windows 安全中心的保护历史记录，然后重试。",
    "adb_server_version_conflict": "当前有另一版本的 adb 服务在运行。请正常关闭其他模拟器或 Android Studio 后重试，或手动选择与当前服务同版本的 adb.exe。小光没有停止现有服务。",
    "mumu_not_started": "MuMu 实例还未启动或 Android 仍在启动。请从 MuMu 多开器打开所需实例，等待 Android 桌面出现后重试。",
    "mumu_adb_disabled": "MuMu 的 ADB 调试已关闭。请在所选实例的设置中开启 ADB 调试，重启该实例，再点检查。",
    "mumu_adb_unauthorized": "MuMu 的 ADB 连接等待授权。请打开模拟器窗口并确认 Android 的调试授权，再点检查。",
    "mumu_adb_offline": "MuMu 已被 adb 识别，但设备不在线。请等待 Android 桌面就绪；仍离线时重启该实例再试。",
    "mumu_not_connected": "adb 没发现在线的 MuMu。请打开模拟器，确认实例开启 ADB 调试；网络桥接时请确认 Windows 能访问该实例，或切回 NAT 后重启再试。",
    "unsupported_device_scope": "小光只支持本机 MuMu 或私网桥接 MuMu，不支持 USB 真机和公网地址。请在 MuMu 中打开光遇，再点检查。",
    "game_not_installed": "已连接 MuMu，但这个实例没有安装光遇官服。请把当前客户端支持的光遇官服装到这个实例，然后打开游戏。",
    "game_state_unknown": "已连接 MuMu，但没读取到光遇运行状态。请确认 Android 桌面已加载，使用 MuMu 自带的 adb.exe，再点检查。",
    "game_not_running": "已连接 MuMu，但光遇官服还没运行。请在模拟器里打开光遇、进入遇境，再点检查。",
    "game_in_background": "光遇已启动但不在前台。请切回模拟器里的光遇游戏画面、进入遇境，然后重试。",
    "game_channel_unsupported": "这个 MuMu 实例安装的是光遇渠道包，当前录制和数据生成只验证了网易官服。请将受支持版本的官服装到 MuMu 后重试。",
}


def _foreground_packages(adb, serial, timeout):
    code, out, _ = adb_run(adb, "shell", "dumpsys activity activities", serial=serial, timeout=timeout)
    if code == 0:
        for label in ("topResumedActivity", "mResumedActivity", "ResumedActivity"):
            packages = set(re.findall(rf"^\s*{label}\s*[:=]\s*[^\n]*?\b([A-Za-z0-9_.]+)/",
                                      out, flags=re.M))
            if packages:
                return packages
    return set()


def is_game_foreground(adb, serial, package=PACKAGE, *, timeout=3):
    """Return None when this Android build does not report reliable activity state."""
    if package != PACKAGE or not is_supported_emulator_serial(serial):
        return None
    foreground = _foreground_packages(adb, serial, timeout)
    return package in foreground if foreground else None


def _android_details(rows):
    # Never show another VM's Android version as the checked instance's version.
    versions = {row.android_version if row is not None else None for row in rows}
    if len(versions) == 1 and (version := next(iter(versions))):
        return {"android_version": version, "android_label": f"安卓 {version}"}
    return {}


def find_game(adb, serials=None, *, mumu_dir=None, diagnostics=None):
    """Find an online, in-scope MuMu running the supported official package."""
    if diagnostics is None:
        diagnostics = {}
    diagnostics.clear()
    cfg = _capture_config()
    environment_serial = os.environ.get("XG_ADB_SERIAL", "").strip()
    configured = str(cfg.get("serial") or "").strip()
    requested = list(dict.fromkeys(str(value).strip() for value in (serials or []) if value))[:32]
    explicit = [value for value in [environment_serial, *requested] if value]
    deadline = time.monotonic() + GAME_DISCOVERY_BUDGET
    instances = discover_mumu_instances(adb, mumu_dir)

    def instance_state(reason, rows):
        for serial in explicit:
            if matched := [row for row in rows if row and serial in row.candidates]:
                rows = matched
                break
        diagnostics.update(reason=reason, message=DEVICE_MESSAGES[reason], **_android_details(rows))

    code, states, err = _list_adb_devices(adb)
    if code != 0:
        reason = ("adb_server_version_conflict" if xg_adb.SERVER_CONFLICT in err else "adb_unavailable")
        instance_state(reason, instances)
        return None

    managed = {serial: row for row in instances if row.running is not False and row.adb_enabled is not False
               for serial in row.candidates}
    inactive = {serial for row in instances if row.running is False or row.adb_enabled is False
                for serial in row.candidates} - managed.keys()
    ready = [serial for serial, state in states.items() if state == "device"
             and is_supported_emulator_serial(serial) and serial not in inactive]
    unknown_available = bool(ready)
    seen = set()
    problems = {}

    def inspect(serial, connect_timeout):
        nonlocal unknown_available
        if serial in seen or serial in inactive or not is_supported_emulator_serial(serial):
            return None
        seen.add(serial)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        if serial not in ready:
            if not _connect(adb, serial, timeout=min(connect_timeout, remaining)):
                return None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            # A textual connect success does not prove the resulting device is online.
            _, new_states, _ = _list_adb_devices(adb, timeout=min(2.0, remaining))
            states.update(new_states)
            if new_states.get(serial) != "device":
                return None
            ready.append(serial)
        unknown_available = True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        metadata = managed.get(serial)
        pid = game_pid(adb, serial, timeout=min(8.0, remaining))
        remaining = deadline - time.monotonic()
        if pid:
            foreground = is_game_foreground(adb, serial, timeout=min(3.0, remaining)) if remaining > 0 else None
            if foreground is False:
                problems.setdefault("game_in_background", (PACKAGE, metadata))
                return None
            diagnostics.update(root_enabled=metadata.root_enabled if metadata else None,
                               adb_enabled=metadata.adb_enabled if metadata else None, package=PACKAGE,
                               **_android_details((metadata,)))
            return {"serial": serial, "pid": pid}
        if remaining > 0:
            rc, packages, _ = adb_run(adb, "shell", "pm list packages " + PACKAGE, serial=serial,
                                       timeout=min(4.0, remaining))
            if rc == 0:
                names = set(re.findall(r"^package:(com\.netease\.sky(?:\.[A-Za-z0-9_]+)*)\s*$",
                                       packages, flags=re.M))
                if PACKAGE in names:
                    problems.setdefault("game_not_running", (PACKAGE, metadata))
                elif names:
                    problems.setdefault("game_channel_unsupported", (sorted(names)[0], metadata))
                else:
                    problems.setdefault("game_not_installed", (None, metadata))
        return None

    # Explicit user selection first. A live manager inventory takes precedence
    # over the last saved bridge address (which DHCP may have reassigned).
    if managed:
        listed = list(dict.fromkeys([*(s for s in explicit if s in ready), *(s for s in managed if s in ready),
                                     *explicit, *managed, configured, *ready]))
    else:
        preferred = [environment_serial, configured, *requested]
        listed = list(dict.fromkeys([*(s for s in preferred if s in ready), *ready, *preferred]))
    legacy = not managed and not (instances and all(i.running is False or i.adb_enabled is False for i in instances))
    if legacy:
        listed += [f"127.0.0.1:{port}" for port in DEFAULT_PORTS]
    for serial in listed:
        if time.monotonic() >= deadline:
            break
        game = inspect(serial, 4.0 if serial in managed or serial in explicit else 1.0)
        if game:
            return game

    for reason in ("game_in_background", "game_channel_unsupported", "game_not_running", "game_not_installed"):
        if reason in problems:
            package, metadata = problems[reason]
            diagnostics.update(reason=reason, message=DEVICE_MESSAGES[reason], package=package,
                               **_android_details((metadata,)))
            return None
    local_states = [state for serial, state in states.items() if is_supported_emulator_serial(serial)]
    if unknown_available:
        instance_state("game_state_unknown", [managed.get(serial) for serial in ready])
    elif instances and all(row.running is False for row in instances):
        instance_state("mumu_not_started", instances)
    elif (active := [row for row in instances if row.running is not False]) and all(
            row.adb_enabled is False for row in active):
        instance_state("mumu_adb_disabled", active)
    elif "unauthorized" in local_states:
        instance_state("mumu_adb_unauthorized", [managed.get(serial) for serial, state in states.items()
                                                if state == "unauthorized" and is_supported_emulator_serial(serial)])
    elif "offline" in local_states:
        instance_state("mumu_adb_offline", [managed.get(serial) for serial, state in states.items()
                                           if state == "offline" and is_supported_emulator_serial(serial)])
    elif any(not is_supported_emulator_serial(s) for s in [*states, *explicit, configured] if s):
        instance_state("unsupported_device_scope", ())
    else:
        instance_state("mumu_not_connected", instances)
    return None


def game_pid(adb, serial, package=PACKAGE, *, timeout=8):
    from .android_compat import find_package_pid
    from .device_scope import is_supported_emulator_serial

    if not is_supported_emulator_serial(serial):
        return None
    deadline = time.monotonic() + timeout

    def shell(command):
        remaining = deadline - time.monotonic()
        return (adb_run(adb, "shell", command, serial=serial, timeout=remaining)
                if remaining > 0 else (-1, "", ""))

    return find_package_pid(shell, package)


def ensure_root(adb, serial, *, root_enabled=None, diagnostics=None):
    """Use already granted root, or request the emulator's normal ADB root mode."""
    import xg_adb
    from . import errors
    from .android_compat import adb_connection_error, detect_root_shell
    from .device_scope import is_supported_emulator_serial, is_supported_emulator_tcp_serial

    def fail(reason):
        if diagnostics is not None:
            error_name = {'adb_server_conflict': 'capture_adb_conflict'}.get(reason, 'capture_' + reason)
            diagnostics.update(reason=reason, message=errors.explain(error_name))
        return False

    if not is_supported_emulator_serial(serial):
        return fail('device_scope')
    if root_enabled is False:
        return fail('root_disabled')
    deadline = time.monotonic() + 28
    transport_failure = None

    def track(result):
        nonlocal transport_failure
        code, out, err = result
        msg = ((out or '') + ' ' + (err or '')).casefold()
        if xg_adb.SERVER_CONFLICT in msg:
            transport_failure = 'adb_server_conflict'
        elif adb_connection_error(out, err):
            transport_failure = 'adb_disconnected'
        elif code == -1:
            if 'timed out' in msg or '[winerror 121]' in msg:
                transport_failure = 'adb_timeout'
            elif '[winerror 2]' in msg or '[winerror 3]' in msg or '[errno 2]' in msg:
                transport_failure = 'adb_missing'
            elif any(marker in msg for marker in ('[winerror 5]', '[winerror 225]', '[winerror 226]', '[errno 13]')):
                transport_failure = 'adb_permission'
        return result

    def shell(command):
        remaining = deadline - time.monotonic()
        if transport_failure:
            return -1, '', 'device offline'  # Stop probing other root command forms.
        if remaining <= 0:
            return track((-1, '', 'timed out'))
        return track(adb_run(adb, "shell", command, serial=serial, timeout=min(5, remaining)))

    if detect_root_shell(shell):
        return True
    if transport_failure:
        return fail(transport_failure)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return fail('adb_timeout')
    code, out, err = track(adb_run(adb, "root", serial=serial, timeout=min(5, remaining)))
    response = ((out or "") + " " + (err or "")).casefold()
    if transport_failure:
        return fail(transport_failure)
    # A production adbd often prints this refusal with exit code zero.
    # The already-granted shell modes have been tried above; don't restart
    # adbd, reconnect, or treat a production build as a disabled MuMu switch.
    if re.search(r"\badbd (?:cannot|can not) run as root in (?:production|user) builds?\b", response):
        return fail('root_user_build')
    if code != 0 or any(s in response for s in ("cannot run as root", "permission denied", "not allowed")):
        return fail('root_unavailable')
    if "restarting" in response:
        if is_supported_emulator_tcp_serial(serial):
            # Give adbd time to restart before reattaching the existing TCP target.
            time.sleep(min(1.0, max(0, deadline - time.monotonic())))
            remaining = deadline - time.monotonic()
            if remaining > 0:
                _connect(adb, serial, timeout=min(4, remaining))
        remaining = deadline - time.monotonic()
        if remaining > 0:
            track(adb_run(adb, "wait-for-device", serial=serial, timeout=min(8, remaining)))
    return (True if detect_root_shell(shell, allow_su=False)
            else fail(transport_failure or 'root_unavailable'))


def game_version(adb, serial, package=PACKAGE, *, diagnostics=None):
    if package != PACKAGE or not is_supported_emulator_serial(serial):
        return None
    from xg_gamedata.android_package import package_diagnostics

    deadline = time.monotonic() + 12

    def shell(command):
        remaining = deadline - time.monotonic()
        return (adb_run(adb, "shell", command, serial=serial, timeout=remaining)
                if remaining > 0 else (-1, "", ""))

    info = package_diagnostics(shell, package, include_abi=diagnostics is not None)
    if diagnostics is not None and "native_abi" in info:
        diagnostics["native_abi"] = info["native_abi"]
    return info.get("version_code")


def guest_page_size(adb, serial):
    """Report the Android guest's page size, not Windows' page size."""
    from .android_compat import read_page_size

    if not is_supported_emulator_serial(serial):
        return None
    return read_page_size(lambda command: adb_run(adb, 'shell', command, serial=serial, timeout=4))


def current_scene(adb, serial, pid, revision="V76318", *, diagnostics=None):
    """返回 (scene, level_id) 或 (None, None)。"""
    from . import errors
    from .android_compat import read_threadtime_log
    from .device_scope import is_supported_emulator_serial

    if not is_supported_emulator_serial(serial):
        return None, None
    deadline = time.monotonic() + 20

    def run(*args):
        remaining = deadline - time.monotonic()
        return (adb_run(adb, *args, serial=serial, timeout=remaining)
                if remaining > 0 else (-1, "", ""))

    lines = read_threadtime_log(run, pid, diagnostics=diagnostics).splitlines()
    if diagnostics is not None and diagnostics.get('scene_log_reason') == 'capture_logcat_denied':
        diagnostics['scene_log_message'] = errors.explain('capture_logcat_denied')
    try:
        rows = native_log.bind(lines, revision, "entered-world")
        return (rows[-1]["scene"], rows[-1]["level_id"]) if rows else (None, None)
    except Exception:
        return None, None


def check_device(adb_path=None):
    """首次向导第 2 步：检查 adb / MuMu / root / 游戏版本，给出可重试的原因。"""
    supplied = str(adb_path or "").strip().strip('"')
    if adb_path:
        adb = _configured_path(adb_path)
        mumu = _mumu_root_for_adb(adb) if adb and _is_file(adb) else None
    else:
        adb, mumu = find_adb()
    out = {"ok": False, "adb": str(adb) if adb else None, "mumu_dir": str(mumu) if mumu else None,
           "serial": None, "pid": None, "root": False, "version_code": None, "scene": None,
           "game_running": False, "reason": "", "message": "", "android_version": None, "android_label": None}
    if not adb or (adb_path and not _is_file(adb)):
        out.update(adb=None, mumu_dir=None, reason="adb_path_invalid" if supplied else "adb_missing",
                   message=("这个 adb 路径找不到文件。请选择 MuMu 安装目录中的 adb.exe，或留空自动查找。"
                            if supplied else "没找到 adb。请先安装 MuMu，或手动选择 MuMu 自带的 adb.exe，再点检查。"))
        return out
    diagnostics = {}
    game = find_game(adb, mumu_dir=mumu, diagnostics=diagnostics)
    if not game:
        out.update(diagnostics)
        return out
    out.update(diagnostics, serial=game["serial"], pid=game["pid"], game_running=True)
    page_size = guest_page_size(adb, game['serial'])
    if page_size is not None:
        out['page_size'] = page_size
        if page_size != 4096:
            from . import errors
            out['page_size_message'] = errors.explain('capture_page_size_unsupported')
    out["version_code"] = game_version(adb, game["serial"], diagnostics=out)
    if out["version_code"] != "421629":
        version = out["version_code"]
        if version is None:
            out.update(reason="game_version_unknown", message="读取不到光遇版本。请在 MuMu 中确认装的是网易官服，打开游戏并重试。")
        else:
            detail = "（旧版 0.16.7）" if version == "421589" else ""
            out.update(reason="game_version_unsupported",
                       message=f"MuMu 中的光遇版本为 {version}{detail}，当前客户端需要网易官服 0.16.8 / 421629。请换成支持的版本再重试。")
        return out
    root_diagnostics = {}
    out["root"] = ensure_root(adb, game["serial"], root_enabled=diagnostics.get("root_enabled"),
                              diagnostics=root_diagnostics)
    if not out["root"]:
        out.update(root_diagnostics)
        return out
    out["scene"], _ = current_scene(adb, game["serial"], game["pid"], diagnostics=out)
    out.update(ok=True, message="MuMu、adb、Root 和光遇版本检查通过。")
    return out
