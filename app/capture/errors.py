# -*- coding: utf-8 -*-
"""录制失败原因 → 后台显示的中文（不输出内存内容、账号哈希或原始异常栈）。"""
from __future__ import annotations

ERRORS = {
    "capture_device_scope": "这个 ADB 设备不在支持范围。小光只支持本机模拟器或使用内网 IP 的 MuMu；请断开 USB 真机或公网 ADB，再重新检查。",
    "current_profile_pause_device_scope": "这个 ADB 设备不能用于录图。请连到本机 MuMu 或使用 MuMu 当前的桥接内网 IP，再重试。",
    "current_profile_pause_package": "录图前游戏进程已变化，或不是支持的光遇客户端。请回到 MuMu 中的光遇，等当前地图加载后重试。",
    "current_profile_pause_resume_unconfirmed": "小光没能确认游戏恢复运行。请先查看 MuMu 中游戏是否仍响应；若卡住，重启游戏，再打开遇境重试。",
    "current_profile_pause_preexisting_stop_or_exit": "游戏已暂停或退出。请把光遇切回前台；若卡住，重启游戏并进入遇境后重试。",
    "capture_adb_disconnected": "与 MuMu 的 ADB 连接已断开。请确认实例还在运行；如开了桥接，请重新检查以读取当前 IP。",
    "capture_adb_timeout": "MuMu 的 ADB 响应超时。请等模拟器启动完成；桥接模式下确认电脑与实例处于同一内网，再重试。",
    "capture_adb_missing": "找不到之前选择的 adb.exe。请在设置里重新选择 MuMu 自带的 adb.exe，再重试。",
    "capture_adb_permission": "Windows 拦住了 adb.exe。请把小光解压到自己可访问的目录，并检查 Windows 安全中心的保护历史，再重试。",
    "capture_adb_launch_failed": "Windows 未能启动 adb.exe。请确认 MuMu 安装完整，在小光中重新选择 MuMu 自带的 adb.exe，再重试。",
    "capture_adb_conflict": "本机已有其他版本的 ADB 服务。请关闭正在使用它的工具，或在小光设置里选择与该服务匹配的 adb.exe，再重试。",
    "capture_root_disabled": "MuMu 的「手机 Root 权限」未开启。请打开这个实例的设置 → 其他设置 → 手机 Root 权限，重启该实例后再检查。",
    "capture_root_user_build": "当前安卓镜像是正式构建，adb root 不可用，也没有检测到已授予的 Root Shell。若 MuMu 提供「手机 Root 权限」设置，请核对这个实例的设置并重启；若仍失败，当前镜像不支持此录制方式。",
    "capture_root_unavailable": "MuMu 未向小光提供 Root Shell。请在对应实例的设置中打开「手机 Root 权限」，重启该实例后再检查。",
    "capture_maps_denied": "已连到 MuMu，但当前镜像禁止读取游戏的进程映射。请确认当前实例启用了 Root 并已重启；若仍失败，请换用支持录图的 MuMu 镜像。",
    "capture_mem_denied": "Android 拒绝读取该进程的内存。录制已停止；保持系统访问控制开启。",
    "capture_page_size_unsupported": "当前镜像不是 4 KiB 页；可继续检查设备或拉取安装包，现有原生录制不支持此页大小。",
    "capture_maps_empty": "没有读到有效的游戏进程映射。请保持光遇在前台，等地图加载完成后重试。",
    "capture_game_exited": "游戏进程已退出、重启，或当前镜像隐藏了该进程。请确认光遇仍在前台，并重新检查 MuMu。",
    "capture_logcat_denied": "当前 Android 镜像未授予 ADB 读取所需日志缓冲区的权限，无法从日志识别地图。",
    "capture_scene_log_missing": "当前进程的日志里还没有进入地图的记录。请切回光遇并重新进入遇境，再重试。",
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
    "current_profile_pause_": "录图时未通过游戏进程状态验证。请确认光遇仍在前台并等地图加载完成，然后重试。",
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
    "adb operation failed": "ADB 命令执行失败。请确认 MuMu 实例还开着、ADB 调试已打开、选择的是 MuMu 自带的 adb.exe，再重新检查。",
    "current_actor_source_not_found": "程序里缺 qfsky 协议代码，装包不完整，重新安装小光",
    "root": "MuMu 拿不到 root：在 MuMu 设置里打开「手机 Root 权限」并重启模拟器",
    "not found": "没找到正在跑光遇的 MuMu",
}


def error_code(error, default="capture_failed"):
    """Prefer an exact capture code over a generic fallback in status JSON."""
    text = str(error or "")
    if text in ERRORS and text != "current_profile_pause_" and text.isidentifier():
        return text
    if text.startswith("current_profile_pause_") and text.isidentifier():
        return text
    return default


def explain(text, default="录制失败，站稳别动重试一次；还不行就看 MuMu 的 Root 和游戏版本"):
    text = str(text or "")
    for key, value in ERRORS.items():
        if key in text:
            return value
    return default
