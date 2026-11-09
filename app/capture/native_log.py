# -*- coding: utf-8 -*-
"""从游戏自己的 logcat 里认“当前在哪张图”（只读，不写游戏、不发请求）。

来源：服务器录制工具的 protocol_hashes.py / import_native_init.py，只保留录制需要的
两个函数（scene_marker / bind）和 NetVersion 常量。判定规则：
  * 相邻两行必须时间戳、PID、TID 完全相同（游戏 native 初始化日志的形状）；
  * 第一行有 NetVersion: [6 个 uint16]、第二行有 levelName: <scene> levelId: <id>；
  * 不保存原始日志行，只保留 scene / level_id / 时间戳。
"""
from __future__ import annotations

import re

MASK = 0xffffffff
TYPE_FIELD = 23536
BUILD = 421589

NET_RE = re.compile(r"\bNetVersion\s*:\s*\[\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,"
                    r"\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\]")
SCENE_RE = re.compile(r"\bscene=([A-Za-z0-9_]{1,96})\b")
LEVEL_NAME_RE = re.compile(r"\blevelName:\s*([A-Za-z0-9_]{1,96})\s+levelId:\s*(\d+)\b")
LEVEL_LABEL_RE = re.compile(r"\bLevel:\s*([A-Za-z0-9_]{1,96})\s+LevelId:\s*(\d+)\b")
PREFIX = re.compile(r"^(\d\d-\d\d \d\d:\d\d:\d\d\.\d{3})\s+(\d+)\s+(\d+)\s+[VDIWEF]\s+.*?:\s*(.*)$")


def scene_marker(line: str):
    match = LEVEL_NAME_RE.search(line) or LEVEL_LABEL_RE.search(line)
    if match and int(match.group(2)) <= MASK:
        return {"scene": match.group(1), "level_id": int(match.group(2)),
                "evidence": "native-scene-log-observed"}
    return None


def log_observations(lines, *, resource_revision: str, scene=None):
    """只产出脱敏后的 tuple（不保留原始行）。"""
    rows = []
    for n, line in enumerate(lines, 1):
        m = NET_RE.search(line)
        if not m:
            continue
        fields = [int(x) for x in m.groups()]
        if any(x > 65535 for x in fields):
            raise ValueError("NetVersion 字段超过 uint16")
        if fields[2] != TYPE_FIELD or fields[3:5] != [0, 0]:
            raise ValueError("NetVersion 不是 421589 构造出来的形状")
        sm = SCENE_RE.search(line)
        mapped_scene = sm.group(1) if sm else scene
        rows.append({"schema": 1, "apk_build": BUILD, "resource_revision": resource_revision,
                     "scene": mapped_scene,
                     "scene_binding": "same-line" if sm else ("operator-asserted" if scene else "unknown"),
                     "netversion_u16": fields, "source_line": n, "evidence": "native-log-observed"})
    if not rows:
        raise ValueError("没有找到 NetVersion 行")
    return rows


def bind(lines, resource_revision, phase):
    """把 NetVersion 行与紧邻的 levelName 行绑成一条“进图完成”观察。"""
    out = []
    for i in range(len(lines) - 1):
        a, b = PREFIX.match(lines[i]), PREFIX.match(lines[i + 1])
        if not a or not b or a.group(1, 2, 3) != b.group(1, 2, 3):
            continue
        if not NET_RE.search(a.group(4)):
            continue
        marker = scene_marker(b.group(4))
        if not marker or "levelName:" not in b.group(4):
            continue
        row = log_observations([a.group(4)], resource_revision=resource_revision)[0]
        row.update(scene=marker["scene"], level_id=marker["level_id"],
                   scene_binding="adjacent-native-init-lines-exact-timestamp-pid-tid",
                   source_lines=[i + 1, i + 2], native_timestamp=a.group(1),
                   observed_phase=phase, online_join_verified=False,
                   map_transition_verified=False, evidence="native-init-observed")
        row.pop("source_line", None)
        out.append(row)
    if not out:
        raise ValueError("没有相邻的 NetVersion/levelName 行（时间戳/PID/TID 必须完全一致）")
    return out
