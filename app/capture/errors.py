# -*- coding: utf-8 -*-
"""录制失败原因 → 后台显示的中文（不输出内存内容、账号哈希或原始异常栈）。"""
from __future__ import annotations

ERRORS = {
    "map_scene_nonown_home_owner_requires_policy":
        "这个房间里有别人放的摆件（桌椅之类），录不了：出去换个房间重新走进来",
    "current_input_actor_native_length": "角色还在加载或正在做动作，站稳别动，马上重试",
    "latest exact native scene event differs": "还在切图，等进图完成",
    "current_input_torn_or_changed": "读的时候画面还在变（切图/加载），站稳别动，马上重试",
    "current_input_scene_length": "场景数据还没准备好，等进图完成再试",
    "current_input_actor_map_or_roundtrip": "游戏数据版本对不上（不是 0.16.8 / 421629）",
    "current_profile_short_batch_read": "读游戏内存时被游戏打断，站稳别动，马上重试",
    "current_profile_player_changed_during_capture": "角色状态刚变过，站稳别动，马上重试",
    "current_profile_account_changed": "读的时候换了号，重新进图再录",
    "current_profile_native_context_changed": "刚切过图或重登过，等进图完成再试",
    "current_profile_world_generation_changed": "世界刚重载，等进图完成再试",
    "current_profile_pause_": "暂停游戏进程做一致性读取时失败；确认 MuMu 的 Root 还开着",
    "current_actor_unreadable_batch_region": "游戏进程内存读不到：确认 MuMu 开了「手机 Root 权限」并重启过模拟器",
    "current_actor_short_batch_read": "读游戏内存不完整，稍后重试",
    "current_actor_pid": "找不到唯一的光遇进程（是不是开了两个）",
    "expected exactly one running official game process": "没找到/找到多个光遇进程，先把游戏开好",
    "installed package build is not421629": "MuMu 里装的光遇不是 0.16.8 / 421629，先换对版本",
    "unknown local native library": "本机 libBootloader.so 和 0.16.8 / 421629 对不上（APK 版本不对或库损坏）",
    "installed mapped native SHA256 does not match known build": "MuMu 里跑的光遇和本地 0.16.8 / 421629 文件不一致",
    "runtime text proof mismatch": "MuMu 里的光遇版本和录制工具对不上（不是 421629）",
    "Game vtable mismatch": "MuMu 里的光遇版本和录制工具对不上（不是 421629）",
    "native NetVersion build invariants mismatch": "MuMu 里的光遇版本和录制工具对不上（不是 421629）",
    "unsupported mapped path": "MuMu 里的 libBootloader.so 路径形状不认识；换官方安装包重装一次",
    "missing or ambiguous loaded libBootloader path": "没找到已加载的 libBootloader.so：确认游戏已经进到遇境/地图里",
    "adb operation failed": "adb 命令失败：确认 MuMu 还开着、Root 开着、adb 路径填对",
    "current_actor_source_not_found": "程序里缺 qfsky 协议代码，装包不完整，重新安装小光",
    "root": "MuMu 拿不到 root：在 MuMu 设置里打开「手机 Root 权限」并重启模拟器",
    "not found": "没找到正在跑光遇的 MuMu",
}


def explain(text, default="录制失败，站稳别动重试一次；还不行就看 MuMu 的 Root 和游戏版本"):
    text = str(text or "")
    for key, value in ERRORS.items():
        if key in text:
            return value
    return default
