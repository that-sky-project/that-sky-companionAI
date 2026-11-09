"""One-room initial static seed followed by actual-cache continuation.

The caller passes observations returned by its authenticated current
LevelRoleReceiver. A typed observation is not a cryptographic attestation.
Cold native header defaults plus the historical public Home body are an
explicit unverified experiment, not A's loaded Scene or an authority grant.
After an authenticated current Game11, retain its exact checked record.
This does NOT implement native SceneBarn application, evolving Scene content,
full native SceneBarn lifecycle, or a cryptographic/native cache attestation.
A current own-Revoke can request one reliable Game23/Game10 pair from an
already actually received checked cache, never from the historical seed.
This is a narrow native handover subset, not a proven fix for join timeout.
Owner0/Revoke yields permission without discarding valid current-room cache.
The outer Net14 sequence, signature and transport belong to the caller.
Only authenticated current-peer Game14 is accepted for the persistent Scene
writer. This helper does not authenticate the transport by itself. Only fixed metadata may be logged. No I/O or fixture runs here.
"""
import hashlib
import math
import struct

from .scope import ConnectionScope as RecordedUplinkScope
from .level_authority import LevelRecord, LevelRoleObservation
from .scene_record import _check_record
from .scene_header import serialize_cached_level_header
from .scene_record import PUBLIC_ELF_SHA256
from .delta_send import SnapshotSendState

_HOME_MAP = 3526133726
_HOME_TAG = 36851
_SCENE_SHA = '8a608f701e561f062fcfb43e5122927a22db84cd0e5fd04c364ab3162f0de0b0'


