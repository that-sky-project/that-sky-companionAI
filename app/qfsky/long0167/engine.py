"""Pure Home continuation, extracted from the supplied 20261026 runtime.

No sockets, HTTP, WSS, file loading, gift actions or shared account state.
This is the idle connection subset, not the complete game client/SceneBarn.
The caller must authenticate incoming packets and bind live_check to the
actual account, cancellation flag and current transport. Build a FrameGroup,
queue it in order without receiving/dispatching between frames, then commit.
Any partial/unknown delivery ends this lifetime; no automatic replay exists.
"""
from dataclasses import dataclass, field
import hashlib
import math
import struct

from .scope import ConnectionScope
from .continuity import _export_idle, _restore_idle
from .room_join import parse_player_sync
from .level_authority import LevelRoleReceiver, LevelRecord
from .actual_controller_heartbeat import ActualControllerHeartbeat
from .scene_candidate import ColdSceneCandidate
from .scene_record import _check_record, PUBLIC_ELF_SHA256
from .codec.scene_wire import inspect_scene_wire
from .delta_sync import SnapshotReceiveState, accept_player_state_delta, _fnv8
from .actor_codec.actor_migration_0167 import migrate_actor_body_0167, OLD_SHA256, NEW_SHA256
from .actor_codec.legacy_actor.actor_wire import inspect_actor_wire, reencode_actor_wire
from .actor_pose_clock_extension import extend_actor_pose_clocks
from .actor_prepared_revision_tick import advance_prepared_actor_revisions
from qfsky.version_profile import profile_for_build

_HOME_PROFILE = profile_for_build(421629)
HOME_MAP = _HOME_PROFILE['home_level_id']
_INITIAL_ACTOR_SECONDS = .091
_ACTOR_INTERVAL_SECONDS = .120
_CONTROLLER_INTERVAL_SECONDS = .113


