# -*- coding: utf-8 -*-
"""送心（国服）：跳过「等三天 + 先解锁击掌 + 提信任等级」，直接送出爱心。

★★ 2026-10-04 真机抓包 + 机位实跑定案：**首选 `send_gift_direct`** ★★
====================================================================
真机抓包（ProxyPin，玩家 0.16.5.412659）证明客户端送心就是**一个 HTTP 请求**：

    POST /account/send_message
    x-sky-level-id: 3526133726                       ← 遇境，不是送心房间
    {"user": <自己 uuid>, "body": <b64 RC4 密文>}
        ↑ body 明文（RC4 后 208B，与抓包密文**字节级吻合**）：
          {"target": <好友 uuid>, "gift_type": "gift"}

机位实测（`l2824206722`，3 连发全部 `result:ok`）：

    蜡烛 5365 → 5362 → 5359 → 5356     每次精确 −3
    sent_message = {msg_id, type:"gift", to_id, currency_type:"candles",
                    currency_count:3, recvd:false, gift_id}

**不需要**：同房间、击掌(4)、tier_up、收方 session3、等三天。
收心方可以是**任意好友**（89 个里随便挑都能送）。
WS（`/account/ws`）在送心全程**只是保活通道**，业务帧不经过它。

`send_heart_direct`（同房 give_candle 链）保留作为兜底
-----------------------------------------------------
它仍是可用的另一条路（2026-10-02 打通），但要求**双方都是站内机位**
（`ability_token` 必须是被送方的 `session3`），且要造两张房间票。
`send_gift_direct` 没有这些限制，**优先用它**。

旧的同房链（`send_heart_direct`）要点
------------------------------------
    ① 收心方 enter_map(ROOM_MAP_ID)                 ← 它先进，服务端建房间
    ② 送心方 enter_map(ROOM_MAP_ID, rejoin_uid=收心方) ← 带 rejoin 进**同一张票**
    ③ 送心方 give_candle(7, 3, recv_user=收心方)      ← x-sky-level-id 必须是 ROOM_MAP_ID

用户感知的两道门都不是送心接口自己的要求
---------------------------------------
  * 「等三天」= `iap_gift_min_friendship_days:3`
    （docs/知识库/协议与逆向/18_国际服完整接口文档.md:112），只管 **IAP 礼包赠送**；
  * 「击掌 / 信任等级」= 关系能力链的中间步骤（`give_candle(4,1)` + `tier_up`），
    真号实测证明送心不经过它们。

送出后怎么判断真成功
--------------------
看响应里的 `currency.candles` 是否 −3，或对比送前送后的 `/account/get_currency`。
`result:ok` 但蜡烛没扣 = 假成功（`send_gift_direct` 已把 currency 带回来）。

房间 id 的来源
--------------
`3429122502` = FNV1a("Prairie_WildLifePark")（云野·野生动物园，
docs/知识库/协议与逆向/45_地图id与场景名的换算_FNV1a.md:69），
沿用 `scratch/_archive/probes-20260921/heart_test.py` 的实测值。

⚠ 不要在这条链里调 `free_gifts/send`：那是另一条「免费礼物」路径，不属于送心链，
  实测返回 `bad_ability`（scratch/_archive/probes-20260921/heart_test.py:243）。
"""
from __future__ import annotations

import time

P_GIVE_CANDLE = "/account/give_candle"
P_TIER_UP = "/service/trust/api/v1/tier_up"
P_SEND_MESSAGE = "/account/send_message"
P_GET_CURRENCY = "/account/get_currency"
P_FRIEND_GIST = "/service/relationship/api/v1/get_all_friend_gist"

GIFT_TYPE_HEART = "gift"        # send_message 的 gift_type；送心固定这个值
GIFT_COST_CANDLES = 3           # 一颗心 = 3 蜡烛（响应里的 currency_count）

ABILITY_NICKNAME = 1     # 解锁昵称（不再需要，保留给别的关系能力调用）
ABILITY_HIGH_FIVE = 4    # 解锁击掌（**送心不需要**）
ABILITY_HEART = 7        # 送爱心本身
ABILITY_HEAL = 253       # 救光

