"""Pure HandHoldLeadRpc codec, recovered from the local CN 0.16.7 ELF.

Native producer 0x2513d00 writes current local PlayerId first, target second;
serializer 0x253aa18 serializes exactly those two PlayerIds. Receiver 0x2537138
checks authenticated sender against field one, then applies its own social and
saved-request gates before updating field-two avatar's leader attachment.

This is a leader confirmation, NOT SocialAcceptOfferRpc and not a synthetic
user acceptance. This module neither sends nor claims the client's gates pass.
The runtime must scope use to the existing accepted, uncancelled handhold trip,
fresh same-room identities and a new-room incarnation/epoch; it must inspect
the user's later Actor for actual attachment. Carry variants are not covered.

Only the existing room_slot_u8 wire layout is supported here; the native
writer's internal byte vs mapped-byte mode is not inferred from wire bytes.
"""
import struct

HANDHOLD_LEAD_KEY = 0x14AAF64A


def _byte(value, name, *, nonzero=False):
    if type(value) is not int or not (1 if nonzero else 0) <= value <= 255:
        raise ValueError('invalid_' + name)
    return value


def build_handhold_lead_game(*, epoch, own_slot, peer_slot, wire_layout):
    if wire_layout != 'room_slot_u8':
        raise ValueError('explicit_room_slot_u8_required')
    _byte(epoch, 'epoch')
    _byte(own_slot, 'own_slot', nonzero=True)
    _byte(peer_slot, 'peer_slot', nonzero=True)
    if own_slot == peer_slot:
        raise ValueError('leader_and_follower_must_differ')
    body = bytes((own_slot, peer_slot))
    return bytes((2, epoch, 0, own_slot)) + struct.pack('<IBH', HANDHOLD_LEAD_KEY, 0, len(body)) + body


def build_handhold_lead_frame(frame_factory, **kwargs):
    if not callable(frame_factory):
        raise TypeError('frame_factory_required')
    return frame_factory(14, build_handhold_lead_game(**kwargs), True, channel=0)
