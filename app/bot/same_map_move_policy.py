"""T73/T74：聊天 WS 同图 move 票的纯路由（无 I/O、无网络、可离线测试）。

背景（见 work/REPORT.md、work/round2/REPORT.md、work/round3/REPORT.md）：
- WS `move` 票字段 `uri/sig/signature/move_ts/platform_filter/other_players`
  与服务器 UDP type 9 Move 的端点+签名同源，表示“这条连接要迁到新 endpoint”。
- 旧代码对同图票只有两种处理：无视（`same_map_move_held`）或**拆掉当前
  连接、用票重新入服**（`move_switch`）。后者实测 1 秒被服务器
  `server_ended_or_changed_connection` 终止，且是在开门前离开主人房间。
- 正确做法：把票交给当前连接的 handoff pump，走服务器自己 Move 用的
  make-before-break 迁移（新 peer 先 Join/EnterGame/注册成功，再退旧 peer）。

T74-R3（中央神坛 DayHubCave 的“配对并房”）：
- 服务器会把同一张配对票**同时**推给原生（大厅）和小光（聊天 WS）：目标
  uri 相同、`move_ts` 相同、`other_players` 互指。door3 抓包 11 张带主人的
  WS 票，原生 11 次 Join 一一对应（见 REPORT §4）。
- 原生每张票都跟；Join 后等 EnterGame 满 6.0s 才放弃，然后在旧连接回
  `0x0a(result=4)`。服务器 ~8s 后重推一张**新 move_ts/新签名**的票，原生
  再跟，一张配对目标最多 3 轮；全失败后才发 `type 9 Move(others=0, split=1)`
  把原生单独挪走。
- 03:27:43 小光只跟了第一张（`arm_pairing` 一次性），之后 7 张带主人的票
  全被 HOLD；而第一张又因为拨号/时机没赶上。于是“配对失败→split→小光追
  split→再配对→再失败”每 ~40s 循环一次，表现就是“小光消失又回来”。
- 本模块的修正：DayHubCave 里**每张新的**带主人配对票都跟（按 uri+move_ts
  去重），直到同一个目标真的 commit 成功（`note_pairing_committed`）；被
  服务器更新的 in-band Move 取代的票（move_ts 不晚于最近一条 Move）不再跟。

本模块只做决策与回调编排，便于在无游戏环境时回归测试。
"""
import time

INBAND = "inband"          # 递交给 pump，按连接内 Move 迁移
HOLD = "hold"              # 不动连接，继续观察
SEGMENT = "segment"        # 旧路径：结束本段，用票重新入服（只在没有 inband 通道时）
IGNORE = "ignore"          # 票无效/无该图录制：直接丢
NO_CAPTURE = "no_capture"
ADOPT_SKIPPED = "adopt_skipped"
SEGMENT_REJECTED = "segment_rejected"

# 中央神坛 DayHubCave：服务器在这里反复做“两人配对并房”。
PAIRING_MAP_IDS = frozenset({2394719185})
# arm_pairing 的有效期：一次带路换场景后 ~10 分钟内的配对都算“带路后的配对”。
_PAIRING_WINDOW_SEC = 600.0
_COMMIT_TTL_SEC = 120.0

_STATE = {
    "armed_until": 0.0,
    "adopted": {},        # ticket_key -> monotonic
    "committed": {},      # uri -> monotonic（该目标已成功同房）
    "last_inband_move_ts": 0,
}


def _now(now):
    return time.monotonic() if now is None else float(now)


def arm_pairing(now=None, ttl=_PAIRING_WINDOW_SEC):
    """带路“同连接换场景”完成时调用：打开配对窗口（不是只跟一张）。"""
    _STATE["armed_until"] = _now(now) + float(ttl)
    _STATE["adopted"].clear()
    _STATE["committed"].clear()


def note_pairing_committed(uri, now=None):
    """配对票 commit 成功且名单里有主人：这个目标已经同房，旧票不必再跟。"""
    if isinstance(uri, str) and uri:
        _STATE["committed"][uri] = _now(now)
        for key in [k for k in _STATE["adopted"] if k == uri or k.startswith(uri + "@")]:
            _STATE["adopted"].pop(key, None)


def note_inband_move(move_ts):
    """服务器 in-band Move 的 move_ts（秒或毫秒）——用于识别被取代的配对票。"""
    try:
        value = int(move_ts)
    except (TypeError, ValueError):
        return
    if value > 10 ** 11:
        value //= 1000
    if value > _STATE["last_inband_move_ts"]:
        _STATE["last_inband_move_ts"] = value


def _armed(now):
    return _now(now) < _STATE["armed_until"]


def _prune(now):
    horizon = _now(now) - _COMMIT_TTL_SEC
    for table in ("adopted", "committed"):
        stale = [k for k, v in _STATE[table].items() if v < horizon]
        for key in stale:
            _STATE[table].pop(key, None)
    if len(_STATE["adopted"]) > 64:
        for key, _ in sorted(_STATE["adopted"].items(), key=lambda kv: kv[1])[:32]:
            _STATE["adopted"].pop(key, None)