COST_NICKNAME = 1
COST_HIGH_FIVE = 1
COST_HEART = 3           # 知识库：ability_id=7 → cost=3

# 送心房间（云野·野生动物园）。两张票都开在这里，送心的 level 头也用这个 id。
ROOM_MAP_ID = 3429122502
JOIN_LEVEL_ID = 55942
ROOM_SETTLE_SEC = 1.5    # 收心方先进，等它的成员登记在服务端落库（heart_test.py 实测值）
ROOM_RETRY_GAP_SEC = 2.0 # invalid_room 重试前多等的这一下
ROOM_GIVE_ATTEMPTS = 2   # 首送 + 一次重送

STATE_OK = "ok"
STATE_ALREADY = "already"              # 该能力此前已解锁（能力一次性）
STATE_ALREADY_TODAY = "already_today"  # 今天已送过（爱心每天一次）
STATE_REJECTED = "rejected"
STATE_SKIPPED = "skipped"

# 三种都算「已完成」，不是失败
DONE_STATES = (STATE_OK, STATE_ALREADY, STATE_ALREADY_TODAY)

_ZH = {
    STATE_OK: "成功",
    STATE_ALREADY: "能力已解锁（跳过）",
    STATE_ALREADY_TODAY: "今天已送过（跳过）",
    STATE_REJECTED: "失败",
    STATE_SKIPPED: "跳过",
}


def classify(resp) -> str:
    """give_candle 回执 → 归一状态。

    只有 `result:false` 且不带 already* 才算真失败；res=already / already_today
    是幂等成功（SkyHeartApi / HeartTaskApi 的判据一致）。
    """
    if not isinstance(resp, dict):
        return STATE_OK
    res = str(resp.get("res") or "").strip().lower()
    if res == "already":
        return STATE_ALREADY
    if res == "already_today":
        return STATE_ALREADY_TODAY
    if resp.get("result") is False:
        return STATE_REJECTED
    return STATE_OK


def failure_reason(resp) -> str:
    """真失败时尽量取服务端给的原因名。

    已知名（SkyHeartApi）：`missing_tier`（前一级能力没解锁 / 关系等级不够）、
    `bad_cost`、`invalid_ability_token`。
    2026-10-02 真号实跑新增 `invalid_room`：服端认定「这次施放没有合法房间上下文」，
    且**先于** ability_token 校验（换缺省/垃圾/本人 token，回执一模一样）。
    """
    if not isinstance(resp, dict):
        return ""
    for k in ("err", "error", "msg", "message", "reason"):
        v = resp.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    res = str(resp.get("res") or "").strip()
    if res and res.lower() not in ("ok", "already", "already_today"):
        return res
    return ""


# 服端原因名 → 面板能直接给管理员看的大白话
REASON_ZH = {
    "invalid_room": "服务端没认到同房（送心房里没同时登记上两人，已自动重试）",
    "invalid_ability_token": "ability_token 不对（必须填被送方的 session3）",
    "missing_tier": "关系等级不够（送心不该出现，出现了说明服端又加了前置）",
    "bad_cost": "蜡烛花费不对（服端认为这个能力不该花这么多）",
    "bad_ability": "这个 ability_id 不在关系能力表里",
    "bad_recv_user": "收方光遇 ID 不对",
    "already": "之前已经解锁过",
    "already_today": "今天已经送过了",
}


def reason_zh(reason: str) -> str:
    """原始原因名 → 中文解释；没收录的原样返回，方便继续加。"""
    r = str(reason or "").strip()
    return REASON_ZH.get(r, r)