def _time(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError('cold_scene_time_invalid')
    return float(value)


class ColdSceneCandidate:
    def __init__(self, *, scope, scene_bytes, experimental_interval=0.113):
        if type(scope) is not RecordedUplinkScope:
            raise ValueError('cold_scene_scope_invalid')
        scope.validate()
        if scope.map_id != _HOME_MAP:
            raise ValueError('cold_scene_requires_home')
        if (type(scene_bytes) is not bytes or len(scene_bytes) != 4292
                or hashlib.sha256(scene_bytes).hexdigest() != _SCENE_SHA):
            raise ValueError('cold_scene_historical_body_changed')
        interval = _time(experimental_interval)
        if interval < 0.1 or interval > 1.0:
            raise ValueError('cold_scene_interval_invalid')
        # Native cold constructor values, not current loaded producer values.
        candidate = LevelRecord(0x63c0, 0, 0, 0, 5, 125, scene_bytes)
        self._record = _check_record(candidate, expected_resource_tag=_HOME_TAG,
                                    verified_profile_sha256=PUBLIC_ELF_SHA256)
        self._scope = scope
        self._interval = interval
        self._armed = False
        self._version = 0
        # Existing cached-scene alias uses period10; Scene-specific native
        # constructor evidence is retained separately, not inferred cadence.
        self._writer = SnapshotSendState(keyframe_period=10)
        self._pending_writer = None
        self._force_pending = self._pending_force = False
        self._forced_full_armed = self._forced_full_sent = 0
        self._scene_ack_received = self._scene_ack_matched = 0
        self._generation = 0
        self._pending = None
        self._pending_time = self._pending_generation = None
        self._last_queued = None
        self._actual_inputs = self._queued = 0
        self._native_record_seen = False
        self._record_from_actual = False
        self._cache_usable = True  # initial explicitly unverified static seed
        self._actual_cache_adoptions = self._actual_cache_rejections = 0
        self._closed = False
        self._delivery_unknown = False

    def __repr__(self):
        return '<ColdSceneCandidate RAM-only unverified experiment>'

    def _check(self, scope_provider, current_roster):
        try:
            return self._checked_scope(scope_provider, current_roster)
        except Exception:
            # Failed authentication/activity/own roster cannot leave an armed
            # candidate waiting to revive without a new valid room lifetime.
            self._armed = False
            self._closed = True
            raise

    def _checked_scope(self, scope_provider, current_roster):
        current = scope_provider()
        if type(current) is not RecordedUplinkScope:
            raise ValueError('cold_scene_scope_invalid')
        current.validate()
        before = self._scope
        if (current.owner_uuid != before.owner_uuid
                or current.peer_identity is not before.peer_identity
                or current.room_generation is not before.room_generation
                or current.auth_generation is not before.auth_generation
                or current.peer_epoch != before.peer_epoch
                or current.own_slot != before.own_slot
                or current.map_id != before.map_id):
            self._armed = False
            self._closed = True
            raise ValueError('cold_scene_scope_changed')
        if type(current_roster) not in (tuple, list, set, frozenset):
            raise ValueError('cold_scene_roster_invalid')
        if (not 1 <= len(current_roster) <= 8
                or any(type(i) is not int or not 1 <= i <= 255 for i in current_roster)
                or len(set(current_roster)) != len(current_roster)
                or current.own_slot not in current_roster):
            raise ValueError('cold_scene_roster_invalid')
        return current, frozenset(current_roster)

    def scene_branch_active(self):
        """Read-only scheduling eligibility, not a native producer attestation.

        True remains true between cadence ticks: a None build result caused
        by throttling is NOT permission to send a competing Game12. False
        for non-own/yield, unavailable validated cache, or permanent closure.
        Caller still checks actual live scope on every build/send as before.
        """
        return (not self._closed and self._cache_usable
                and (self._armed or self._pending_force
                     or (self._force_pending and self._record_from_actual)))

    def accept(self, observation, *, scope_provider, current_roster):
        """Only the caller's authenticated current LevelRoleReceiver output.

        Game11 copies its exact record even when include_record=False.
        Included Game8 needs a previously retained actual Game11/cache record
        and exact slot4 compatibility; missing/malformed/mismatched content
        yields Scene output without killing the account or UDP transport.
        A rejected content transition cannot rearm old bytes via bare Elect;
        a new valid Game11 or compatible included Elect is required.
        """
        current, roster = self._check(scope_provider, current_roster)
        self._armed = False
        if self._pending is not None:
            self._delivery_unknown = True
            self._closed = True
        self._generation += 1
        if (type(observation) is not LevelRoleObservation
                or observation.kind not in ('LevelData_Elect', 'LevelData_Revoke',
                                            'LevelData_Snapshot')
                or observation.level_id != current.map_id
                or type(observation.include_record) is not bool
                or type(observation.sender_network_id) is not int
                or (observation.sender_network_id and observation.sender_network_id not in roster)
                or type(observation.owner_network_id) is not int
                or (observation.owner_network_id and observation.owner_network_id not in roster)
                or type(observation.version) is not int
                or not 0 <= observation.version <= 65535):
            raise ValueError('cold_scene_observation_not_current')
        self._actual_inputs += 1
        if self._closed:
            # No role event can reopen a changed scope/uncertain-delivery run.
            return self.metadata()
        self._version = observation.version
        if observation.kind == 'LevelData_Revoke':
            # Native1ed8f14..28/1ed8fbc..fc4: valid ownID == revokedID sets
            # force even when the previous stored controller owner differs.
            # A non-own Revoke does not clear an already pending force flag.
            # This research subset additionally requires actual checked cache.
            if (observation.owner_network_id == current.own_slot
                    and self._record_from_actual and self._cache_usable):
                if not self._force_pending:
                    self._forced_full_armed += 1
                self._force_pending = True
            return self.metadata()
        adopts_record = (observation.kind == 'LevelData_Snapshot'
                         or observation.include_record)
        if adopts_record:
            try:
                checked = _check_record(observation.record,
                    expected_resource_tag=_HOME_TAG,
                    verified_profile_sha256=PUBLIC_ELF_SHA256)
            except ValueError:
                self._cache_usable = False
                self._actual_cache_rejections += 1
                return self.metadata()
            if (observation.kind == 'LevelData_Elect'
                    and (not self._record_from_actual
                         or checked.slot_4 != self._record.slot_4)):
                self._cache_usable = False
                self._actual_cache_rejections += 1
                return self.metadata()
            # Exact immutable LevelRecord from the actual current receiver:
            # all six header slots and body are preserved, not re-created.
            self._record = checked
            self._record_from_actual = self._native_record_seen = True
            self._cache_usable = True
            self._actual_cache_adoptions += 1
        if self._cache_usable and observation.owner_network_id == current.own_slot:
            self._armed = True
        return self.metadata()

    def build_payload(self, *, now, scope_provider, current_roster):
        current, _ = self._check(scope_provider, current_roster)
        now = _time(now)
        if self._pending is not None:
            raise ValueError('cold_scene_delivery_pending')
        if not self.scene_branch_active():
            return None
        if self._last_queued is not None:
            if now < self._last_queued:
                raise ValueError('cold_scene_clock_regressed')
            if now - self._last_queued < self._interval:
                return None
        # This exact serializer API exists in the v22 embedded alias (v3).
        header = serialize_cached_level_header(
            self._record, own_map=current.map_id, received_version=self._version)
        forced = self._force_pending and self._record_from_actual
        # Native1ef357c..35bc: a FRESH temporary period10 writer, not raw
        # baseline0 and not a reset/replacement of the persistent writer.
        staged = SnapshotSendState(10) if forced else self._writer.fork()
        staged.update(header)
        frame = staged.build_frame()
        if forced:
            # Native1ef362c clears2024 before zero-length check and enqueue.
            # Partial delivery still permanently closes via existing guards.
            self._force_pending = False
        if frame is None:
            # A native raw-mode no-frame update still advances its tick. Do
            # not consume app sequence or create a companion Aux on this path.
            if not forced:
                self._writer = staged
            self._last_queued = now
            return None
        payload = bytes((10 if forced else 11, current.peer_epoch, 0)) + frame
        self._pending_force = forced
        self._pending_writer = staged
        self._pending = payload
        self._pending_time = now
        self._pending_generation = self._generation
        return payload

    def validate_before_send(self, payload, *, scope_provider, current_roster):
        self._check(scope_provider, current_roster)
        if (payload is not self._pending or self._pending is None
                or self._closed or (not self._armed and not self._pending_force)
                or self._pending_generation != self._generation):
            raise ValueError('cold_scene_candidate_no_longer_sendable')

    def delivery_reliable(self, payload, *, scope_provider, current_roster):
        """Use this same flag for BOTH pending Aux23 and Scene10/11 sends."""
        self.validate_before_send(payload, scope_provider=scope_provider,
                                  current_roster=current_roster)
        return self._pending_force

    def build_aux_payload(self, payload, *, scope_provider, current_roster):
        """Companion for this pending Scene, never a standalone grant.

        Queue Game23 then Game10/11 in the same serialized loop, using two
        values from the existing shared app sequence allocator. No await or
        incoming-role dispatch between these calls and finish_delivery.
        Both use delivery_reliable(): forced23+10 reliable/channel0, normal
        23+11 request unreliable/channel0; ENet may fragment large packets
        reliably. Zeros model the native cached-producer early-return subset,
        not measured current gameplay statistics or a verified cadence.
        """
        self.validate_before_send(payload, scope_provider=scope_provider,
                                  current_roster=current_roster)
        return bytes((23, self._scope.peer_epoch, 0)) + struct.pack(
            '<III', self._scope.map_id, 0, 0)

    def finish_delivery(self, payload, *, queued, scope_provider, current_roster):
        if self._delivery_unknown:
            raise ValueError('cold_scene_transport_recovery_required')
        if payload is not self._pending or self._pending is None:
            raise ValueError('cold_scene_delivery_not_current')
        if queued is not True:
            # False/None/unknown never becomes a retry of this saved sequence.
            self._delivery_unknown = True
            self._armed = False
            self._closed = True
            raise ValueError('cold_scene_transport_recovery_required')
        try:
            self._check(scope_provider, current_roster)
        except Exception:
            self._delivery_unknown = True
            self._armed = False
            self._closed = True
            raise
        # Queue success is only local transport evidence, not server acceptance.
        self.validate_before_send(payload, scope_provider=scope_provider,
                                  current_roster=current_roster)
        if self._pending_force:
            self._forced_full_sent += 1
        else:
            self._writer = self._pending_writer
        self._pending_force = False
        self._pending_writer = None
        self._queued += 1
        self._last_queued = self._pending_time
        self._pending = self._pending_time = self._pending_generation = None
        return self.metadata()

    def acknowledge_game14(self, payload, *, scope_provider, current_roster):
        """Current transport context required; only the second ACK is Scene.

        The caller must first authenticate/filter its current peer. Header
        checks below are additional binding checks, not source attestation.
        Valid late ACKs may prune the retained writer while the role yields.
        Unknown/transient ACKs never advance a baseline or reopen the role.
        """
        current, roster = self._check(scope_provider, current_roster)
        if self._pending is not None:
            # An ACK between stage and pair commit would invalidate the
            # staged writer's ancestry. Serialized caller loops forbid it.
            self._delivery_unknown = True
            self._armed = False
            self._closed = True
            raise ValueError('cold_scene_ack_delivery_pending')
        if self._closed:
            return False
        if (type(payload) is not bytes or len(payload) != 5
                or payload[0] != 14 or payload[1] != current.peer_epoch
                or payload[2] not in (0, *roster)):
            return False
        self._scene_ack_received += 1
        matched = self._writer.acknowledge(payload[4])
        if matched:
            self._scene_ack_matched += 1
        return matched

    def stop(self):
        self._armed = False
        self._closed = True
        self._generation += 1

    def metadata(self):
        return {'cold_scene_candidate': True,
                'historical_seed': not self._record_from_actual,
                'nativeproducer': False,
                'same_source_complete_record': self._record_from_actual,
                'experimental_historical_body': not self._record_from_actual,
                'cold_constructor_header_used': not self._record_from_actual,
                'canonical_resource_checked': True,
                'actual_input_seen': self._actual_inputs > 0,
                'actual_own_elect_armed': self._armed,
                'actual_native_record_seen': self._native_record_seen,
                'actual_cache_retained': self._record_from_actual,
                'actual_cache_usable': self._cache_usable,
                'actual_cache_adoptions': self._actual_cache_adoptions,
                'actual_cache_rejections': self._actual_cache_rejections,
                'actual_owner_allows_output': self.scene_branch_active(),
                'native_cache_installation_verified': False,
                'native_force_full_implemented': False,
                'own_revoke_handover_subset': True,
                'forced_full_armed': self._forced_full_armed,
                'forced_full_sent': self._forced_full_sent,
                'force_pending': self._force_pending,
                'candidate_closed': self._closed,
                'delivery_pending': self._pending is not None,
                'transport_recovery_required': self._delivery_unknown,
                'locally_queued_frames': self._queued,
                'scene_ack_received': self._scene_ack_received,
                'scene_ack_matched': self._scene_ack_matched,
                'scene_writer_raw_mode': self._writer.metadata()['raw_mode'],
                'native_cadence_verified': False,
                'scene_loaded_verified': False,
                'cache_receipt_created': False,
                'local_authority_self_granted': False,
                'server_application_verified': False}
