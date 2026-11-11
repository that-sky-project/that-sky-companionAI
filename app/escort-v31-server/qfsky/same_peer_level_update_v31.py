"""Opt-in same-peer LevelUpdate; never encodes a fictitious server roster."""
from dataclasses import dataclass, replace
import struct

from . import register0167_codec
from .long0167.room_join import SessionMatch

_SEAL = object()


def next_counter(value):
    if type(value) is not int or not 1 <= value <= 255:
        raise ValueError('same_peer_counter')
    return 1 if value == 255 else value + 1


def update_level(match, slot, level):
    if (type(match) is not SessionMatch or not match.ready
            or type(slot) is not int or not 1 <= slot <= 255
            or type(level) is not int or not 0 < level <= 0xffffffff
            or slot not in {s for s, _ in match.roster}):
        raise ValueError('same_peer_unknown_identity')
    rows = tuple((s, level if s == slot else old) for s, old in match.roster)
    return replace(match, roster=rows,
        self_level_id=level if slot == match.self_net_player_id else match.self_level_id)


@dataclass(repr=False)
class LocalRegistrationBinding:
    """Real prior membership + locally ACKed own announcement, NOT new roster."""
    source: object
    transport: object
    owner: bytes
    slot: int
    source_map: int
    map_id: int
    epoch: int
    match: object
    roster_body: bytes
    acknowledged_at: float
    deadline: float
    seal: object
    consumed: bool = False

    def validate(self, *, account_uuid, map_id, epoch, roster_body):
        if (self.seal is not _SEAL or self.consumed or self.source._closed
                or self.transport.closed or account_uuid != self.owner
                or map_id != self.map_id or epoch != self.epoch
                or roster_body != self.roster_body
                or self.source._owner != self.owner
                or self.source._match.self_net_player_id != self.slot):
            raise ValueError('local_registration_binding_invalid')
        return self.match


def migrate(pump, target, *, guard, commit_guard, make_engine, before_install, after_install):
    """Called only on the connection/pump thread between complete frame groups.

    send_registration services this existing transport until reliable ACK. It
    creates no socket, no Join, no friend ticket. A partial failure is terminal;
    callers must not resume old-map Actors or retry via a different room.
    """
    slot = pump.active
    source, transport = slot.engine, slot.transport
    if (pump.pending is not None or slot.stage != 'active' or source.has_pending
            or source._closed or source._map == target.map_id or not guard()):
        raise ValueError('same_peer_update_not_available')
    if not hasattr(source, '_roster_body'):
        raise ValueError('same_peer_real_roster_missing')
    if target.profile.binding != pump._profile.binding:
        raise ValueError('same_peer_account_changed')
    original_owner, original_slot = source._owner, source._match.self_net_player_id
    original_map, original_epoch = source._map, source._epoch
    epoch, counter = next_counter(source._epoch), next_counter(slot.counter)
    p = target.profile.player_fields
    fields = dict(build=421629, netversion=target.profile.record['netversion_u16'],
                  level_flag=p['level_flag'], level_id=target.map_id,
                  matrix=p['matrix'], tail_flag=False)
    body = register0167_codec.encode(field00=epoch, **fields)
    fresh_body = register0167_codec.encode(field00=1, **fields)
    cutoff = min(pump._deadline, pump._clock() + 5.0)
    def live():
        if (pump._clock() >= cutoff or not guard() or pump.active is not slot
                or slot.engine is not source or source._closed
                or source._owner != original_owner or source._map != original_map
                or source._epoch != original_epoch
                or source._match.self_net_player_id != original_slot):
            raise ValueError('same_peer_update_scope_expired')
    slot.stage = 'same_peer_registration'
    slot.counter = counter  # a possibly sent sequence is never rolled back
    transport._same_peer_level_guard = live
    new_engine = None
    try:
        live()
        transport.send_registration(struct.pack('<IBB', pump._peer, 4, counter) + body)
        live()
        # No peer map is projected. Only self's acknowledged local announcement
        # changes its map, with the same actual room identity/UUID/slot.
        binding = LocalRegistrationBinding(source, transport, source._owner,
            source._match.self_net_player_id, source._map, target.map_id, epoch,
            update_level(source._match, source._match.self_net_player_id, target.map_id),
            source._roster_body, pump._clock(), pump._deadline, _SEAL)
        if commit_guard() is not True:raise ValueError('same_peer_command_changed')
        before_install(target)
        new_engine = make_engine(target, transport, source._roster_body,
                                 epoch, pump._clock(), binding)
        if (new_engine._owner != source._owner or new_engine._map != target.map_id
                or new_engine._match.self_net_player_id != binding.slot
                or new_engine._epoch != epoch):
            raise ValueError('same_peer_destination_engine_mismatch')
        if commit_guard() is not True:raise ValueError('same_peer_command_changed')
        slot.engine = new_engine
        # This is a NEW-transport template for a later legitimate server Move.
        # Its first registration must remain 1, not the current active epoch.
        pump._registration = fresh_body
        pump._map_id, pump._profile = target.map_id, target.profile
        pump._revision = target.resource_revision
        pump._fields['netversion'] = list(target.profile.record['netversion_u16'])
        for key in ('spawn', 'matrix', 'level_flag', 'level_id', 'blob',
                    'state_extra_u32_a', 'state_extra_u32_b', 'tail_u8'):
            pump._fields.pop(key, None)  # target validated cache supplies these
        after_install(target, fresh_body)
        source.close()  # tracker close hook ignores the now-current new engine
        slot.stage = 'active'
        pump._stats['same_peer_level_updates'] = pump._stats.get('same_peer_level_updates', 0) + 1
        pump._stats['matching_generation_observed'] = False
        return dict(map_id=target.map_id, same_transport=True, own_slot=binding.slot,
                    registration_epoch=epoch, local_registration_acked=True,
                    server_roster_received=False, native_acceptance_verified=False)
    except Exception:
        source.close()
        if new_engine is not None:
            new_engine.close()
        pump._failed = True
        raise
    finally:
        transport._same_peer_level_guard = None
