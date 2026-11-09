# -*- coding: utf-8 -*-
"""统一子进程入口（W9）。

背景
====
后台/脱离进程（DETACHED_PROCESS，无控制台）里直接 ``subprocess.run`` 时，
Windows 会给子进程一个无效的标准输入句柄；adb.exe 这类会检查 STDIN 的程序
就直接失败（前台跑同样的代码却正常）。另外从无控制台进程起 console 子进程
时，系统可能新开一个黑窗。

约定
====
* Windows：没显式给 stdin 时用 ``DEVNULL``；creationflags 一律补
  ``CREATE_NO_WINDOW``（已带 DETACHED_PROCESS/CREATE_NEW_PROCESS_GROUP 也兼容）。
* POSIX：完全保持调用方原样，不额外加参数（服务器行为不变）。
* 调用方显式传 ``stdin`` / ``creationflags`` 时尊重原值，只做“补默认”和按位
  或 ``CREATE_NO_WINDOW``，不覆盖。

所有 app 内的 ``subprocess.run`` / ``subprocess.Popen`` 都应走这里；测试
``w9_detached_probe.py`` 会静态扫描，防止以后又直接调 subprocess。
"""
from __future__ import annotations

import os
import subprocess

CREATE_NO_WINDOW = 0x08000000


def windows_options(kwargs: dict) -> dict:
    """返回补好 Windows 安全默认值的 kwargs（就地修改并返回同一 dict）。"""
    if os.name != "nt":
        return kwargs
    # input= 会让 subprocess 自己建 PIPE；此时不能再传 stdin。
    if "input" not in kwargs:
        kwargs.setdefault("stdin", subprocess.DEVNULL)
    flags = int(kwargs.get("creationflags") or 0)
    kwargs["creationflags"] = flags | CREATE_NO_WINDOW
    return kwargs


def run(argv, **kwargs):
    return subprocess.run(argv, **windows_options(kwargs))


def popen(argv, **kwargs):
    return subprocess.Popen(argv, **windows_options(kwargs))


def call(argv, **kwargs):
    return subprocess.call(argv, **windows_options(kwargs))


def check_output(argv, **kwargs):
    return subprocess.check_output(argv, **windows_options(kwargs))


def check_call(argv, **kwargs):
    return subprocess.check_call(argv, **windows_options(kwargs))
