# -*- coding: utf-8 -*-
"""遇境神坛每日季蜡（季卡专属每日额外 1 根）。

机制（官方 FAQ + 仓库实证）：
- 季蜡每日人人 5 根：4 个季任务各 1 根 + 4 簇季烛光合成 1 根；
- **持有季卡（Season Pass / Adventure Pass）的旅人，在遇境（Home）每日可额外领 1 根**，
  上限 6 根。没季卡时神坛没有可领取的季蜡 —— 服务端不会给出这个拾取物。

仓库实证：`research/experiments/游戏接口程序集_源码.txt:2568` 子程序 `SKY_遇境_季节蜡烛`
（易语言国际服机器人，被 `SKY_执行_每日任务` 收尾调用）：
    POST /account/collect_pickup_batch
    {"level_id":3526133726, "pickup_ids":[], "global_pickup_ids":[4180017154], "emitters":[]}
且该机器人的 `访问请求头` helper 不塞 `x-sky-level-id` —— 与国服 collect 的
`include_level_id=False` 一致。提炼记录见 docs/知识库/协议与逆向/28_易语言国际服游戏接口实现.md:224/:236。

为什么以前领不到：神坛季蜡是 **global pickup**（不在任何一张图的 `pickup_ids` 名单里），
而跑图链路只发 `pickup_ids`、`global_pickup_ids` 恒为 `[]` —— 于是有季卡的号也永远收不到它。

判据（不猜季卡，以服务端入账为准）：
  GET 钱包 before → collect(global) → 空响应则进遇境图(3526133726)重试 → GET 钱包 after；
  season_candle / season_wax 差值 > 0 = 真入账；= 0 且 r=already = 今日已领；
  **r=missing_season_pass = 服务端判定无季卡**（2026-09-19 生产实跑实测，无季卡的号返回
  {"r":"missing_season_pass","id":4180017154}）—— 这就是「没季卡时没有可领取季蜡」的服务端权威答复。
⚠️ `currency.season_pass_token` **不能**当季卡判据：扫了 174 台生产机位（含有季蜡的号）它全是 0。
顺带结论：**在线即可收到该 global pickup 的逐项结果，无需先进遇境图**（实跑首发包就拿到 r=missing_season_pass）；
进图重试只在「响应空表（未就绪）」时才走。
"""
from __future__ import annotations

import threading
import time

from . import pickup, session as cnsession
from .skyapi import SkyApiError

HOME_LEVEL_ID = 3526133726              # 遇境（= session.CN_LEVEL_ID；国服可进、collect 有 batch_results，已实测）
HOME_SEASON_CANDLE_PICKUP = 4180017154  # 神坛每日季蜡（global_pickup）
ONLINE_ATTEMPTS = 3
COLLECT_ATTEMPTS = 3
RETRY_SLEEP = 0.8
CREDENTIAL_STATUSES = (401, 403, 423)


def _obj(resp: dict | None, key: str) -> dict:
    """取业务对象：顶层有 key 用顶层，否则进 data 层；都没有返回 {}。"""
    if not isinstance(resp, dict):
        return {}
    v = resp.get(key)
    if isinstance(v, dict):
        return v
    d = resp.get("data")
    if isinstance(d, dict) and isinstance(d.get(key), dict):
        return d[key]
    return {}


def _results(resp: dict | None) -> list:
    """collect 响应的 batch_results（空表 = 未就绪 / 服务端没有这个拾取物）。"""
    if not isinstance(resp, dict):
        return []
    d = resp.get("data") if isinstance(resp.get("data"), dict) else resp
    arr = d.get("batch_results") if isinstance(d, dict) else None
    return arr if isinstance(arr, list) else []


def read_season(client) -> dict:
    """读钱包里季蜡相关字段。读失败返回 0（不让它把任务搞挂）。"""
    try:
        cur = _obj(client.get_currency(), "currency")
    except Exception:
        return {"season_candle": 0, "season_wax": 0, "pass_token": 0}

    def n(k: str) -> int:
        try:
            return int(cur.get(k) or 0)
        except (TypeError, ValueError):
            return 0

    return {"season_candle": n("season_candle"), "season_wax": n("season_wax"),
            "pass_token": n("season_pass_token")}


def _ensure_online(client, log) -> bool:
    """短连接转在线，最多 3 次（与 daily.ensure_online_3x 同语义；独立实现避免 import 环）。"""
    msg = "未尝试"
    for attempt in range(1, ONLINE_ATTEMPTS + 1):
        online, _cached, msg = cnsession.ensure_online(client)
        if online:
            log("✓ " + msg)
            return True
        if attempt < ONLINE_ATTEMPTS:
            log("转在线重试 %d/%d：%s" % (attempt, ONLINE_ATTEMPTS, msg))
            time.sleep(RETRY_SLEEP)
    log("✗ " + msg)
    return False