def _time(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError('long0167_invalid_time')
    return float(value)


def _migrate(actor):
    # Explicit Home-only hypotheses from the supplied runtime, not universal
    # native defaults. No timed records may be dropped or fabricated.
    result = migrate_actor_body_0167(actor, prefix_raw8=bytes(8),
        timed_extra_raw1=(), helper_extra_bool=False,
        verified_old_profile_sha256=OLD_SHA256,
        verified_new_profile_sha256=NEW_SHA256)
    if result.timed_records != 0:
        raise ValueError('long0167_timed_actor_uncovered')
    return result.wire_bytes()


def _validate_neutral(observed):
    # Read-only typed-token subset of the supplied runtime's animation reader.
    # No action builder or business module is imported. Match pre-alignment
    # boundaries exactly, including the verified history trailer shape.
    tokens, cursor = [], 0
    for token in observed._tokens:
        before = cursor
        if token.kind == 'raw':
            cursor = (cursor + 7) & ~7
            width = token.extent * 8
        elif token.kind == 'bits':
            width = token.extent
        else:
            raise ValueError('long0167_neutral_token_kind')
        tokens.append((before, token))
        cursor += width
    if cursor != observed.bit_length:
        raise ValueError('long0167_neutral_token_extent')
    def boundary(label):
        found = [position for name, position in observed._boundaries if name == label]
        if len(found) != 1:
            raise ValueError('long0167_neutral_boundary')
        return found[0]
    lo, hi = boundary('0x21447a0'), boundary('0x21447ac')
    values = {}
    for name, label, extent in (('mode', '0x2143278', 1), ('current', '0x21431e0', 8)):
        position = boundary(label)
        matches = [token for before, token in tokens if before == position]
        if (not lo <= position < hi or len(matches) != 1 or
                matches[0].kind != 'raw' or matches[0].extent != extent):
            raise ValueError('long0167_neutral_current_layout')
        values[name] = int.from_bytes(matches[0].value, 'little')
    lo, hi = boundary('0x2144670'), boundary('0x21446ac')
    history = [token for before, token in tokens if lo <= before < hi]
    expected = [('raw', 4), ('raw', 8), ('bits', 9), ('raw', 4),
                ('raw', 1), ('raw', 1), ('raw', 1), ('raw', 1), ('bits', 4)]
    if len(history) < 16 or [(t.kind, t.extent) for t in history[-9:]] != expected:
        raise ValueError('long0167_neutral_history_layout')
    historical = int.from_bytes(history[-8].value, 'little')
    if values['mode'] != 0 or 0x1A0B23EE716A1D1B in (values['current'], historical):
        raise ValueError('long0167_template_not_neutral')


def validate_templates(scene, actor):
    """Validate structurally current, Home-associated templates.

    Reads no file and opens no connection. The pinned neutral Actor seed is
    not an arbitrary user-produced Actor; only clock/revision changes made
    inside this package are accepted for subsequent updates.

    场景/角色的逐字节哈希不再绑死 0.16.7 的旧模板：0.16.8 的遇境重做后
    场景和 Actor 字节都会变化。这里按当前版本档案校验 resource tag、
    Scene/Actor 规范编码、HOME map 关联和迁移形状。
    """
    if type(scene) is not bytes or not 4 <= len(scene) <= 0x2000:
        raise ValueError('long0167_scene_template_mismatch')
    if type(actor) is not bytes or not 1 <= len(actor) <= 0x528:
        raise ValueError('long0167_actor_template_mismatch')
    _check_record(LevelRecord(0x63c0, 0, 0, 0, 5, 125, scene),
        verified_profile_sha256=PUBLIC_ELF_SHA256)
    scene_observation = inspect_scene_wire(scene, verified_profile_sha256=PUBLIC_ELF_SHA256,
                                          check_zero_padding=True)
    owner = scene_observation._tokens[scene_observation._home_owner_end_token_index-1]
    if owner.kind != 'raw' or owner.extent != 16 or any(owner.value):
        raise ValueError('long0167_historical_scene_owner_not_empty')
    observed = inspect_actor_wire(actor, verified_profile_sha256=OLD_SHA256,
        check_zero_padding=True, require_canonical_writer=True)
    if (not observed.matches_map_association(HOME_MAP) or
            dict(observed._counts).get('raw8_records') != 0 or
            reencode_actor_wire(observed) != actor):
        raise ValueError('long0167_actor_not_canonical_home')
    _validate_neutral(observed)
    _migrate(actor)  # Includes exact old-token preservation + current roundtrip.


@dataclass(frozen=True, repr=False)
class Frame:
    """Application BODY only; caller supplies outer header/shared counter."""
    outer_type: int
    payload: bytes = field(repr=False)
    reliable: bool = False
    channel: int = 0

    def __repr__(self):
        return '<long0167.Frame body-only>'


@dataclass(frozen=True, repr=False, eq=False)
class FrameGroup:
    frames: tuple[Frame, ...] = field(repr=False)

    def __repr__(self):
        return '<long0167.FrameGroup ordered RAM-only>'


class Engine:
    """One validated account/peer/registration generation, Home only.

    now is the actual monotonic time after successful initial 0x04 transport
    acknowledgment. generation is the registration generation, NOT own slot.
    The original loop sends first Actor at +91ms, then its first revision
    extension in the same pass; later extensions have a 120ms minimum gap.
    Controller/Scene scheduling is 113ms. Native cadence/content completeness
    is not asserted. A real account lifetime callback replaces the original
    WSS precondition; it is not represented as synthetic ws_alive=True.
    """
    def __init__(self, *, account_uuid, enter_game_body, generation, now,
                 scene, actor, live_check, local_idle=None):
        validate_templates(scene, actor)
        if type(generation) is not int or not 1 <= generation <= 255:
            raise ValueError('long0167_invalid_generation')
        match = parse_player_sync(enter_game_body, own_uuid=account_uuid,
                                  expected_level_id=HOME_MAP)
        if not match.ready or match.self_net_player_id not in match.level_roster_ids:
            raise ValueError('long0167_own_home_not_matched')
        self._scope = ConnectionScope(account_uuid, object(), object(), object(),
            generation, match.self_net_player_id, HOME_MAP, True, live_check)
        self._scope.validate()
        self._scene_digest = hashlib.sha256(scene).hexdigest()
        self._actor_digest = hashlib.sha256(actor).hexdigest()
        self._match = match
        self._role = LevelRoleReceiver(match,
            room_generation=self._scope.room_generation, peer_epoch=generation)
        self._heartbeat = ActualControllerHeartbeat(scope=self._scope)
        self._scene = ColdSceneCandidate(scope=self._scope, scene_bytes=scene,
            experimental_interval=_CONTROLLER_INTERVAL_SECONDS)
        self._player = SnapshotReceiveState()
        self._actor_seed = actor
        self._initial_actor = _migrate(actor)
        self._registered_at = self._last_now = _time(now)
        self._actor_captured_at = self._last_extension = self._last_heartbeat = None
        self._actor_origin = None
        self._idle_revision_base = 0
        self._local_idle_restored = local_idle is not None
        if local_idle is not None:
            self._actor_origin,self._idle_revision_base = _restore_idle(local_idle,
                owner=account_uuid,map_id=HOME_MAP,scene_digest=self._scene_digest,
                actor_digest=self._actor_digest,now=self._registered_at)
        self._actor_sequence = 0
        self._pending = None
        self._pending_data = None
        self._closed = False
        self._counts = {name: 0 for name in (
            'groups_committed', 'frames_committed', 'actor_frames', 'actor_extensions',
            'controller_frames', 'scene_pairs', 'role_events', 'invalid_role_events',
            'delta_acks', 'scene_confirmations', 'nettime_replies', 'roster_updates',
            'stale_generation_drops', 'unknown_sender_drops', 'ignored_messages')}

    def __repr__(self):
        return '<long0167.Engine Home idle RAM-only>'

    @property
    def has_pending(self):
        return self._pending is not None

    def _scope_provider(self):
        if self._closed:
            raise ValueError('long0167_closed')
        self._scope.validate()
        return self._scope

    def _check(self, now):
        self._scope_provider()
        if self._pending is not None:
            raise ValueError('long0167_delivery_pending')
        now = _time(now)
        if now < self._last_now:
            raise ValueError('long0167_clock_regressed')
        if now - self._registered_at > 1200 or (self._actor_origin is not None and now-self._actor_origin>1200):
            raise ValueError('long0167_lifetime_limit')
        self._last_now = now
        return now

    def _context(self):
        return dict(scope_provider=self._scope_provider,
                    current_roster=self._match.level_roster_ids)

    def _group(self, frames, **staged):
        if not frames:
            return None
        self._scope_provider()
        group = FrameGroup(tuple(frames))
        self._pending = group
        self._pending_data = staged
        return group

    def _actor_frame(self, wire, sequence):
        return Frame(14, bytes((3, self._scope.peer_epoch, 0, sequence, 0,
                                _fnv8(wire))) + wire)

    def _progress_actor(self, now, steps, origin):
        body=extend_actor_pose_clocks(self._actor_seed,elapsed_seconds=now-origin,
            map_id=HOME_MAP,verified_profile_sha256=OLD_SHA256)
        if steps:
            body=advance_prepared_actor_revisions(body,tick_steps=steps,
                map_id=HOME_MAP,verified_profile_sha256=OLD_SHA256)
        return _migrate(body)

    def export_local_idle(self):
        """Snapshot after final complete commit, before old lifetime is closed.

        Export immediately at handoff promotion, not before old-peer ticks.
        This contains no old room/cache/role/roster/ACK or saved TX sequence.
        New Engine registration +91ms emits the retained progress with a new
        saved sequence1, then extension(total committed revisions+1).
        """
        self._scope_provider()
        if self._pending is not None:
            raise ValueError('long0167_idle_export_delivery_pending')
        self._check(self._last_now)
        return _export_idle(self._scope.owner_uuid,HOME_MAP,self._scene_digest,self._actor_digest,
            self._actor_origin,self._idle_revision_base+self._counts['actor_extensions'],
            self._last_now)

    def tick(self, now):
        """Stage one ordered group; no catch-up bursts after delayed polling."""
        try:
            now = self._check(now)
            frames, staged = [], {}
            first = (self._actor_captured_at is None and
                     now - self._registered_at >= _INITIAL_ACTOR_SECONDS)
            sequence = self._actor_sequence
            if first:
                sequence = sequence % 255 + 1
                initial = self._initial_actor
                if self._actor_origin is not None:
                    initial = self._progress_actor(now,self._idle_revision_base,
                                                   self._actor_origin)
                frames.append(self._actor_frame(initial, sequence))
                staged['actor_captured_at'] = now
                staged['actor_first'] = 1
            context = self._context()
            if not self._scene.scene_branch_active():
                if (self._last_heartbeat is None or
                        now - self._last_heartbeat >= _CONTROLLER_INTERVAL_SECONDS):
                    frames.append(Frame(14, self._heartbeat.build_payload(**context)))
                    staged['heartbeat_at'] = now
            scene = self._scene.build_payload(now=now, **context)
            if scene is not None:
                reliable = self._scene.delivery_reliable(scene, **context)
                frames.append(Frame(14, self._scene.build_aux_payload(scene, **context), reliable))
                frames.append(Frame(14, scene, reliable))
                staged['scene_payload'] = scene
            captured = self._actor_origin if self._actor_origin is not None else (now if first else self._actor_captured_at)
            if ((first or self._actor_captured_at is not None) and captured is not None and (self._last_extension is None or
                    now - self._last_extension >= _ACTOR_INTERVAL_SECONDS)):
                extended = extend_actor_pose_clocks(self._actor_seed,
                    elapsed_seconds=now-captured, map_id=HOME_MAP,
                    verified_profile_sha256=OLD_SHA256)
                extended = advance_prepared_actor_revisions(extended,
                    tick_steps=self._idle_revision_base + self._counts['actor_extensions'] + 1,
                    map_id=HOME_MAP, verified_profile_sha256=OLD_SHA256)
                sequence = sequence % 255 + 1
                frames.append(self._actor_frame(_migrate(extended), sequence))
                staged['extension_at'] = now
            if sequence != self._actor_sequence:
                staged['actor_sequence'] = sequence
            return self._group(frames, **staged)
        except BaseException:
            self.close()
            raise

    def _ack(self):
        return Frame(14, bytes((14, self._scope.peer_epoch, 0,
            self._player.acknowledgment, self._role.level_snapshot_state.acknowledgment)))

    def handle(self, outer_type, payload, now):
        """Consume authenticated body; commit returned ACK group before receive."""
        try:
            self._check(now)
            if type(outer_type) is not int or not 0 <= outer_type <= 255:
                raise ValueError('long0167_invalid_outer_type')
            if type(payload) is not bytes or len(payload) > 0x10000:
                raise ValueError('long0167_invalid_payload')
            if outer_type == 13:
                if len(payload) != 18:
                    raise ValueError('long0167_invalid_nettime')
                return self._group([Frame(12, payload[:2])], nettime_replies=1)
            if outer_type == 17:
                match = parse_player_sync(payload, own_uuid=self._scope.owner_uuid,
                                          expected_level_id=HOME_MAP)
                if (not match.ready or match.self_net_player_id != self._scope.own_slot or
                        any(not 1 <= ident <= 255 for ident in match.level_roster_ids)):
                    raise ValueError('long0167_own_membership_changed')
                self._match = match
                # Same peer, own slot and map: preserve actual decoder baseline.
                self._role._roster = match.level_roster_ids
                self._counts['roster_updates'] += 1
                return None
            if outer_type in (1, 2, 9, 0x53):
                # Disconnect, move/transfer and regeneration require a new
                # externally authenticated lifecycle, never a Home fallback.
                raise ValueError('long0167_lifecycle_transition')
            if outer_type != 14:
                self._counts['ignored_messages'] += 1
                return None
            if len(payload) < 3:
                raise ValueError('long0167_invalid_game_header')
            kind, generation, sender = payload[:3]
            if generation != self._scope.peer_epoch:
                self._counts['stale_generation_drops'] += 1
                return None
            if sender and sender not in self._match.level_roster_ids:
                self._counts['unknown_sender_drops'] += 1
                return None
            context = self._context()
            if kind == 17:
                if len(payload) != 9:
                    raise ValueError('long0167_invalid_scene_confirmation')
                if struct.unpack_from('<I', payload, 3)[0] != HOME_MAP:
                    return None
                return self._group([Frame(14, bytes((17, generation, 0)) + payload[3:], True)],
                                   scene_confirmations=1)
            if kind == 3:
                accept_player_state_delta(payload, receive_state=self._player)
                return self._group([self._ack()], delta_acks=1)
            if kind == 14:
                self._scene.acknowledge_game14(payload, **context)
                return None
            if kind in (8, 9, 11):
                try:
                    observation = self._role.observe(payload,
                        room_generation=self._scope.room_generation, peer_epoch=generation)
                except ValueError:
                    # Invalid/unknown role input must not arm output; strict
                    # receiver forks before parsing and does not advance ACK.
                    self._counts['invalid_role_events'] += 1
                    return None
                if observation is None:
                    return None
                self._heartbeat.accept(observation, **context)
                self._scene.accept(observation, **context)
                self._counts['role_events'] += 1
                if kind == 11:
                    return self._group([self._ack()], delta_acks=1)
                if kind == 9:
                    body = bytes((17, generation, 0)) + struct.pack('<IH', HOME_MAP, observation.version)
                    return self._group([Frame(14, body, True)], scene_confirmations=1)
                return None
            self._counts['ignored_messages'] += 1
            return None
        except BaseException:
            self.close()
            raise

    def commit(self, group, sent_count):
        """Commit only after ALL frames entered transport in this exact order.

        Local queued success is not application ACK or server acceptance.
        A failure, cancellation or unknown/partial count permanently closes.
        """
        try:
            self._scope_provider()
            if (group is not self._pending or group is None or type(sent_count) is not int
                    or sent_count != len(group.frames)):
                raise ValueError('long0167_incomplete_or_unknown_delivery')
            staged = self._pending_data
            scene = staged.get('scene_payload')
            if scene is not None:
                self._scene.finish_delivery(scene, queued=True, **self._context())
                self._counts['scene_pairs'] += 1
            if 'actor_captured_at' in staged:
                self._actor_captured_at = staged['actor_captured_at']
                if self._actor_origin is None:self._actor_origin=self._actor_captured_at
                self._counts['actor_frames'] += 1
            if 'extension_at' in staged:
                self._last_extension = staged['extension_at']
                self._counts['actor_extensions'] += 1
                self._counts['actor_frames'] += 1
            if 'actor_sequence' in staged:
                self._actor_sequence = staged['actor_sequence']
            if 'heartbeat_at' in staged:
                self._last_heartbeat = staged['heartbeat_at']
                self._counts['controller_frames'] += 1
            for name in ('delta_acks', 'scene_confirmations', 'nettime_replies'):
                self._counts[name] += staged.get(name, 0)
            self._counts['groups_committed'] += 1
            self._counts['frames_committed'] += sent_count
            self._pending = self._pending_data = None
        except BaseException:
            self.close()
            raise

    def close(self):
        self._closed = True
        if hasattr(self, '_scene'):
            self._scene.stop()
        self._pending = self._pending_data = None
        self._actor_seed = self._initial_actor = b''

    def metadata(self):
        result = dict(self._counts)
        scene_meta = self._scene.metadata()
        result.update(closed=self._closed, delivery_pending=self.has_pending,
            home_only=True, local_lifecycle_binding=True, synthetic_ws_state=False,
            actor_initial_sent=self._actor_captured_at is not None,
            actor_clock_revision_only=True, native_actor_producer=False,
            local_idle_restored=self._local_idle_restored,
            actor_total_revision_steps=self._idle_revision_base+self._counts['actor_extensions'],
            native_new_actor_defaults_verified=False, native_scene_producer=False,
            supplied_home_baseline=scene_meta['historical_seed'],
            actual_record_received=scene_meta['actual_cache_retained'],
            server_application_verified=False)
        # Keep only fixed integer counters and booleans: never arbitrary source
        # text, IDs, bytes, UUID, roster or identity repr in public diagnostics.
        for key, value in scene_meta.items():
            if type(value) in (bool, int):
                result['scene_' + key] = value
        for key, value in self._heartbeat.metadata().items():
            if type(value) in (bool, int):
                result['controller_' + key] = value
        return result
