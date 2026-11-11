"""Pure build421629 Home MoveGame protocol; no transport, file or account I/O.

Call only after authenticating the old/current peer and decrypting its packet.
This module cannot authenticate a UDP source, authorize a Scene, send a reply,
flush a reliable queue or promote a transport. All returned wire/state is private.
"""
from dataclasses import dataclass, field
import ipaddress
import struct
from types import MappingProxyType
from typing import Mapping

from .join0167_codec import decode as decode_join
from .join0167_profile import (
    LoadedProfile, NATIVE_SHA256, build_from_cache, uuid_raw, validate_record,
)
from .handshake0167_fields import advance_message_sequence, peer_key as uid_peer_key
from .receive0167 import ServerMessage

BUILD = 421629
HOME_SCENE = 'CandleSpace'
HOME_LEVEL = 3526133726
HOME_RESOURCE = 'V76318'
HOME_NETVERSION = (36851, 49307, 23536, 0, 0, 25536)
RESERVATION_SENTINEL = 0xffffffff


class HandoffError(ValueError):
    """Messages contain fixed error codes only, never private field values."""


def _uint(value, bits, code):
    if type(value) is not int or not 0 <= value < (1 << bits):
        raise HandoffError(code)
    return value


def _bytes(value, size, code):
    if type(value) is not bytes or len(value) != size:
        raise HandoffError(code)
    return value


def _endpoint(value):
    if type(value) is not tuple or len(value) != 2 or type(value[0]) is not str:
        raise HandoffError('endpoint_requires_literal_ipv4_tuple')
    try:
        host = ipaddress.IPv4Address(value[0])
    except ipaddress.AddressValueError:
        raise HandoffError('endpoint_requires_literal_ipv4_tuple') from None
    port = _uint(value[1], 16, 'endpoint_port_invalid')
    if port == 0:
        raise HandoffError('endpoint_port_invalid')
    return str(host), port


@dataclass(frozen=True, repr=False)
class MoveGame:
    signature: bytes
    ipv4: bytes
    port: int
    move_ts: int
    ticket_flag: int
    others: tuple
    extra_u32_a: int
    extra_u8: int
    extra_u32_b: int
    split_requested: bool
    split_wire_byte: int
    split_id: bytes

    def __repr__(self):
        return '<MoveGame private-current-ticket>'

    @property
    def endpoint(self):
        return str(ipaddress.IPv4Address(self.ipv4)), self.port


def encode_move(move):
    """Strict inverse preserving ignored high split padding and all opaque data."""
    if type(move) is not MoveGame:
        raise HandoffError('move_type_invalid')
    _bytes(move.signature, 32, 'move_signature_length')
    _bytes(move.ipv4, 4, 'move_ipv4_length')
    _uint(move.port, 16, 'move_port_width')
    _uint(move.move_ts, 64, 'move_timestamp_width')
    _uint(move.ticket_flag, 8, 'move_platform_width')
    if type(move.others) is not tuple or len(move.others) > 7:
        raise HandoffError('move_previous_players_count')
    for item in move.others:
        _bytes(item, 16, 'move_previous_uuid_length')
    _uint(move.extra_u32_a, 32, 'move_extra_a_width')
    _uint(move.extra_u8, 8, 'move_extra_byte_width')
    _uint(move.extra_u32_b, 32, 'move_extra_b_width')
    _uint(move.split_wire_byte, 8, 'move_split_byte_width')
    if type(move.split_requested) is not bool or move.split_requested != bool(move.split_wire_byte & 1):
        raise HandoffError('move_split_bit_conflict')
    _bytes(move.split_id, 16, 'move_split_uuid_length')
    return (move.signature + move.ipv4
            + struct.pack('<HQBI', move.port, move.move_ts, move.ticket_flag, len(move.others))
            + b''.join(move.others)
            + struct.pack('<IBIB', move.extra_u32_a, move.extra_u8, move.extra_u32_b, move.split_wire_byte)
            + move.split_id)


