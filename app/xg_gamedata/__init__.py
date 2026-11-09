# -*- coding: utf-8 -*-
"""xg_gamedata：在用户电脑上用用户自己的 APK / 录制生成小光游戏数据。

统一入口：``python -m xg_gamedata <子命令>``。

子命令名和逐行 JSON 输出格式是给安装向导的约定：
每行 ``{"stage","done","total","eta_s","msg"}``，最后一行 ``{"ok":true/false,...}``。
"""
from __future__ import annotations

__version__ = "1.0.0"
