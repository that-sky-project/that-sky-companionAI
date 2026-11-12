# -*- coding: utf-8 -*-
"""国服客户端版本档位：0.16.8 / 421629（默认）与 0.16.7 / 421589（只读识别）。

这里只放静态身份与资源版本，不做网络访问。账号绑定域刻意不在这里：
录制/目录的账号绑定域仍是 421589（见 STAGING-NOTES 第 2 条），不能跟着
协议 build 一起改，否则旧录制会全部失配。
"""
from __future__ import annotations

CURRENT_VERSION = "0.16.8"
CURRENT_BUILD = 421629
CURRENT_GAME_VERSION = "0.16.8-421629"
# 用户从 0.16.8 客户端读到的资源版本。
CURRENT_RESOURCE_REVISION = "V76318"
# asset catalog / bootstrap 里使用的 APK 资源版本（不是运行时 V 号）。
CURRENT_APK_RESOURCE_REVISION = "apk-v76318-421629"
CURRENT_NATIVE_SHA256 = "0b18ecc44e873f6caa181e6bc49f14b7e07058888523e47db52a14b32077a7ed"
CURRENT_APK_DIRECTORY_SHA256 = "b3bab17209ddacc225daa2eef932eefc6230ebaf7102331da523b7d95fc87555"

# 遇境（CandleSpace）的 map_id 不随版本变，但 0.16.8 重做了场景，
# resource tag / NetVersion[0] 从 36851 变成 63789。它是版本档案的一部分，
# 不允许在协议/目录代码里再写死。
HOME_SCENE = "CandleSpace"
HOME_LEVEL_ID = 3526133726
CURRENT_HOME_RESOURCE_TAG = 63789
CURRENT_HOME_NETVERSION = (63789, 49307, 23536, 0, 0, 25536)

# 账号绑定域固定为 421589；这是历史录制/目录的兼容契约，不是版本号。
ACCOUNT_BINDING_DOMAIN = "sky-cn-421589-level-cache-account-v1"
ACCOUNT_BINDING_ALGORITHM = "sha256-domain-sky-cn-421589-level-cache-account-v1"

LEGACY_VERSION = "0.16.7"
LEGACY_BUILD = 421589
LEGACY_GAME_VERSION = "0.16.7-421589"
LEGACY_RESOURCE_REVISION = "V75885"
LEGACY_APK_RESOURCE_REVISION = "apk-v75506-421589"
LEGACY_NATIVE_SHA256 = "0b81f1da54d9c6f28c08746b2ec19519d8eb78c4f1b3c5e4e420ff726dc25321"
LEGACY_APK_DIRECTORY_SHA256 = "b26db74613f606fd60b942ff58f00d0c41b12a6d83303228a1143d683adefb14"
LEGACY_HOME_RESOURCE_TAG = 36851
LEGACY_HOME_NETVERSION = (36851, 49307, 23536, 0, 0, 25536)

PROFILES = {
    LEGACY_BUILD: {
        "build": LEGACY_BUILD,
        "version": LEGACY_VERSION,
        "game_version": LEGACY_GAME_VERSION,
        "resource_revision": LEGACY_RESOURCE_REVISION,
        "apk_resource_revision": LEGACY_APK_RESOURCE_REVISION,
        "native_sha256": LEGACY_NATIVE_SHA256,
        "apk_directory_sha256": LEGACY_APK_DIRECTORY_SHA256,
        "home_scene": HOME_SCENE,
        "home_level_id": HOME_LEVEL_ID,
        "home_resource_tag": LEGACY_HOME_RESOURCE_TAG,
        "home_netversion": LEGACY_HOME_NETVERSION,
        "legacy": True,
    },
    CURRENT_BUILD: {
        "build": CURRENT_BUILD,
        "version": CURRENT_VERSION,
        "game_version": CURRENT_GAME_VERSION,
        "resource_revision": CURRENT_RESOURCE_REVISION,
        "apk_resource_revision": CURRENT_APK_RESOURCE_REVISION,
        "native_sha256": CURRENT_NATIVE_SHA256,
        "apk_directory_sha256": CURRENT_APK_DIRECTORY_SHA256,
        "home_scene": HOME_SCENE,
        "home_level_id": HOME_LEVEL_ID,
        "home_resource_tag": CURRENT_HOME_RESOURCE_TAG,
        "home_netversion": CURRENT_HOME_NETVERSION,
        "legacy": False,
    },
}


def profile_for_build(build):
    """按 build 取档位；未知 build 返回 None（调用方 fail-closed）。"""
    if type(build) is int and build in PROFILES:
        return dict(PROFILES[build])
    return None


def profile_for_version(version):
    text = str(version or "").strip()
    for build, row in PROFILES.items():
        if text in (row["version"], str(build), "%s.%s" % (row["version"], build)):
            return dict(row)
    return None


def profile_for_native_sha256(sha256):
    """按 native_sha256 反查档位；未知摘要返回 None（调用方 fail-closed）。"""
    if type(sha256) is not str:
        return None
    for row in PROFILES.values():
        if row["native_sha256"] == sha256:
            return dict(row)
    return None


def home_profile(build=CURRENT_BUILD):
    """遇境所属版本档案；未知 build 返回 None。"""
    return profile_for_build(build)


def home_resource_tag(build=CURRENT_BUILD):
    profile = home_profile(build)
    if profile is None:
        raise ValueError("unsupported_home_build")
    return profile["home_resource_tag"]


def home_netversion(build=CURRENT_BUILD):
    profile = home_profile(build)
    if profile is None:
        raise ValueError("unsupported_home_build")
    return tuple(profile["home_netversion"])


def supported_builds():
    return tuple(sorted(PROFILES))
