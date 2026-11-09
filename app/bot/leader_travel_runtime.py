"""Explicit, zero-resource leader travel adapter for the existing controller.

No direct socket and no coordinate teleport. A native reliable ChangeLevel RPC
must commit through the current engine before the runner changes its own map.
"""
from __future__ import annotations

import math
import os
import re
import threading
import time
import types
import uuid

from travel_coordinator import (AttachmentEvidence, CarryKind, Destination,
    EffectKind, Observation, OfferEvidence, Phase, Presence, Receipt, ReceiptState,
    Request, Scene, SlashPermit, TERMINAL, TravelCoordinator)


def fnv32(scene):
    h = 0x811c9dc5
    for c in scene.encode('ascii'):
        h = ((h ^ c) * 0x01000193) & 0xffffffff
    return h


def valid_uuid(value):
    try:
        return type(value) is str and str(uuid.UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


class LeaderTravelRuntime:
    def __init__(self, ctl, *, session_deadline, has_capture, rpc_port,
                 clock=time.monotonic, wall=time.time):
        self.ctl = ctl
        self.clock, self.wall = clock, wall
        self.session_deadline = session_deadline
        self.has_capture, self.rpc = has_capture, rpc_port
        self.lock = threading.RLock()
        self.revision = 0
        self.current = None
        self.command = None
        self.pending_transfer = None
        self.transfer_taken = False
        self._same_map_snapshot = None
        self._same_peer_ws_intent = None
        self.seen = set()
        self.last_status = None
        self.last_snapshot = None
        self._last_tick = 0.0
        self.resume_gate = None
        self.resume_authorization = None
        self.resume_reported = False

    def log(self, event, **fields):
        self.ctl.log('escort_' + event, **fields)

    def is_current(self, binding):
        return bool(self.current is not None and self.current.binding == binding
                    and self.current.phase not in TERMINAL
                    and self.revision == binding.command_revision
                    and not self.ctl.stop.is_set() and self.clock() < binding.deadline
                    and self.wall() < self.session_deadline)

    def available_without_resources(self, kind):
        return kind in (EffectKind.INVITE, EffectKind.TRANSFER, EffectKind.STOP)

    def _validate(self, cmd):
        now, wall = self.clock(), self.wall()
        if (type(cmd) is not dict or cmd.get('pose') != 'escort_travel'
                or cmd.get('src') != 'chat' or not valid_uuid(cmd.get('requester'))
                or cmd.get('requester') != cmd.get('target')
                or cmd.get('requester') == self.ctl.tracker.own_uuid):
            raise ValueError('invalid_request_owner')
        raw, scene = cmd.get('raw_command'), cmd.get('scene')
        if (type(raw) is not str or len(raw) > 512 or '\n' in raw or '\r' in raw
                or not raw.lstrip().startswith(('/', '／'))):
            raise ValueError('explicit_slash_required')
        # Verify the command's destination a second time at the receiver. Chat
        # never gets to supply an unrelated scene/hash pair or arbitrary XYZ.
        from destination_resolver_v25 import load_default
        from travel_chat_v25 import parse
        parsed = parse(raw)
        query = getattr(parsed, 'destination_query', None)
        if query is None or parsed.operation != 'start' or parsed.carry_kind != cmd.get('carry_kind'):
            raise ValueError('unrecognized_travel_command')
        resolved = load_default().resolve(query)
        dest = resolved.get('destination') or {}
        variant = cmd.get('variant')
        if type(variant) not in (type(None), str):
            raise ValueError('invalid_spawn_variant')
        if variant is not None and (re.fullmatch(r'[a-z0-9_]{1,32}', variant) is None
                                    or dest.get('spawnVariant') != variant):
            raise ValueError('destination_variant_mismatch')
        # 普通目的地必须是已验证场景；landmark 只有在带 spawnVariant 时才是可执行落点
        # （没有变体的 landmark 例如 OOB Castle 仍然只能当资料，不能带路）。
        status_ok = (resolved.get('status') == 'resolved' if variant is None
                     else resolved.get('status') in ('resolved', 'unverified_landmark'))
        if (not status_ok
                or dest.get('sceneName') != scene
                or dest.get('sceneHash') != cmd.get('map_id')
                or type(scene) is not str or not scene.isascii()
                or fnv32(scene) != cmd.get('map_id') or cmd.get('arrival') != 'scene_spawn'):
            raise ValueError('destination_mismatch')
        kind = CarryKind(cmd.get('carry_kind'))
        for key in ('issued_at', 'expires_at'):
            if type(cmd.get(key)) not in (int, float) or not math.isfinite(cmd[key]):
                raise ValueError('invalid_command_time')
        if not 0 <= wall - cmd['issued_at'] <= 30:
            raise ValueError('stale_command')
        expiry = min(cmd['expires_at'], self.session_deadline, wall + 180)
        if not wall < expiry or cmd['expires_at'] > cmd['issued_at'] + 180.5:
            raise ValueError('command_expired')
        if not self.has_capture(self.ctl.tracker.own_uuid, cmd['map_id']):
            raise ValueError('destination_capture_unavailable')
        request_id = cmd.get('request_id')
        if type(request_id) is not str or not 8 <= len(request_id) <= 80 or not all(
                c.isascii() and (c.isalnum() or c in '_-') for c in request_id):
            raise ValueError('invalid_request_id')
        return now, expiry - wall, kind, dest

    def accept(self, cmd):
        with self.lock:
            try:
                now, remaining, kind, dest = self._validate(cmd)
                if cmd['request_id'] in self.seen:
                    return False
                if len(self.seen) >= 256:
                    raise ValueError('request_limit')
                self.preempt('new_travel')
                reused = self._attached_kind(cmd['requester'])
                self.reused_attachment = reused is not None
                if reused is not None and reused is not kind:
                    self.log('kind_reused', requested=kind.value, existing=reused.value)
                    kind = reused
                self.revision += 1
                permit = SlashPermit(cmd['requester'], self.revision,
                    '/' + cmd['raw_command'].lstrip()[1:], now, now + remaining)
                # 2026-11-07 T21：第二次发同一个命名落点时，ctl.variant 还是
                # qianniaocheng，但人和哥哥可能已经被游戏拉回神殿。把上一次
                # 传送记录的实际落点交给协调器，让它按真实坐标判断是否重做。
                target = None
                if (cmd.get('variant') is not None
                        and cmd.get('variant') == getattr(self.ctl, 'variant', None)):
                    raw = getattr(self.ctl, '_variant_position', None)
                    if (isinstance(raw, (tuple, list)) and len(raw) >= 3
                            and all(type(v) in (int, float) and math.isfinite(v) for v in raw[:3])):
                        target = tuple(float(v) for v in raw[:3])
                request = Request(cmd['request_id'], self.ctl.tracker.own_uuid, permit,
                    Destination(dest['id'], cmd['map_id'], target, arrival='scene_spawn',
                                variant=cmd.get('variant')), kind, now + remaining)
                self.command = dict(cmd)
                self.pending_transfer = None
                self.transfer_taken = False
                # 2026-11-07 T21：同一个落点的重试要保留最初的传送前快照
                # （例如第一次失败后本地还停在 85.49，但快照是神殿 137.95）；
                # 换图/换落点才丢弃旧快照。
                snap = self._same_map_snapshot
                if (snap is not None
                        and (cmd['map_id'] != getattr(self.ctl, 'map_id', None)
                             or isinstance(snap, dict)
                             and snap.get('map_id') not in (None, cmd['map_id'])
                             or (cmd.get('variant') or None)
                             != (getattr(self.ctl, 'variant', None) or None))):
                    self._same_map_snapshot = None
                self.resume_gate = self.resume_authorization = None
                self.resume_reported = False
                self.current = TravelCoordinator(request, now=now, port=self,
                                                  reuse_existing_attachment=True)
                # 同一个 phase/reason 也要在新请求里重新记一次状态，否则日志里
                # 第二次“没反应”会被上一次的 succeeded 去重掉。
                self.last_status = None
                self.seen.add(cmd['request_id'])
                # Suspend only the old follow intent. No home snap or movement
                # operation is used as a substitute for travelling together.
                self.ctl.follow = self.ctl.after_arrive = None
                self.ctl.route = []
                if self.ctl.motion is not None:
                    self.ctl.position = tuple(self.ctl.motion.pos(now))
                    self.ctl.motion = None
                self.ctl._set_override(self.ctl.base)
                self.log('requested', scene=cmd['scene'], map_id=cmd['map_id'],
                         carry_kind=kind.value, deadline_seconds=round(remaining, 1))
                try:
                    tr = self.ctl.tracker
                    now_m = self.clock()
                    with tr.lock:
                        own = tr._own_social_binding_locked()
                        peer = next((k for k, u in tr.slots.items() if u == cmd['requester']), None)
                        evs = [e for e in tr._attachment_events if e['slot'] == peer]
                        recent = [{'age': round(now_m - e['observed_mono'], 2),
                                   'leader_slot': (e.get('value') or {}).get('leader_slot'),
                                   'hold_state': (e.get('value') or {}).get('hold_state'),
                                   'carrier_slot': (e.get('value') or {}).get('carrier_slot'),
                                   'gen_ok': e['room_generation'] == tr.room_generation,
                                   'ver_ok': e['version'] == tr._slot_versions.get(peer)} for e in evs[-3:]]
                    self.log('hold_probe', own_slot=own, peer_slot=peer, samples=len(evs), recent=recent,
                             social=(self.ctl.social or {}).get('kind'))
                except Exception as exc:
                    self.log('hold_probe_failed', error=repr(exc)[:160])
                return True
            except (ValueError, TypeError, KeyError, AttributeError) as exc:
                self.log('rejected', reason=str(exc)[:100])
                return False

    def preempt(self, reason):
        with self.lock:
            self._same_peer_ws_intent = None
            previous = self.current
            if previous is not None and previous.phase not in TERMINAL:
                previous.preempt(command_revision=self.revision + 1)
                self.log('stopped', reason=reason)
            self.pending_transfer = None
            self.resume_authorization = None
            if self.resume_gate is not None:
                self.resume_gate.cancel(reason)
            self.rpc.cancel_pending()

    def cancel(self, cmd):
        with self.lock:
            c = self.current
            from travel_chat_v25 import parse
            intent = parse(cmd.get('raw_command'))
            if (c is None or cmd.get('requester') != c.binding.speaker_uuid
                    or cmd.get('target') != c.binding.speaker_uuid or cmd.get('src') != 'chat'
                    or intent is None or intent.operation != 'cancel'):
                return False
            self._same_peer_ws_intent = None
            c.cancel(now=self.clock(), speaker_uuid=cmd['requester'],
                     command_revision=self.revision + 1)
            self.pending_transfer = None
            self.resume_authorization = None
            if self.resume_gate is not None:
                self.resume_gate.cancel('explicit_owner_cancel')
            self.rpc.cancel_pending()
            return True

    def snapshot(self, now):
        ctl, tr, cur = self.ctl, self.ctl.tracker, self.current
        if cur is None:
            return Observation(self.revision, None, None, connected=False)
        who = cur.binding.speaker_uuid
        with ctl._active_lock:
            with tr.lock:
                engine = tr._engine
                own = tr._own_social_binding_locked()
                peer = next((s for s, u in tr.slots.items() if u == who), None)
                if (not tr.ready or engine is None or getattr(engine, '_closed', True)
                        or own is None or peer is None or own == peer):
                    return Observation(self.revision, None, None, connected=False)
                seen = tr.position_observed.get(peer)
                origin = tr.origins.get(peer)
                if not seen or origin is None:
                    return Observation(self.revision, None, None)
                own_version, peer_version = tr._slot_versions.get(own), tr._slot_versions.get(peer)
                if (getattr(engine,'_local_registration_binding',None) is not None
                        and peer not in engine._match.level_roster_ids):
                    return Observation(self.revision, None, None)
                scene = Scene(ctl.map_id, str(id(engine)), tr.epoch, tr.room_generation,
                              variant=getattr(ctl, 'variant', None))
                bot = Presence(tr.own_uuid, scene, own_version, now, tuple(ctl.position))
                player = Presence(who, scene, peer_version, seen[1], tuple(origin[:3]))
                samples = [e for e in tr._attachment_events
                    if e['room_generation'] == tr.room_generation and e['slot'] == peer
                    and e['uuid'] == who and e['version'] == peer_version
                    and 0 <= now - e['observed_mono'] <= 2.0]
                active = ctl._active_offer
                active_matches = bool(active and active.get('peer_uuid') == who
                    and active.get('own_uuid') == tr.own_uuid
                    and active.get('engine') is engine and active.get('epoch') == tr.epoch
                    and active.get('own_slot') == own and active.get('peer_slot') == peer
                    and active.get('own_version') == own_version and active.get('peer_version') == peer_version
                    and active.get('accepted_at') is not None)
                outcome = getattr(ctl, '_active_outcome', {}) or {}
                explicitly_ended = bool(active_matches and active.get('phase') in ('ended', 'failed')
                    and outcome.get('reason') != 'local_timeout')
                if active_matches:
                    # Real catalog identity distinguishes the two carrier
                    # variants even though both use the same carrier slot.
                    expected_name = {CarryKind.HANDHOLD:'hand_hold', CarryKind.PIGGYBACK:'carry',
                                     CarryKind.PRINCESS:'princesscarry'}[cur.request.kind]
                    # resolve_action returns the existing catalog entry; match
                    # its canonical identifier, never a raw relation flag alone.
                    active_matches = active['entry'].get('family') == expected_name
                attachment = offer = None
                if samples:
                    # Only the newest valid source evidence can authorize. Do
                    # not search backwards for an attached frame after release.
                    sample = max(samples, key=lambda e: e['sample_seq'])
                    value = sample.get('value')
                    if isinstance(value, dict):
                        kind = cur.request.kind
                        linked = (value.get('leader_slot') == own and value.get('hold_state') in (0, 1)
                                  if kind is CarryKind.HANDHOLD else value.get('carrier_slot') == own)
                        # Before transfer, carrying kind comes from validated
                        # invitation acceptance; the carrier field alone cannot
                        # distinguish piggyback from princess carry.
                        if explicitly_ended:
                            attachment = AttachmentEvidence(tr.own_uuid, who, kind, scene,
                                own_version, peer_version, now, 'detached')
                        # 牵手由小光领着（leader_slot==自己、hold_state==1 的新鲜帧）本身就是确定证据：
                        # 已经牵着时直接沿用，不再重新发邀请（背/抱的载体槽分不清种类，仍要求本次邀请）。
                        elif linked and (cur.transferred_at is not None or active_matches or kind is CarryKind.HANDHOLD
                                         or getattr(self, 'reused_attachment', False)):
                            attachment = AttachmentEvidence(tr.own_uuid, who, kind, scene,
                                own_version, peer_version, sample['observed_mono'], 'attached')
                        elif active_matches and active.get('phase') in ('ended', 'failed') and active.get('attachment_observed'):
                            outcome = getattr(ctl, '_active_outcome', {}) or {}
                            if outcome.get('reason') in ('observed_peer_release', 'observed_actor_detachment', 'cancelled'):
                                attachment = AttachmentEvidence(tr.own_uuid, who, kind, scene,
                                    own_version, peer_version, sample['observed_mono'], 'detached')
                if active_matches:
                    entry_kind = active['entry'].get('kind')
                    desired_kind = 'hold' if cur.request.kind is CarryKind.HANDHOLD else 'carry'
                    if entry_kind != desired_kind:
                        attachment = None
                    elif active.get('phase') == 'accepted' or (active.get('phase') == 'ended'
                            and (getattr(ctl, '_active_outcome', {}) or {}).get('reason') == 'local_timeout'):
                        offer = OfferEvidence(cur.request.request_id, who, tr.own_uuid, scene,
                            own_version, peer_version, now, 'accepted')
                self.last_snapshot = (engine, own, peer, scene, own_version, peer_version)
                return Observation(self.revision, bot, player, offer, attachment)

    def submit(self, effect):
        if not self.is_current(effect.binding):
            return Receipt(effect.effect_id, ReceiptState.REJECTED)
        if effect.kind is EffectKind.STOP:
            self.pending_transfer = None
            self.rpc.cancel_pending()
            return Receipt(effect.effect_id, ReceiptState.SUBMITTED)
        obs = self.snapshot(self.clock())
        if obs.bot is None or obs.peer is None or obs.bot.scene != effect.scene:
            return Receipt(effect.effect_id, ReceiptState.REJECTED)
        if effect.kind is EffectKind.INVITE:
            action = {CarryKind.HANDHOLD: '牵手', CarryKind.PIGGYBACK: '背背',
                      CarryKind.PRINCESS: '公主抱'}[effect.carry_kind]
            result = self.ctl._request_active_offer({'request_id':effect.binding.request_id,
                'requester':effect.binding.speaker_uuid, 'target':effect.binding.speaker_uuid, 'action':action,
                'travel_relead':True})
            ok = result and result.get('phase') in ('queued', 'waiting_accept', 'accepted')
            return Receipt(effect.effect_id, ReceiptState.SUBMITTED if ok else ReceiptState.REJECTED)
        if effect.kind is EffectKind.TRANSFER:
            if obs.attachment is None or obs.attachment.state != 'attached':
                return Receipt(effect.effect_id, ReceiptState.REJECTED)
            engine, own, peer, scene, own_ver, peer_ver = self.last_snapshot
            # 2026-11-07 T19/T21：同图换命名落点不需要网络 ChangeLevel/重连；
            # 记成本地传送，由 long_runner 换种子 + 原生 AvatarTeleportRpc。
            # T21：即使 ctl.variant 已经是目标变体（传送被游戏拒绝、实际位置
            # 还在原处），协调器决定重做时也要走本地传送，不能退回网络 RPC。
            same_map = effect.destination.map_id == getattr(self.ctl, 'map_id', None)
            self.pending_transfer = {'engine':engine, 'scene':scene, 'own':own, 'peer':peer,
                'own_version':own_ver, 'peer_version':peer_ver, 'effect':effect,
                'same_map':bool(same_map),
                'submitted_at':self.clock(),'attachment_at':obs.attachment.observed_at}
            if same_map:
                self.log('transfer_queued', scene=self.command['scene'],
                         map_id=self.command['map_id'], same_map=True, udp_ack_verified=False)
                return Receipt(effect.effect_id, ReceiptState.SUBMITTED)
            if not self.rpc.enqueue(engine=engine, epoch=scene.epoch, own_slot=own,
                    scene=self.command['scene'], kind=effect.carry_kind.value):
                self.pending_transfer = None
                return Receipt(effect.effect_id, ReceiptState.REJECTED)
            self.log('transfer_queued', scene=self.command['scene'],
                     map_id=self.command['map_id'], udp_ack_verified=False)
            return Receipt(effect.effect_id, ReceiptState.SUBMITTED)
        return Receipt(effect.effect_id, ReceiptState.UNAVAILABLE)

    def tick(self):
        with self.lock:
            if self.current is None or self.current.phase in TERMINAL:
                return
            now = self.clock()
            if now - self._last_tick < 0.1:
                return
            self._last_tick = now
            self._resume_tick(now)
            status = self.current.tick(now, self.snapshot(now))
            key = (status.phase, status.reason)
            if key != self.last_status:
                self.last_status = key
                self.log('state', phase=status.phase.value, reason=status.reason,
                         map_id=self.command['map_id'])

    def take_pending_move(self):
        with self.lock:
            p = self.pending_transfer
            if p is None or self.transfer_taken or not self.is_current(p['effect'].binding):
                return None
            now = self.clock()
            if now - p['submitted_at'] > 8:
                self.preempt('rpc_commit_timeout')
                return None
            if p.get('same_map'):
                # 同图：没有网络 RPC 可等；不拆连接，直接交给 long_runner 本地换种子。
                obs = self.snapshot(now)
                if self.ctl.tracker._engine is not p['engine']:
                    self.preempt('room_changed_before_transfer')
                    return None
                if obs.attachment is not None and obs.attachment.state == 'detached':
                    self.preempt('peer_released_before_transfer')
                    return None
                self.transfer_taken = True
                self.log('transfer_local_commit', scene=self.command['scene'],
                         map_id=self.command['map_id'], same_map=True, udp_ack_verified=False)
                return {'level':self.command['map_id'], 'ticket':None,
                    'join':self.current.binding.speaker_uuid, 'why':'escort_same_map_variant',
                    'variant':self.command.get('variant'), 'same_map_relocate':True,
                    'escort_leader':True,
                    'same_peer_handhold':self.current.request.kind is CarryKind.HANDHOLD,
                    '_binding':p['effect'].binding, '_engine':p['engine']}
            if not self.rpc.committed(engine=p['engine']):
                return None
            # A successful local commit is not receiver ACK. Retain reliable
            # retransmission for a brief bounded drain, then let the client and
            # normal friend-ticket join establish the destination room.
            if self.rpc.committed_age(now) < 0.8:
                return None
            obs = self.snapshot(now)
            # The peer can already have left after receiving the RPC. In that
            # case, do not require it to remain in the old room indefinitely.
            if self.ctl.tracker._engine is not p['engine']:
                self.preempt('room_changed_before_transfer')
                return None
            if obs.attachment is not None and obs.attachment.state == 'detached':
                self.preempt('peer_released_before_transfer')
                return None
            self.transfer_taken = True
            if self.current.request.kind is CarryKind.HANDHOLD:
                from handhold_resume_gate import HandholdResumeGate
                cur = self.current
                initial = cur.initial_scene
                self.resume_gate = HandholdResumeGate({
                    'request_id':cur.request.request_id,'revision':self.revision,
                    'bot_uuid':cur.binding.bot_uuid,'peer_uuid':cur.binding.speaker_uuid,
                    'source_scene':(initial.map_id,initial.room_id,initial.epoch,initial.generation),
                    'source_bot':(p['own'],p['own_version']),
                    'source_peer':(p['peer'],p['peer_version']),
                    'destination_map':self.command['map_id'],'kind':'handhold',
                    'accepted':cur.accepted,'was_attached':cur.was_attached,
                    'committed':True,'accepted_at':cur.accepted_at,
                    'attached_at':p['attachment_at'],'commit_at':self.rpc.commit_at,
                    'deadline':cur.binding.deadline})
            self.log('transfer_local_commit', scene=self.command['scene'],
                     map_id=self.command['map_id'], udp_ack_verified=False)
            return {'level':self.command['map_id'], 'ticket':None,
                'join':self.current.binding.speaker_uuid, 'why':'escort_leader',
                'variant':self.command.get('variant'),
                'escort_leader':True, 'same_peer_handhold':self.current.request.kind is CarryKind.HANDHOLD, '_binding':p['effect'].binding, '_engine':p['engine']}

    def capture_same_map_snapshot(self):
        """long_runner 在本地换种子/传送前调用：记住传送前的 ctl 状态。

        同图传送被游戏/对端拒绝时，协调器会通过
        ``same_map_relocation_failed`` 把这份状态恢复回来，避免本地以为
        已经在千鸟城而服务器/哥哥还在神殿。
        """
        with self.lock:
            if self._same_map_snapshot is None:
                snapshot = getattr(self.ctl, 'snapshot_relocation', None)
                self._same_map_snapshot = snapshot() if callable(snapshot) else None
            return self._same_map_snapshot

    def record_scene_target(self, position):
        """本地传送成功后登记实际落点，供协调器做到达判定。"""
        try:
            target = tuple(float(v) for v in position[:3])
        except (TypeError, ValueError, IndexError):
            return False
        if len(target) != 3 or not all(math.isfinite(v) for v in target):
            return False
        with self.lock:
            cur = self.current
            accepted = False
            if cur is not None and cur.phase not in TERMINAL:
                accepted = cur.set_scene_target(target)
            # 即使协调器刚好已用旧语义结束，也要把落点记到 ctl 上；下一次
            # 请求 accept 时会拿它做到达判定。
            try:
                self.ctl._variant_position = target
            except Exception:
                pass
            self.log('scene_target_recorded', at=[round(v, 3) for v in target],
                     variant=getattr(self.ctl, 'variant', None),
                     request_active=bool(cur is not None and cur.phase not in TERMINAL))
            return accepted or cur is None

    def drop_same_map_snapshot(self, reason):
        """真的换图/重连后丢弃旧图的同图传送快照，避免回滚写回别的图。"""
        with self.lock:
            had = self._same_map_snapshot is not None
            self._same_map_snapshot = None
            if had:
                self.log('same_map_snapshot_dropped', reason=reason,
                         map_id=getattr(self.ctl, 'map_id', None),
                         variant=getattr(self.ctl, 'variant', None))
            return had

    def same_map_relocation_failed(self, binding, reason):
        """协调器判定同图落点没有真的到达：恢复传送前的本地状态。"""
        with self.lock:
            cur = self.current
            if cur is None or cur.binding != binding:
                return False
            snapshot, self._same_map_snapshot = self._same_map_snapshot, None
            restore = getattr(self.ctl, 'restore_relocation', None)
            if snapshot is None or not callable(restore):
                return False
            # 2026-11-07 T22：快照必须属于“当前这张图 + 当前这次请求”。
            # 期间真的换过图（rejoin/move_switch/服务器复位）时，写回旧图
            # 种子/坐标会让小光出现在别的图的高度（遇境地底可疑路径）。
            snap_map = snapshot.get('map_id') if isinstance(snapshot, dict) else None
            current_map = getattr(self.ctl, 'map_id', None)
            command_map = (self.command or {}).get('map_id')
            if ((snap_map is not None and current_map is not None and snap_map != current_map)
                    or (snap_map is not None and command_map is not None
                        and snap_map != command_map)):
                self.log('same_map_snapshot_dropped', reason='map_changed',
                         snapshot_map_id=snap_map, map_id=current_map,
                         command_map_id=command_map,
                         variant=getattr(self.ctl, 'variant', None))
                # 本图状态已经变了：把 runner 的 args.variant 同步成当前值，
                # 防止下一次重连仍按旧落点 prepare。
                callback = getattr(self.ctl, '_on_relocation_reverted', None)
                if callable(callback):
                    try:
                        callback(getattr(self.ctl, 'variant', None))
                    except Exception:
                        pass
                return False
            if not restore(snapshot):
                return False
            try:
                self.ctl._variant_position = tuple(self.ctl.position)
            except Exception:
                pass
            # long_runner 的 args.variant 也要跟着回退；否则下一次重连会按
            # 千鸟城 prepare，重新把本地放到错误落点。
            callback = getattr(self.ctl, '_on_relocation_reverted', None)
            if callable(callback):
                try:
                    callback(getattr(self.ctl, 'variant', None))
                except Exception:
                    pass
            self.log('same_map_reverted', reason=reason,
                     variant=getattr(self.ctl, 'variant', None),
                     at=[round(v, 3) for v in getattr(self.ctl, 'position', (0.0, 0.0, 0.0))])
            return True

    def same_peer_move_current(self, move):
        with self.lock:
            if not (move.get('escort_leader') is True and self.transfer_taken
                    and self.is_current(move.get('_binding'))):return False
            # 同图换落点是本地传送，不要求网络 same-peer handhold 迁移。
            if move.get('same_map_relocate') is not True and move.get('same_peer_handhold') is not True:
                return False
            p,tr=self.pending_transfer,self.ctl.tracker
            if p is None:return False
            with tr.lock:
                if (tr._engine is not move.get('_engine') or not tr.ready
                        or tr._own_social_binding_locked()!=p['own']
                        or tr._slot_versions.get(p['own'])!=p['own_version']):return False
                peer_id=self.current.binding.speaker_uuid
                if p['peer'] in tr.slots and (tr.slots[p['peer']]!=peer_id
                        or tr._slot_versions.get(p['peer'])!=p['peer_version']):return False
                if any(u==peer_id and s!=p['peer'] for s,u in tr.slots.items()):return False
                return not self._release_in_current_room_locked(tr,p['own'],p['peer'])

    def _same_peer_ws_current_locked(self, state):
        cur = self.current
        return bool(state is not None and cur is state['current']
            and cur.binding is state['binding']
            and cur.request.request_id == state['request_id']
            and self.revision == state['revision'] == cur.binding.command_revision
            and cur.request.kind is CarryKind.HANDHOLD and self.transfer_taken
            and cur.accepted and cur.was_attached and cur.transferred_at is not None
            and cur.phase in (Phase.WAITING_SCENE, Phase.WAITING_ATTACHMENT,
                              Phase.NAVIGATING, Phase.PAUSED, Phase.SUCCEEDED)
            and self.command is not None and self.command['map_id'] == state['target_map']
            and not self.ctl.stop.is_set()
            and self.clock() < state['expires_at']
            and self.clock() < cur.binding.deadline
            and self.wall() < self.session_deadline)

    def begin_same_peer_ws_intent(self, move):
        """One bounded private token for an already committed leader request."""
        with self.lock:
            if not self.same_peer_move_current(move):return None
            source = move['_engine'];cur = self.current
            if (not self.rpc.committed(engine=source)
                    or source._map == move['level']):return None
            token = object()
            state = dict(token=token, current=cur, binding=cur.binding,
                request_id=cur.request.request_id, revision=self.revision,
                source_engine=source, source_map=source._map, source_epoch=source._epoch,
                target_map=move['level'], target_engine=None,
                expires_at=min(cur.binding.deadline,self.clock()+30.0))
            if not self._same_peer_ws_current_locked(state):return None
            self._same_peer_ws_intent=state
            return token

    def activate_same_peer_ws_intent(self, token, target_map):
        """Called after actual same-transport destination-engine installation."""
        with self.lock:
            state=self._same_peer_ws_intent;tr=self.ctl.tracker
            if (state is None or token is not state['token']
                    or not self._same_peer_ws_current_locked(state)
                    or target_map != state['target_map'] or self.ctl.map_id != target_map):return False
            with tr.lock:
                engine=tr._engine
                expected_epoch=1 if state['source_epoch']==255 else state['source_epoch']+1
                if (not tr.ready or engine is state['source_engine'] or engine._closed
                        or engine._map != target_map or engine._epoch != expected_epoch
                        or tr.own_uuid != self.current.binding.bot_uuid):return False
                state.update(target_engine=engine,target_epoch=engine._epoch,
                             target_room_generation=tr.room_generation)
                return True

    def ignore_source_ws_advisory(self, level, current_map):
        """Only source-map WS advice loses to this committed explicit intent.

        No UDP Move or ticket is consumed, rewritten or replayed by this method.
        A new command, release, room/engine change or 30-second expiry revokes it.
        """
        with self.lock:
            state=self._same_peer_ws_intent
            if state is not None and self.clock() >= state['expires_at']:
                self._same_peer_ws_intent=None
                return False
            if (not self._same_peer_ws_current_locked(state)
                    or type(level)is not int or level != state['source_map']
                    or current_map != state['target_map']
                    or self.ctl.map_id != state['target_map']
                    or state['target_engine'] is None):return False
            tr=self.ctl.tracker
            with tr.lock:
                engine=tr._engine
                if (not tr.ready or engine is not state['target_engine'] or engine._closed
                        or engine._map != state['target_map']
                        or engine._epoch != state['target_epoch']
                        or tr.room_generation != state['target_room_generation']
                        or tr.own_uuid != self.current.binding.bot_uuid):return False
                own=tr._own_social_binding_locked()
                peers=[slot for slot,who in tr.slots.items()
                       if who == self.current.binding.speaker_uuid]
                if own is None or len(peers)>1:return False
                if peers and self._release_in_current_room_locked(tr,own,peers[0]):return False
                return True

    def _release_in_current_room_locked(self, tr, own, peer):
        """Explicit native release beats the travel's previous acceptance."""
        after = self.current.transferred_at
        return any(e.get('event_kind') == 'release' and e.get('kind') == 'hold'
            and e.get('room_generation') == tr.room_generation and e.get('epoch') == tr.epoch
            and e.get('source_slot') in (own, peer)
            and {e.get('actor_slot'), e.get('target_slot')} == {own, peer}
            and e.get('observed_mono', -1) >= after
            for e in getattr(tr, 'social_events', ()))

    def _resume_tick(self, now):
        gate, cur, tr = self.resume_gate, self.current, self.ctl.tracker
        if gate is None or cur is None or not self.is_current(cur.binding):
            return
        obs = self.snapshot(now)
        if obs.bot is None or obs.peer is None:
            return
        engine, own, peer, scene, own_ver, peer_ver = self.last_snapshot
        if scene.map_id != cur.request.destination.map_id or scene == cur.initial_scene:
            return
        with tr.lock:
            samples = [s for s in tr._attachment_events if s['slot'] == peer
                and s['uuid'] == cur.binding.speaker_uuid and s['version'] == peer_ver
                and s['room_generation'] == scene.generation and 0 <= now-s['observed_mono'] <= 2]
            sample = max(samples,key=lambda s:s['sample_seq']) if samples else None
            release = self._release_in_current_room_locked(tr, own, peer)
        value = sample.get('value') if sample else None
        pair = {'scene':(scene.map_id,scene.room_id,scene.epoch,scene.generation),
            'bot':(own,own_ver,obs.bot.observed_at),'peer':(peer,peer_ver,obs.peer.observed_at),
            'bot_uuid':cur.binding.bot_uuid,'peer_uuid':cur.binding.speaker_uuid,
            'leader_slot':value.get('leader_slot') if isinstance(value,dict) else None,
            'hold_state':value.get('hold_state') if isinstance(value,dict) else None,
            'carrier_slot':value.get('carrier_slot') if isinstance(value,dict) else None,
            'attachment_at':sample['observed_mono'] if sample else None,
            'sample_seq':sample['sample_seq'] if sample else None}
        decision = gate.tick(now, pair, current_revision=self.revision,
                             owner_current=True, explicit_release=release)
        action = decision['action']
        if action == 'confirm_once':
            self.resume_authorization = {'engine':engine,'scene':scene,'own':own,'peer':peer,
                'own_version':own_ver,'peer_version':peer_ver,'binding':cur.binding,
                'created_at':now,'decision':decision['binding']}
            ok = self.rpc.enqueue_lead(engine=engine,epoch=scene.epoch,own_slot=own,peer_slot=peer)
            # The port logs queued -> committed/failed/cancelled separately.
            if not ok:
                self.resume_authorization = None
                gate.cancel('queue_rejected')
        elif action == 'restored':
            if not self.resume_reported:
                self.resume_reported = True
                self.log('handhold_restored', map_id=scene.map_id,
                         evidence='fresh_peer_actor_leader_slot', native_acceptance_verified=True)
        elif action in ('cancelled','expired'):
            self.resume_authorization = None
            self.rpc.cancel_pending()
            if action == 'cancelled':
                self.current.cancel(now=now,speaker_uuid=cur.binding.speaker_uuid,
                                    command_revision=self.revision+1)

    def lead_authorized_locked(self, engine, own, peer, own_ver, peer_ver):
        """Called active->tracker locked; never acquire runtime.lock here."""
        a, tr, cur = self.resume_authorization, self.ctl.tracker, self.current
        valid = bool(a is not None and cur is not None and self.is_current(a['binding'])
            and cur.request.kind is CarryKind.HANDHOLD and cur.accepted and cur.was_attached
            and self.transfer_taken and cur.transferred_at is not None
            and a['engine'] is engine and a['own'] == own and a['peer'] == peer
            and a['own_version'] == own_ver and a['peer_version'] == peer_ver
            and a['scene'].map_id == cur.request.destination.map_id
            and not self._release_in_current_room_locked(tr, own, peer))
        if not valid:
            return False
        now = self.clock()
        samples = [s for s in tr._attachment_events if s['slot'] == peer
            and s['uuid'] == cur.binding.speaker_uuid and s['version'] == peer_ver
            and s['room_generation'] == tr.room_generation and 0 <= now-s['observed_mono'] <= 2]
        value = max(samples,key=lambda s:s['sample_seq']).get('value') if samples else None
        if not isinstance(value,dict):
            return False
        leader, hold, carrier = (value.get(k) for k in ('leader_slot','hold_state','carrier_slot'))
        return (type(leader) is int and type(hold) is int and type(carrier) is int
                and carrier == 0 and (leader, hold) in ((0, 0), (own, 0), (own, 1)))

    def _attached_kind(self, requester):
        """发命令的人此刻是否已被小光牵着/背着/抱着（同房间、版本一致、2 秒内的新鲜帧）。
        牵手 → HANDHOLD；被小光背/抱（carrier_slot==自己）→ 按小光最近对他发出的邀请区分公主抱，否则算背背。"""
        tr = self.ctl.tracker
        now = self.clock()
        with tr.lock:
            own = tr._own_social_binding_locked()
            peer = next((k for k, u in tr.slots.items() if u == requester), None)
            if own is None or peer is None:
                return None
            ver = tr._slot_versions.get(peer)
            ev = [e for e in tr._attachment_events if e['slot'] == peer and e.get('uuid') == requester
                  and e['version'] == ver and e['room_generation'] == tr.room_generation
                  and 0 <= now - e['observed_mono'] <= 2]
        if not ev:
            return None
        v = max(ev, key=lambda e: e['sample_seq']).get('value') or {}
        if v.get('leader_slot') == own and v.get('hold_state') in (0, 1):
            return CarryKind.HANDHOLD
        if v.get('carrier_slot') == own:
            a = getattr(self.ctl, '_active_offer', None)
            fam = (a.get('entry') or {}).get('family') if a and a.get('peer_uuid') == requester else None
            return CarryKind.PRINCESS if fam == 'princesscarry' else CarryKind.PIGGYBACK
        return None

    def keep_local_relationship(self, active):
        # Called with the controller active lock held. No reverse runtime-lock
        # acquisition: use immutable request identity and a bounded fresh proof.
        cur = self.current
        if (cur is None or cur.phase in TERMINAL or not self.is_current(cur.binding)
                or active.get('peer_uuid') != cur.binding.speaker_uuid
                or active.get('accepted_profile') not in ('hold', 'carrier')):
            return False
        tr = self.ctl.tracker
        with tr.lock:
            own = tr._own_social_binding_locked()
            now = self.clock()
            samples = [s for s in tr._attachment_events if s['slot'] == active['peer_slot']
                and s['uuid'] == active['peer_uuid'] and s['version'] == active['peer_version']
                and s['room_generation'] == tr.room_generation and 0 <= now-s['observed_mono'] <= 2]
            if not samples or self.current is not cur:
                return False
            v = max(samples, key=lambda x:x['sample_seq']).get('value') or {}
            return (v.get('leader_slot') == own and v.get('hold_state') in (0, 1)
                    if cur.request.kind is CarryKind.HANDHOLD else v.get('carrier_slot') == own)


def install(ctl, *, has_capture, rpc_port, session_deadline=None):
    if getattr(ctl, '_escort_travel', None) is not None:
        return ctl._escort_travel
    if session_deadline is None:
        session_deadline = float(os.environ['SKY_AERIAL_TRIAL_STOP_AT_UNIX'])
    if not math.isfinite(session_deadline) or session_deadline <= time.time():
        raise ValueError('escort_session_deadline_missing')
    runtime = LeaderTravelRuntime(ctl, session_deadline=session_deadline,
        has_capture=has_capture, rpc_port=rpc_port)
    ctl._escort_travel = runtime
    old_apply, old_tick, old_end = ctl._apply, ctl._follow_tick, ctl._active_end
    def apply(self, cmd):
        if cmd.get('pose') == 'escort_travel':
            return runtime.accept(cmd)
        if cmd.get('pose') == 'escort_cancel':
            return runtime.cancel(cmd)
        if (cmd.get('src') == 'wander' and runtime.current is not None
                and runtime.current.phase not in TERMINAL):
            return
        if cmd.get('src') != 'wander':
            runtime.preempt('other_command')
        return old_apply(cmd)
    def tick(self):
        result = old_tick()
        try:
            runtime.tick()
        except Exception as exc:
            runtime.preempt('runtime_error')
            runtime.log('error', error=type(exc).__name__)
        return result
    def active_end(self, reason, **kw):
        active = self._active_offer
        if reason == 'local_timeout' and active is not None and runtime.keep_local_relationship(active):
            return
        return old_end(reason, **kw)
    ctl._apply = types.MethodType(apply, ctl)
    ctl._follow_tick = types.MethodType(tick, ctl)
    ctl._active_end = types.MethodType(active_end, ctl)
    runtime.log('installed', deadline_unix=session_deadline, resource_spending=False)
    return runtime
