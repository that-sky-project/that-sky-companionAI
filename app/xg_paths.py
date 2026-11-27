# -*- coding: utf-8 -*-
"""小光跨平台路径常量（W1）。

设计原则
========
1. **服务器默认行为不变**：不设环境变量时，`ROOT` / `DATA` / `RELEASE` / `BOT` /
   `SERVER_ROOT` / `SERVER_QFSKY` / `CONTROL` / `LOGS` / `CAPTURES` /
   `RUNTIME_PRIVATE` / `RUNTIME_DATA` / `STABLE` 全部等于服务器原路径
   `<server-data-root>/...`。合回服务器时这些常量原样成立。
2. **程序与数据分开**：`ROOT` 是代码/运行根；`DATA` 是用户数据根，默认等于
   `ROOT`（非 Windows）。Windows 启动器显式传入已有数据目录；直接导入则使用桌面宿主的用户数据目录。
   所有运行时会写的东西（control/ logs/ captures/ runtime/private、
   runtime/runtime-data、地形与目录资产、配置）都在 `DATA` 下；程序目录只读。
3. **代码目录跟着包走**：`APP` = 本文件所在目录（安装版的 `app\\`）。安装版里
   bot/ qfsky/ engine/ src/ 在 `APP` 下；服务器上它们在 `ROOT` 下（qfsky 在
   runtime/source/.../server 下）。`BOT_CODE` / `QFSKY_CODE` / `ENGINE` / `SRC`
   / `OVERLAY` 自动选择存在的那一套，所以同一份代码在两边都能跑，且服务器
   仍然优先用它原来的位置。

环境变量
========
SKY_AI_ROOT   代码/运行根（服务器默认 <server-data-root>）
XG_DATA_DIR   用户数据根（默认 = ROOT）
XG_GAME_VERSION  游戏数据版本目录名（默认 0.16.8-421629；旧数据可显式设 0.16.7-421589）
XG_PYTHON     跑子进程用的解释器（默认 ROOT/.venv/bin/python，没有就用 sys.executable）
XG_NODE       显式 Node 覆盖；默认优先物理安装目录的 runtime/node/node.exe，再找 PATH
XG_RELEASE    覆盖 RELEASE
XG_SUPERVISOR 覆盖 supervisor 脚本路径
XG_PILOT_CONFIG 覆盖地形引擎 pilot config
"""
from __future__ import annotations

import json
import math
import tempfile
import os
import re
import shutil
import subprocess
import threading
import time
import sys
from pathlib import Path

# ---------------------------------------------------------------- 基本根
APP = Path(__file__).resolve().parent          # 程序目录（安装版 app\）
INSTALL_ROOT = APP.parent                     # 只随本模块物理位置，不随代码/数据环境变化
ROOT = Path(os.environ.get("SKY_AI_ROOT") or APP.parent)
def default_desktop_data():
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or Path.home()
    return Path(base) / "XiaoGuangPrivate" / "data"


DATA = Path(os.environ.get("XG_DATA_DIR") or (default_desktop_data() if os.name == "nt" else ROOT))

IS_WINDOWS = os.name == "nt"

# 只读程序位置的“服务器原值”（兼容常量；不随 APP 改变）
RELEASE = Path(os.environ.get("XG_RELEASE") or
               (ROOT / "runtime/terrain-experimental-20261104/release-v20-hop-trial"))
BOT = ROOT / "bot"
SERVER_ROOT = ROOT / "runtime/source/online/03_网页前后端源码/server"
SERVER_QFSKY = SERVER_ROOT / "qfsky"
STABLE = ROOT / "stable"

# ---------------------------------------------------------------- 用户数据
CONTROL = DATA / "control"
LOGS = DATA / "logs"
CAPTURES = DATA / "captures"
BOT_DATA = DATA / "bot"            # 服务器上 seats_*.json 等学习数据就写在 bot/ 旁
REF = DATA / "ref"
RUNTIME = DATA / "runtime"
RUNTIME_PRIVATE = RUNTIME / "private"
RUNTIME_DATA = RUNTIME / "runtime-data"
PROTOCOL_RESOURCES = RUNTIME / "protocol_results/handshake/resources"
DICTIONARY = PROTOCOL_RESOURCES / "CompressDictionary.zst"
ASSET_RELEASE = RUNTIME / "terrain-experimental-20261104/release-v20-hop-trial"
LEASE_DIR = RUNTIME / "terrain-experimental-20261104/online-asset-catalog-32maps-v32c"
STABLE_DATA = DATA / "stable"

