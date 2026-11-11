"""Pure same-current-map Move preparation for native build 421629.

No network, I/O, target-map selection, account rebinding, or transport promotion.
The caller must freeze the active profile/endpoint/map at Move installation and
serialize local map changes until the pending transition ends. A Move ticket does
not contain a destination level. This module does not supply missing captures.
"""
from copy import deepcopy
from dataclasses import dataclass, field
import ipaddress
import re
import struct
from types import MappingProxyType
from typing import Mapping

from .handoff0167 import HandoffError, MoveGame, _bytes, _endpoint, _uint, encode_move
from .handshake0167_fields import peer_key as uid_peer_key
from .join0167_codec import decode as decode_join
from .join0167_profile import BUILD, LoadedProfile, NATIVE_SHA256, build_from_cache, uuid_raw, validate_record


@dataclass(frozen=True, repr=False)
class CurrentPendingJoin:
    fields: Mapping = field(repr=False)
    plaintext: bytes = field(repr=False)
    move: MoveGame = field(repr=False)
    pending_generation: int = 0
    pending_app_counter: int = 0
    first_registration_generation: int = 1
    first_registration_app_counter: int = 1

    def __repr__(self):
        return '<CurrentPendingJoin private-fresh-same-current-map>'


def prepare_current_pending_join(profile, current_join_fields, move, *, account_uuid,
        resource_revision, old_endpoint, current_endpoint, installed_level_id,
        newly_allocated_connection):
    """Same signature as prepare_home_pending_join; no Home constants.

`profile` is the frozen currently active capture, not a profile chosen from Move.
`old_endpoint` / `installed_level_id` are the values frozen at Move installation.
The coordinator must reject a changed current profile/resource before calling.
The literal active endpoint must still equal old_endpoint here. Those conditions
prove the supported same-map tail-A=1 case; crossing a map during Move is rejected.

Freshly allocated pending object: Join counter/generation/app-counter=0; first
registration generation/app-counter=1. This is not valid for reused native peers.
New signature/flags/others/opaque extras replace ALL earlier ticket values.
The full decoded Move (including non-Join split fields) is retained privately.
"""
    if newly_allocated_connection is not True:
        raise HandoffError('pending_requires_newly_allocated_connection')
    if type(profile) is not LoadedProfile or not isinstance(current_join_fields, Mapping):
        raise HandoffError('validated_current_profile_and_fields_required')
    record = deepcopy(profile.record)
    scene = record.get('scene')
    if not isinstance(scene, str) or not re.fullmatch(r'[A-Za-z0-9_]{1,31}', scene):
        raise HandoffError('current_profile_scene_invalid')
    if not isinstance(resource_revision, str) or not re.fullmatch(r'V[0-9]+', resource_revision):
        raise HandoffError('current_resource_revision_required')
    try:
        checked = validate_record(record, bytes(profile.cache), scene=scene,
            resource_revision=resource_revision, account_uuid=account_uuid, apk_build=BUILD)
    except (ValueError, TypeError, KeyError, struct.error):
        raise HandoffError('current_profile_validation_failed') from None
    if (checked.record['native_sha256'] != NATIVE_SHA256 or checked.binding != profile.binding
            or checked.blob != profile.blob):
        raise HandoffError('current_profile_binding_or_blob_mismatch')
    _uint(installed_level_id, 32, 'installed_level_width')
    old, active = _endpoint(old_endpoint), _endpoint(current_endpoint)
    if old != active or installed_level_id != checked.record['level_id']:
        raise HandoffError('current_map_or_endpoint_changed_since_Move_installation')
    encode_move(move)
    if _endpoint(move.endpoint) == old:
        raise HandoffError('same_endpoint_is_not_a_handoff')
    uid = uuid_raw(account_uuid)
    fields = deepcopy(dict(current_join_fields))
    if 'uid' in fields and fields['uid'] != uid:
        raise HandoffError('Join_uid_differs_from_current_account')
    _bytes(fields.get('session'), 16, 'current_session16_required')
    if fields.get('server_version') != 442 or fields.get('client_version') != 5127:
        raise HandoffError('current_wire_versions_required')
    key = uid_peer_key(uid)
    if 'peer_key' in fields and fields['peer_key'] != key:
        raise HandoffError('peer_key_differs_from_current_account')
    # decode_join uses tuple NetVersion; build_from_cache stores the captured list.
    # Normalize only after exact equality, never replace a conflicting old tuple.
    if 'netversion' in fields:
        if (not isinstance(fields['netversion'], (tuple, list)) or
                tuple(fields['netversion']) != tuple(checked.record['netversion_u16'])):
            raise HandoffError('explicit_netversion_conflicts_with_current_capture')
        fields['netversion'] = list(fields['netversion'])
    fields.update(uid=uid, peer_key=key, counter=0,
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
        raise HandoffError('current_pending_Join_field_validation_failed') from None
    complete['others'] = tuple(complete['others'])
    complete['netversion'] = tuple(complete['netversion'])
    return CurrentPendingJoin(MappingProxyType(complete), plaintext, move)