def _home_join_id() -> int:
    """遇境图进图用的短值 join_level_id（路线表里查，缺省 55942）。"""
    try:
        for r in pickup.load_routes():
            if int(r.get("map_id") or 0) == HOME_LEVEL_ID:
                jid = int(r.get("join_level_id") or 0)
                if jid > 0:
                    return jid
    except Exception:
        pass
    return cnsession._p("lh", cnsession.CN_LEVEL_HASH)


def _collect_home(client, log) -> list:
    """发一次神坛季蜡收取（global pickup），返回 batch_results。"""
    try:
        return _results(client.collect_pickup_batch(HOME_LEVEL_ID, [], [HOME_SEASON_CANDLE_PICKUP]))
    except SkyApiError as e:
        if e.status in CREDENTIAL_STATUSES:
            raise                      # 会话失效交给任务层续期重试
        log("[神坛季蜡] collect HTTP %s：%s" % (e.status, str(e)[:100]))
        return []
    except Exception as e:
        log("[神坛季蜡] collect 异常：%s" % str(e)[:120])
        return []


def claim(client, log=lambda m: None, stop_flag: threading.Event | None = None,
          ensure_online: bool = True) -> dict:
    """领遇境神坛每日季蜡。

    ensure_online=True 时自己转在线（独立任务入口用）；从每日任务调用时传 False，
    复用那一次「已转在线」的窗口，不额外开一次 UDP。
    """
    if ensure_online and not _ensure_online(client, log):
        return {"ok": False, "gained": 0, "summary": "转在线失败"}

    before = read_season(client)
    log("[神坛季蜡] 领取前：季蜡 %d · 颗粒 %d · 季卡 %d"
        % (before["season_candle"], before["season_wax"], before["pass_token"]))

    results = _collect_home(client, log)
    if not results:
        # 空响应 = 未进图/未就绪 → 进遇境图后重试（该图国服可进、collect 有 batch_results）
        build = cnsession.resolve_build(client.client_version)
        ok, sid, msg = cnsession.enter_map(client, HOME_LEVEL_ID,
                                           join_level_id=_home_join_id(), build_number=build)
        if not ok:
            log("[神坛季蜡] 进遇境失败：%s" % msg)
        else:
            try:
                for attempt in range(1, COLLECT_ATTEMPTS + 1):
                    if stop_flag is not None and stop_flag.is_set():
                        break
                    results = _collect_home(client, log)
                    if results:
                        break
                    time.sleep(RETRY_SLEEP)
            finally:
                cnsession.POOL.close(sid)

    after = read_season(client)
    dc = after["season_candle"] - before["season_candle"]
    dw = after["season_wax"] - before["season_wax"]
    gained = dc + dw
    already = any(isinstance(x, dict) and str(x.get("r")) == "already" for x in results)
    ok_new = any(isinstance(x, dict) and str(x.get("r")) == "ok" for x in results)
    # ★ 服务端权威季卡判据（2026-09-19 生产实跑实测）：无季卡的号对该 global pickup 返回
    #   {"r":"missing_season_pass","id":4180017154}。currency.season_pass_token **不能**当季卡判据
    #   （扫了 174 台机位，包括有季蜡的号，season_pass_token 全是 0）。
    missing_pass = any(isinstance(x, dict) and str(x.get("r")) == "missing_season_pass" for x in results)
    has_pass = (not missing_pass) and (ok_new or already
                                      or after["pass_token"] > 0 or before["pass_token"] > 0)

    if gained > 0:
        log("[神坛季蜡] ✓ 入账：季蜡 %+d · 颗粒 %+d（季蜡 %d→%d）"
            % (dc, dw, before["season_candle"], after["season_candle"]))
        return {"ok": True, "gained": gained, "gained_candle": dc, "gained_wax": dw,
                "has_pass": True, "already": already, "results": results,
                "summary": "遇境神坛季蜡 +%d" % gained}
    if ok_new:
        log("[神坛季蜡] ✓ 服务端 r=ok（钱包快照未刷新）")
        return {"ok": True, "gained": 1, "gained_candle": 0, "gained_wax": 0,
                "has_pass": True, "already": False, "results": results,
                "summary": "遇境神坛季蜡 +1"}
    if already:
        log("[神坛季蜡] 今日已领过（r=already）")
        return {"ok": True, "gained": 0, "has_pass": True, "already": True, "results": results,
                "summary": "神坛季蜡今日已领"}
    if missing_pass:
        log("[神坛季蜡] 服务端判定无季卡（r=missing_season_pass）")
        return {"ok": True, "gained": 0, "has_pass": False, "already": False,
                "reason": "missing_season_pass", "results": results,
                "summary": "无季卡·无可领取"}
    log("[神坛季蜡] 无可领取（%s）" % ("有季卡但本次未入账" if has_pass else "无季卡"))
    return {"ok": True, "gained": 0, "has_pass": has_pass, "already": False, "results": results,
            "summary": "有季卡·本次未入账" if has_pass else "无季卡·无可领取"}