def give_candle(client, ability_id: int, cost: int, recv_user: str,
                ability_token: str = "", level_id: int | None = None) -> dict:
    """单次关系能力调用。返回 {state, reason, raw}。

    `level_id` 只在送心（ability 7）时传 `ROOM_MAP_ID`：服务端按这个头判定
    「这次施放发生在哪个房间」，用遇境 id 会直接 `invalid_room`。
    """
    body = {
        "ability_id": int(ability_id),
        "ability_cost": int(cost),
        "recv_user": str(recv_user),
    }
    tok = str(ability_token or "").strip()
    if tok:
        # 空 token 绝不写进 body：服务端会把「填了但非法」直接判 invalid；
        # 缺字段反而让它走到能力前置检查，错误信息更有意义。
        body["ability_token"] = tok
    resp = client.post(P_GIVE_CANDLE, body, level_id=level_id)
    return {"state": classify(resp), "reason": failure_reason(resp), "raw": resp}


def tier_up(client, target: str) -> dict:
    """提信任等级（官方「解锁关系能力」）。

    `/service/*` **不能**带 `x-sky-level-id`，否则 HTTP 400 Invalid Request
    （docs/知识库/协议与逆向/08_国服接入.md:246）→ include_level_id=False。

    送心链**不再需要**它（真号实测 7/3 直接过），留着给别的关系能力调用。
    """
    resp = client.post(P_TIER_UP, {"target": str(target)}, include_level_id=False)
    return {"ok": bool(isinstance(resp, dict) and resp.get("success")), "raw": resp}


def _step(key, label, state, reason="", zh="", ability_id=0, cost=0) -> dict:
    return {
        "key": key,
        "label": label,
        "ability_id": ability_id,
        "cost": cost,
        "state": state,
        "zh": zh or _ZH.get(state, state),
        "reason": reason,
        "reason_zh": reason_zh(reason) if reason else "",
    }


def get_currency(client) -> dict:
    """读蜡烛等货币。送心前后对比用（`candles` 每次应 −3）。"""
    resp = client.post(P_GET_CURRENCY, {})
    return resp if isinstance(resp, dict) else {}


def list_friends(client) -> list[dict]:
    """拉好友明细（含 `is_online`）。

    网易在失败时会把字段写成**字符串** `"undefined"`（不是 JSON null）——
    所以下面所有取值都做类型判定，别直接 `.get()`。
    """
    resp = client.post(P_FRIEND_GIST, {})
    if not isinstance(resp, dict):
        return []
    for key in ("friends", "friend_ids", "all_friends", "set_friends"):
        rows = resp.get(key)
        if not rows:
            continue
        out = []
        for f in rows:
            if not isinstance(f, dict):
                continue
            fid = f.get("friend_id") or f.get("id") or f.get("uuid")
            if not fid:
                continue
            online = f.get("is_online")
            out.append({
                "friend_id": str(fid),
                "nickname": str(f.get("nickname") or ""),
                # 只认真正的布尔/0/1，字符串 "undefined" 一律当未知（None）
                "is_online": online if isinstance(online, bool) else None,
            })
        return out
    return []


def list_friend_ids(client) -> list[str]:
    """拉好友 uuid 列表（`/service/relationship/api/v1/get_all_friend_gist`）。

    ⚠ 必须走**默认**参数（带 `x-sky-level-id`）——手动传 `include_level_id=False`
    会被网易回 `HTTP 400 {"status":"Invalid Request"}`（2026-10-04 实测）。
    """
    return [f["friend_id"] for f in list_friends(client)]


