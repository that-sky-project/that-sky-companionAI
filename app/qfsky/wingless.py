# -*- coding: utf-8 -*-
"""强制无翼（光翼清零）：把 wing_buffs 的 collected 清成 0 并长期维持。

背景（2026-10-04 A/C 两阶段实测定案，见 docs/知识库/游戏功能/91_强制无翼与隐形斗篷_总纲.md）：
- `wing_buffs/drop` 是**改标记**不是删行：清完 6 行仍在、全是 collected=false；
- 客户端全表（327 条 REST 路径）里 wing 家族**只有 5 个端点**，没有任何 reset/delete 动词，
  服务端对 21 个候选动词也一律 404；unlock 域不是翼的第二处表示。
  ⇒ **真·null 态（wing_buffs: []）用已知通道做不到**，本模块能到的是「0 态」
  （有行、collected 全 false），也就是「一直处于无翼状态」这个效果本身。

★ 硬约束（写死在代码里，不做成配置项）：
  1) **绝不调用 deposit / convert** —— 这两个把光翼换成红蜡，不可逆；
  2) drop 前必须短转在线（与 run_sacrifice 同序，否则写操作不生效）；
  3) collectible_only=True 时只清「在 get_map_defs 清单里」的翼，清单外的**不动**并记进
     skipped —— 保证清掉的翼随时能按名 collect 回来（可回滚）。
"""
from __future__ import annotations

import time

from .daily import ensure_online_3x
from . import eden

# 光翼状态四态（与 scratch/_a_wingstate.py 的 classify 同义，命名对齐）
STATE_EMPTY = "EMPTY_LIST"          # 从未收过翼：wing_buffs 存在且为空 ← 真·null 态
STATE_ZERO = "ROWS_NO_COLLECTED"    # 有行、collected 全 false ← 本功能的稳定终态
STATE_HELD = "HAS_COLLECTED"        # 身上有光翼（需要清）
STATE_UNREADABLE = "UNREADABLE"     # 读不到（会话失效/限流）

DROP_PAUSE = 0.4                    # drop 批次间隔（与 A 阶段 roundtrip 实测同步）
DROP_BATCH = int(getattr(eden, "DROP_BATCH", 20) or 20)


def _rows_of(resp) -> list:
    rows = eden._data_obj(resp).get("wing_buffs") if resp is not None else None
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def read_state(client) -> dict:
    """只读体检。**不写任何东西**：只发一次 wing_buffs/get（include_level_id=False）。

    返回 {state, rows, collected, deposited, held, deposited_names, error}
    · held = collected==true 的翼名清单（要清的就是它）
    · deposited_names = 已献祭过的翼名（这些**收不回来**，见 docstring 铁律 1）
    """
    try:
        resp = eden._post_or_none(client, eden.GET_PATH, {}, False)
    except Exception as e:                      # 网络/会话异常
        return {"state": STATE_UNREADABLE, "error": str(e)[:200]}
    if resp is None:
        return {"state": STATE_UNREADABLE, "error": "wing_buffs/get 无响应"}
    rows = _rows_of(resp)
    held = [str(r.get("name")) for r in rows if r.get("collected")]
    deposited = [str(r.get("name")) for r in rows if r.get("deposited")]
    if not rows:
        state = STATE_EMPTY
    elif not held:
        state = STATE_ZERO
    else:
        state = STATE_HELD
    return {"state": state, "rows": len(rows), "collected": len(held),
            "deposited": len(deposited), "held": held, "deposited_names": deposited,
            "error": ""}


def _collectible_names(client) -> set | None:
    """get_map_defs(type==wing_buff) 的翼名集合；取不到返回 None（=校验不可用）。"""
    try:
        resp = eden._post_or_none(client, eden.GET_MAP_DEFS_PATH, {}, True)
        if resp is None:
            return None
        names = eden._wing_buff_names_from_map_defs(resp)
        return set(names) if names else None
    except Exception:
        return None