def decode_move(body, *, apk_build=BUILD):
    """Decode the decrypted outer0x09 body; no target map exists in this body."""
    if type(apk_build) is not int or apk_build != BUILD:
        raise HandoffError('unsupported_native_build')
    if type(body) is not bytes or not 77 <= len(body) <= 189:
        raise HandoffError('move_body_length')
    count = struct.unpack_from('<I', body, 47)[0]
    if count > 7 or len(body) != 77 + 16 * count:
        raise HandoffError('move_body_count_length')
    end = 51 + 16 * count
    split_byte = body[end + 9]
    value = MoveGame(
        body[:32], body[32:36], struct.unpack_from('<H', body, 36)[0],
        struct.unpack_from('<Q', body, 38)[0], body[46],
        tuple(body[51 + 16*i:67 + 16*i] for i in range(count)),
        struct.unpack_from('<I', body, end)[0], body[end + 4],
        struct.unpack_from('<I', body, end + 5)[0], bool(split_byte & 1),
        split_byte, body[end + 10:end + 26])
    if encode_move(value) != body:
        raise HandoffError('move_roundtrip_failed')
    return value


def initial_reservation(*, newly_initialized_session):
    """Native C+37c=-1 at0x1f78ed0/ee8 and0x1f86ac8/ad8, not HTTP guessing."""
    if newly_initialized_session is not True:
        raise HandoffError('reservation_initialization_requires_fresh_session')
    return RESERVATION_SENTINEL


@dataclass(frozen=True)
class MoveDecision:
    action: str
    reply_result: int | None = None


def decide_move(move, *, current_endpoint, migration_in_progress, current_reservation):
    encode_move(move)
    old = _endpoint(current_endpoint)
    _uint(current_reservation, 32, 'reservation_width')
    if type(migration_in_progress) is not bool:
        raise HandoffError('migration_busy_flag_invalid')
    # Native active handler checks busy before invoking the ticket installer.
    if migration_in_progress:
        return MoveDecision('busy', 2)
    if move.extra_u32_b == RESERVATION_SENTINEL and current_reservation != RESERVATION_SENTINEL:
        return MoveDecision('reservation-blocked')
    if move.endpoint == old:
        return MoveDecision('same-endpoint')
    _endpoint(move.endpoint)
    return MoveDecision('stage')


def reservation_transition(current_reservation, *, event, move=None,
                           local_outcome_known=False, old_success_sent=False):
    """Return the next C+37c model value; does not mutate session state.

    Caller commits install only after a validated stage genuinely begins.
    Known native terminal failure clears to -1, never restores a prior value.
    Unknown delivery or irreversible old-success has no automatic rollback.
    """
    _uint(current_reservation, 32, 'reservation_width')
    if type(local_outcome_known) is not bool or type(old_success_sent) is not bool:
        raise HandoffError('reservation_outcome_flags_invalid')
    if event in ('no-op', 'busy', 'commit'):
        return current_reservation
    if event == 'install':
        encode_move(move)
        if old_success_sent:
            raise HandoffError('reservation_install_after_success')
        if move.extra_u32_b == RESERVATION_SENTINEL and current_reservation != RESERVATION_SENTINEL:
            raise HandoffError('reservation_install_blocked')
        return move.extra_u32_b
    if event == 'terminal-failure':
        if not local_outcome_known or old_success_sent:
            raise HandoffError('reservation_recovery_required_no_rollback')
        # Native failure state6:0x1f72568..0x1f72614, after result04/state0.
        return RESERVATION_SENTINEL
    raise HandoffError('reservation_event_invalid')


@dataclass(frozen=True, repr=False)
class PendingJoin:
    fields: Mapping = field(repr=False)
    plaintext: bytes = field(repr=False)
    pending_generation: int = 0
    pending_app_counter: int = 0
    first_registration_generation: int = 1
    first_registration_app_counter: int = 1

    def __repr__(self):
        return '<PendingJoin private-fresh-Home-only>'


