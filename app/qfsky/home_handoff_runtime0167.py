"""Single-owner Home long-connection pump; server Move is not a map switch.

There are no HTTP calls or automatic ticket retries here. Only an authenticated
active peer can supply a new ticket. New peer state, keys, counters, ACK history
and authority are never copied from the old connection. Local idle continuity
is the narrow Engine snapshot, not a network-state snapshot.
"""
from __future__ import annotations

import collections
import os
import struct
import time
from dataclasses import dataclass, field, replace

from . import enet, handoff0167 as wire
from .long_transport0167 import OutboundFrame
from .receive0167 import validate_pending_enter_game
from .handshake0167_transport import ProbeTransportError
from . import ticket_route0167 as route
from . import t75_dayhub_pairing as dayhub

HOME = 3526133726


class HandoffRuntimeError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


# T73：聊天 WS 推来的同图 move 票，字段就是服务器 type 9 Move 的端点+签名。
# 票在服务器时钟的该窗口内有效；过期票不采用（旧代码用 20 秒，见 long_runner MOVE_MAX_AGE）。
EXTERNAL_MOVE_WINDOW = 20.0
_EXTERNAL_SEEN_MAX = 64

# T73-R2：真实入服票（好友票/quick_rejoin/WS 同图票）的窗口与主人名单记录长度。
# PlayerSync 每条记录固定 37 字节：net_id(1) + uuid(16) + ... + level_id(4)。
TICKET_JOIN_MAX_AGE = 20.0
OWNER_ROSTER_RECORD = 37


def _ts_sec(value):
    """票/Move 的 move_ts 统一成秒（原生有时用毫秒：1795199005565）。"""
    try:
        value = int(value)
    except (TypeError, ValueError):
        return 0
    return value // 1000 if value > 10 ** 11 else value


def roster_contains_uuid(body, uuid_bytes):
    """服务器 EnterGame/PlayerSync 名单里是否有这个 16 字节 UUID。

    纯函数，不记日志、不暴露其他玩家身份；只用于“新房间是否真的包含主人”
    这个布尔判定。格式不合法一律返回 False（宁可回滚也不误判成功）。
    """
    if not isinstance(uuid_bytes, (bytes, bytearray)) or len(uuid_bytes) != 16:
        return False
    if not isinstance(body, (bytes, bytearray)) or not body:
        return False
    body = bytes(body)
    # Claude 审核：人数按长度算（每人 37 字节）。body[0] & 15 在人多的大厅/并房后的
    # 房间会算错（room_pose RoomTracker._roster 早已改成按长度），并房恰恰是人多的场景。
    if (len(body) - 1) % OWNER_ROSTER_RECORD != 0:
        return False
    count = (len(body) - 1) // OWNER_ROSTER_RECORD
    for index in range(count):
        offset = 1 + index * OWNER_ROSTER_RECORD
        if body[offset + 1:offset + 17] == bytes(uuid_bytes):
            return True
    return False


@dataclass
class TicketJoinPlan:
    """一张“服务器真实入服票”对应的 Join 计划（T74-R2）。

    与服务器的 type 9 Move 不同，这里不经过 handoff0167.prepare_*_join，
    而是由 connection0167 用原生迁移形状构造 Join（origin 写 ticket_ipv4 /
    previous_address，拨号走候选阶梯）。

    - ``endpoint``：当前候选择拨号地址（socket 用）；
    - ``origin_endpoint``：票面 origin（Join 字段用），两者不能混；
    - ``previous_endpoint``：当前活动连接的票面 origin（previous_address）；
    - ``dial_candidates``：``((host, port, source), ...)`` 有序候选。
    """
    plaintext: bytes
    endpoint: tuple
    signature: bytes
    move_ts: int
    platform_filter: int
    level: int
    origin: str = ''
    expected_owner: bytes = None
    origin_endpoint: tuple = None
    previous_endpoint: tuple = None
    dial_candidates: tuple = ()
    dial_index: int = 0
    # WS move 帧同时带 `sig` 与 `signature`；服务器 in-band Move 的签名可能对应
    # 其中之一。这里保留第二个，只用于“这是同一张票吗”的匹配，不改 RC4。
    signature_alt: bytes = b''
    # T74-R3：'native' = 原生顺序（票内 speed/ip → 路由 → 学习 → 边缘 → origin）；
    # 'pairing' = 服务器配对票，origin 优先、450ms/候选、预算 4 个。
    dial_policy: str = 'native'
    others_count: int = 0
    # 换签名重试用：原始票 + 生成参数（不含任何凭据输出）。
    ticket_core: dict = None
    sig_swapped: bool = False
    ws_declared_level: int = None  # 保留 WS 原票地图；不把入口回填值当作配对依据。
    ws_sig_manual: bool = False  # 配置显式选择另一票字段；不影响既有选签行为。

    def with_dial(self, index):
        if not self.dial_candidates or not 0 <= index < len(self.dial_candidates):
            return self
        host, port, *_ = self.dial_candidates[index]
        return replace(self, endpoint=(host, port), dial_index=index)


CLAUDEWORK_LISTENERS = []  # claudework：下行消息监听（房间追踪），运行器在同进程内注册


def _claudework_notify(messages):
    for fn in list(CLAUDEWORK_LISTENERS):
        try:
            fn(messages)
        except Exception:
            pass


def _claudework_capture(messages):
    """claudework：记录下行消息类型、长度与正文（正文最多 8KB）。失败不影响连接。"""
    try:
        import json
        with open(os.environ['CLAUDEWORK_CAPTURE'], 'a', encoding='utf-8') as f:
            now = time.time()
            only_game = os.environ.get('CLAUDEWORK_CAPTURE_ONLY') == 'gamemsg'
            for m in messages:
                body = bytes(m.body)
                # 精简模式：只留房间名单(0x11)与 GameMsg(0x0e 且首字节 2)，好友请求在这里
                if only_game and not (m.message_type == 0x11 or (m.message_type == 14 and body[:1] == b'\x02')):
                    continue
                f.write(json.dumps({'t': round(now, 3), 'type': m.message_type, 'len': len(body),
                                    'hex': body[:8192].hex()}) + chr(10))
    except Exception:
        pass

@dataclass(repr=False)
class _Slot:
    transport: object
    engine: object = None
    counter: int = 1
    stage: str = 'active'
    token: object = None
    plan: object = None
    move: object = None
    idle: object = None
    enter_body: bytes = b''
    buffered: list = field(default_factory=list)
    stage_deadline: float = 0.0
    retired_at: float = 0.0
    # T73：True = 由聊天 WS move 票触发的外部迁移；失败只回滚本 slot，不断开当前房间。
    external: bool = False
    # T74-R2：这条连接占用的票面 origin（不是拨号地址）；迁移后要传给下一条。
    ticket_origin: tuple = None
    # T74-R2：失败时是否需要回 0x0a(result=4)；只有服务器对同一张票发过
    # in-band Move（或 SKY_T74_R2_FAIL_REPLY=always）时才回。
    reply_failure: bool = False
    failure_replied: bool = False
    # T74-R3：配对票的即时诊断（staged/connected/join_sent/enter/committed/aborted）。
    dial_policy: str = 'native'
    started_at: float = 0.0
    connected_at: float = 0.0
    join_sent_at: float = 0.0
    dial_log: list = field(default_factory=list)
    alt_tried: bool = False