def send_gift(client, target: str, ws=None) -> dict:
    """★ 送一颗心给 `target`（好友 uuid）。**单请求**，真机抓包 + 机位实跑定案。

    ⚠⚠ **必须先有一条活跃的 `/account/ws` 连接**，否则服务端一律回
       `{"result":"not_online"}` 且蜡烛不扣 —— 2026-10-04 三组对照实验证实：

           不连 WS → not_online ×3
           连着 WS → ok ×2（蜡烛 5356→5353→5350）
           断开后  → not_online ×3

       `not_online` 的真实含义是「**发送方**没有活跃会话通道」，不是「对方不在线」。
       所以正常用法是 `send_gift_many` / `send_gift_with_verify`（它们内部自动挂 WS），
       直接调本函数时要自己传 `ws`（见 `skyws.keepalive_for`）。

    实测（2026-10-04，机位 l2824206722）：
        send_gift(c, "00499d08-…") → {"ok": True, "candles": 5353, ...}

    返回 {ok, result, candles, sent_message, raw}。
      * `ok`      —— `result == "ok"`
      * `candles` —— 响应里带回来的蜡烛余额（网易在送礼响应里直接给了）
    其他业务码：`already_pending`（有笔待处理的心）、`not_online`（无 WS 通道）。
    """
    tgt = str(target or "").strip()
    if not tgt:
        raise ValueError("send_gift 需要 target（好友 uuid）")

    inner = {"target": tgt, "gift_type": GIFT_TYPE_HEART}
    # `/account/*` 带 x-sky-level-id；CN_LEVEL_ID 由 SkyClient 默认给（= 遇境 3526133726）
    resp = client.post(P_SEND_MESSAGE, inner)
    resp = resp if isinstance(resp, dict) else {}

    cur = resp.get("currency") or {}
    # ⚠ 失败时 `sent_message` 是字符串 `"undefined"`（不是 null/{}），必须判型
    sm = resp.get("sent_message")
    sm = sm if isinstance(sm, dict) else {}
    return {
        "ok": resp.get("result") == "ok",
        "result": str(resp.get("result") or ""),
        "candles": cur.get("candles") if isinstance(cur, dict) else None,
        "sent_message": sm,
        "raw": resp,
    }


def send_gift_with_verify(client, target: str, before_candles: int | None = None,
                          ws=None) -> dict:
    """`send_gift` + **蜡烛核对**，防「假成功」。

    `result:ok` 是网易的应答，但真正的判据是蜡烛有没有真扣。实测每次都精确 −3。
      * `before_candles` 为 None 时先读一次 `/account/get_currency`
      * 响应里已带 `currency` 就直接用，省一次请求
    返回在 `send_gift` 的结果上补 {before, after, delta, verified}。
    """
    before = before_candles
    if before is None:
        try:
            before = (get_currency(client).get("currency") or {}).get("candles")
        except Exception:  # noqa: BLE001 —— 读不到余额不该让送礼本身失败
            before = None

    r = send_gift(client, target, ws=ws)
    after = r.get("candles")
    if after is None:
        try:
            after = (get_currency(client).get("currency") or {}).get("candles")
        except Exception:  # noqa: BLE001
            after = None

    delta = None
    if isinstance(before, int) and isinstance(after, int):
        delta = after - before
    r.update({
        "before": before,
        "after": after,
        "delta": delta,
        # 只要有余额对比就看差额，没有余额就退回 `result:ok`（无法核对时不误报失败）
        "verified": bool(r["ok"] and (delta == -GIFT_COST_CANDLES if delta is not None else True)),
    })
    return r


def send_gift_one(client, target: str, keepalive: bool = True) -> dict:
    """单好友送心 = `send_gift_with_verify` + **自动挂 WS 保活**。

    ★ `send_message` 必须有活跃的 `/account/ws`，否则一律 `not_online`（蜡烛不扣）。
      单发场景也必须走这里，别直接调 `send_gift_with_verify`（那个默认不带 WS）。
    """
    from . import skyws

    ws = skyws.keepalive_for(client) if keepalive else None
    # ⚠ `ws_ok` 必须在 close() **之前**取：close() 会把 alive 置 False，
    #    在 finally 之后读永远是 False（2026-10-05 踩过，误报成「WS 没连上」）。
    ws_ok = bool(ws and ws.alive)
    r: dict = {}
    try:
        r = send_gift_with_verify(client, target, ws=ws)
    finally:
        if ws:
            if r.get("ok"):
                time.sleep(skyws.WS_LINGER_SEC)
            ws.close()
    r["ws_ok"] = ws_ok
    return r