def ensure_wingless(client, log=None, stop_flag=None, collectible_only: bool = True) -> dict:
    """幂等：已经是无翼就直接返回；否则转在线 → 分批 drop → 复读确认。

    collectible_only=True（默认）：只清在 get_map_defs 里的翼；清单外的只保留不动，
    并写进 summary 的 skipped —— 这是「可回滚」的前提。
    """
    def _log(m):
        if log is not None:
            log(m)

    before = read_state(client)
    if before["state"] == STATE_UNREADABLE:
        return {"ok": False, "summary": "读不到光翼状态：%s" % before.get("error"),
                "state": STATE_UNREADABLE, "dropped": 0}
    if before["state"] == STATE_EMPTY:
        _log("[强制无翼] 该机位从未收过光翼（wing_buffs 为空 = 真·无翼度），无需处理")
        return {"ok": True, "summary": "已是无翼（从未收过光翼）", "state": STATE_EMPTY,
                "rows": 0, "dropped": 0, "skipped": []}
    if before["state"] == STATE_ZERO:
        _log("[强制无翼] 已处于无翼状态（row %d · collected 0），零写入返回" % before["rows"])
        return {"ok": True, "summary": "已无翼（row %d · collected 0）" % before["rows"],
                "state": STATE_ZERO, "rows": before["rows"], "dropped": 0, "skipped": []}

    targets = list(before["held"])
    skipped: list = []
    if collectible_only:
        defs = _collectible_names(client)
        if defs is None:
            _log("[强制无翼] ⚠ 取不到光翼定义，无法校验可复原性 → 本次不清（fail-closed）")
            return {"ok": False, "summary": "取不到光翼定义，拒绝清理（不可复原风险）",
                    "state": before["state"], "dropped": 0, "skipped": []}
        skipped = [n for n in targets if n not in defs]
        targets = [n for n in targets if n in defs]
        if skipped:
            _log("[强制无翼] %d 个翼不在 get_map_defs 清单内，跳过不动：%s"
                 % (len(skipped), ", ".join(skipped[:5])))
    if not targets:
        _log("[强制无翼] 没有可安全清理的翼（全部跳过），不动任何状态")
        return {"ok": True, "summary": "无可清理的翼（跳过 %d）" % len(skipped),
                "state": before["state"], "dropped": 0, "skipped": skipped}

    _log("[强制无翼] 当前 row %d · collected %d → 开始清理 %d 个"
         % (before["rows"], before["collected"], len(targets)))
    if not ensure_online_3x(client, lambda m: _log("[强制无翼] " + m)):
        return {"ok": False, "summary": "转在线失败", "state": before["state"],
                "dropped": 0, "skipped": skipped}

    dropped = 0
    batch_ok = 0
    for i in range(0, len(targets), DROP_BATCH):
        if stop_flag is not None and stop_flag.is_set():
            _log("[强制无翼] 已停止：剩余 %d 个未清，可稍后重跑（幂等，已清的不会重复）"
                 % (len(targets) - i))
            break
        chunk = targets[i:i + DROP_BATCH]
        try:
            resp = eden._post_or_none(client, eden.DROP_PATH, {"names": chunk}, False)
        except Exception as e:
            _log("[强制无翼] drop 批次 %d 异常：%s" % (i // DROP_BATCH + 1, str(e)[:120]))
            resp = None
        rows = eden._data_obj(resp).get("update_wing_buffs") if resp is not None else None
        ok = sum(1 for r in (rows or []) if isinstance(r, dict) and r.get("collected") is False)
        dropped += ok
        batch_ok += 1 if ok >= len(chunk) else 0
        _log("[强制无翼] 批次 %d · %d/%d%s" % (i // DROP_BATCH + 1, ok, len(chunk),
              "" if ok >= len(chunk) else "（部分未生效）"))
        time.sleep(DROP_PAUSE)

    after = read_state(client)
    if after["state"] == STATE_UNREADABLE:
        _log("[强制无翼] ⚠ 清理后复读失败，结果以请求受理数为准：%d" % dropped)
        return {"ok": False, "summary": "清理后复读失败（受理 %d）" % dropped,
                "state": before["state"], "dropped": dropped, "skipped": skipped,
                "rows_before": before["rows"]}

    ok_all = after["collected"] == 0
    if not ok_all:
        _log("[强制无翼] ⚠ 仍有 %d 个光翼未清掉，可重跑（幂等）" % after["collected"])
    parts = ["collected %d→%d" % (before["collected"], after["collected"]),
             "row %d→%d" % (before["rows"], after["rows"])]
    if dropped:
        parts.append("丢弃 %d" % dropped)
    if skipped:
        parts.append("跳过 %d" % len(skipped))
    _log("[强制无翼] %s · %s" % ("✓ 完成" if ok_all else "✗ 未完成", " · ".join(parts)))
    return {"ok": ok_all, "summary": " · ".join(parts), "state": after["state"],
            "rows": after["rows"], "collected": after["collected"], "dropped": dropped,
            "skipped": skipped, "rows_before": before["rows"],
            # 可复原清单：清掉的这些随时能按名 collect 回来（前提是没被 deposit）
            "collectible": list(targets), "deposited_names": before["deposited_names"]}