GAME_VERSION = os.environ.get("XG_GAME_VERSION", "0.16.8-421629")
GAME_DIR = DATA / "game" / GAME_VERSION
NAMES = GAME_DIR / "names"
TERRAIN = GAME_DIR / "terrain"
CATALOG_DATA = GAME_DIR / "catalog"


def props_dir(game_version: str | None = None) -> Path:
    """用户本机生成的版本专属道具目录；绝不回退到随包 APK 几何。"""
    version = GAME_VERSION if game_version is None else game_version
    if not isinstance(version, str) or re.fullmatch(r'[0-9][A-Za-z0-9_.-]{0,79}', version) is None:
        raise ValueError('props_game_version')
    return DATA / 'game' / version / 'props'

CONFIG_FILE = DATA / "config.json"
WEB_JSON = CONTROL / "web.json"
WIZARD_TASK = CONTROL / "wizard_task.json"       # 后台长任务（names/terrain/catalog/pull-apk）
CAPTURE_STATUS = CONTROL / "capture.json"        # 录图进程状态（网页轮询）
CAPTURE_STOP = CONTROL / "capture.stop"
NATIVE_DIR = RUNTIME / "native"                  # 从用户 APK 取出的 native 库（不随包）
APK_DIR = GAME_DIR / "apk"                       # 从 MuMu 拉出来的安装包/拆分包
POSE_STATE = CONTROL / "pose.state.json"
POSE_CONTROL = CONTROL / "pose.json"
ACTIVE_ACCOUNT = CONTROL / "active_account"
OWNER_UUID = CONTROL / "owner_uuid"
ACCOUNTS_JSON = RUNTIME_DATA / "accounts.json"
PROFILES_JSON = RUNTIME_PRIVATE / "connection0167_profiles.local.json"
CURRENT_PROFILES = RUNTIME_PRIVATE / "current_profiles"

# ---------------------------------------------------------------- 程序位置（自动适配）
APP_BOT = APP / "bot"
APP_QFSKY = APP / "qfsky"
BOT_CODE = APP_BOT if APP_BOT.is_dir() else BOT
QFSKY_CODE = APP_QFSKY if APP_QFSKY.is_dir() else SERVER_QFSKY
OVERLAY = APP / "escort-v31-server" if (APP / "escort-v31-server").is_dir() else RELEASE / "escort-v31-server"
ENGINE = APP / "engine" if (APP / "engine").is_dir() else RELEASE / "engine"
_SRC_CANDS = (APP / "src", RELEASE / "src", ROOT / "src")
SRC = next((p for p in _SRC_CANDS if (p / "skyterrain").is_dir()), _SRC_CANDS[-1])
LAUNCHER = APP / "launcher"
TOOLS = APP / "tools"
WEB = APP / "web"
CONFIG_DIR = APP / "config"
ASSETS = APP / "assets"                       # 随包只读运行时资产
PROP_ASSETS = props_dir()                       # 用户本地生成；冷预载 + RAM LRU

# 运行入口（服务器优先用 release 的那份，保持原行为）
RELEASE_BOT_ENTRY = RELEASE / "bot/experimental_ground_entry.py"
RUNTIME_ENTRY = RELEASE_BOT_ENTRY if RELEASE_BOT_ENTRY.is_file() else BOT_CODE / "experimental_ground_entry.py"
PILOT_CONFIG = Path(os.environ.get("XG_PILOT_CONFIG") or
                    (RELEASE / "experimental-hop-trial.runtime.json"
                     if (RELEASE / "experimental-hop-trial.runtime.json").is_file()
                     else CONFIG_DIR / "experimental-hop-trial.runtime.json"))
SUPERVISOR = Path(os.environ.get("XG_SUPERVISOR") or (LAUNCHER / "supervisor_sixhour.py"))

# 兼容旧名字（有的模块按 runtime 路径拼）
RUNTIME_TERRAIN = RUNTIME / "terrain-experimental-20261104"


def python_exe() -> str:
    """显式覆盖优先；Windows 找物理安装目录，不依赖用户名、PATH 或项目工作目录。"""
    env = os.environ.get("XG_PYTHON")
    if env:
        return env
    bundled = INSTALL_ROOT / "runtime/python/python.exe"
    if IS_WINDOWS and bundled.is_file():
        return str(bundled)
    for cand in (ROOT / ".venv/bin/python", ROOT / ".venv/Scripts/python.exe"):
        if cand.is_file():
            return str(cand)
    return sys.executable