def send_gift_many(client, targets, gap_sec: float = 0.35, stop_on_fail: bool = True,
                   online_only: bool = False, ws=None, keepalive: bool = True) -> dict:
    """批量送心（好友 uuid 列表）。默认每次间隔 0.35s（真机抓包实测客户端约 300ms）。

    ★★ **自动挂 `/account/ws` 保活**（`keepalive=True`，默认开）★★
       没有活跃 WS 时 `send_message` 一律回 `not_online` 且蜡烛不扣 —— 2026-10-04
       三组对照实验：不连 WS → not_online×3；连着 WS → ok×2（蜡烛真扣）；断开后 →
       又是 not_online×3。整批只连**一条** WS，全程复用（见 qfsky/skyws.py）。
       自己已经有 ws 就传进来复用（`ws=`），不会重复连。

    `online_only=True` 时先用 `list_friends` 过滤出 `is_online is True` 的再送。
    ⚠ 实测该字段 **89/89 全 False**，是个不可用字段 —— 用它会把整张表筛空。
       保留参数只为兼容，**正常别开**。

    返回 {ok, sent, failed, skipped, results, before, after, ws_ok, ...}：
      * `sent`/`failed` —— 成功/失败条数；`skipped` —— 被 `online_only` 筛掉的
      * `ws_ok` —— WS 是否连上（连不上时全批会 not_online，前端要提示）
      * 遇到第一个失败默认停（`stop_on_fail=True`）—— 会话失效/限流时别把整张好友表
        白刷一遍；要看全量就传 False。
    """
    from . import skyws  # 惰性导入，避免和 skyapi 形成循环

    rows: list[dict] = []
    sent = failed = skipped = 0
    running = None

    todo = [str(t).strip() for t in targets if str(t or "").strip()]
    if online_only:
        online = {f["friend_id"] for f in list_friends(client) if f.get("is_online")}
        kept = [t for t in todo if t in online]
        skipped = len(todo) - len(kept)
        todo = kept

    own_ws = ws is None and keepalive
    if own_ws:
        ws = skyws.keepalive_for(client)
    # ⚠ 同 send_gift_one：close() 会把 alive 置 False，必须在关闭前取
    ws_ok = bool(ws and getattr(ws, "alive", False))

    try:
        for i, tgt in enumerate(todo):
            # ★ 每次送礼前确保 WS 还活着：实测同一条连接在两次送礼之间会掉，
            #   掉了之后 send_message 就回 not_online（蜡烛不扣）—— 单发看不出来，
            #   批量才会暴露成「第一个成功、后面全失败」。断了就重连。
            if i:
                ws = skyws.ensure_alive(ws, client)
            if own_ws:
                ws_ok = ws_ok or bool(ws and getattr(ws, "alive", False))

            r = send_gift_with_verify(client, tgt, running, ws=ws)
            r["target"] = tgt
            rows.append(r)
            if r["ok"]:
                sent += 1
                if isinstance(r.get("after"), int):
                    running = r["after"]
            else:
                failed += 1
                if stop_on_fail:
                    break
            if i + 1 < len(todo):
                time.sleep(max(0.0, gap_sec))
        # 送完再保持一小会儿：礼物消息要在这条通道上推给对端
        if own_ws and sent:
            time.sleep(skyws.WS_LINGER_SEC)
    finally:
        if own_ws and ws:
            ws.close()

    first = rows[0].get("before") if rows else None
    last = rows[-1].get("after") if rows else None
    return {
        "ok": failed == 0 and sent > 0,
        "sent": sent,
        "failed": failed,
        "skipped": skipped,
        "before": first,
        "after": last,
        "ws_ok": ws_ok,
        "reconnects": getattr(ws, "reconnects", 0) if ws else 0,
        "results": rows,
    }


