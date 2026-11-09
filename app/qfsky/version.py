# -*- coding: utf-8 -*-
"""国服版本号管理：UA 版本（华为商店自动刷新）+ JoinGame build 推导。

对齐 SkyApi.refreshCnHuaweiVersion / cnUaVersion / buildFromVersion。
2026-07-31 起华为接口用 pkgName=com.huawei.appmarket + appId 定位。
"""
from __future__ import annotations

import threading
import time

import requests

from . import netcfg

HUAWEI_VERSION_URL = "https://web-dra.hispace.dbankcloud.com/edge/single/filtered"
HUAWEI_UA = (
    "Mozilla/5.0 (Windows NT 6.1; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/47.0.2526.80 Safari/537.36 Core/1.47.277.400 QQBrowser/9.4.7658.400"
)
CN_HUAWEI_APP_ID = "C100685413"

# 兜底：华为接口拉不到时使用。随游戏大版本更新手动/同步升级。
CN_FALLBACK_VERSION = "0.16.7.421589"

_lock = threading.Lock()
_cached: tuple[str, float] | None = None
_TTL = 10 * 60.0


def _huawei_fetch() -> str | None:
    try:
        r = netcfg.session("version:huawei").post(
            HUAWEI_VERSION_URL,
            json={"pkgName": "com.huawei.appmarket", "zone": "", "locale": "zh-cn", "appId": CN_HUAWEI_APP_ID},
            headers={"User-Agent": HUAWEI_UA, "Content-Type": "application/json"},
            timeout=15,
        )
        if not r.ok:
            return None
        app = (r.json() or {}).get("appInfo") or {}
        name = (app.get("version") or "").strip()
        code = str(app.get("versionCode") or "").strip()
        if not name or not code.isdigit():
            return None
        return "%s.%s" % (name.split(" ")[0].strip(), code)
    except Exception:
        return None


def refresh_cn_version() -> str | None:
    """主动刷新（华为商店）。失败返回 None（保留现值）。"""
    global _cached
    v = _huawei_fetch()
    if v:
        with _lock:
            _cached = (v, time.time())
    return v


def cn_version() -> str:
    """UA 版本号：缓存(10min) → 华为实时 → 兜底。"""
    global _cached
    with _lock:
        if _cached and time.time() - _cached[1] < _TTL:
            return _cached[0]
    v = _huawei_fetch()
    if v:
        with _lock:
            _cached = (v, time.time())
        return v
    return CN_FALLBACK_VERSION


def build_from_version(version: str) -> int | None:
    """'0.16.5.412659' → 412659；残缺版本（1~2 个点）返回 None。"""
    v = (version or "").strip()
    if not v or v == "0":
        return None
    if 1 <= v.count(".") <= 2:
        return None
    tail = v.rsplit(".", 1)[-1]
    try:
        n = int(tail)
    except ValueError:
        return None
    return n if n > 0 else None


def cn_build() -> int | None:
    return build_from_version(cn_version())


# ── 网易 MPay 客户端版本号 ────────────────────────────────────────────────
# 2026-09-16 真机抓包对齐（Redmi 23013RK75C 上官方光遇客户端，见 docs/知识库/66）。
# 真机实测 UA（0.16.3 时代）：
#     com.netease.sky/412289 NeteaseMobileGame/a5.16.0 (23013RK75C;36)
# 对应表单字段：gv=412289、gvn=0.16.3、cv=a5.16.0、sv=36。
#
# 2026-09-24 0.16.5 正式版真机复测：客户端 /account/gameconfig/get_config 实发
#     app_ver="0.16.5(412659)"，并请求 /config/0.16.5-412659-Android-obt.json；
#     华为商店同版本 → gv/gvn 兜底随之上抬到 412659 / 0.16.5。
#     cv 本次无新证据（该轮抓包未产生 mkey 登录流量）→ 保持 a5.16.0 不动。
#
# 改造前的旧值（383089 / 0.15.8 / a5.9.1 / 31）比官方落后 7 个小版本 ——
# 对一个「声称是官方客户端」的请求来说，这本身就是极强的非官方信号。
#
# 分工：
#   gv / gvn —— 与游戏大版本同步。本模块已从华为商店自动刷新，所以走 mpay_gv()/mpay_gvn()
#               动态取，**不要再硬编码**，否则会再次落后。
#   cv       —— 客户端版本，没有自动来源，随客户端更新手动改这里。
#   sv       —— **不放在本模块**：它必须等于该账号设备指纹的 Android API 级别
#               （fp.device(key)["sdk"]），否则同一个请求的 UA 写 (机型;33)、
#               表单写 sv=31，自己跟自己矛盾 —— 真机不可能出现。
MPAY_CV = "a5.16.0"
MPAY_GV_FALLBACK = "421589"
MPAY_GVN_FALLBACK = "0.16.7"


def mpay_gv() -> str:
    """MPay 表单里的 gv（412659）：跟随本模块的游戏版本刷新，取不到用兜底。"""
    return str(cn_build() or MPAY_GV_FALLBACK)


def mpay_gvn() -> str:
    """MPay 表单里的 gvn（0.16.5）。

    `cn_version()` 形如 `0.16.5.412659`（版本名 + 构建号，**四段**），
    gvn 就是去掉末段构建号的部分。与 `mpay_gv()` 同源，保证两者一致 ——
    它们必须来自同一次版本读取，否则会出现 gv=新版 / gvn=旧版 这种真机不可能有的组合。
    """
    v = str(cn_version() or "").strip()
    parts = v.split(".")
    if len(parts) >= 4 and parts[-1].isdigit():
        return ".".join(parts[:-1])
    # 拿不到四段格式（接口异常等）就用兜底，别猜
    return MPAY_GVN_FALLBACK


def mpay_sv(dev: dict | None = None) -> str:
    """MPay 表单里的 sv —— 该设备指纹的 Android API 级别（与 UA 末尾的 ;NN 同值）。"""
    sdk = str((dev or {}).get("sdk") or "").strip()
    return sdk or "33"