NODE_MIN_MAJOR = 20
_NODE_VERSION_CACHE = {}
_NODE_PROBE_LOCK = threading.Lock()
_NODE_EXEC = None


def _node_exec():
    """Load the shared Windows-safe runner without relying on desktop sys.path."""
    global _NODE_EXEC
    if _NODE_EXEC is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("_xg_node_exec", APP / "xg_exec.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _NODE_EXEC = module
    return _NODE_EXEC



class NodeResolutionError(RuntimeError):
    """Stable safe diagnostics shared by runtime, generators and UI."""
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def _probe_node_version(path):
    """Probe once per file identity; no per-tick --version subprocesses."""
    try:
        info = path.stat()
        key = (os.path.normcase(str(path)), info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    except OSError as exc:
        raise NodeResolutionError("node_unusable", "Node 可执行文件无法读取，请检查安装目录或显式设置。") from exc
    with _NODE_PROBE_LOCK:
        cached = _NODE_VERSION_CACHE.get(key)
        now = time.monotonic()
        if cached and (cached[0] == "version" or now - cached[2] < 5):
            kind, value, _ = cached
            if kind == "error":
                raise NodeResolutionError(*value)
            return value
        try:
            result = _node_exec().run([str(path), "--version"], capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=3,
                                    creationflags=0x08000000 if os.name == "nt" else 0)
            version = (result.stdout or "").strip()
            match = re.fullmatch(r"v?(\d+)\.\d+\.\d+(?:[-+][A-Za-z0-9_.-]+)?", version)
            if result.returncode != 0 or not match:
                raise NodeResolutionError("node_unusable", "Node 无法正常运行或版本输出无效，请重新解压客户端或检查显式设置。")
            value = (int(match.group(1)), version)
            _NODE_VERSION_CACHE[key] = ("version", value, now)
        except (OSError, subprocess.TimeoutExpired) as exc:
            value = ("node_unusable", "Node 无法启动或版本检查超时，请重新解压客户端或检查显式设置。")
            _NODE_VERSION_CACHE[key] = ("error", value, now)
            raise NodeResolutionError(*value) from exc
        except NodeResolutionError as exc:
            _NODE_VERSION_CACHE[key] = ("error", (exc.code, exc.message), now)
            raise
        finally:
            while len(_NODE_VERSION_CACHE) > 32:
                _NODE_VERSION_CACHE.pop(next(iter(_NODE_VERSION_CACHE)))
        return value


def _checked_node(value, *, override=False, search_path=None):
    text = os.fspath(value).strip().strip('"')
    path = Path(text).expanduser()
    # An explicitly named command may be resolved on the supplied PATH; concrete
    # paths are never silently substituted with another installation.
    if not path.is_absolute() and len(path.parts) == 1 and "/" not in text and "\\" not in text:
        found = shutil.which(text, path=search_path)
        if found:
            path = Path(found)
    try:
        if not path.is_file():
            # The shipped launcher pins this managed path even when the package
            # is incomplete. Do not mislabel it as a user's custom override.
            managed = os.path.normcase(os.path.abspath(path)) == os.path.normcase(
                os.path.abspath(INSTALL_ROOT / "runtime/node/node.exe"))
            custom = override and not managed
            raise NodeResolutionError(
                "node_invalid_override" if custom else "node_missing",
                "显式指定的 Node 路径无效；请修正或清除 XG_NODE / --node 设置。"
                if custom else "安装包不完整，未找到可用 Node.js；请重新解压完整客户端（含 runtime/node）。")
        path = path.resolve()
    except (OSError, ValueError) as exc:
        raise NodeResolutionError("node_invalid_override" if override else "node_missing",
                                  "Node 路径无效，请检查安装目录或显式设置。") from exc
    major, version = _probe_node_version(path)
    if major < NODE_MIN_MAJOR:
        raise NodeResolutionError("node_version_unsupported",
                                  "需要 Node.js 20 或更高版本，当前为 %s；请使用随包 Node 或修正显式设置。" % version)
    return str(path)


def _resolve_node(value=None, *, environment=None):
    environment = os.environ if environment is None else environment
    search_path = environment.get("PATH", "")
    text = os.fspath(value).strip() if value is not None else ""
    if text and text.casefold() not in ("node", "node.exe"):
        return _checked_node(text, override=True, search_path=search_path)
    override = environment.get("XG_NODE")
    if override and str(override).strip():
        return _checked_node(str(override), override=True, search_path=search_path)
    # This root is deliberately independent of SKY_AI_ROOT, XG_DATA_DIR and cwd.
    bundled = INSTALL_ROOT / "runtime/node/node.exe"
    if bundled.is_file():
        return _checked_node(bundled)
    found = shutil.which("node", path=search_path)
    if found:
        return _checked_node(found)
    # Retain historical server layouts only after the documented desktop order.
    for candidate in (ROOT / "runtime/node/node.exe", RELEASE.parent / "node/node.exe",
                      ROOT / "runtime/node/bin/node", RELEASE.parent / "node/bin/node"):
        if candidate.is_file():
            return _checked_node(candidate)
    raise NodeResolutionError("node_missing",
                              "安装包不完整，未找到可用 Node.js；请重新解压完整客户端（含 runtime/node）。")


def require_node_exe(value=None) -> str:
    """Resolve placeholders centrally; validate explicit paths without fallback."""
    return _resolve_node(value)


# 某个依赖包里必须存在的文件（只装了一半时 package.json 在、关键文件不在）。
_ENGINE_DEP_FILES = {"meshoptimizer": "meshopt_decoder.mjs"}
TERRAIN_ENGINE = APP / "xg_gamedata/terrain/pipeline/engine"


def engine_deps_missing(engine_dir) -> list:
    """List lock-file dependencies absent from <engine_dir>/node_modules (cheap file checks)."""
    engine_dir = Path(engine_dir)
    try:
        deps = json.loads((engine_dir / "package.json").read_text(encoding="utf-8")).get("dependencies") or {}
    except (OSError, ValueError):
        return []          # 没有清单的旧布局：不在这里拦，交给真正启动时报错
    missing = []
    for name in sorted(deps):
        pkg = engine_dir / "node_modules" / name
        need = _ENGINE_DEP_FILES.get(name, "package.json")
        if not (pkg / "package.json").is_file() or not (pkg / need).is_file():
            missing.append(name)
    return missing


def require_engine_deps(engine_dir, purpose: str) -> None:
    """Fail fast with a Chinese hint instead of a late worker crash / per-map failure."""
    missing = engine_deps_missing(engine_dir)
    if not missing:
        return
    try:
        rel = Path(engine_dir).resolve().relative_to(INSTALL_ROOT.resolve()).as_posix()
    except (OSError, ValueError):
        rel = str(engine_dir)
    raise NodeResolutionError(
        "engine_deps_missing",
        "%s缺少 Node 依赖：%s。便携包请重新解压完整客户端；源码运行请在安装目录执行 npm ci --prefix %s"
        % (purpose, "、".join(missing), rel))


def node_exe() -> str:
    """Compatibility API: absolute usable Node path, or an empty string."""
    try:
        return require_node_exe()
    except NodeResolutionError:
        return ""


def node_child_env(environment=None) -> dict:
    """Pin a valid absolute Node for children without blocking a Node-free UI.

    A bad explicit override remains intact so the eventual consumer reports its
    true error. Missing Node is not replaced by a guessed executable string.
    """
    result = dict(os.environ if environment is None else environment)
    try:
        result["XG_NODE"] = _resolve_node(environment=result)
    except NodeResolutionError:
        pass
    return result


def ensure_dirs() -> None:
    """建立用户数据目录（只建目录，不写业务文件）。"""
    for d in (DATA, CONTROL, LOGS, CAPTURES, BOT_DATA, REF, RUNTIME_PRIVATE, RUNTIME_DATA,
              CURRENT_PROFILES, PROTOCOL_RESOURCES, ASSET_RELEASE, LEASE_DIR, GAME_DIR, NAMES,
              TERRAIN, CATALOG_DATA, NATIVE_DIR, APK_DIR):
        try:
            os.makedirs(long_path(d), exist_ok=True)
        except OSError:
            pass
    # 空中跟随开关：服务器上是手工放的 {"enabled": true, "follow": true}；
    # 安装版默认没有这个文件，room_pose 就永远不创建 AirMotion（跟随只能走/平移）。
    try:
        air = CONTROL / "air_follow.json"
        if not air.exists():
            air.write_text('{"enabled": true, "follow": true, "explore": false}', encoding="utf-8")
    except OSError:
        pass


LONG_PATH_THRESHOLD = 240


def long_path(path, *, force: bool = False) -> str:
    """Windows 长路径：绝对路径超过阈值时加 ``\\\\?\\`` 前缀；其它平台原样返回。

    只改调用层传给 Windows API 的字符串，不改变 DATA/配置里保存的路径。
    已经是扩展路径的直接返回；UNC（``\\\\server\\share``）转成
    ``\\\\?\\UNC\\server\\share``。非 Windows 一律原样，服务器行为不变。
    ``force=True`` 时即使当前路径不长也加前缀（递归遍历/删除要用：根不长，
    但子树里的文件名可能超过 260）。
    """
    p = os.fspath(path)
    if os.name != "nt" or not p:
        return p
    if p.startswith(("\\\\?\\", "\\\\.\\")):
        return p
    p = p.replace("/", "\\")
    try:
        # 扩展命名空间不会折叠 ..；先在普通路径下按 Windows 规则规范化。
        p = os.path.abspath(p)
    except (OSError, ValueError):
        return p
    if not force and len(p.encode("utf-16-le", "surrogatepass")) < LONG_PATH_THRESHOLD * 2:
        return p
    if p.startswith("\\\\"):
        return "\\\\?\\UNC\\" + p[2:]
    return "\\\\?\\" + p


def win_path(path) -> str:
    """``long_path`` 的别名，给调用点更直白的名字。"""
    return long_path(path)


def short_path(path) -> str:
    """Windows：取 8.3 短路径，让只能吃 MAX_PATH 的子进程（Node/临时文件）也能用。

    路径必须已经存在；取不到短名（卷关闭了 8.3）就原样返回，调用方再用
    long_path 兜底。非 Windows 原样返回。
    """
    p = os.fspath(path)
    if os.name != "nt" or not p:
        return p
    if p.startswith("\\\\?\\") and p[4:8].casefold() == "unc\\":
        p = "\\\\" + p[8:]
    elif p.startswith("\\\\?\\") and re.match(r"^[A-Za-z]:\\", p[4:]):
        p = p[4:]
    elif p.startswith(("\\\\?\\", "\\\\.\\")):
        return p
    try:
        p = os.path.abspath(p)
        import ctypes
        size = 32768
        buf = ctypes.create_unicode_buffer(size)
        n = ctypes.windll.kernel32.GetShortPathNameW(long_path(p), buf, size)
        if 0 < n < size and buf.value:
            result = buf.value
            if result.startswith("\\\\?\\") and result[4:8].casefold() == "unc\\":
                return "\\\\" + result[8:]
            return result[4:] if result.startswith("\\\\?\\") else result
    except Exception:
        pass
    return p


def private(path) -> Path:
    """尽量收紧权限；Windows 上 chmod 基本无害，失败忽略。"""
    p = Path(path)
    try:
        os.chmod(long_path(p), 0o700 if p.is_dir() else 0o600)
    except OSError:
        pass
    return p


def read_json(path, default=None):
    try:
        return json.loads(Path(long_path(path)).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path, value, private_file: bool = True) -> Path:
    """同行并发使用独立暂存文件；替换失败保留旧版本，绝不截断原文件。"""
    p = Path(path)
    # 暂存名比最终文件名可能更长；父路径没有达到阈值也必须留足长度。
    parent = long_path(p.absolute().parent, force=True)
    os.makedirs(parent, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=parent,
                                         prefix=".xg-write-", suffix=".pending", delete=False) as f:
            tmp = f.name
            json.dump(value, f, ensure_ascii=False, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        if private_file:
            private(tmp)
        _replace_retry(tmp, long_path(p))
        tmp = None
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass  # 只处理本次暂存，不改原文件；持续被占用的残件留待检查
    return p


def _replace_retry(src, dst, attempts=12, delay=0.05):
    for attempt in range(attempts):
        try:
            return os.replace(src, dst)
        except PermissionError:
            if attempt + 1 == attempts:
                raise
            time.sleep(delay * (1 + attempt))


def config() -> dict:
    return read_json(CONFIG_FILE, {}) or {}


def save_config(doc: dict) -> Path:
    return write_json(CONFIG_FILE, doc)


def _first(cands):
    for p in cands:
        try:
            if p and Path(p).is_file():
                return Path(p)
        except OSError:
            continue
    return Path(cands[0]) if cands else None


def name_table(filename: str) -> Path:
    """名称表位置：用户机生成的 names/ 优先，其次程序里（服务器旧位置）。"""
    return _first([NAMES / filename, GAME_DIR / filename, BOT_CODE / filename, APP / filename])


def destination_records() -> Path:
    """APK 提取的目的地表：用户机上生成；没有就返回一个不存在的候选路径。"""
    return _first([NAMES / "destination-records-v25.json",
                   CATALOG_DATA / "destination-records-v25.json",
                   BOT_CODE / "destination-records-v25.json",
                   DATA / "ref/destination-records-v25.json"])


def load_name_table(filename: str, default=None):
    value = read_json(name_table(filename), None)
    return value if value is not None else (default if default is not None else {})


_TABLE_LIMIT = 2 * 1024 * 1024


def _table_document(name):
    """固定文件名、用户目录优先；表缺失/坏表用随包表，无用户数据写入。"""
    if name not in {"tour_routes.json", "spawn_y_fix.json"}:
        raise ValueError("未知静态表名")
    for base in (CONTROL, CONFIG_DIR):
        path = Path(long_path(base / name))
        try:
            with path.open("rb") as f:
                raw = f.read(_TABLE_LIMIT + 1)
            if len(raw) <= _TABLE_LIMIT:
                doc = json.loads(raw.decode("utf-8-sig"))
                if isinstance(doc, dict):
                    yield doc
        except (OSError, ValueError, RecursionError):
            continue


def _finite(v):
    return type(v) in (int, float) and -1e12 <= v <= 1e12 and math.isfinite(v)


def read_tour_routes():
    """只读取本地路线；返回副本。校验失败不拼接部分路线。"""
    for doc in _table_document("tour_routes.json"):
        maps = doc.get("maps")
        if doc.get("schema") != "xg-tour-routes-v1" or not isinstance(maps,list):
            continue
        seen = set()
        for m in maps:
            if not isinstance(m,dict):
                break
            ident, pts = m.get("map_id"), m.get("waypoints")
            if (type(ident) is not int or not 0 <= ident < 2**32 or ident in seen
                    or not all(isinstance(m.get(k),str) and m[k] for k in ("scene","travel_query"))
                    or any(k in m and type(m[k]) is not bool for k in ("default","auto_when_event"))
                    or any(k in m and not isinstance(m[k],str) for k in ("label","region"))
                    or not isinstance(pts,list) or not all(isinstance(p,list) and len(p) == 3
                    and all(_finite(c) for c in p) for p in pts)):
                break
            seen.add(ident)
        else:
            return doc
    return {"schema": "xg-tour-routes-v1", "maps": []}


def corrected_spawn_y(map_id, y):
    if not _finite(y):
        return y
    for doc in _table_document("spawn_y_fix.json"):
        row = doc.get(str(map_id))
        if not isinstance(row, dict):
            continue
        wire, delta = row.get("wire"), row.get("delta")
        if _finite(wire) and _finite(delta) and 0.03 <= delta <= 0.5:
            return wire + delta if abs(y - wire) <= 0.05 else y
    return y


def safe_console(stream):
    """保留真实控制台编码；无法表示的字符转义而不是引发异常。"""
    try:
        stream.reconfigure(errors="backslashreplace")
    except (AttributeError, OSError, ValueError):
        pass


def spawn_points() -> Path:
    """哥哥标的出生点（允许随包分发；红线白名单里写明就是这个文件）。"""
    return _first([CONFIG_DIR / "spawn_points.json", DATA / "config/spawn_points.json"])


def describe() -> dict:
    """给启动器/日志/测试看的一份路径快照。"""
    return {
        "app": str(APP), "root": str(ROOT), "data": str(DATA), "release": str(RELEASE),
        "bot": str(BOT_CODE), "qfsky": str(QFSKY_CODE), "overlay": str(OVERLAY),
        "engine": str(ENGINE), "src": str(SRC), "web": str(WEB),
        "control": str(CONTROL), "logs": str(LOGS), "captures": str(CAPTURES),
        "runtime_private": str(RUNTIME_PRIVATE), "runtime_data": str(RUNTIME_DATA),
        "dictionary": str(DICTIONARY),
        "names": str(NAMES), "game": str(GAME_DIR), "props": str(PROP_ASSETS), "python": python_exe(), "node": node_exe(),
        "native": str(NATIVE_DIR), "apk": str(APK_DIR),
        "platform": "windows" if IS_WINDOWS else os.name,
    }


if __name__ == "__main__":
    safe_console(sys.stdout)
    print(json.dumps(describe(), ensure_ascii=False, indent=1))
