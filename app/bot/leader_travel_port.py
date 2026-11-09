"""Single-flight adapter to the existing reliable engine queue, no raw socket."""
import time

from leader_travel_rpc import LocalCommitWatch, build_current_slot_frame


class CurrentEnginePort:
    def __init__(self, controller, engine_module, frame_factory, *, clock=time.monotonic):
        self.ctl = controller
        self.engine_module = engine_module
        self.frame_factory = frame_factory
        self.clock = clock
        self.watch = None
        self.commit_at = None
        self.binding = None
        self.lead_retry = None
        self.lead_attempt = 0
        self.lead_queued_at = None

    def _current(self, *, require_peer=False):
        b = self.binding
        r = getattr(self.ctl, '_escort_travel', None)
        if b is None or r is None or not r.is_current(b['effect_binding']):
            return False
        tr = self.ctl.tracker
        with self.ctl._active_lock, tr.lock:
            own_ok = (tr.ready and tr._engine is b['engine'] and not b['engine']._closed
                and tr.epoch == b['epoch'] and tr.room_generation == b['generation']
                and tr._own_social_binding_locked() == b['own']
                and tr._slot_versions.get(b['own']) == b['own_version'])
            if not own_ok or not require_peer:
                return own_ok
            if (tr.slots.get(b['peer']) != b['peer_uuid']
                    or tr._slot_versions.get(b['peer']) != b['peer_version']):
                return False
            now=self.clock()
            seen=tr.position_observed.get(b['peer'])
            if seen is None or not 0 <= now-seen[1] <= 2:
                return False
            if b.get('purpose') == 'lead_confirmation':
                return r.lead_authorized_locked(b['engine'], b['own'], b['peer'],
                                               b['own_version'], b['peer_version'])
            active=self.ctl._active_offer
            outcome=getattr(self.ctl,'_active_outcome',{}) or {}
            # 牵手：发命令的人自己牵住了小光（小光领头），没有小光发起的邀请也算数，
            # 由下面“新鲜 leader_slot==自己 且 hold_state==1”的实测帧把关。背/抱仍要求本次邀请。
            reuse_hold = (b['family'] == 'hand_hold' or getattr(r, 'reused_attachment', False)) and not (active and active.get('accepted_at') is not None
                and active.get('peer_uuid') == b['peer_uuid'] and active.get('engine') is b['engine'])
            if not reuse_hold and (not active or active.get('peer_uuid') != b['peer_uuid']
                    or active.get('engine') is not b['engine']
                    or active.get('accepted_at') is None
                    or active.get('entry',{}).get('family') != b['family']
                    or (active.get('phase') != 'accepted' and not (active.get('phase') == 'ended'
                        and outcome.get('reason') == 'local_timeout'))):
                return False
            samples=[e for e in tr._attachment_events if e['slot']==b['peer']
                and e['uuid']==b['peer_uuid'] and e['version']==b['peer_version']
                and e['room_generation']==b['generation'] and 0<=now-e['observed_mono']<=2]
            if not samples:
                return False
            v=max(samples,key=lambda e:e['sample_seq']).get('value') or {}
            return (v.get('leader_slot')==b['own'] and v.get('hold_state') in (0,1)
                    if b['family']=='hand_hold' else v.get('carrier_slot')==b['own'])

    def enqueue(self, *, engine, epoch, own_slot, scene, kind):
        if self.watch is not None:
            return False
        r = self.ctl._escort_travel
        tr = self.ctl.tracker
        with self.ctl._active_lock:
            with tr.lock:
                if tr._engine is not engine or tr.epoch != epoch or tr._own_social_binding_locked() != own_slot:
                    return False
                frame = build_current_slot_frame(self.frame_factory, epoch=epoch, own_slot=own_slot,
                    scene=scene, attachment_kind='handhold' if kind == 'handhold' else 'carrier',
                    wire_layout='room_slot_u8')
                self.binding = {'effect_binding':r.current.binding,'engine':engine,'epoch':epoch,
                    'generation':tr.room_generation,'own':own_slot,
                    'own_version':tr._slot_versions.get(own_slot),
                    'peer':r.last_snapshot[2], 'peer_uuid':r.current.binding.speaker_uuid,
                    'peer_version':r.last_snapshot[5],
                    'family':{'handhold':'hand_hold','piggyback':'carry','princess_carry':'princesscarry'}[kind]}
        # _current follows the same active->tracker order; do not recursively
        # acquire the tracker's lock. Recheck immediately before publishing.
        if not self._current(require_peer=True):
            self.binding=None
            return False
        self.watch = LocalCommitWatch(engine, frame)
        self.engine_module.OUTBOX.append(frame)
        return True

    def _lead_event(self, phase, *, binding=None, reason=None, **extra):
        # Telemetry is non-controlling: a disk/log failure must not turn an
        # already successful engine.commit into an apparent transport failure.
        try:
            self._lead_event_unchecked(phase, binding=binding, reason=reason, **extra)
            return True
        except Exception:
            return False

    def _lead_event_unchecked(self, phase, *, binding=None, reason=None, **extra):
        b = binding or self.binding or {}
        if b.get('purpose') != 'lead_confirmation':
            return
        tr = self.ctl.tracker
        r = getattr(self.ctl, '_escort_travel', None)
        gate = getattr(r, 'resume_gate', None)
        old = getattr(gate, '_g', {})
        source = old.get('source_scene') or (None, None, None, None)
        value = None
        with tr.lock:
            samples = [s for s in tr._attachment_events if s.get('slot') == b.get('peer')
                and s.get('version') == b.get('peer_version') and s.get('uuid') == b.get('peer_uuid')
                and s.get('room_generation') == b.get('generation')
                and 0 <= self.clock()-s.get('observed_mono', -1) <= 2]
            if samples:
                value = max(samples, key=lambda s:s['sample_seq']).get('value')
        fields = dict(phase=phase, reason=reason, attempt=self.lead_attempt,
            source_map_id=source[0], map_id=old.get('destination_map'),
            source_epoch=source[2], source_generation=source[3],
            source_own_slot=(old.get('source_bot') or (None,))[0],
            source_peer_slot=(old.get('source_peer') or (None,))[0],
            epoch=b.get('epoch'), generation=b.get('generation'),
            own_slot=b.get('own'), peer_slot=b.get('peer'),
            leader_slot=value.get('leader_slot') if isinstance(value,dict) else None,
            hold_state=value.get('hold_state') if isinstance(value,dict) else None,
            carrier_slot=value.get('carrier_slot') if isinstance(value,dict) else None,
            native_acceptance_verified=False)
        fields.update(extra)
        self.ctl.log('escort_lead_confirmation_' + phase, **fields)

    def _lead_scope_current(self, b):
        # A transient freshness gap is recoverable, but identity/room/release is not.
        r, tr = getattr(self.ctl, '_escort_travel', None), self.ctl.tracker
        if r is None or not r.is_current(b['effect_binding']):
            return False
        with self.ctl._active_lock, tr.lock:
            if not (tr.ready and tr._engine is b['engine'] and not b['engine']._closed
                    and tr.epoch == b['epoch'] and tr.room_generation == b['generation']
                    and tr._own_social_binding_locked() == b['own']
                    and tr._slot_versions.get(b['own']) == b['own_version']
                    and tr.slots.get(b['peer']) == b['peer_uuid']
                    and tr._slot_versions.get(b['peer']) == b['peer_version']
                    and tr.own_uuid == b['effect_binding'].bot_uuid):
                return False
            if r._release_in_current_room_locked(tr, b['own'], b['peer']):
                return False
            samples=[s for s in tr._attachment_events if s.get('slot') == b['peer']
                and s.get('uuid') == b['peer_uuid'] and s.get('version') == b['peer_version']
                and s.get('room_generation') == b['generation']
                and 0 <= self.clock()-s.get('observed_mono',-1) <= 2]
            if samples:
                v=max(samples,key=lambda s:s['sample_seq']).get('value') or {}
                if (v.get('carrier_slot') != 0 or
                        (v.get('leader_slot'),v.get('hold_state')) not in ((0,0),(b['own'],0),(b['own'],1))):
                    return False
            return True

    def _definitely_unsent(self, w):
        return (w.state == 'pending'
            and sum(f is w.frame for f in self.engine_module.OUTBOX) == 1
            and not any(f is w.frame for f in getattr(getattr(w.engine, '_pending', None), 'frames', ())))

    def _try_lead_retry(self, engine):
        pending = self.lead_retry
        if pending is None:
            return
        b = pending['binding']
        if (engine is not b['engine'] or self.clock() >= pending['until']
                or not self._lead_scope_current(b)):
            self._lead_event('cancelled', binding=b, reason='unsent_retry_scope_or_window_expired',
                             definitely_unsent=True, retry_pending=False)
            self.lead_retry = None
            return
        self.binding = b
        if not self._current(require_peer=True):
            self.binding = None
            return
        frame = pending['frame']
        self.lead_retry = None
        self.lead_attempt += 1
        self.lead_queued_at = self.clock()
        self.watch = LocalCommitWatch(engine, frame)
        self.engine_module.OUTBOX.append(frame)
        self._lead_event('queued', reason='fresh_binding_unsent_retry',
                         definitely_unsent=True, local_commit=False, queued=True)

    def before_tick(self, engine):
        self._try_lead_retry(engine)
        if (self.watch is not None and (self.watch.engine is not engine or
                not self._current(require_peer=self.watch.state=='pending'))):
            self.cancel_pending(reason='binding_not_current', allow_unpublished_retry=True)

    def enqueue_lead(self, *, engine, epoch, own_slot, peer_slot):
        """One native leader confirmation for an already accepted trip only."""
        if self.watch is not None or self.lead_retry is not None:
            return False
        from handhold_lead_rpc_v26 import build_handhold_lead_frame
        r, tr = self.ctl._escort_travel, self.ctl.tracker
        with self.ctl._active_lock, tr.lock:
            if tr._engine is not engine or tr.epoch != epoch or tr._own_social_binding_locked() != own_slot:
                return False
            own_version, peer_version = tr._slot_versions.get(own_slot), tr._slot_versions.get(peer_slot)
            if not r.lead_authorized_locked(engine, own_slot, peer_slot, own_version, peer_version):
                return False
            frame = build_handhold_lead_frame(self.frame_factory, epoch=epoch,
                own_slot=own_slot, peer_slot=peer_slot, wire_layout='room_slot_u8')
            self.binding = {'effect_binding':r.current.binding, 'engine':engine, 'epoch':epoch,
                'generation':tr.room_generation, 'own':own_slot, 'own_version':own_version,
                'peer':peer_slot, 'peer_uuid':r.current.binding.speaker_uuid,
                'peer_version':peer_version, 'family':'hand_hold', 'purpose':'lead_confirmation'}
        if not self._current(require_peer=True):
            self.binding = None
            return False
        self.lead_attempt = 1
        self.lead_queued_at = self.clock()
        self.watch = LocalCommitWatch(engine, frame)
        self.engine_module.OUTBOX.append(frame)
        self._lead_event('queued', reason='initial_confirmation', queued=True, local_commit=False)
        return True

    def on_commit(self, engine, group, sent_count):
        w = self.watch
        if w is None:
            return
        before = w.state
        committed = w.on_commit_returned(engine, group, sent_count,
                                         current=self._current(require_peer=True))
        if committed:
            self.commit_at = self.clock()
            self._lead_event('committed', reason='exact_frame_after_engine_commit', local_commit=True,
                engine_commit_returned=True,
                sent_count=sent_count, waited_ms=round(1000*(self.commit_at-self.lead_queued_at),3)
                    if self.lead_queued_at is not None else None)
        elif before == 'pending' and w.state != before:
            self._lead_event('failed', reason=w.state, local_commit=False,
                             engine_commit_returned=True, retry_allowed=False)

    def on_commit_failed(self, engine, group):
        w = self.watch
        if w is not None:
            before = w.state
            w.on_commit_failed(engine, group)
            if before == 'pending' and w.state != before:
                self._lead_event('failed', reason='engine_commit_failed_delivery_unknown',
                                 local_commit=False, engine_commit_returned=False, retry_allowed=False)

    def committed(self, *, engine):
        return bool(self.watch is not None and self.watch.engine is engine
                    and self.watch.state == 'committed' and self._current())

    def committed_age(self, now):
        return -1 if self.commit_at is None else now - self.commit_at

    def cancel_pending(self, reason='owner_or_lifecycle_cancelled', *, allow_unpublished_retry=False):
        w, b = self.watch, self.binding
        pending = self.lead_retry
        self.lead_retry = None
        if pending is not None:
            self._lead_event('cancelled', binding=pending['binding'], reason=reason,
                             definitely_unsent=True, retry_pending=False)
        if w is not None:
            is_lead = b is not None and b.get('purpose') == 'lead_confirmation'
            unsent = is_lead and self._definitely_unsent(w)
            retry = bool(allow_unpublished_retry and unsent and self.lead_attempt < 2
                         and self._lead_scope_current(b))
            if retry:
                self.lead_retry = dict(binding=b, frame=w.frame,
                    until=min(self.clock()+2.0,b['effect_binding'].deadline))
            if is_lead and w.state == 'pending':
                self._lead_event('cancelled', binding=b, reason=reason,
                    definitely_unsent=bool(unsent), retry_pending=retry, local_commit=False)
            w.revoke()
            # Never touch unrelated social messages or revive an already-drained Frame.
            self.engine_module.OUTBOX[:] = [f for f in self.engine_module.OUTBOX if f is not w.frame]
        self.watch = None
        self.commit_at = None
        self.binding = None


def install_engine_port(ctl):
    from qfsky.map_long0167 import map_long_engine as module
    from qfsky.long0167.engine import Frame
    port = CurrentEnginePort(ctl, module, Frame)
    # One active controller per runner. Hook this exact class, not a second
    # package alias; existing room lifecycle wrappers remain underneath.
    cls = module.MapLongEngine
    if not hasattr(cls, '_escort_ports'):
        cls._escort_ports = []
        old_tick, old_commit = cls.tick, cls.commit
        def tick(engine, now):
            for p in tuple(cls._escort_ports):
                p.before_tick(engine)
            return old_tick(engine, now)
        def commit(engine, group, sent_count):
            try:
                result = old_commit(engine, group, sent_count)
            except BaseException:
                for p in tuple(cls._escort_ports):
                    p.on_commit_failed(engine, group)
                raise
            for p in tuple(cls._escort_ports):
                p.on_commit(engine, group, sent_count)
            return result
        cls.tick, cls.commit = tick, commit
    cls._escort_ports[:] = [port]
    return port