def prepare_home_pending_join(profile, current_join_fields, move, *, account_uuid,
        resource_revision, old_endpoint, current_endpoint, installed_level_id,
        newly_allocated_connection):
    """Build current plaintext; caller encrypts with the NEW Move signature.

    current_join_fields supplies session16 and unchanged explicit local fields.
    Native-cache fields are filled/checked by build_from_cache. New ticket fields
    replace old ones; no initial HTTP ticket defaults are applied. No send occurs.
    """
    if newly_allocated_connection is not True:
        raise HandoffError('pending_requires_newly_allocated_connection')
    if type(profile) is not LoadedProfile or not isinstance(current_join_fields, Mapping):
        raise HandoffError('validated_profile_and_fields_required')
    if resource_revision != HOME_RESOURCE:
        raise HandoffError('unobserved_Home_resource_revision')
    try:
        checked = validate_record(profile.record, profile.cache, scene=HOME_SCENE,
            resource_revision=resource_revision, account_uuid=account_uuid, apk_build=BUILD)
    except (ValueError, TypeError, KeyError, struct.error):
        raise HandoffError('Home_profile_validation_failed') from None
    if (checked.record['native_sha256'] != NATIVE_SHA256
            or checked.record['level_id'] != HOME_LEVEL
            or tuple(checked.record['netversion_u16']) != HOME_NETVERSION
            or checked.binding != profile.binding or checked.blob != profile.blob):
        raise HandoffError('Home_profile_binding_or_tuple_mismatch')
    _uint(installed_level_id, 32, 'installed_level_width')
    old = _endpoint(old_endpoint)
    active = _endpoint(current_endpoint)
    if old != active or installed_level_id != HOME_LEVEL:
        raise HandoffError('Home_changed_since_Move_installation')
    encode_move(move)
    destination = _endpoint(move.endpoint)
    if destination == old:
        raise HandoffError('same_endpoint_is_not_a_handoff')
    uid = uuid_raw(account_uuid)
    fields = dict(current_join_fields)
    if 'uid' in fields and fields['uid'] != uid:
        raise HandoffError('Join_uid_differs_from_current_account')
    _bytes(fields.get('session'), 16, 'current_session16_required')
    if fields.get('server_version') != 442 or fields.get('client_version') != 5127:
        raise HandoffError('current_wire_versions_required')
    expected_peer_key = uid_peer_key(uid)
    if 'peer_key' in fields and fields['peer_key'] != expected_peer_key:
        raise HandoffError('peer_key_differs_from_current_account')
    fields.update(uid=uid, peer_key=expected_peer_key, counter=0,
        signature=move.signature, ticket_ipv4=move.ipv4, ticket_port=move.port,
        move_ts=move.move_ts, ticket_flag=move.ticket_flag, others=move.others,
        ticket_extra_u32_a=move.extra_u32_a, ticket_extra_u8=move.extra_u8,
        ticket_extra_u32_b=move.extra_u32_b,
        tail_bool_a=1, tail_bool_b=0, previous_address_tag=1,
        previous_address=ipaddress.IPv4Address(old[0]).packed,
        previous_port=old[1], previous_level_id=installed_level_id)
    try:
        plaintext = build_from_cache(checked, fields, frame=True)
        complete = decode_join(plaintext, frame=True)
    except (ValueError, TypeError, KeyError, struct.error):
        raise HandoffError('pending_Join_field_validation_failed') from None
    complete['others'] = tuple(complete['others'])
    complete['netversion'] = tuple(complete['netversion'])
    return PendingJoin(MappingProxyType(complete), plaintext)


@dataclass(frozen=True, repr=False)
class MoveReply:
    plaintext: bytes = field(repr=False)
    next_counter: int
    result: int

    def __repr__(self):
        return '<MoveReply private-old-peer-only>'


