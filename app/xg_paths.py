# -*- coding: utf-8 -*-
"""小光跨平台路径常量（W1）。

设计原则
========
1. **服务器默认行为不变**：不设环境变量时，`ROOT` / `DATA` / `RELEASE` / `BOT` /
   `SERVER_ROOT` / `SERVER_QFSKY` / `CONTROL` / `LOGS` / `CAPTURES` /
   `RUNTIME_PRIVATE` / `RUNTIME_DATA` / `STABLE` 全部等于服务器原路径
   `<server-data-root>/...`。合回服务器时这些常量原样成立。
2. **程序与数据分开**：`ROOT` 是代码/运行根；`DATA` 是用户数据根，默认等于
   `ROOT`（服务器），Windows 安装版由启动器设成 `%LOCALAPPDATA%\\XiaoGuang\\data`。
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
XG_GAME_VERSION  游戏数据版本目录名（默认 0.16.7-421589）
XG_PYTHON     跑子进程用的解释器（默认 ROOT/.venv/bin/python，没有就用 sys.executable）
XG_NODE       Node 可执行文件（默认 PATH 里的 node）
XG_RELEASE    覆盖 RELEASE
XG_SUPERVISOR 覆盖 supervisor 脚本路径
XG_PILOT_CONFIG 覆盖地形引擎 pilot config
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

# ---------------------------------------------------------------- 基本根
APP = Path(__file__).resolve().parent          # 程序目录（安装版 app\）
ROOT = Path(os.environ.get("SKY_AI_ROOT") or APP.parent)
DATA = Path(os.environ.get("XG_DATA_DIR") or ROOT)

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

GAME_VERSION = os.environ.get("XG_GAME_VERSION", "0.16.7-421589")
GAME_DIR = DATA / "game" / GAME_VERSION
NAMES = GAME_DIR / "names"
TERRAIN = GAME_DIR / "terrain"
CATALOG_DATA = GAME_DIR / "catalog"

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
    """跑子进程的解释器：优先项目 .venv，其次当前解释器（便携版就是它）。"""
    env = os.environ.get("XG_PYTHON")
    if env:
        return env
    for cand in (ROOT / ".venv/bin/python", ROOT / ".venv/Scripts/python.exe"):
        if cand.is_file():
            return str(cand)
    return sys.executable


def node_exe() -> str:
    """Node 可执行文件；找不到返回空串（调用方要如实报“缺 Node”）。"""
    env = os.environ.get("XG_NODE")
    if env:
        return env
    found = shutil.which("node")
    if found:
        return found
    for cand in (ROOT / "runtime/node/node.exe", RELEASE.parent / "node/node.exe"):
        if cand.is_file():
            return str(cand)
    return ""


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
    if p.startswith("\\\\?\\"):
        return p
    p = p.replace("/", "\\")
    if not os.path.isabs(p):
        try:
            p = os.path.abspath(p)
        except OSError:
            return p
    if not force and len(p) < LONG_PATH_THRESHOLD:
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
    if p.startswith("\\\\?\\"):
        p = p[4:]
    try:
        p = os.path.abspath(p)
        import ctypes
        size = 32768
        buf = ctypes.create_unicode_buffer(size)
        n = ctypes.windll.kernel32.GetShortPathNameW(p, buf, size)
        if 0 < n < size and buf.value:
            return buf.value
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
    """原子写 JSON（先写 .pending 再 os.replace，Windows 长路径也安全）。"""
    p = Path(path)
    os.makedirs(long_path(p.parent), exist_ok=True)
    tmp = p.with_name(p.name + ".pending")
    Path(long_path(tmp)).write_text(json.dumps(value, ensure_ascii=False, sort_keys=True),
                                    encoding="utf-8")
    os.replace(long_path(tmp), long_path(p))
    if private_file:
        private(p)
    return p


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
        "names": str(NAMES), "game": str(GAME_DIR), "python": python_exe(), "node": node_exe(),
        "native": str(NATIVE_DIR), "apk": str(APK_DIR),
        "platform": "windows" if IS_WINDOWS else os.name,
    }


if __name__ == "__main__":
    print(json.dumps(describe(), ensure_ascii=False, indent=1))
