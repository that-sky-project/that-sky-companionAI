# -*- coding: utf-8 -*-
"""AI Key 的安全保存（W2）。

Windows：用 DPAPI（CryptProtectData / CryptUnprotectData，当前用户范围）加密，
存到 DATA/secrets/ai_key.bin；同一台机器、同一个 Windows 用户才能解开。
非 Windows（服务器）：存 0600 文件；仍然兼容原来的 secrets.env / DEEPSEEK_API_KEY
环境变量（读取顺序见 llm_providers）。

测试可以在 Windows 上用 force_backend="file" 强制走 0600 文件分支，或者设
XG_AI_KEY_BACKEND=file / dpapi。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import xg_paths

MAGIC = b"XGAI-DPAPI-1\n"
BACKENDS = ("dpapi", "file")


class SecretError(RuntimeError):
    """密钥保存/读取失败；消息可以直接给用户看，不含 key 本身。"""


def _path() -> Path:
    return Path(xg_paths.DATA) / "secrets" / "ai_key.bin"


def backend(force: str | None = None) -> str:
    if force:
        if force not in BACKENDS:
            raise SecretError(f"未知的密钥保存方式：{force}")
        return force
    env = (os.environ.get("XG_AI_KEY_BACKEND") or "").strip().lower()
    if env in BACKENDS:
        return env
    return "dpapi" if os.name == "nt" else "file"


# ------------------------------------------------------------------ DPAPI
def _blob(data: bytes):
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, max(1, len(data)))
    value = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    return DATA_BLOB, value, buf


def _dpapi_protect(data: bytes) -> bytes:
    import ctypes
    DATA_BLOB, blob_in, _keep = _blob(data)
    blob_out = DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(blob_in), None, None, None, None, 0x1, ctypes.byref(blob_out))
    if not ok:
        raise SecretError("Windows 加密保存失败（DPAPI），没有保存 Key")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def _dpapi_unprotect(data: bytes) -> bytes:
    import ctypes
    DATA_BLOB, blob_in, _keep = _blob(data)
    blob_out = DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0x1, ctypes.byref(blob_out))
    if not ok:
        raise SecretError("Key 解不开：可能换了 Windows 用户或文件被改过，请重新填一次 Key")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


# ------------------------------------------------------------------ 读写
def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".pending")
    with open(tmp, "wb") as f:
        f.write(data)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def store_key(key: str, force_backend: str | None = None) -> str:
    """保存 key；返回实际使用的后端。空 key 拒绝（要清除请用 clear_key）。"""
    key = (key or "").strip()
    if not key:
        raise SecretError("Key 是空的")
    kind = backend(force_backend)
    if kind == "dpapi":
        data = MAGIC + _dpapi_protect(key.encode("utf-8"))
    else:
        data = key.encode("utf-8")
    try:
        _write_private(_path(), data)
    except OSError as exc:
        raise SecretError(f"Key 保存失败：{type(exc).__name__}") from exc
    return kind


def load_key(force_backend: str | None = None) -> str:
    """读回 key；没有就返回空串。文件坏了抛 SecretError。"""
    path = _path()
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise SecretError(f"Key 读取失败：{type(exc).__name__}") from exc
    if data.startswith(MAGIC):
        return _dpapi_unprotect(data[len(MAGIC):]).decode("utf-8", "replace")
    kind = backend(force_backend)
    if kind == "dpapi":
        # 有文件但不是 DPAPI 格式：可能是旧版/手工放的明文，明确拒绝而不是当明文用。
        raise SecretError("Key 文件格式不对（不是 DPAPI 加密格式），请重新填一次 Key")
    return data.decode("utf-8", "replace")


def clear_key() -> bool:
    path = _path()
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise SecretError(f"Key 删除失败：{type(exc).__name__}") from exc


def status(force_backend: str | None = None) -> dict:
    """给页面看的摘要，绝不含 key。"""
    try:
        key = load_key(force_backend)
        error = ""
    except SecretError as exc:
        key, error = "", str(exc)
    return {
        "backend": backend(force_backend),
        "has_key": bool(key),
        "key_hint": ("已设置（末 4 位 " + key[-4:] + "）") if key else "未设置",
        "error": error,
    }