def pairing_state():
    """给离线测试/日志用的只读快照（无凭据）。"""
    return {"armed": _armed(None), "armed_until": _STATE["armed_until"],
            "adopted": len(_STATE["adopted"]), "committed": len(_STATE["committed"]),
            "last_inband_move_ts": _STATE["last_inband_move_ts"]}


def reset_state():
    _STATE.update({"armed_until": 0.0, "adopted": {}, "committed": {},
                   "last_inband_move_ts": 0})


def decide_same_map(*, with_owner, owner_stale, seen_count, owner_seen=False,
                    adopt_available=True, owner_present=None, uri_key=None,
                    ticket_key=None, move_ts=None, pairing_map=False, now=None):
    """同图票是否要跟（T74-R3）。

    带主人的票（服务器“配对并房”）：
      1. 同一张票（uri+move_ts）只递一次；
      2. 该 uri 已经 commit 过 → HOLD（already_committed，同房后不再折腾）；
      3. move_ts 不晚于最近一条 in-band Move → HOLD（被 split Move 取代）；
      4. DayHubCave（或主人本段在房间里 / arm 窗口内）→ 跟，**每张新的都跟**。
    不带主人的票：沿用 T74-R2 收紧后的规则（主人还在房间就不跟；主人曾在本段
    但已消失则按住，交给 owner_rejoin；只有 stale/重复推送才跟）。
    """
    now = _now(now)
    _prune(now)
    key = ticket_key or uri_key
    if with_owner:
        if not adopt_available:
            return {"action": SEGMENT, "why": "owner_in_ticket"}
        if key and key in _STATE["adopted"]:
            return {"action": HOLD, "why": "pairing_duplicate"}
        if uri_key and uri_key in _STATE["committed"]:
            return {"action": HOLD, "why": "pairing_already_committed"}
        try:
            ts = int(move_ts) if move_ts is not None else None
        except (TypeError, ValueError):
            ts = None
        if ts is not None and _STATE["last_inband_move_ts"] and ts <= _STATE["last_inband_move_ts"]:
            return {"action": HOLD, "why": "pairing_superseded_by_move"}
        # Claude 审核：只在中央神坛（pairing_map）或带路进中央神坛后的 arm 窗口内跟；
        # 不能因为“主人在房”就跟——其他图本来就由服务器连接内一起挪，跟票反而来回换房。
        if pairing_map or _armed(now):
            return {"action": INBAND, "why": "pairing_ticket"}
        return {"action": HOLD, "why": "owner_not_in_room"}
    if owner_present is True:
        return {"action": HOLD, "why": "owner_already_here"}
    if owner_seen and owner_present is False:
        return {"action": HOLD, "why": "owner_left_room_wait_rejoin"}
    if owner_seen and (owner_stale or (seen_count or 0) >= 2):
        return {"action": INBAND if adopt_available else SEGMENT,
                "why": "owner_was_here_then_stale_or_repeat"}
    return {"action": HOLD, "why": "not_grouped_with_owner"}


def route_same_map_ticket(*, level, uri, ticket_fields, with_owner, owner_stale,
                          seen_count, has_capture, adopt, publish, log,
                          owner_seen=False, adopt_available=True, owner_present=None,
                          ticket_key=None, move_ts=None, pairing_map=False, now=None):
    """执行同图票路由；副作用全部经注入的回调完成。

    返回动作字符串，供调用方记录/断言。`adopt` 返回 True 表示票已递交给
    当前连接的 pump（真正是否迁移成功由 pump 的 external_move_* 指标决定）。
    """
    if not has_capture:
        return NO_CAPTURE
    ticket_fields = dict(ticket_fields)
    ticket_fields.setdefault("level", level)
    key = ticket_key or uri
    now = _now(now)
    decision = decide_same_map(uri_key=uri, with_owner=with_owner, owner_stale=owner_stale,
                               seen_count=seen_count, owner_seen=owner_seen,
                               adopt_available=adopt_available,
                               owner_present=owner_present, ticket_key=key,
                               move_ts=move_ts, pairing_map=pairing_map, now=now)
    action = decision["action"]
    if action == HOLD:
        log("same_map_move_held", uri=uri, count=seen_count,
            with_owner=bool(with_owner), why=decision["why"],
            move_ts=move_ts, ticket=key)
        return HOLD
    if action == INBAND:
        if adopt(ticket_fields):
            if key:
                _STATE["adopted"][key] = now
            log("same_map_move_adopted", uri=uri, count=seen_count,
                with_owner=bool(with_owner), why=decision["why"],
                move_ts=move_ts, ticket=key)
            return INBAND
        log("same_map_move_adopt_skipped", uri=uri, count=seen_count,
            with_owner=bool(with_owner), why=decision["why"])
        return ADOPT_SKIPPED
    # 兜底旧路径：只有调用方没有 inband 通道时才走（会结束本段重建连接）。
    if publish(ticket_fields):
        log("move_follow", level=level, why="same_map_merge", uri=uri,
            count=seen_count, with_owner=bool(with_owner))
        return SEGMENT
    return SEGMENT_REJECTED
