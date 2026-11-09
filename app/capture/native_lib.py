# -*- coding: utf-8 -*-
"""从用户自己的 APK 里取录制要用的 native 库（libBootloader.so），放到 DATA，不随包。"""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import zipfile
from pathlib import Path

from .native_profile import NATIVE_SHA256

LIB_NAME = "libBootloader.so"
APK_ENTRIES = ("lib/arm64-v8a/" + LIB_NAME, "lib/arm64/" + LIB_NAME)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _dest_dir():
    import xg_paths
    return xg_paths.NATIVE_DIR


def native_path():
    import xg_paths
    cfg = (xg_paths.config() or {}).get("capture") or {}
    p = cfg.get("native_library")
    if p and Path(p).is_file():
        return Path(p)
    return xg_paths.NATIVE_DIR / LIB_NAME


def _candidate_apks(apk_path=None):
    out = []
    if apk_path:
        out.append(Path(apk_path))
    try:
        import xg_paths
        cfg = (xg_paths.config() or {}).get("capture") or {}
        for v in (cfg.get("apk"), (cfg.get("gamedata") or {}).get("apk")):
            if v:
                out.append(Path(str(v)))
        for d in (xg_paths.APK_DIR, xg_paths.GAME_DIR / "apk"):
            if d.is_dir():
                out.extend(sorted(d.glob("*.apk")))
    except Exception:
        pass
    return [p for p in out if p.is_file()]


def ensure_native_library(apk_path=None, dest=None, expected_sha256=None, force=False):
    """确保 DATA/runtime/native/libBootloader.so 存在且是 421589 的那份。

    返回 {"ok":True,"path":...,"sha256":...}；失败抛 ValueError（中文由 errors.explain 转）。
    """
    expected = expected_sha256 or NATIVE_SHA256
    dest = Path(dest) if dest else native_path()
    if dest.is_file() and not force:
        got = sha256_file(dest)
        if got == expected:
            return {"ok": True, "path": str(dest), "sha256": got, "source": "existing"}
    apks = _candidate_apks(apk_path)
    if not apks:
        raise ValueError("还没选游戏安装包：先在向导第 3 步从 MuMu 拉 APK 或手动选文件")
    last = None
    for apk in apks:
        try:
            with zipfile.ZipFile(apk) as zf:
                name = next((n for n in APK_ENTRIES if n in zf.namelist()), None)
                if not name:
                    last = f"{apk.name} 里没有 lib/arm64-v8a/{LIB_NAME}"
                    continue
                body = zf.read(name)
        except (OSError, zipfile.BadZipFile) as exc:
            last = f"{apk.name} 打不开：{type(exc).__name__}"
            continue
        got = hashlib.sha256(body).hexdigest()
        if got != expected:
            last = (f"{apk.name} 里的 {LIB_NAME} 不是 0.16.7 / 421589 的版本"
                    f"（sha256={got[:12]}…），换对版本的安装包")
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".native-", dir=str(dest.parent))
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(body)
            os.replace(tmp, dest)
        finally:
            if Path(tmp).exists():
                Path(tmp).unlink()
        return {"ok": True, "path": str(dest), "sha256": got, "source": str(apk)}
    raise ValueError(last or "APK 里没有找到 libBootloader.so")


def copy_from_dir(src_dir, dest=None, expected_sha256=None):
    """从已经解包/已存在的目录复制（手动选 APK 拆分包时用）。"""
    expected = expected_sha256 or NATIVE_SHA256
    dest = Path(dest) if dest else native_path()
    src = Path(src_dir) / "lib/arm64-v8a" / LIB_NAME
    if not src.is_file():
        raise ValueError(f"{src_dir} 里没有 lib/arm64-v8a/{LIB_NAME}")
    got = sha256_file(src)
    if got != expected:
        raise ValueError("这个 libBootloader.so 不是 0.16.7 / 421589 的版本")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    return {"ok": True, "path": str(dest), "sha256": got, "source": str(src)}