def send_heart_direct(recv_client, sender_client, recv_uid: str, recv_session3: str) -> dict:
    """把两张票开进送心房间，然后直接 give_candle(7,3)。返回 {ok, state, reason, error, steps}。

    参数（两个 client 都由调用方用 `tasks.make_client` / 续期后的账号构造）：
      * `recv_client`  —— 收心方的 client（它先进房，它的 session3 当 ability_token）
      * `sender_client`—— 送心方的 client（带 rejoin_uid 进同一张票，然后发能力）
      * `recv_uid`     —— 收心方光遇 ID（`give_candle.recv_user`）
      * `recv_session3`—— 收心方 session3（`give_candle.ability_token`）

    房间由**服务端侧**自动造：收心方先 enter_map 建房间，送心方 rejoin 进同一张票，
    各自等 1.5s 让服务端把成员登记落库（heart_test.py 实测值）。失败时整组会话
    一定在 finally 里 close，不留残留连接。
    """
    from . import session as cnsession  # 惰性导入：session ↔ hearts 互相不吵

    uid = str(recv_uid or "").strip()
    token = str(recv_session3 or "").strip()
    if not uid:
        raise RuntimeError("缺少收方光遇 ID")
    if not token:
        raise RuntimeError("收方不是站内机位（缺 session3，无法作为 ability_token）")

    steps: list[dict] = []
    sids: list[str] = []

    def enter(client, rejoin_uid=None):
        ok, sid, msg = cnsession.enter_map(
            client, ROOM_MAP_ID, join_level_id=JOIN_LEVEL_ID, rejoin_uid=rejoin_uid)
        if sid:
            sids.append(str(sid))
        return bool(ok), str(msg or "")

    try:
        ok, msg = enter(recv_client)
        if not ok:
            steps.append(_step("recv_room", "收心方进送心房间", STATE_REJECTED,
                               reason=msg or "拉票/进图失败"))
            return _result(steps, "收心方进房失败")
        steps.append(_step("recv_room", "收心方进送心房间", STATE_OK, zh="已进房"))
        time.sleep(ROOM_SETTLE_SEC)

        ok, msg = enter(sender_client, rejoin_uid=uid)
        if not ok:
            steps.append(_step("sender_room", "送心方进同一张票", STATE_REJECTED,
                               reason=msg or "拉票/进图失败"))
            return _result(steps, "送心方进房失败")
        steps.append(_step("sender_room", "送心方进同一张票", STATE_OK,
                           zh="已进房（和收心方同一个房间实例）"))
        time.sleep(ROOM_SETTLE_SEC)

        for n in range(1, ROOM_GIVE_ATTEMPTS + 1):
            key = "heart" if n == 1 else "heart_retry"
            label = "直接送爱心" if n == 1 else "直接送爱心（重试）"
            r = give_candle(sender_client, ABILITY_HEART, COST_HEART, uid, token,
                            level_id=ROOM_MAP_ID)
            steps.append(_step(key, label, r["state"], reason=r["reason"],
                               ability_id=ABILITY_HEART, cost=COST_HEART))
            if r["state"] in DONE_STATES:
                return _result(steps, "")
            if r["reason"] != "invalid_room" or n >= ROOM_GIVE_ATTEMPTS:
                return _result(steps, "送爱心被拒")
            # 房间成员登记有延迟时服端会先回 invalid_room：等一会儿原样再发一次
            time.sleep(ROOM_RETRY_GAP_SEC)
        return _result(steps, "送爱心被拒")
    finally:
        for sid in sids:
            try:
                cnsession.POOL.close(sid)
            except Exception:  # noqa: BLE001
                pass


def _result(steps: list[dict], err: str) -> dict:
    # 取**最后**一个 heart/heart_retry 步（重试过就看重试那次的结果）
    heart = next((s for s in reversed(steps) if str(s["key"]).startswith("heart")), None)
    ok = bool(heart and heart["state"] in DONE_STATES)
    reason = str((heart or {}).get("reason") or "")
    if not ok and not reason:
        # 没走到送爱心（或送爱心被拒但没给原因）时，把最早失败那一步的原因带出来，
        # 否则面板上只剩「被拒」两个字，看不出是 invalid_room 还是 bad_cost。
        bad = next((s for s in steps if s["state"] not in DONE_STATES), None)
        reason = str((bad or {}).get("reason") or "")
    return {
        "ok": ok,
        "state": str((heart or {}).get("state") or ""),
        "reason": reason,
        "reason_zh": reason_zh(reason),
        "error": "" if ok else (reason or err or "失败"),
        "error_zh": "" if ok else reason_zh(reason or err or "失败"),
        "steps": steps,
    }

