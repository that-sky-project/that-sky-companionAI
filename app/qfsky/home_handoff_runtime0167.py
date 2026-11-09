"""Single-owner Home long-connection pump; server Move is not a map switch.

There are no HTTP calls or automatic ticket retries here. Only an authenticated
active peer can supply a new ticket. New peer state, keys, counters, ACK history
and authority are never copied from the old connection. Local idle continuity
is the narrow Engine snapshot, not a network-state snapshot.
"""
from __future__ import annotations

import os
import struct
import time
from dataclasses import dataclass, field

from . import enet, handoff0167 as wire
from .long_transport0167 import OutboundFrame
from .receive0167 import validate_pending_enter_game

HOME = 3526133726


class HandoffRuntimeError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)



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
                 validate_enter=validate_pending_enter_game):
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
        self._reservation = wire.initial_reservation(newly_initialized_session=True)
        self._closed = False
        self._failed = False
        self._stats = dict(move_received=0, move_started=0, move_completed=0,
                           move_busy_replies=0, move_ignored=0,
                           move_graceful_closed=0, move_forced_closed=0,
                           long_groups=0, long_actor_frames=0,
                           long_controller_frames=0, long_scene_frames=0,
                           long_feedback_frames=0, reply_count=0,
                           matching_generation_observed=False,
                           registration_transport_acked=True)

    def __repr__(self):
        return '<' + type(self).__name__ + ' private-single-account>'

    def _fatal(self, code):
        self._failed = True
        raise HandoffRuntimeError(code)

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
        except Exception:
            self._failed = True
            raise

    def _move(self, message, now):
        self._stats['move_received'] += 1
        move = wire.decode_move(message.body)
        active = self.active
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
        if self._stats['move_started'] >= self._max_moves:
            self._fatal('move_count_bound')
        if self._deadline - now < 3:
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
                     move=move, idle=idle, stage_deadline=min(self._deadline, now + 5))
        self.pending = slot
        self._all.append(slot)
        if any(transport is item.transport for item in self._all[:-1]):
            self._fatal('move_reused_transport')
        self._reservation = wire.reservation_transition(self._reservation,
                                                         event='install', move=move)
        self._stats['move_started'] += 1
        transport.connect()  # Emits CONNECT only; no wait/receive.

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
        if terminal_types and all(t in (1, 2) for t in terminal_types):
            self._stats['terminal_message_type'] = terminal_types[0]
            self._reply(self.active, 4)
            self._reservation = wire.reservation_transition(self._reservation,
                event='terminal-failure', local_outcome_known=True, old_success_sent=False)
        self._fatal('pending_server_rejection' if terminal_types and all(t in (1, 2) for t in terminal_types) else 'pending_unsupported_control')

    def _advance_pending(self, now):
        slot = self.pending
        if slot is None:
            return
        transport = slot.transport
        transport.service()
        now = self._clock()
        messages = self._drain(transport)
        if now >= slot.stage_deadline:
            self._fatal('pending_' + slot.stage + '_timeout')
        if slot.stage == 'connect':
            if messages:
                self._fatal('pending_application_before_join')
            if transport.state != enet.ST_CONNECTED:
                return
            slot.token = transport.queue_join(slot.plan.plaintext)
            slot.stage = 'enter'
            slot.stage_deadline = min(self._deadline, now + 8)
            return
        # Whole cumulative batch, including previously buffered data, is preflighted.
        self._buffer(slot, messages)
        decision = wire.preflight_pending_batch(slot.buffered)
        if decision.action == 'veto':
            self._known_rejection(decision.terminal_types)
        if transport.reliable_status(slot.token) == 'failed':
            self._fatal('pending_join_failed')
        if decision.action != 'validate-enter':
            return
        self._validate_enter(decision.enter_message, account_uuid=self._account,
                             expected_level_id=self._map_id, pending_state=3)
        slot.enter_body = decision.enter_message.body
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
        slot.stage = 'registration'
        slot.counter = 1
        self._stats['registration_transport_acked'] = False
        self._stats['matching_generation_observed'] = False
        self._reservation = wire.reservation_transition(self._reservation, event='commit')
        slot.token = transport.queue_registration(
            struct.pack('<IBB', self._peer, 4, 1) + self._registration)
        slot.stage_deadline = min(self._deadline, now + 5)

    def _advance_registration(self, messages, now):
        slot = self.active
        # Veto before any Engine creation, even if registration ACK arrived as well.
        self._terminal(messages)
        if any(m.message_type in (9, 17) for m in messages):
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
        slot.engine = self._make_engine(slot.transport, slot.enter_body, now, slot.idle)
        slot.idle = None
        slot.plan = None
        slot.move = None
        slot.stage = 'active'
        self._stats['move_completed'] += 1
        self._stats['registration_transport_acked'] = True
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
            self._advance_pending(now)
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
