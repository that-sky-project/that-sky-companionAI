"""方式③：别人在好友树对小光递蜡烛时，自动接受，并补上送方的完成动画。

流程（与礼包 v17a 一致）：
  1. 收到递给小光的 OfferCandleRpc status0 → 回 status1（信封第 4 字节是小光自己的 slot，
     即礼包 owned_acceptance_context_0167 的修正）。
  2. 送方客户端收到接受后会发 status2（完成）→ 用礼包 match_current_completion 核对与本次邀请一致
     → 回一条 LevelUpFxRpc（礼包 levelup_fx_0167）：送方据此播完递烛动作、放下蜡烛。
  3. 同时回调 on_complete：小光播 TakeCandle 接烛动作（room_pose.PoseController.play）。
解析/去重用礼包的 offer_codec（friend_offer_acceptance 副本）；status1 字节格式与礼包 build_once 一致，
终端串取账号的 session3。发送走引擎 OUTBOX（同进程、长模式）。
"""
import secrets
import struct
import threading
import time
import uuid as uuidlib

import offer_codec as oc
from qfsky.long0167.engine import Frame
from qfsky.map_long0167 import map_long_engine

LEVELUP_FX_KEY = 0x3A75DA25
# 礼包 AUTHORIZED_TREE_NODES：普通好友树节点，完成特效 enum 0 只对这些能力验证过
TREE_NODES = frozenset({2, 3, 4, 5, 6, 8, 34, 35, 36, 37, 38, 40, 41, 42, 43, 44, 45, 46, 47, 49, 50,
                        55, 56, 57, 58, 59, 60, 61, 62, 63, 64, 65, 66, 67, 70, 71, 72, 73, 74, 75, 76, 77, 78, 79,
                        80, 81, 82, 83, 84, 85, 86, 87, 90, 91, 92, 93, 94, 95, 96, 97, 100, 101, 102, 103, 104})


def levelup_fx_game(epoch, own, friend, ability_id):
    """礼包 build_scoped_completion_game2：[own, own, friend, enum0, 能力ID, false, (0,0,0)]。"""
    body = struct.pack("<BBBBIBfff", own, own, friend, 0, ability_id, 0, 0.0, 0.0, 0.0)
    return bytes((2, epoch, 0, own)) + struct.pack("<IBH", LEVELUP_FX_KEY, 0, len(body)) + body


