# -*- coding: utf-8 -*-
"""公共部件：路径、进度 JSON 行、原子写、中文错误。"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
APP_ROOT = PKG_DIR.parent
GAME_VERSION = os.environ.get("XG_GAME_VERSION", "0.16.8-421629")
APK_BUILD = 421629
APK_VERSION = "0.16.8"
APK_PACKAGE = "com.netease.sky"


class GameDataError(RuntimeError):
    """带中文说明和机器码的业务错误。"""

    def __init__(self, message: str, code: str = "gamedata_error"):
        super().__init__(message)
        self.code = code
        self.message = message


def ensure_app_imports() -> Path:
    """把应用根目录放进 sys.path，返回它。

    安装版：包就在 app/xg_gamedata，父目录即 app（xg_paths.py 在这里）。
    开发/测试：允许用 XG_APP_ROOT 指定一个已合并的 app 目录。
    """
    candidates = [APP_ROOT]
    env = os.environ.get("XG_APP_ROOT")
    if env:
        candidates.insert(0, Path(env))
    for cand in candidates:
        if (cand / "xg_paths.py").is_file():
            if str(cand) not in sys.path:
                sys.path.insert(0, str(cand))
            return cand
    # 测试/开发：xg_paths 可能已经在 PYTHONPATH 上的 app 目录里。
    try:
        import importlib.util
        spec = importlib.util.find_spec("xg_paths")
    except (ImportError, ValueError):
        spec = None
    if spec is not None and spec.origin:
        root = Path(spec.origin).resolve().parent
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        return root
    raise GameDataError(
        "找不到 xg_paths.py：xg_gamedata 必须并入小光程序目录 app\\ 后再运行。",
        "app_root_missing")


def xg_paths():
    ensure_app_imports()
    import xg_paths as mod  # noqa: E402
    return mod


def long_path(path):
    """Windows 长路径前缀；优先用 xg_paths.long_path（W5 统一入口）。"""
    try:
        return xg_paths().long_path(path)
    except Exception:  # xg_paths 不可用时的最小兜底（W4a standalone 测试）
        p = os.fspath(path)
        if os.name != "nt" or not p or p.startswith("\\\\?\\"):
            return p
        p = p.replace("/", "\\")
        if not os.path.isabs(p):
            p = os.path.abspath(p)
        if len(p) < 240:
            return p
        return ("\\\\?\\UNC\\" + p[2:]) if p.startswith("\\\\") else ("\\\\?\\" + p)


def force_long_path(path):
    """强制扩展路径：递归 rmtree/copytree 的根即使不长也要加前缀。"""
    try:
        return xg_paths().long_path(path, force=True)
    except Exception:
        p = os.fspath(path)
        if os.name != "nt" or not p or p.startswith("\\\\?\\"):
            return p
        p = os.path.abspath(p.replace("/", "\\"))
        return ("\\\\?\\UNC\\" + p[2:]) if p.startswith("\\\\") else ("\\\\?\\" + p)


def rmtree(path) -> None:
    shutil.rmtree(force_long_path(path))


def ensure_parent(path) -> Path:
    parent = Path(path).parent
    os.makedirs(long_path(parent), exist_ok=True)
    return parent


def resolve_data(explicit: str | None = None) -> Path:
    """DATA 根：--data > XG_DATA_DIR > xg_paths.DATA。"""
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get("XG_DATA_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return Path(xg_paths().DATA).expanduser().resolve()


def game_dir(data: Path) -> Path:
    return Path(data) / "game" / GAME_VERSION


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def sha256_file(path: Path, chunk: int = 8 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(long_path(path), "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def canonical_json(value) -> bytes:
    # 与运行时校验器（asset_catalog_v32c / asset_bootstrap_v32）完全一致：
    # sort_keys + 紧凑分隔符 + 默认 ensure_ascii=True（中文写成 \\uXXXX）。
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def read_json(path, default=None):
    try:
        return json.loads(Path(long_path(path)).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def atomic_write(path: Path, data: bytes) -> Path:
    path = Path(path)
    os.makedirs(long_path(path.parent), exist_ok=True)
    tmp = path.with_name(path.name + ".t")
    Path(long_path(tmp)).write_bytes(data)
    os.replace(long_path(tmp), long_path(path))
    return path


def atomic_json(path: Path, value) -> Path:
    return atomic_write(path, json.dumps(value, ensure_ascii=False, sort_keys=True,
                                         indent=1, allow_nan=False).encode("utf-8"))


def copy_verified(src: Path, dst: Path) -> Path:
    src, dst = Path(src), Path(dst)
    os.makedirs(long_path(dst.parent), exist_ok=True)
    tmp = dst.with_name(dst.name + ".t")
    raw = Path(long_path(src)).read_bytes()
    Path(long_path(tmp)).write_bytes(raw)
    os.replace(long_path(tmp), long_path(dst))
    if sha256_file(src) != sha256_file(dst):
        raise GameDataError("复制校验失败：%s" % dst, "copy_verify")
    return dst


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d%s" % (n, unit)) if unit == "B" else ("%.1f%s" % (n, unit))
        n /= 1024.0
    return "?"


class Progress:
    """逐行 JSON 进度；eta 用已完成量的线性外推。"""

    def __init__(self, stage: str, total: int, stream=None, quiet: bool = False):
        self.stage = stage
        self.total = max(0, int(total))
        self.done = 0
        self.started = time.monotonic()
        self.last = 0.0
        self.stream = stream or sys.stdout
        self.quiet = quiet

    def emit(self, msg: str, done: int | None = None, force: bool = False,
             total: int | None = None):
        if done is not None:
            self.done = max(0, int(done))
        if total is not None:
            self.total = max(0, int(total))
        now = time.monotonic()
        if not force and self.done and now - self.last < 0.15:
            return
        self.last = now
        elapsed = now - self.started
        rate = elapsed / self.done if self.done else 0.0
        eta = rate * (self.total - self.done) if self.total else 0.0
        row = {"stage": self.stage, "done": self.done, "total": self.total,
               "eta_s": round(eta, 1), "msg": msg}
        if not self.quiet:
            self.stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            self.stream.flush()

    def bump(self, msg: str, step: int = 1):
        self.emit(msg, done=self.done + step)

    def finish(self, msg: str):
        self.emit(msg, done=self.total, force=True)


def final(ok: bool, **fields):
    row = {"ok": bool(ok)}
    row.update(fields)
    sys.stdout.write(json.dumps(row, ensure_ascii=False) + "\n")
    sys.stdout.flush()
