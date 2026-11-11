# -*- coding: utf-8 -*-
"""0.16.8 网关访问密钥（x-sky-build-access-key）的本机解析。

安全边界：
  * 密钥绝不写进仓库、日志、异常消息或界面。
  * 取值顺序：显式环境变量 → 本机私有文件 → 私有登录服务下发后缓存。
  * 三处都没有时抛 BuildAccessKeyMissing，中文提示怎么补。
  * 旧 0.16.8 默认值已删除；这里没有任何内置密钥。

私有文件（推荐给不想用登录服务的自建用户）：
  runtime/private/build_access_key      纯文本，一行，权限 0600
服务下发缓存（自动）：
  runtime/private/build_access_key.cache.json  {"key": "...", "fetched_at": ...}
"""
from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path

ENV_NAME = "SKY_BUILD_ACCESS_KEY_NETEASE"
SERVICE_PATH = "/v1/build-access-key"
MIN_LEN = 8
MAX_LEN = 512


class BuildAccessKeyMissing(RuntimeError):
    code = "build_access_key_missing"

    def __init__(self):
        super().__init__(
            "缺少 0.16.8 网关访问密钥（x-sky-build-access-key）。"
            "请先在启动器登录并保持订阅有效，让私有登录服务下发；"
            "或把密钥写进 runtime/private/build_access_key（一行，勿提交仓库）；"
            "或设置环境变量 %s。" % ENV_NAME)


class BuildAccessKeyUnavailable(RuntimeError):
    code = "build_access_key_unavailable"


def _data_root():
    env = os.environ.get("XG_DATA_DIR")
    if env:
        return Path(env)
    try:
        import xg_paths
        return Path(xg_paths.DATA)
    except Exception:
        return Path(__file__).resolve().parents[2]


def private_path():
    return _data_root() / "runtime" / "private" / "build_access_key"


def cache_path():
    return _data_root() / "runtime" / "private" / "build_access_key.cache.json"


def _clean(value):
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if not value or value.startswith("<") or "REDACTED" in value.upper():
        return ""
    if not MIN_LEN <= len(value) <= MAX_LEN or any(c in value for c in "\r\n\t "):
        return ""
    return value


def _read_private(path):
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""
    return _clean(raw.splitlines()[0] if raw.splitlines() else "")


def _read_cache(path):
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return _clean(doc.get("key") if isinstance(doc, dict) else "")


def _atomic_private(path, body):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(body, encoding="utf-8", newline="\n")
    try:
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    os.replace(tmp, path)
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def store_downloaded(key):
    value = _clean(key)
    if not value:
        raise BuildAccessKeyUnavailable("登录服务返回的网关密钥形状不对")
    _atomic_private(cache_path(), json.dumps(
        {"key": value, "fetched_at": int(time.time())}, ensure_ascii=False))
    return value


def fetch_from_service():
    """用已配置的私有登录服务取密钥；失败返回空串（不吞掉本机已有值）。"""
    try:
        import xg_auth_client
        reply = xg_auth_client.Client().request("GET", SERVICE_PATH, timeout=20)
    except Exception:
        return ""
    value = _clean(reply.get("build_access_key") if isinstance(reply, dict) else "")
    if not value:
        return ""
    return store_downloaded(value)


def resolve(*, allow_service=True):
    """返回可用密钥；没有就抛 BuildAccessKeyMissing。"""
    value = _clean(os.environ.get(ENV_NAME, ""))
    if value:
        return value
    value = _read_private(private_path()) or _read_cache(cache_path())
    if value:
        return value
    if allow_service:
        value = fetch_from_service()
        if value:
            return value
    raise BuildAccessKeyMissing()


def status():
    """给界面/诊断看的脱敏状态：只有来源与布尔，不含密钥。"""
    if _clean(os.environ.get(ENV_NAME, "")):
        return {"configured": True, "source": "environment"}
    if _read_private(private_path()):
        return {"configured": True, "source": "private_file"}
    if _read_cache(cache_path()):
        return {"configured": True, "source": "service_cache"}
    return {"configured": False, "source": "none"}