def encode_move_reply(*, peer_key, previous_app_counter, result):
    """Old-peer plaintext outer0x0A: result0 success,2 busy,4 known failure.

    Caller must enforce phase/irreversibility, use OLD key and reliable channel0.
    """
    _uint(peer_key, 32, 'reply_peer_key_width')
    _uint(previous_app_counter, 8, 'reply_counter_width')
    if type(result) is not int or result not in (0, 2, 4):
        raise HandoffError('unsupported_Move_result')
    counter = advance_message_sequence(previous_app_counter, 10)
    return MoveReply(struct.pack('<IBBB', peer_key, 10, counter, result), counter, result)


@dataclass(frozen=True, repr=False)
class BatchDecision:
    action: str
    reason: str
    observed_types: tuple
    terminal_types: tuple
    enter_message: ServerMessage | None = field(default=None, repr=False)
    deferred_messages: tuple = field(default=(), repr=False)

    def __repr__(self):
        return '<PendingBatch decision=' + self.action + '>'


def preflight_pending_batch(messages):
    """Inspect the WHOLE decoded batch before any EnterGame side effect.

    Local conservative supported set: outer12,14,17. Outer1/2 are rejection;
    9/0x53 are distinct unsupported controls, not renamed Kick. Unknown types
    veto. This function does not validate the EnterGame account/membership.
    """
    if type(messages) not in (tuple, list) or len(messages) > 1024:
        raise HandoffError('pending_batch_invalid')
    if any(type(m) is not ServerMessage for m in messages):
        raise HandoffError('pending_batch_message_type')
    for m in messages:
        _uint(m.message_type, 8, 'pending_outer_type_width')
        if type(m.body) is not bytes:
            raise HandoffError('pending_message_body_type')
    types = tuple(m.message_type for m in messages)
    terminal = tuple(t for t in types if t in (1, 2))
    if terminal:
        return BatchDecision('veto', 'server-rejection', types, terminal)
    unsupported = tuple(t for t in types if t not in (12, 14, 17))
    if unsupported:
        return BatchDecision('veto', 'unsupported-pending-control', types, unsupported)
    enters = tuple(m for m in messages if m.message_type == 17)
    if len(enters) > 1:
        return BatchDecision('veto', 'multiple-EnterGame-messages', types, ())
    if not enters:
        return BatchDecision('wait', 'no-EnterGame', types, ())
    return BatchDecision('validate-enter', 'whole-batch-clear', types, (), enters[0],
                         tuple(m for m in messages if m.message_type != 17))


@dataclass(frozen=True, repr=False)
class RoomResetPlan:
    new_scope: object = field(repr=False)
    discard: tuple = (
        'actor_receive_baselines', 'scene_receive_baselines',
        'actor_writer_saved_tags_and_acks', 'scene_writer_saved_tags_and_acks',
        'feedback_history', 'role_owner_and_version', 'forced_scene_handover',
        'pending_scene_pair', 'heartbeat_controller', 'old_roster_and_slot',
    )
    scene_starts_unarmed: bool = True
    may_retain_bound_local_idle_content_and_clock: bool = True

    def __repr__(self):
        return '<RoomResetPlan fresh-room-state-required>'


def fresh_room_state(previous_scope, new_scope, *, old_history=None):
    """Pure reset contract, not an Engine constructor or native ring-reset claim.

    Caller instantiates fresh TX/RX/role objects after a verified promotion.
    Any attempt to pass old ACK/authority/history objects is rejected, even {}.
    """
    if previous_scope is None or new_scope is None or previous_scope is new_scope:
        raise HandoffError('new_room_scope_must_be_distinct')
    if old_history is not None:
        raise HandoffError('old_room_ACK_authority_history_cannot_be_adopted')
    return RoomResetPlan(new_scope)