class OfferAcceptor:
    """注册到 CLAUDEWORK_LISTENERS：收名单(17)与 GameMsg(14/02)。"""

    def __init__(self, account, map_id, log, *, max_accepts=64, on_complete=None):
        self.on_complete = on_complete     # 送方完成时调用（小光播 TakeCandle 接烛动作）
        self.own = uuidlib.UUID(account["user_uuid"]).bytes
        self.terminal = (account.get("session3") or "").encode("utf-8")
        self._map_id, self.log, self.max_accepts = map_id, log, max_accepts
        self.lock = threading.Lock()
        self.roster = None
        self.room_ref = secrets.token_urlsafe(32)
        self.account_ref = secrets.token_urlsafe(32)
        self.room_gen = self.roster_gen = 1
        self.states = {}       # 送方 uuid -> PendingAcceptance
        self.receipts = []     # [(送方 uuid, scope, CompletionReceipt, own_slot, friend_slot)]，等 status2
        self.accepted = 0
        self.rejections = {}
        self.level_fixed = False
        self.raw_roster = None
        if not self.terminal:
            log("offer_accept_disabled", reason="账号没有 session3")

    @property
    def map_id(self):
        """当前地图：可传函数（跟随换图后会变）。"""
        return self._map_id() if callable(self._map_id) else self._map_id

    @map_id.setter
    def map_id(self, value):
        self._map_id = value

    def room_level(self, who):
        """名单原文里某人那行的地图（小光自己那行不可信）；不在名单里返回 None。"""
        raw = self.raw_roster
        n = raw[0] & 15 if raw else 0
        if not raw or len(raw) != 1 + n * 37:
            return None
        for i in range(n):
            off = 1 + 37 * i
            if raw[off + 1:off + 17] == who:
                return struct.unpack_from("<I", raw, off + 33)[0]
        return None

    def __call__(self, messages):
        for m in messages:
            body = bytes(m.body)
            if m.message_type == 17 and body:
                self._roster(body)
            elif m.message_type == 14 and body[:1] == b"\x02" and self.terminal:
                self._game(body)

    def _normalize(self, raw):
        """小光自己那行的地图可能是她声明的旧图；改成同房间其他玩家一致的那个地图（真实所在）。"""
        n = raw[0] & 15 if raw else 0
        if not raw or len(raw) != 1 + n * 37:
            return raw
        rows = [bytearray(raw[1 + 37 * i:38 + 37 * i]) for i in range(n)]
        others = {bytes(r[33:37]) for r in rows if bytes(r[1:17]) != self.own}
        if len(others) != 1:
            return raw
        level = others.pop()
        for r in rows:
            if bytes(r[1:17]) == self.own and bytes(r[33:37]) != level:
                r[33:37] = level
                if not self.level_fixed:
                    self.level_fixed = True
                    self.log("offer_roster_level_fixed", own_level=self.map_id,
                             room_level=struct.unpack("<I", level)[0])
        return raw[:1] + b"".join(bytes(r) for r in rows)

    def _roster(self, raw):
        self.raw_roster = raw
        raw = self._normalize(raw)
        with self.lock:
            if raw != self.roster:
                self.roster = raw
                self.roster_gen += 1
                self.states.clear()      # 名单变了：旧邀请与回执全部作废（与礼包 remember_offer_roster 一致）
                self.receipts.clear()

    def _sender_uuid(self, slot):
        raw = self.roster
        if not raw or len(raw) != 1 + (raw[0] & 15) * 37:
            return None
        for i in range(raw[0] & 15):
            off = 1 + 37 * i
            if raw[off] == slot:
                return raw[off + 1:off + 17], struct.unpack_from("<I", raw, off + 33)[0]
        return None

    def _scope(self, epoch, sender, level=None):
        return oc.AcceptanceScope(owner_id=1, account_identity=self.account_ref, auth_generation=1,
                                  room_reference=self.room_ref, room_generation=self.room_gen,
                                  roster_generation=self.roster_gen, expected_level_id=level or self.map_id,
                                  peer_epoch=epoch, own_uuid=self.own, friend_uuid=sender)

    def _game(self, game):
        if struct.pack("<I", oc.OFFER_KEY) not in game:   # 只看带 OfferCandleRpc 的消息
            return
        with self.lock:
            found = self._sender_uuid(game[2])
            if found is None or found[0] == self.own:
                return
            sender, level = found
            scope = self._scope(game[1], sender, level)       # 以送方所在地图为准
            now = time.monotonic()
            self._completion(game, sender, scope, now)
            self._offer(game, sender, scope, now)

    def _completion(self, game, sender, scope, now):
        """status2：送方确认完成 → 回 LevelUpFx，让送方播完动作。"""
        keep = []
        for row in self.receipts:
            r_sender, r_scope, receipt, own_slot, friend_slot = row
            if r_sender == sender and oc.match_current_completion(receipt, game, player_sync_payload=self.roster,
                                                                  scope=scope, now=now):
                if receipt._ability_id in TREE_NODES:
                    map_long_engine.OUTBOX.append(Frame(14, levelup_fx_game(game[1], own_slot, friend_slot,
                                                                             receipt._ability_id), True))
                    self.log("offer_completion_fx", sender=str(uuidlib.UUID(bytes=sender))[:8],
                             ability_id=receipt._ability_id)
                    if self.on_complete:
                        self.on_complete(str(uuidlib.UUID(bytes=sender)))   # 送方 uuid（用于随后点大节点）
                else:
                    self.log("offer_completed_no_fx", ability_id=receipt._ability_id)
                continue
            if now < receipt._deadline:
                keep.append(row)
        self.receipts = keep[-64:]

    def _offer(self, game, sender, scope, now):
        """status0：别人递给小光 → 回 status1。"""
        state = self.states.setdefault(sender, oc.PendingAcceptance(ttl_seconds=60))
        try:
            ticket = state.observe(game, player_sync_payload=self.roster, scope=scope, now=now)
            if ticket is None:
                return
            meta = state.pending_metadata(ticket, player_sync_payload=self.roster, scope=scope, now=now)
        except oc.AcceptanceError as e:
            self.rejections[e.reason] = self.rejections.get(e.reason, 0) + 1
            if self.rejections[e.reason] == 1:
                self.log("offer_rejected", reason=e.reason)
            return
        who = str(uuidlib.UUID(bytes=sender))[:8]
        self.log("offer_seen", sender=who, ability_id=meta["ability_id"], cost=meta["ability_cost"])
        if self.accepted >= self.max_accepts:
            self.log("offer_skipped", reason="本次连接接受次数已达上限")
            return
        pending = state._pending
        rpc = self._status1_rpc(pending)
        state._pending = None              # 先消耗，发送结果不确定时也不重放（与礼包一致）
        self.accepted += 1
        receipt = oc.CompletionReceipt(scope, pending.roster_digest, pending.offer.ability_id, pending.offer.cost,
                                       pending.offer.opaque32, now, pending.deadline, pending.ticket.generation)
        self.receipts.append((sender, scope, receipt, pending.own_slot, pending.friend_slot))
        # 信封：礼包修正后的形态 (2, epoch, 0, 自己 slot)
        map_long_engine.OUTBOX.append(Frame(14, bytes((2, scope.peer_epoch, 0, pending.own_slot)) + rpc, True))
        self.log("offer_accepted", sender=who, ability_id=meta["ability_id"], cost=meta["ability_cost"])

    def _status1_rpc(self, pending):
        """与礼包 build_once 同格式：(送方slot, 自己slot, 1) + 能力ID + 花费 + 20 零 + 邀请串 + 终端串。"""
        cstring = pending.offer.opaque32 + b"\0"
        body = (bytes((pending.friend_slot, pending.own_slot, 1))
                + struct.pack("<II", pending.offer.ability_id, pending.offer.cost) + bytes(20)
                + struct.pack("<I", len(cstring)) + cstring
                + struct.pack("<I", len(self.terminal)) + self.terminal)
        return struct.pack("<IBH", oc.OFFER_KEY, 0, len(body)) + body