class _RoomHandoffPump:
    """No threads, waits or reentrant callbacks; step services every owned peer.

    Factories are supplied by the account-bound owner, never by an HTTP request.
    Retired peers continue ACK/RTO processing for at most five seconds. Forced
    physical closure is recorded separately from acknowledged graceful closure.
    """
    def __init__(self, *, transport, engine, peer_key, profile, account_uuid,
                 resource_revision, current_join_fields, registration_body,
                 make_transport, make_engine, check, deadline, counter=1,
                 map_id, clock=time.monotonic, on_terminal=None, max_moves=4,
                 prepare_join=wire.prepare_home_pending_join,
                 validate_enter=validate_pending_enter_game,
                 owner_uuid=None, make_ticket_join=None, owner_attached=None,
                 initial_origin=None, route_book=None):
        if type(map_id) is not int or not 1 <= map_id <= 0xffffffff:
            raise ValueError('invalid explicit current map ID')
        self._map_id = map_id
        _move_cap = 100000 if os.environ.get('CLAUDEWORK_LONG') == '1' else 8  # CLAUDEWORK_LONG
        if type(max_moves) is not int or not 1 <= max_moves <= _move_cap:
            raise ValueError('invalid Move bound')
        if type(counter) is not int or not 1 <= counter <= 255:
            raise ValueError('invalid active application counter')
        if type(registration_body) is not bytes or not registration_body or registration_body[0] != 1:
            raise ValueError('fresh registration generation must be one')
        self.active = _Slot(transport, engine, counter)
        self.active.ticket_origin = initial_origin
        self.pending = None
        self.retired = []
        self._all = [self.active]
        self._peer = peer_key
        self._profile = profile
        self._account = account_uuid
        self._revision = resource_revision
        self._fields = dict(current_join_fields)
        self._registration = registration_body
        self._make_transport = make_transport
        self._make_engine = make_engine
        self._check = check
        self._deadline = deadline
        self._clock = clock
        self._on_terminal = on_terminal
        self._prepare_join = prepare_join
        self._validate_enter = validate_enter
        self._max_moves = max_moves
        self._make_ticket_join = make_ticket_join   # T73-R2：真实入服票 → Join 明文
        self._owner_uuid = (bytes(owner_uuid)
                            if isinstance(owner_uuid, (bytes, bytearray))
                            and len(owner_uuid) == 16 else None)
        self._owner_attached = owner_attached if callable(owner_attached) else None
        self._route_book = route_book if route_book is not None else route.RouteBook()
        self._move_others_enabled = os.environ.get('SKY_T73_R2_MOVE_OTHERS', '1') != '0'
        self._reservation = wire.initial_reservation(newly_initialized_session=True)
        self._closed = False
        self._failed = False
        self._events = []
        self._stats = dict(move_received=0, move_started=0, move_completed=0,
                           move_busy_replies=0, move_ignored=0,
                           move_graceful_closed=0, move_forced_closed=0,
                           long_groups=0, long_actor_frames=0,
                           long_controller_frames=0, long_scene_frames=0,
                           long_feedback_frames=0, reply_count=0,
                           matching_generation_observed=False,
                           registration_transport_acked=True,
                           external_move_received=0, external_move_adopted=0,
                           external_move_failed=0, external_move_busy=0,
                           external_move_ignored=0, external_move_action='',
                           external_move_error='', external_move_duplicate_move=0,
                           # T73-R2 真实入服/主人房指标
                           ticket_join_staged=0, ticket_join_committed=0,
                           ticket_join_aborted=0, ticket_join_failed=0,
                           ticket_join_ignored=0, ticket_join_error='',
                           owner_rejoin_requested=0, owner_rejoin_ticket_miss=0,
                           owner_rejoin_staged=0, owner_rejoin_committed=0,
                           owner_rejoin_aborted=0, owner_rejoin_action='',
                           move_others_injected=0, owner_in_roster=False,
                           # T74-R2：票面/拨号分离与快速重拨指标
                           ticket_dial_attempts=0, ticket_dial_retries=0,
                           ticket_dial_source='', ticket_origin_source='',
                           ticket_fail_replies=0, ticket_join_server_disconnect=0,
                           owner_left_room=0, owner_gone=False,
                           # T74-R3：配对票即时诊断/重试指标
                           ticket_join_stage='', ticket_join_elapsed_ms=0,
                           ticket_join_dial='', ticket_join_move_ts=0,
                           ticket_sig_fallback=0, pairing_fail_replies=0,
                           external_move_superseded=0, external_move_busy_retry=0)
        # 一票只采用一次：sig 去重（有界，仅 RAM）。
        self._external_seen = collections.OrderedDict()
        # T74-R3：最近一条 in-band 服务器 Move 的 move_ts（秒）。配对票的
        # move_ts 不晚于它时说明服务器已改发 split Move（door3 03:29:22 实测）。
        self._last_move_ts = 0

    def __repr__(self):
        return '<' + type(self).__name__ + ' private-single-account>'

    def _fatal(self, code):
        self._failed = True
        raise HandoffRuntimeError(code)

    def _event(self, name, **fields):
        """有界事件队列：只给 connection0167 线程消费，不落盘、不含地址。"""
        if len(self._events) >= 64:
            self._events.pop(0)
        row = {'event': name}
        row.update(fields)
        self._events.append(row)

    def pop_events(self):
        events, self._events = self._events, []
        return events

    def mark_owner_left(self):
        """T74-R2：主人从当前房间消失（不是自己被 Move）。只记指标 + 事件。"""
        self._stats['owner_left_room'] += 1
        self._event('owner_left_room')

    def _ticket_endpoint(self, ticket):
        uri, sig, ts = ticket.get('uri'), ticket.get('sig'), ticket.get('move_ts')
        if (type(uri) is not str or ':' not in uri or type(sig) is not str
                or len(sig) != 64 or type(ts) is not int):
            return None
        host, port = uri.rsplit(':', 1)
        try:
            port, signature = int(port), bytes.fromhex(sig)
        except (TypeError, ValueError):
            return None
        if type(host) is not str or not host or not 1 <= port <= 65535:
            return None
        level = ticket.get('level', self._map_id)
        if type(level) is not int:
            level = self._map_id
        return (host, port), signature, int(ts), level

    def plan_from_ticket(self, ticket, *, expected_owner=None, origin='', dial_policy=None):
        """把一张票变成 TicketJoinPlan；票面 origin 与拨号地址从此分开。

        - Join 的 ticket_ipv4/previous_address 用 ``origin_endpoint``；
        - socket 拨号用 ``ticket_route0167`` 的有序候选；
        - 配对票（``origin='ws_same_map'``）走 ``dial_policy='pairing'``：
          通常 origin 优先；中央神坛换房命中实测表时，中转优先、origin 后备。
        - others 用票里的 other_players（主人优先），不是只写主人一个。
        """
        if self._make_ticket_join is None or type(ticket) is not dict:
            return None
        if dial_policy is None:
            dial_policy = 'pairing' if str(origin).startswith('ws_same_map') else 'native'
        # T94：配对换房只在神坛门开后出现 → 用“门开”场景数据
        if dial_policy == 'pairing' and self._map_id == 2394719185:
            try:
                from .join0167_profile import t94_set_dayhub_pref
                t94_set_dayhub_pref('open')
            except Exception:
                pass
        origin_ep = route.ticket_origin(ticket)
        if origin_ep is None:
            return None
        # 原生 wire 的 ticket_ipv4 就是票面 origin：即使实际拨号是 speed host，
        # Join 里也必须写 origin。
        ticket_core = dict(ticket)
        ticket_core['uri'] = '%s:%d' % origin_ep
        parsed = self._ticket_endpoint(ticket_core)
        if parsed is None:
            return None
        endpoint, signature, move_ts, level = parsed
        if endpoint != origin_ep:
            return None
        # 首登不经此处；同时核对票的有效地图与本机当前地图，不为跨图票选中转。
        candidates = tuple(self._route_book.candidates(
            ticket_core, prefer_origin=(dial_policy == 'pairing'),
            handoff_level=level if level == self._map_id else None))
        if not candidates:
            candidates = ((origin_ep[0], origin_ep[1], 'origin'),)
        # T89（11-21）：服务器的校验串连同行名单的原顺序一起算。22:47 两张票原名单
        # [他人, 主人]，旧代码把主人挪到第一位 → 服务器回 "Invalid signature"。
        # 配对票有名单时按原顺序照抄；SKY_T89_KEEP_ORDER=0 回到旧的“主人优先”。
        _t89_list = ticket.get('other_players') or []
        if (dial_policy == 'pairing' and _t89_list
                and os.environ.get('SKY_T89_KEEP_ORDER', '1') != '0'):
            others = route.merge_others(_t89_list, self_uuid=self._account)
        else:
            others = route.merge_others(
                [expected_owner] if expected_owner else [],
                _t89_list,
                self_uuid=self._account)
        previous_ep, previous_source = self.active.ticket_origin, 'active_origin'
        if previous_ep is None:
            try:
                previous_ep = tuple(self.active.transport.remote)
            except (TypeError, ValueError):
                previous_ep = None
            previous_source = 'dial_fallback'
        try:
            plaintext = self._make_ticket_join(
                ticket_core, origin_endpoint=origin_ep,
                previous_endpoint=previous_ep, level=level, others=others)
        except TypeError:
            # 兼容旧注入签名（不在本轮变更范围的调用方）。
            plaintext = self._make_ticket_join(ticket_core)
        if type(plaintext) is not bytes or not plaintext:
            return None
        if dayhub.scoped(self._map_id, level, origin, dial_policy, expected_owner, ticket):
            plaintext = dayhub.bind(self, ticket_core, plaintext)
        pf = ticket.get('platform_filter', 0)
        if type(pf) is not int:
            pf = 0
        self._stats['ticket_origin_source'] = previous_source
        self._stats['ticket_dial_source'] = candidates[0][2] if candidates else ''
        signature_alt = b''
        alt = ticket.get('signature')
        if isinstance(alt, str) and len(alt) == 64:
            try:
                signature_alt = bytes.fromhex(alt)
            except ValueError:
                signature_alt = b''
        return TicketJoinPlan(plaintext=plaintext,
                              endpoint=(candidates[0][0], candidates[0][1]),
                              signature=signature, move_ts=move_ts,
                              platform_filter=pf, level=level,
                              origin=str(origin or ''), expected_owner=expected_owner,
                              origin_endpoint=origin_ep,
                              previous_endpoint=previous_ep,
                              dial_candidates=candidates, dial_index=0,
                              signature_alt=signature_alt,
                              dial_policy=dial_policy,
                              others_count=len(others),
                              ticket_core=ticket_core)

    def stage_ticket_join(self, plan, now=None):
        """T73-R2：用一张真实入服票做 make-before-break 新 peer。

        与服务器 Move 的区别：不调用 prepare_*_join（第一轮 begin_failed 的
        位置），Join 明文由调用方按“新段入服”同一套字段生成；不碰服务器
        Move 的 reservation 状态。外部票失败只回滚本 slot，当前房间不断。
        expected_owner 非空时，EnterGame 名单里必须真的出现主人，否则回滚。
        """
        now = self._clock() if now is None else now
        if self._closed or self._failed:
            return {'action': 'terminal'}
        if not isinstance(plan, TicketJoinPlan):
            self._stats['ticket_join_ignored'] += 1
            return {'action': 'malformed'}
        if plan.level != self._map_id:
            self._stats['ticket_join_ignored'] += 1
            return {'action': 'other_map'}
        if abs(time.time() - (plan.move_ts / 1000.0 if plan.move_ts > 10**11 else plan.move_ts)) > TICKET_JOIN_MAX_AGE:
            self._stats['ticket_join_ignored'] += 1
            return {'action': 'stale'}
        if self.pending is not None or self.active.stage != 'active':
            self._stats['external_move_busy'] += 1
            return {'action': 'busy'}
        if self._deadline - now < 5.0:
            self._stats['ticket_join_ignored'] += 1
            return {'action': 'deadline'}
        if self._stats['move_started'] >= self._max_moves:
            self._stats['ticket_join_ignored'] += 1
            return {'action': 'bound'}
        try:
            current = tuple(self.active.transport.remote)
        except (TypeError, ValueError):
            current = None
        if self.active.ticket_origin is not None and tuple(plan.origin_endpoint) == tuple(self.active.ticket_origin):
            return {'action': 'same_endpoint'}
        if current == tuple(plan.endpoint) and plan.origin_endpoint == plan.endpoint:
            return {'action': 'same_endpoint'}
        try:
            idle = self.active.engine.export_local_idle()
            transport = self._make_transport(plan, self._deadline - now)
        except Exception as exc:
            self._stats['ticket_join_failed'] += 1
            self._stats['ticket_join_error'] = type(exc).__name__
            self._event('ticket_join_failed', reason=type(exc).__name__,
                        origin=plan.origin)
            return {'action': 'failed', 'error': type(exc).__name__}
        policy = getattr(plan, 'dial_policy', 'native')
        connect_window = (route.pairing_connect_timeout_sec()
                          if policy == 'pairing' else route.connect_timeout_sec())
        slot = _Slot(transport=transport, counter=0, stage='connect', plan=plan,
                     move=plan, idle=idle,
                     stage_deadline=min(self._deadline, now + connect_window),
                     external=True, ticket_origin=plan.origin_endpoint,
                     dial_policy=policy, started_at=now)
        self.pending = slot
        self._all.append(slot)
        if any(transport is item.transport for item in self._all[:-1]):
            try:
                self._all.remove(slot)
            except ValueError:
                pass
            self.pending = None
            try:
                transport.force_close()
            except Exception:
                pass
            self._stats['ticket_join_failed'] += 1
            self._stats['ticket_join_error'] = 'reused_transport'
            return {'action': 'reused_transport'}
        self._stats['move_started'] += 1
        self._stats['ticket_join_staged'] += 1
        self._stats['ticket_dial_attempts'] += 1
        self._stats['ticket_join_stage'] = 'connect'
        self._stats['ticket_join_elapsed_ms'] = 0
        self._stats['ticket_join_dial'] = '%s:%d' % (plan.endpoint[0], plan.endpoint[1])
        self._stats['ticket_join_move_ts'] = plan.move_ts
        if plan.origin.startswith('owner_rejoin'):
            self._stats['owner_rejoin_staged'] += 1
            self._stats['owner_rejoin_action'] = 'staged'
        if plan.origin == 'ws_same_map':
            self._stats['external_move_adopted'] += 1
            self._stats['external_move_action'] = 'staged'
        self._event('ticket_join_staged', origin=plan.origin,
                    require_owner=plan.expected_owner is not None,
                    dial='%s:%d' % (plan.endpoint[0], plan.endpoint[1]),
                    dial_source=self._stats.get('ticket_dial_source', ''),
                    candidates=len(plan.dial_candidates or ()),
                    policy=policy, move_ts=plan.move_ts,
                    sig2=plan.signature[:2].hex() if plan.signature else '',
                    others=plan.others_count,
                    level=plan.level)
        try:
            transport.connect()  # Emits CONNECT only; no wait/receive.
        except Exception as exc:
            self._stats['ticket_join_error'] = 'ticket_join_connect_' + type(exc).__name__
            self._pending_fail('ticket_join_connect_' + type(exc).__name__)
            return {'action': 'failed', 'error': type(exc).__name__}
        return {'action': 'staged', 'origin': plan.origin}

    def _rotate_dial(self, slot):
        """T74-R2：新 peer 在 connect 阶段没等到 VERIFY 时，换下一个拨号候选。

        原生 VERIFY 往返 36ms；服务器给配对留的窗口约 1.25s。所以候选切换
        必须短（默认 900ms/个），且只试本进程真正连过的游戏边缘 IP。
        """
        plan = slot.plan
        if not getattr(plan, 'dial_candidates', None):
            return False
        next_index = plan.dial_index + 1
        if next_index >= len(plan.dial_candidates):
            return False
        now = self._clock()
        window = (route.pairing_connect_timeout_sec()
                  if getattr(plan, 'dial_policy', 'native') == 'pairing'
                  else route.connect_timeout_sec())
        if self._deadline - now < window:
            return False
        try:
            slot.transport.force_close()
        except Exception:
            pass
        new_plan = plan.with_dial(next_index)
        try:
            transport = self._make_transport(new_plan, self._deadline - now)
        except Exception as exc:
            self._stats['ticket_join_error'] = 'ticket_dial_' + type(exc).__name__
            return False
        slot.transport = transport
        slot.plan = new_plan
        slot.move = new_plan
        slot.stage = 'connect'
        slot.stage_deadline = min(self._deadline, now + window)
        self._stats['ticket_dial_retries'] += 1
        self._stats['ticket_dial_attempts'] += 1
        self._stats['ticket_dial_source'] = plan.dial_candidates[next_index][2]
        self._stats['ticket_join_dial'] = '%s:%d' % (
            plan.dial_candidates[next_index][0], plan.dial_candidates[next_index][1])
        self._event('ticket_dial_retry', origin=plan.origin, index=next_index,
                    dial='%s:%d' % (new_plan.endpoint[0], new_plan.endpoint[1]),
                    dial_source=plan.dial_candidates[next_index][2])
        try:
            transport.connect()
        except Exception as exc:
            self._stats['ticket_join_error'] = 'ticket_dial_connect_' + type(exc).__name__
            return False
        return True

    def _terminal(self, messages):
        # Inspect the entire receive batch before any Move or Enter side effect.
        for message in messages:
            if message.message_type in (1, 2, 0x53):
                self._stats['terminal_message_type'] = message.message_type
                if self._on_terminal:
                    self._on_terminal(message)
                self._fatal('server_ended_connection')

    def _drain(self, transport):
        if len(transport.messages) > 256:
            self._fatal('handoff_message_bound')
        messages = list(transport.messages)
        transport.messages.clear()
        if messages and os.environ.get('CLAUDEWORK_CAPTURE'):  # claudework 抓包
            _claudework_capture(messages)
        if messages and CLAUDEWORK_LISTENERS:
            _claudework_notify(messages)
        if sum(len(m.body) for m in messages) > 1024 * 1024:
            self._fatal('handoff_message_byte_bound')
        return messages

    def _buffer(self, slot, messages):
        if len(slot.buffered) + len(messages) > 256:
            self._fatal('handoff_buffer_bound')
        if sum(len(m.body) for m in slot.buffered) + sum(len(m.body) for m in messages) > 1024 * 1024:
            self._fatal('handoff_buffer_byte_bound')
        slot.buffered.extend(messages)

    def _group(self, slot, group):
        if group is None:
            return
        self._check()
        frames = []
        for frame in group.frames:
            slot.counter = 1 if slot.counter == 255 else slot.counter + 1
            frames.append(OutboundFrame(
                struct.pack('<IBB', self._peer, frame.outer_type, slot.counter) + frame.payload,
                channel=frame.channel, reliable=frame.reliable))
        try:
            receipt = slot.transport.send_frame_group(frames)
            slot.engine.commit(group, sent_count=receipt.sent_frames)
        except Exception:
            slot.engine.close()
            self._failed = True
            raise
        self._stats['long_groups'] += 1
        for frame in group.frames:
            if frame.outer_type != 14 or not frame.payload:
                continue
            kind = frame.payload[0]
            name = {3:'long_actor_frames', 12:'long_controller_frames',
                    10:'long_scene_frames', 11:'long_scene_frames',
                    14:'long_feedback_frames'}.get(kind)
            if name:
                self._stats[name] += 1
            if kind == 17:
                self._stats['reply_count'] += 1
                self._stats['matching_generation_observed'] = True

    def _reply(self, slot, result):
        reply = wire.encode_move_reply(peer_key=self._peer,
                    previous_app_counter=slot.counter, result=result)
        # Advance before sending: an uncertain partial send must never be replayed.
        slot.counter = reply.next_counter
        try:
            receipt = slot.transport.send_frame_group([
                OutboundFrame(reply.plaintext, channel=0, reliable=True)])
            if receipt.sent_frames != 1:
                self._fatal('move_reply_partial')
            dayhub.remember_control(self, slot, reply=result)
        except Exception:
            self._failed = True
            raise

    def _refresh_move_others(self, move=None):
        """T74-R2：更正 R2 的误导性指标。

        `handoff0167.prepare_home_pending_join()` 的 `others` 直接取自
        ``move.others``（服务器 Move 正文），所以改 ``self._fields['others']``
        对最终的 Join **没有作用**。这里只如实记录服务器给的 others 数量。
        真正要让“被牵方和领路方同实例”的，是 WS/好友票那条 ticket Join 的
        others（见 plan_from_ticket / merge_others）。
        """
        if move is not None and hasattr(move, 'others'):
            self._stats['move_others_injected'] = len(move.others)
        else:
            self._stats['move_others_injected'] = 0

    def _move(self, message, now):
        self._stats['move_received'] += 1
        move = wire.decode_move(message.body)
        self._refresh_move_others(move)
        active = self.active
        dayhub.remember_control(self, active, move=move)
        move_ts = _ts_sec(getattr(move, 'move_ts', 0))
        # 外部票已经在迁同一张票（endpoint+sig 相同）时，服务器随后补发的
        # type 9 Move 不要再回 busy(2)——那会让服务器以为迁移被拒。
        pending_move = getattr(self.pending, 'move', None) if self.pending is not None else None
        if dayhub.bound(self.pending):
            dayhub.observe_move(self, self.pending, move)
        if getattr(self.pending, 'external', False) and pending_move is not None:
            try:
                same_ticket = (move.endpoint == pending_move.endpoint
                               and (move.signature == pending_move.signature
                                    or move.signature == getattr(pending_move, 'signature_alt', b'')))
            except AttributeError:
                same_ticket = False
            if same_ticket:
                # T74-R2：服务器确实对这张票发了 in-band Move；外部迁移失败时
                # 要像原生一样在旧连接回 0x0a(result=4)。
                self.pending.reply_failure = True
                self._stats['external_move_duplicate_move'] = (
                    self._stats.get('external_move_duplicate_move', 0) + 1)
                if move_ts > self._last_move_ts:
                    self._last_move_ts = move_ts
                return
            # T74-R3：服务器已经改发一条**更新**的 Move（door3 03:29:22：
            # 配对票 12038 与 split Move 10429 的 move_ts 同为 1795202963，
            # 原生先试 12038 被 30 拒，随后接受 10429）。这里放弃配对票、
            # 按服务器最新的 Move 走，避免“小光单独跑去配对房”。
            if move_ts and move_ts >= _ts_sec(getattr(pending_move, 'move_ts', 0)):
                self._stats['external_move_superseded'] += 1
                try:
                    self._pending_fail('server_move_superseded')
                except Exception:
                    pass
                pending_move = None
        if move_ts > self._last_move_ts:
            self._last_move_ts = move_ts
        # T76（11-21 三号抓包）：中央神坛门开后配对两端都失败，服务器只对小光发
        # 单人 split Move，原生主人/手机号都没收到、留在原房。小光照跟就被拆开。
        # 原生对单人 split 也会回 0x0a(2) 忙而不跟。这里在神坛内对“无同行”的
        # Move 回忙、留在原房；每个连接最多 8 次，超出后按原逻辑走。开关 SKY_T76_SOLO_BUSY=0。
        try:
            # T86（11-21）：主人说神殿打坐、禁阁每层打坐后也会这样。除神坛外，任何地图在
            # “配对换房刚被拒”后的窗口内（默认 600s，每次被拒刷新）也对单人拆分挪动回忙。
            # 普通地图不会出现配对被拒，所以不受影响。SKY_T86_SOLO_BUSY_AFTER_PAIR_FAIL=0 关。
            _t86_at = getattr(self, '_t86_pair_fail_at', None)
            _t86 = (os.environ.get('SKY_T86_SOLO_BUSY_AFTER_PAIR_FAIL', '1') != '0'
                    and _t86_at is not None
                    and bool(getattr(move, 'split_requested', False))
                    and self._clock() - _t86_at <= float(os.environ.get('SKY_T86_WINDOW_SEC', '600')))
            _solo = ((self._map_id == 2394719185 or _t86) and self.pending is None
                     and os.environ.get('SKY_T76_SOLO_BUSY', '1') != '0'
                     and (len(getattr(move, 'others', ()) or ()) == 0
                          # T92（11-22）：同行名单里有别人却没有主人 = 把小光和陌生人编组挪走，
                          # 同样回忙留在主人身边。SKY_T92_BUSY_WITHOUT_OWNER=0 关。
                          or (os.environ.get('SKY_T92_BUSY_WITHOUT_OWNER', '1') != '0'
                              and isinstance(getattr(self, '_owner_uuid', None), (bytes, bytearray))
                              and bytes(self._owner_uuid) not in
                                  {bytes(x) for x in (getattr(move, 'others', ()) or ())
                                   if isinstance(x, (bytes, bytearray))})))
        except Exception:
            _solo = False
        # T99（11-22 实验）：control/t99_split.json {"allow":1,"stay_sec":60} 时放行一次单人拆分，
        # 观察服务器把小光送去哪种房间；放行后 stay_sec 秒内不追主人（connection0167 读 hold_until）。
        if _solo:
            try:
                import json as _json, time as _time
                _p = os.environ.get('SKY_T99_SPLIT_FILE', '')  # 客户端：T99 实验默认关闭
                if os.path.exists(_p):
                    _c = _json.load(open(_p))
                    if int(_c.get('allow', 0)) > 0 and bool(getattr(move, 'split_requested', False)):
                        _c['allow'] = int(_c['allow']) - 1
                        _c['hold_until'] = _time.time() + float(_c.get('stay_sec', 60))
                        _c['allowed_move_ts'] = move_ts
                        _c['target'] = '%s:%s' % tuple(move.endpoint) if getattr(move, 'endpoint', None) else ''
                        _json.dump(_c, open(_p, 'w'))
                        self._event('t99_split_allowed', target=_c['target'], move_ts=move_ts,
                                    others=len(getattr(move, 'others', ()) or ()))
                        _solo = False
            except Exception:
                pass
        if _solo:
            n = getattr(self, '_t76_solo_busy', 0)
            if n < int(os.environ.get('SKY_T76_SOLO_BUSY_MAX', '1000000')):
                self._t76_solo_busy = n + 1
                self._reply(active, 2)
                self._stats['move_busy_replies'] += 1
                try:
                    self._event('t76_solo_move_busy', count=n + 1,
                                split=bool(getattr(move, 'split_requested', False)),
                                target='%s:%s' % tuple(move.endpoint) if getattr(move, 'endpoint', None) else '',
                                move_ts=move_ts)
                except Exception:
                    pass
                return
        decision = wire.decide_move(move, current_endpoint=active.transport.remote,
                  migration_in_progress=self.pending is not None or active.stage != 'active',
                  current_reservation=self._reservation)
        if decision.action == 'busy':
            self._reply(active, 2)
            self._stats['move_busy_replies'] += 1
            return
        if decision.action != 'stage':
            self._stats['move_ignored'] += 1
            return
        self._begin_move(move, now)

    def _begin_move(self, move, now, *, external=False, reservation=True):
        """Stage one migration for an already-decoded move payload.

        Native server Moves keep the exact original reservation/rollback rules.
        T73 external tickets only borrow the same connect/join/enter/register
        plan; they do not touch the server Move reservation state.
        """
        active = self.active
        if self._stats['move_started'] >= self._max_moves:
            if external:
                raise ValueError('external_move_count_bound')
            self._fatal('move_count_bound')
        if self._deadline - now < 3:
            if external:
                raise ValueError('external_move_insufficient_time')
            self._fatal('move_insufficient_time')
        plan = self._prepare_join(self._profile, self._fields, move,
                    account_uuid=self._account, resource_revision=self._revision,
                    old_endpoint=active.transport.remote,
                    current_endpoint=active.transport.remote,
                    installed_level_id=self._map_id, newly_allocated_connection=True)
        idle = active.engine.export_local_idle()
        transport = self._make_transport(move, self._deadline - now)
        # Register ownership before connect; all subsequent failures close this peer.
        slot = _Slot(transport=transport, counter=0, stage='connect', plan=plan,
                     move=move, idle=idle, stage_deadline=min(self._deadline, now + 5),
                     external=external, ticket_origin=tuple(move.endpoint),
                     started_at=now)
        self.pending = slot
        self._all.append(slot)
        if any(transport is item.transport for item in self._all[:-1]):
            if external:
                try:
                    self._all.remove(slot)
                except ValueError:
                    pass
                self.pending = None
                raise ValueError('external_move_reused_transport')
            self._fatal('move_reused_transport')
        if reservation:
            self._reservation = wire.reservation_transition(self._reservation,
                                                             event='install', move=move)
        self._stats['move_started'] += 1
        transport.connect()  # Emits CONNECT only; no wait/receive.

    def adopt_external_move(self, ticket, now=None):
        """T73-R2：把聊天 WS 同图票变成一次“真票 Join”的 make-before-break。

        Called only on the pump/connection thread. The current room is never
        torn down first: a new peer joins with the ticket, and only an
        EnterGame roster containing the owner (when the ticket is with_owner)
        promotes it. Otherwise the new peer is closed and the current room is
        kept. This is the same shape as the server's own type-9 Move.
        """
        now = self._clock() if now is None else now
        if self._closed or self._failed:
            return {'action': 'terminal'}
        if type(ticket) is not dict:
            return {'action': 'malformed'}
        ticket = dict(ticket)
        # long_runner 的显式字段交换先于本入口。仅保留诊断标志，不改选择顺序。
        ws_sig_manual = bool(os.environ.get('SKY_T74_R2_WS_SIG_FIELD') == 'signature'
                             and ticket.get('signature') and ticket.get('sig'))
        # T74-R3：WS 票同时带 sig/signature，字段语义只能靠线上日志判定。
        # 默认 sig（沿用旧行为），SKY_T74_R3_PAIRING_SIG=signature 可整体切换。
        selected = dayhub.select_r2(self, ticket)
        if selected is False:
            self._stats['external_move_ignored'] += 1
            return {'action':'malformed','error':'pair_signature_invalid'}
        if (selected is None and route.pairing_sig_field() == 'signature' and ticket.get('signature')
                and ticket.get('sig')):
            ticket['sig'], ticket['signature'] = ticket['signature'], ticket['sig']
            ws_sig_manual = True
            if ticket.get('_t75_first_field') in ('sig', 'signature'):
                ticket['_t75_first_field'] = ('sig' if ticket['_t75_first_field']=='signature' else 'signature')
        uri, sig, ts = ticket.get('uri'), ticket.get('sig'), ticket.get('move_ts')
        level, pf = ticket.get('level'), ticket.get('platform_filter', 0)
        if (type(uri) is not str or ':' not in uri or type(sig) is not str
                or len(sig) != 64 or type(ts) is not int):
            self._stats['external_move_ignored'] += 1
            return {'action': 'malformed'}
        host, port = uri.rsplit(':', 1)
        try:
            port, signature = int(port), bytes.fromhex(sig)
        except (TypeError, ValueError):
            self._stats['external_move_ignored'] += 1
            return {'action': 'malformed'}
        if type(host) is not str or not host or not 1 <= port <= 65535:
            self._stats['external_move_ignored'] += 1
            return {'action': 'malformed'}
        if type(pf) is not int:
            pf = 0
        key = sig.lower()
        if key in self._external_seen:
            self._stats['external_move_ignored'] += 1
            return {'action': 'duplicate'}
        if type(level) is int and level != self._map_id:
            self._stats['external_move_ignored'] += 1
            return {'action': 'other_map'}
        if abs(time.time() - (ts / 1000.0 if ts > 10**11 else ts)) > EXTERNAL_MOVE_WINDOW:
            self._stats['external_move_ignored'] += 1
            return {'action': 'stale'}
        # T74-R3：服务器已经改发 split Move（move_ts 不晚于最近一条 in-band
        # Move）时，这张配对票已被取代。door3 03:29:22 的 12038 票 move_ts
        # 与 10429 的 split Move 完全相同，原生也是先被 30 拒。
        ts_sec = _ts_sec(ts)
        if self._last_move_ts and ts_sec <= self._last_move_ts:
            self._stats['external_move_ignored'] += 1
            self._stats['external_move_superseded'] += 1
            return {'action': 'superseded'}
        # busy 不消耗票：连接线程会把票放回队列，等 pump 空出来再试。
        # （旧代码在这里就把 sig 记进 _external_seen，重试会被当 duplicate 丢掉。）
        if self.pending is not None or self.active.stage != 'active':
            self._stats['external_move_busy'] += 1
            return {'action': 'busy'}
        endpoint = (host, port)
        try:
            current = tuple(self.active.transport.remote)
        except (TypeError, ValueError):
            current = None
        if current == endpoint:
            return {'action': 'same_endpoint'}
        if self._deadline - now < 5.0:
            self._stats['external_move_ignored'] += 1
            return {'action': 'deadline'}
        if self._stats['move_started'] >= self._max_moves:
            self._stats['external_move_ignored'] += 1
            return {'action': 'bound'}
        self._external_seen[key] = now
        while len(self._external_seen) > _EXTERNAL_SEEN_MAX:
            self._external_seen.popitem(last=False)
        self._stats['external_move_received'] += 1
        # T73-R2：不再把票对象塞给 handoff0167.prepare_*_join
        # （第一轮这里 100% 抛错导致 begin_failed，票一张都没进 Join）。
        # 改为用“新段入服”同一套字段生成 Join 明文，再走 stage_ticket_join。
        with_owner = bool(ticket.get('with_owner'))
        if not with_owner and self._owner_uuid is not None:
            want = self._owner_uuid.hex()
            with_owner = any(
                isinstance(row, str) and row.replace('-', '').lower() == want
                for row in (ticket.get('other_players') or []))
        try:
            plan = self.plan_from_ticket(
                {**ticket, 'level': self._map_id},
                expected_owner=(self._owner_uuid if with_owner else None),
                origin='ws_same_map', dial_policy='pairing')
        except Exception as exc:
            # Join 明文的构造失败也要留类型名（第一轮 begin_failed 就是没留）。
            self._stats['external_move_failed'] += 1
            self._stats['external_move_error'] = 'join_plan_' + type(exc).__name__
            self._stats['external_move_action'] = 'failed'
            return {'action': 'failed', 'error': type(exc).__name__}
        if plan is None:
            self._stats['external_move_failed'] += 1
            self._stats['external_move_error'] = 'no_join_plan'
            self._stats['external_move_action'] = 'failed'
            return {'action': 'failed', 'error': 'no_join_plan'}
        plan.ws_declared_level = level if type(level) is int else None
        plan.ws_sig_manual = ws_sig_manual
        return self.stage_ticket_join(plan, now=now)

    def _reply_failure(self, slot):
        """T74-R2：外部迁移失败时，在旧连接回 0x0a(result=4)。

        原生 door.pcap 02:25:24.003 / 02:25:38.018：新 peer 被 36 拒后，
        客户端在旧连接回 MoveReply(4)，服务器随后发 0x11 重新同步旧房间。
        door3 实测更彻底：每次配对新 peer 6s 超时后，原生都会在旧连接回
        result=4（03:27:47.698 / 03:28:16.176 / 03:28:59.325），而服务器
        对这张票并没有发 in-band Move——这是配对状态回执，不是 Move 应答。
        因此 T74-R3 对“配对票”默认也回（SKY_T74_R3_PAIR_FAIL_REPLY=0 可关）。
        """
        if slot.failure_replied:
            return False
        mode = route.fail_reply_mode()
        pairing_reply = (getattr(slot, 'dial_policy', 'native') == 'pairing'
                         and route.pair_fail_reply_enabled())
        if mode == 'off' or (mode == 'move' and not slot.reply_failure and not pairing_reply):
            return False
        active = self.active
        if active.stage != 'active' or active.transport is None:
            return False
        try:
            self._reply(active, 4)
        except Exception:
            return False
        slot.failure_replied = True
        self._stats['ticket_fail_replies'] += 1
        if pairing_reply:
            self._stats['pairing_fail_replies'] = self._stats.get('pairing_fail_replies', 0) + 1
        self._event('ticket_fail_reply', origin=self._plan_origin(slot),
                    pairing=bool(pairing_reply))
        return True

    def _pending_fail(self, code):
        """Fail a pending migration. External tickets abort only themselves."""
        slot = self.pending
        if slot is not None and getattr(slot, 'external', False):
            origin = self._plan_origin(slot)
            # T86（11-21）：记下“配对换房被服务器拒绝”的时刻，供下方单人挪动留人判断。
            try:
                if (getattr(slot, 'dial_policy', 'native') == 'pairing'
                        and (str(code).startswith('server_disconnect_')
                             or str(code).startswith('pending_server'))):
                    self._t86_pair_fail_at = self._clock()
            except Exception:
                pass
            # T94：神坛加入被 36/应用拒绝 → 换另一份场景数据（门开⇄门关）
            try:
                if (self._map_id == 2394719185
                        and (str(code).startswith('server_disconnect_36')
                             or str(code).startswith('pending_server'))):
                    from .join0167_profile import t94_flip_dayhub
                    self._event('t94_dayhub_flip', to=t94_flip_dayhub(), reason=str(code)[:40])
            except Exception:
                pass
            try:
                self._reply_failure(slot)
            except Exception:
                pass
            self._stats['external_move_failed'] += 1
            self._stats['external_move_error'] = code
            self._stats['external_move_action'] = 'aborted'
            self._stats['ticket_join_aborted'] += 1
            self._stats['ticket_join_error'] = code
            self._stats['ticket_join_stage'] = 'aborted'
            now = self._clock()
            self._stats['ticket_join_elapsed_ms'] = self._elapsed_ms(slot, now)
            if origin.startswith('owner_rejoin'):
                self._stats['owner_rejoin_aborted'] += 1
                self._stats['owner_rejoin_action'] = 'aborted'
            self._event('ticket_join_aborted', reason=code, origin=origin,
                        stage=slot.stage, dial=self._dial_text(slot),
                        elapsed_ms=self._stats['ticket_join_elapsed_ms'],
                        policy=getattr(slot, 'dial_policy', 'native'),
                        dial_log=list(slot.dial_log[-4:]),
                        sig2=(slot.plan.signature[:2].hex()
                              if getattr(slot, 'plan', None) is not None
                              and getattr(slot.plan, 'signature', None) else ''))
            try:
                slot.transport.force_close()
            except Exception:
                pass
            try:
                self._all.remove(slot)
            except ValueError:
                pass
            self.pending = None
            return True
        self._fatal(code)
        return False

    # ---- T74-R3：配对票即时诊断 / 换签名字段 / 换候选 -------------------
    def _plan_origin(self, slot):
        return str(getattr(getattr(slot, 'plan', None), 'origin', '') or '')

    def _dial_text(self, slot):
        try:
            host, port = slot.transport.remote
            return '%s:%d' % (host, int(port))
        except Exception:
            return ''

    def _elapsed_ms(self, slot, now=None):
        started = getattr(slot, 'started_at', 0.0) or 0.0
        if not started:
            return 0
        return max(0, int(((self._clock() if now is None else now) - started) * 1000))

    def _dial_source(self, slot):
        plan = getattr(slot, 'plan', None)
        try:
            return plan.dial_candidates[getattr(plan, 'dial_index', 0)][2]
        except Exception:
            return ''

    def _try_alt(self, reason):
        slot = self.pending
        if dayhub.first_only(slot):
            return False  # 本端这张新票只发送一次 Join；明确拒绝后等新的服务端票。
        if (slot is None or not getattr(slot, 'external', False)
                or getattr(slot, 'alt_tried', False)):
            return False
        if not (reason.startswith('server_disconnect_')
                or reason in ('pending_enter_timeout', 'pending_join_failed',
                              'pending_server_rejection')):
            return False
        alt = self._alternate_sig_plan(slot.plan)
        if alt is None:
            return False
        return self._replace_pending(alt, reason)

    def _alternate_sig_plan(self, plan):
        """WS 票同时带 sig/signature；用另一个字段重建同一张票的 Join。

        原生 Join 的 32B 签名是“每端一张票各自一份”，door3 里既不是小光 WS
        的 sig 也不是 signature，无法从抓包判定哪个字段才是服务器认的那个。
        因此第一次被服务器拒/等不到 EnterGame 时，用另一个字段重试一次，
        线上日志会直接告诉我们哪个能通。
        """
        if plan is None or plan.sig_swapped or not route.alt_sig_enabled():
            return None
        alt = plan.signature_alt
        core = dict(plan.ticket_core or {})
        if not alt or len(alt) != 32 or not core:
            return None
        current = (plan.signature or b'').hex()
        core['sig'] = alt.hex()
        core['signature'] = current
        try:
            new = self.plan_from_ticket(core, expected_owner=plan.expected_owner,
                                        origin=plan.origin,
                                        dial_policy=getattr(plan, 'dial_policy', 'native'))
        except Exception:
            return None
        if new is None:
            return None
        new.sig_swapped = True
        return new

    def _replace_pending(self, plan, reason):
        """保留本 slot，只换 Join 明文/签名/新 peer；不改旧连接。"""
        slot = self.pending
        if slot is None:
            return False
        now = self._clock()
        try:
            slot.transport.force_close()
        except Exception:
            pass
        try:
            transport = self._make_transport(plan, self._deadline - now)
        except Exception:
            return self._pending_fail(reason)
        slot.transport = transport
        slot.plan = plan
        slot.move = plan
        slot.token = None
        slot.buffered = []
        slot.enter_body = b''
        slot.connected_at = 0.0
        slot.join_sent_at = 0.0
        slot.alt_tried = True
        slot.stage = 'connect'
        window = (route.pairing_connect_timeout_sec()
                  if getattr(plan, 'dial_policy', 'native') == 'pairing'
                  else route.connect_timeout_sec())
        slot.stage_deadline = min(self._deadline, now + window)
        self._stats['ticket_sig_fallback'] += 1
        self._stats['ticket_join_stage'] = 'connect'
        self._stats['ticket_join_dial'] = '%s:%d' % (plan.endpoint[0], plan.endpoint[1])
        self._event('ticket_sig_fallback', reason=reason,
                    origin=self._plan_origin(slot),
                    dial=self._stats['ticket_join_dial'],
                    sig2=plan.signature[:2].hex())
        try:
            transport.connect()
        except Exception as exc:
            return self._pending_fail('ticket_join_connect_' + type(exc).__name__)
        return True

    def _fail_or_alt(self, reason):
        """外部票失败前的最后两级：先换签名，再判失败。

        只有“服务器真的回了拒码 / Join 后等不到 EnterGame”才试另一个签名；
        连不上（timeout/transport）说明是拨号问题，换签名没意义。
        """
        if self._try_alt(reason):
            return True
        return self._pending_fail(reason)

    def _active_batch(self, messages, now):
        self._terminal(messages)
        for message in messages:
            if message.message_type == 9:
                self._move(message, now)
            elif self.pending is None and self.active.stage == 'active':
                self._group(self.active, self.active.engine.handle(
                    message.message_type, message.body, now))
            # The old native producer is paused while pending; ENet still services.

    def accept_initial(self, messages):
        self._check()
        if CLAUDEWORK_LISTENERS:
            _claudework_notify(list(messages))
        self._active_batch(list(messages), self._clock())

    def _known_rejection(self, terminal_types):
        # Native state6/result04 is a known pre-success failure, not delivery unknown.
        known = bool(terminal_types and all(t in (1, 2) for t in terminal_types))
        slot = self.pending
        external = slot is not None and getattr(slot, 'external', False)
        if known:
            self._stats['terminal_message_type'] = terminal_types[0]
            if not external:
                self._reply(self.active, 4)
                self._reservation = wire.reservation_transition(self._reservation,
                    event='terminal-failure', local_outcome_known=True, old_success_sent=False)
        try:  # 11-21：记录被拒消息（类型+前48字节），排查配对票为何秒拒
            if dayhub.bound(slot):
                dayhub.terminal(self, slot, 'application', types=terminal_types)
            elif slot is not None:
                self._event('ticket_reject_detail', dial=self._dial_text(slot),
                            dial_source=self._dial_source(slot),
                            msgs=[[m.message_type, len(m.body), m.body[:48].hex()]
                                  for m in list(slot.buffered)[-6:]])
        except Exception:
            pass
        if self._fail_or_alt('pending_server_rejection' if known else 'pending_unsupported_control'):
            return

    def _t82_pair_join(self, slot):
        """仅核对最终待排队帧。未命中原样返回；不改票、槽计数、签名或后续状态。"""
        plan = slot.plan
        frame = plan.plaintext
        level = 2394719185
        if (self._map_id != level or not slot.external or
                os.environ.get('SKY_T82_PAIR_JOIN_ALIGN', '1') == '0' or
                not isinstance(plan, TicketJoinPlan) or plan.origin != 'ws_same_map' or
                slot.dial_policy != 'pairing' or plan.dial_policy != 'pairing' or
                plan.level != level or plan.ws_declared_level != level or
                plan.ws_sig_manual or plan.sig_swapped or slot.alt_tried or plan.dial_index != 0):
            return frame, None
        core, owner = plan.ticket_core, plan.expected_owner
        if (not isinstance(core, dict) or core.get('level') != level or
                not isinstance(owner, bytes) or len(owner) != 16 or owner != self._owner_uuid):
            return frame, None
        players = core.get('other_players')
        if (not isinstance(players, (tuple, list)) or len(players) != 1 or
                route.merge_others(players) != (owner,) or type(frame) is not bytes):
            return frame, None
        from .join0167_codec import decode
        try:
            f = decode(frame, frame=True)
        except (ValueError, TypeError, struct.error):
            return frame, None
        if (f['uid'] != route.uuid_raw(self._account) or f['move_ts'] != plan.move_ts or
                core.get('move_ts') != plan.move_ts or
                f['level_id'] != level or f['previous_level_id'] != level or
                f['tail_bool_a'] != 1 or f['tail_bool_b'] != 0 or
                f['previous_address_tag'] != 1 or not f['previous_port'] or
                f['others'] != [owner] or f['uid'] == owner):
            return frame, None
        old = f['counter']
        return (frame if old == 0 else frame[:5] + b'\x00' + frame[6:]), {
            'input_counter': old, 'effective_counter': 0, 'changed': old != 0}

    def _advance_pending(self, now):
        slot = self.pending
        if slot is None:
            return
        transport = slot.transport
        try:
            transport.service()
        except ProbeTransportError as exc:
            code = getattr(transport, 'remote_disconnect_code', None)
            if code is not None and getattr(slot, 'external', False):
                if dayhub.bound(slot):
                    dayhub.terminal(self, slot, 'enet', code=int(code))
                self._stats['ticket_join_server_disconnect'] += 1
                reason = 'server_disconnect_%d' % code
                slot.dial_log.append({'dial': self._dial_text(slot),
                                      'code': int(code), 'stage': slot.stage,
                                      'source': self._dial_source(slot)})
                if dayhub.first_only(slot):
                    self._pending_fail(reason)
                    return
                # T74-R3：origin 是机器人验证过可达的直连地址。如果 origin 被
                # 服务器拒（30/36），第一嫌疑是签名字段而不是房间，所以先换
                # 另一个签名字段；其他候选（known_edge 猜的实例）被拒才换地址。
                if slot.stage == 'connect' and self._dial_source(slot) == 'origin':
                    if self._try_alt(reason):
                        return
                if slot.stage == 'connect' and self._rotate_dial(slot):
                    return
                if self._fail_or_alt(reason):
                    return
                return
            if (getattr(slot, 'external', False) and slot.stage == 'connect'
                    and self._rotate_dial(slot)):
                return
            if getattr(slot, 'external', False):
                if dayhub.bound(slot):
                    dayhub.terminal(self, slot, 'transport')
                self._fail_or_alt('pending_transport_' + type(exc).__name__)
                return
            raise
        now = self._clock()
        messages = self._drain(transport)
        if now >= slot.stage_deadline:
            if dayhub.bound(slot):
                dayhub.terminal(self, slot, 'deadline')
            if (getattr(slot, 'external', False) and slot.stage == 'connect'
                    and self._rotate_dial(slot)):
                return
            if self._fail_or_alt('pending_' + slot.stage + '_timeout'):
                return
        if slot.stage == 'connect':
            if messages:
                if self._fail_or_alt('pending_application_before_join'):
                    return
            if transport.state != enet.ST_CONNECTED:
                return
            if not slot.connected_at:
                slot.connected_at = now
                self._stats['ticket_join_stage'] = 'connected'
                self._event('ticket_join_connected', origin=self._plan_origin(slot),
                            dial=self._dial_text(slot),
                            dial_source=self._stats.get('ticket_dial_source', ''),
                            verify=True,
                            elapsed_ms=self._elapsed_ms(slot, now))
            outgoing, aligned = self._t82_pair_join(slot)
            slot.token = transport.queue_join(outgoing)
            if aligned is not None:
                self._event('t82_pair_join_align', **aligned)
            slot.join_sent_at = now
            slot.stage = 'enter'
            self._stats['ticket_join_stage'] = 'join_sent'
            self._event('ticket_join_join_sent', origin=self._plan_origin(slot),
                        dial=self._dial_text(slot),
                        elapsed_ms=self._elapsed_ms(slot, now),
                        join_len=len(outgoing),
                        **({'t82_pair_join_align': aligned} if aligned is not None else {}))
            # 原生 Join→EnterGame 238ms、配对失败要等满 6.0s；配对票给 4s。
            window = (route.pairing_enter_timeout_sec()
                      if getattr(slot, 'dial_policy', 'native') == 'pairing'
                      else route.enter_timeout_sec())
            if dayhub.bound(slot):
                dayhub.sent(self, slot)
                # 原生从 Join 发出计六秒。明确拒绝仍立即处理；保留显式运维超时。
                if not os.environ.get('SKY_T74_R3_PAIRING_ENTER_MS'):
                    window = 6.0
            slot.stage_deadline = min(self._deadline, now + window)
            return
        # Whole cumulative batch, including previously buffered data, is preflighted.
        self._buffer(slot, messages)
        decision = wire.preflight_pending_batch(slot.buffered)
        if decision.action == 'veto':
            self._known_rejection(decision.terminal_types)
            return
        if transport.reliable_status(slot.token) == 'failed':
            if self._fail_or_alt('pending_join_failed'):
                return
        if decision.action != 'validate-enter':
            return
        self._validate_enter(decision.enter_message, account_uuid=self._account,
                             expected_level_id=self._map_id, pending_state=3)
        slot.enter_body = decision.enter_message.body
        # T73-R2：真实入服必须用服务器下发的 EnterGame/PlayerSync 名单证明
        # “主人在这个新房间里”。证明不了就回滚新 peer，旧房间原样保留。
        # 一键关：SKY_T73_R2_OWNER_CHECK=0（不建议）。
        required_owner = getattr(slot.plan, 'expected_owner', None)
        owner_in = (roster_contains_uuid(slot.enter_body, required_owner)
                    if required_owner is not None else None)
        self._stats['ticket_join_stage'] = 'enter'
        self._event('ticket_join_enter', origin=self._plan_origin(slot),
                    dial=self._dial_text(slot),
                    elapsed_ms=self._elapsed_ms(slot, now),
                    roster=(len(slot.enter_body) - 1) // OWNER_ROSTER_RECORD
                    if slot.enter_body else 0,
                    owner_in_roster=owner_in)
        if (slot.external and required_owner is not None
                and (dayhub.bound(slot) or os.environ.get('SKY_T73_R2_OWNER_CHECK', '1') != '0')
                and not owner_in):
            if self._fail_or_alt('owner_not_in_destination'):
                return
        slot.buffered = list(decision.deferred_messages)
        old = self.active
        # The following order is native-observed. No old ACK wait before promotion.
        self._reply(old, 0)
        old.transport.disconnect_later(2)
        old.transport.flush()
        old.engine.close()
        old.stage = 'retired'
        old.retired_at = now
        self.retired.append(old)
        self.active = slot
        self.pending = None
        # T74-R2：新连接继承“票面 origin”，并学习 origin→实际拨号 的路由。
        if slot.ticket_origin is None:
            slot.ticket_origin = (getattr(slot.plan, 'origin_endpoint', None)
                                  or tuple(transport.remote))
        self.active.ticket_origin = slot.ticket_origin
        try:
            self._route_book.learn(slot.ticket_origin, tuple(transport.remote))
        except Exception:
            pass
        slot.stage = 'registration'
        slot.counter = 1
        if dayhub.bound(slot):
            prior = slot.plan.plaintext[5]
            slot.counter = 1 if prior == 255 else prior + 1
        self._stats['registration_transport_acked'] = False
        self._stats['matching_generation_observed'] = False
        if not slot.external:
            self._reservation = wire.reservation_transition(self._reservation, event='commit')
        slot.token = transport.queue_registration(
            struct.pack('<IBB', self._peer, 4, slot.counter) + self._registration)
        slot.stage_deadline = min(self._deadline, now + 5)

    def _advance_registration(self, messages, now):
        slot = self.active
        # Veto before any Engine creation, even if registration ACK arrived as well.
        self._terminal(messages)
        if os.environ.get('CLAUDEWORK_HANDOFF_BUSY_REG', '1') != '0':
            # claudework T70-8：注册还没完成又收到搬迁（type 9，服务器连续搬迁）。原来直接断开
            # （registration_unexpected_control，小光掉线换房间）。按原生 decide_move 的规则
            # “迁移进行中→回复 busy(2)”，服务器稍后会重发，不断开。CLAUDEWORK_HANDOFF_BUSY_REG=0 退回原行为。
            kept = []
            for m in messages:
                if m.message_type == 9:
                    wire.decode_move(m.body)        # 格式不对照旧抛错
                    self._stats['move_received'] += 1
                    self._reply(slot, 2)
                    self._stats['move_busy_replies'] += 1
                    self._stats['registration_busy_replies'] = self._stats.get('registration_busy_replies', 0) + 1
                else:
                    kept.append(m)
            messages = kept
            # claudework T70-9：注册期间重发的同一条 EnterGame（type 17，内容与已收到的完全相同）忽略，不断开。
            kept = []
            for m in messages:
                if (m.message_type == 17 and getattr(slot, 'enter_body', None) is not None
                        and m.body == slot.enter_body):
                    self._stats['registration_dup_enter'] = self._stats.get('registration_dup_enter', 0) + 1
                else:
                    kept.append(m)
            messages = kept
        bad = [m for m in messages if m.message_type in (9, 17)]
        if bad:
            self._stats['registration_unexpected_type'] = bad[0].message_type
            self._stats['registration_unexpected_len'] = len(bad[0].body)
            self._stats['registration_unexpected_same_enter'] = (
                getattr(slot, 'enter_body', None) is not None and bad[0].body == slot.enter_body)
            self._fatal('registration_unexpected_control')
        self._buffer(slot, messages)
        now = self._clock()  # Registration starts at the actual ACK observation.
        if now >= slot.stage_deadline:
            self._fatal('pending_registration_timeout')
        status = slot.transport.reliable_status(slot.token)
        if status == 'failed':
            self._fatal('pending_registration_failed')
        if status != 'acked':
            if now >= slot.stage_deadline:
                self._fatal('pending_registration_timeout')
            return
        origin = getattr(slot.plan, 'origin', '') or ''
        try:
            committed_move_ts = int(getattr(slot.plan, 'move_ts', 0) or 0)
        except (TypeError, ValueError):
            committed_move_ts = 0
        if not committed_move_ts:
            try:
                committed_move_ts = int(getattr(slot.move, 'move_ts', 0) or 0)
            except (TypeError, ValueError):
                committed_move_ts = 0
        slot.engine = self._make_engine(slot.transport, slot.enter_body, now, slot.idle)
        slot.idle = None
        slot.plan = None
        slot.move = None
        slot.stage = 'active'
        self._stats['move_completed'] += 1
        self._stats['registration_transport_acked'] = True
        owner_in = (roster_contains_uuid(slot.enter_body, self._owner_uuid)
                    if self._owner_uuid is not None else None)
        if owner_in is not None:
            self._stats['owner_in_roster'] = bool(owner_in)
        if slot.external:
            self._stats['external_move_action'] = 'committed'
            self._stats['external_move_error'] = ''
            self._stats['ticket_join_committed'] += 1
            self._stats['ticket_join_stage'] = 'committed'
            self._stats['ticket_join_elapsed_ms'] = self._elapsed_ms(slot, now)
            if origin.startswith('owner_rejoin') and owner_in:
                self._stats['owner_rejoin_committed'] += 1
                self._stats['owner_rejoin_action'] = 'committed'
        target = ''
        try:
            target = '%s:%d' % tuple(slot.ticket_origin or ())
        except (TypeError, ValueError):
            target = ''
        self._event('move_committed', external=slot.external, origin=origin,
                    owner_in_roster=owner_in, move_ts=committed_move_ts,
                    target=target, dial=self._dial_text(slot),
                    elapsed_ms=self._elapsed_ms(slot, now),
                    players=((len(slot.enter_body) - 1) // OWNER_ROSTER_RECORD
                             if slot.enter_body else 0))
        buffered, slot.buffered = slot.buffered, []
        for message in buffered:
            self._group(slot, slot.engine.handle(message.message_type, message.body, now))

    def _retire(self, now):
        for slot in list(self.retired):
            try:
                slot.transport.service()
                # No retired peer can start another migration or mutate new Engine.
                self._drain(slot.transport)
                status = slot.transport.disconnect_status()
                complete = status['graceful_complete']
                if not complete and now - slot.retired_at < 5 and status['phase'] != 'failed':
                    continue
            except Exception:
                complete = False
            slot.transport.force_close()
            if slot.transport.socket.fileno() != -1:
                self._fatal('retired_close_failed')
            self._stats['move_graceful_closed' if complete else 'move_forced_closed'] += 1
            self.retired.remove(slot)

    def step(self):
        if self._closed or self._failed:
            self._fatal('handoff_pump_terminal')
        self._check()
        now = self._clock()
        try:
            self.active.transport.service()
            now = self._clock()
            messages = self._drain(self.active.transport)
            if self.active.stage == 'registration':
                self._advance_registration(messages, now)
            else:
                self._active_batch(messages, now)
            try:
                self._advance_pending(now)
            except Exception as exc:
                # 外部真票在“退旧 peer 之前”的解析/校验异常（EnterGame、
                # 名单、plan 字段不符）只回滚新 peer；协议边界/handoff 自身
                # 的 fatal 仍按原逻辑终止，不能被软回滚掩盖。
                if isinstance(exc, HandoffRuntimeError):
                    raise
                slot = self.pending
                if slot is None or not getattr(slot, 'external', False):
                    raise
                self._stats['ticket_join_error'] = 'ticket_join_' + type(exc).__name__
                self._pending_fail('ticket_join_' + type(exc).__name__)
            self._retire(now)
            if self.pending is None and self.active.stage == 'active':
                self._group(self.active, self.active.engine.tick(self._clock()))
        except Exception:
            self._failed = True
            raise

    def metrics(self):
        result = dict(self._stats)
        for public, attribute in [('application_packets','raw_packets'),
                                  ('received_datagrams','pkt_recv'),
                                  ('sent_datagrams','pkt_sent'),
                                  ('crc_failures','crc_fail_count')]:
            result[public] = sum(getattr(s.transport, attribute, 0) for s in self._all)
        result['pending_reliable_commands'] = sum(len(s.transport.pending_cmds) for s in self._all)
        result['remote_disconnect_code'] = getattr(self.active.transport, 'remote_disconnect_code', None)
        result['move_in_progress'] = self.pending is not None or self.active.stage == 'registration'
        return result

    def close(self):
        self._closed = True
        confirmed = True
        disconnect_sent = False
        seen = set()
        for slot in self._all:
            if slot.engine is not None:
                try:
                    slot.engine.close()
                except Exception:
                    pass
            transport = slot.transport
            if id(transport) in seen:
                continue
            seen.add(id(transport))
            try:
                transport.close()
            except Exception:
                pass
            try:
                confirmed = confirmed and transport.socket.fileno() == -1
            except Exception:
                confirmed = False
            disconnect_sent = disconnect_sent or bool(transport.disconnect_sent)
        result = self.metrics()
        result.update(transport_closed=confirmed, disconnect_sent=disconnect_sent,
                      move_in_progress=False)
        return result


class HomeHandoffPump(_RoomHandoffPump):
    """Compatibility wrapper: the verified Home entry remains Home-only."""
    def __init__(self, **kwargs):
        super().__init__(map_id=HOME, **kwargs)
