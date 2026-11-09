"""Offline normal-path Scene wire inspection with explicit build selection.

0.16.7 is a static-only candidate. Its anonymous one-bit addition follows
the raw16 UUID field; no business meaning or online compatibility is claimed.
The original build and old SHA gate retain their existing layout.

Main 1d029d0, record producer 1d03ed0, and all twenty nested helpers were
audited against public ELF SHA c5a7c9756f97b76562e7e28d86cbe4a58763c1295cd0c01f316f9e49d76fc9f8.
1d03ed0 selects contextual entity mode 2; wire entity values are raw byte
dictionary indices, NOT native entity handles. This module never invents a
dictionary, initializes map objects, or turns constructor defaults into a
live Scene. Quantized fields retain wire codes; no lossy float decode/reencode.

It inspects caller-supplied bytes in RAM and can reencode their typed wire
tokens on a fresh zero-padded buffer. It implements no account lookup, native
memory access, filesystem I/O, room/authority eligibility, sockets, or sends.
Successful parsing proves layout only, not valid gameplay or stable online.
"""
from dataclasses import dataclass, field
import math
import struct

from .bit_stream import BitReader, BitWriter

PUBLIC_ELF_SHA256 = 'c5a7c9756f97b76562e7e28d86cbe4a58763c1295cd0c01f316f9e49d76fc9f8'
PUBLIC_ELF_SHA256_0167 = '0b81f1da54d9c6f28c08746b2ec19519d8eb78c4f1b3c5e4e420ff726dc25321'
_PROFILE_HASHES = frozenset((PUBLIC_ELF_SHA256, PUBLIC_ELF_SHA256_0167))
_UNSET_ANONYMOUS_BOOL = object()
MAX_SCENE_WIRE_BYTES = 0x2000


@dataclass(frozen=True, repr=False)
class _Token:
    kind: str
    extent: int
    value: int | bool | bytes = field(repr=False)


@dataclass(frozen=True, repr=False)
class SceneWireObservation:
    """Opaque private values; public metadata excludes entities and UUIDs."""
    _tokens: tuple[_Token, ...] = field(repr=False)
    _wire_bytes: bytes = field(repr=False)
    _counts: tuple[tuple[str, int], ...] = field(repr=False)
    bit_length: int
    _boundaries: tuple[tuple[str, int], ...] = field(repr=False)

    _profile_sha256: str = field(default=PUBLIC_ELF_SHA256, repr=False)
    _home_owner_end_token_index: int = field(default=-1, repr=False)
    _anonymous_bool_0167: bool | None = field(default=None, repr=False)

    def matches_resource_tag(self, expected_resource_tag):
        """Compare body Scene+0x10 with the loaded world's exact U16 tag.

        Native consumer1d04120..130 compares a zero-extended world U16 at
        NetWorld+0x99b78 against the complete decoded U32. It does not mask
        away high bits, compare record.slot_4, or use the room's map ID.
        Matching a resource tag alone cannot prove final loaded object state.
        This method neither loads a map nor validates an own room assignment.
        """
        if type(expected_resource_tag) is not int or not 0 <= expected_resource_tag <= 0xffff:
            raise ValueError('scene_invalid_expected_resource_tag')
        if (type(self._wire_bytes) is not bytes or len(self._wire_bytes) < 4
                or not self._tokens or self._tokens[0].kind != 'raw'
                or self._tokens[0].extent != 4
                or self._tokens[0].value != self._wire_bytes[:4]):
            raise ValueError('scene_resource_header_unverified')
        return struct.unpack_from('<I', self._wire_bytes)[0] == expected_resource_tag

    def metadata(self):
        return {'scene_layout_checked': True,
                'wire_entity_context': 2,
                'entity_dictionary_resolved': False,
                'scene_bytes': len(self._wire_bytes),
                'scene_bits': self.bit_length,
                'variable_group_counts': dict(self._counts),
                'scene_initialized_by_this_module': False,
                'scene_authority_verified': False,
                'stable_online_verified': False,
                'game_requests_sent': False}


class _Walk:
    def __init__(self, body, check_zero_padding):
        self.reader = BitReader(body, max_bytes=MAX_SCENE_WIRE_BYTES)
        self.padding = check_zero_padding
        self.tokens = []
        self.counts = []
        self.boundaries = []

    def boundary(self, label):
        self.boundaries.append((label, self.reader.bit_position))

    def raw(self, n):
        value = self.reader.read_raw(n, check_zero_padding=self.padding)
        self.tokens.append(_Token('raw', n, value))
        return value

    def raw_float(self, bits):
        value = self.raw(bits//8)
        if math.isnan(struct.unpack('<f' if bits == 32 else '<d', value)[0]):
            raise ValueError('scene_native_invalid_raw_float')

    def boolean(self):
        value = self.reader.read_bool()
        self.tokens.append(_Token('bits', 1, int(value)))
        return value

    def bits(self, width):
        value = self.reader.read_bits(width)
        self.tokens.append(_Token('bits', width, value))
        return value

    def bounded(self, maximum, *, storage_bits=8):
        value = self.reader.read_uint(0, maximum, storage_bits=storage_bits)
        self.tokens.append(_Token('bits', maximum.bit_length(), value))
        return value

    def count(self, label, maximum, *, storage_bits=8):
        value = self.bounded(maximum, storage_bits=storage_bits)
        self.counts.append((label, value))
        return value

    def entity(self):
        # 1ed2254 mode2 aligns then writes one dictionary-index byte.
        self.raw(1)


def inspect_scene_wire(body, *, verified_profile_sha256, check_zero_padding=False):
    """Parse the normal native Scene layout, retain values in RAM only.

    The supplied SHA is the caller's assertion of the current original build,
    not an attestation. Optional padding checks are stricter local validation;
    original reads normally ignore internal padding. Native raw float helpers
    reject NaN, but allow infinity. Unresolved dictionary indices stay opaque.
    Fixed arrays are always present, even when a map has no corresponding
    active objects. Suppression guards are not serialized and do not remove
    fields; the star fragment's two boolean gates DO control entity presence.
    """
    if (type(verified_profile_sha256) is not str
            or verified_profile_sha256 not in _PROFILE_HASHES):
        raise ValueError('scene_profile_unverified')
    if type(check_zero_padding) is not bool:
        raise ValueError('scene_invalid_padding_option')
    if type(body) is not bytes or not 1 <= len(body) <= MAX_SCENE_WIRE_BYTES:
        raise ValueError('scene_invalid_wire_length')
    w = _Walk(body, check_zero_padding)
    w.boundary('header')
    w.raw(4)  # main+10: folded public resource ID, not room map hash.
    w.boundary('stream_links')
    for _ in range(w.count('stream_links', 7)):
        w.raw(2)
    w.boundary('move_rails')
    for _ in range(w.count('move_rails', 24)):
        w.boolean()
        w.raw_float(64)
    w.boundary('floaty')
    for _ in range(w.count('floaty', 16)):
        w.raw_float(64)
    w.boundary('rotations')
    for _ in range(w.count('rotations', 32)):
        w.bits(16)  # quantized FP32 [-pi,pi], center0.
    w.boundary('tags')
    for _ in range(w.count('tags', 8)):
        w.raw(1)
    w.boolean()  # main+2be
    w.boolean()  # main+2bf
    w.boundary('entities')
    for _ in range(w.count('entities', 63, storage_bits=32)):
        w.entity()
    w.boundary('king')
    w.entity()  # main+3c0
    w.entity()  # main+3c4
    w.raw_float(32)
    w.raw(1)
    for _ in range(3):
        w.raw_float(32)
    w.raw(4)
    w.raw_float(32)
    for _ in range(8):
        w.entity()  # main+3e8..404
    w.boundary('turrets')
    for _ in range(8):
        w.raw_float(32)
        w.entity()
        w.raw(1)
        w.raw(1)
    w.boundary('candle_gates')
    for _ in range(16):
        w.boolean()
        for _ in range(6):
            w.entity()
            w.boolean()
            w.raw(2)
            w.boolean()
            w.boolean()
    w.boundary('rain')
    w.bounded(4)
    w.bits(16)  # [0,300],center0; native +Inf constructor encodes maxcode.
    w.boundary('darkstones')
    for _ in range(384+512+256):
        w.bits(8)
        w.boolean()
    w.boundary('darkvines')
    for _ in range(32):
        w.bits(8)
        w.bits(8)
        w.boolean()
    w.boundary('microdarkness')
    for _ in range(256):
        w.entity()
        w.bounded(2, storage_bits=32)
        w.bits(8)
    w.boundary('dark_creatures')
    for _ in range(12):
        w.entity()
        w.raw(4)
        w.bounded(4, storage_bits=32)
        for _ in range(4):
            w.boolean()
        w.raw_float(32)
        w.raw(1)
    w.boundary('star_fragments')
    for _ in range(160):
        w.bounded(5)
        w.boolean()  # +10
        w.boolean()  # +11
        if w.boolean():  # +15 -> entity+4
            w.entity()
        if w.boolean():  # +14 -> entity+8
            w.entity()
        w.raw(1)  # +c
        w.raw(1)  # +d
        w.raw(1)  # +0; native tail call.
    w.boundary('ancestor_engines')
    for _ in range(16):
        for _ in range(3):
            w.boolean()
            w.bits(8)
            w.bits(7)
    w.boundary('pickup_emitters')
    for _ in range(23):
        w.bounded(3)
    w.boundary('wax_chunks')
    for _ in range(32):
        w.raw(4)
        for _ in range(3):
            w.bits(10)
        for _ in range(3):
            w.bits(8)
        w.boolean()
    w.boundary('quests')
    for _ in range(w.count('quests', 16)):
        w.boolean()
        w.bounded(5, storage_bits=32)
        w.bits(6)
        w.bits(6)
        w.raw(8)
    w.boundary('quest_slots')
    for _ in range(16):
        w.boolean()
        w.bounded(4, storage_bits=32)
        w.raw_float(64)
    w.boundary('placeables')
    for _ in range(16):
        w.boolean()
    w.boundary('home_owner_uuid')
    w.raw(16)
    home_owner_end_token_index = len(w.tokens)
    anonymous_bool_0167 = None
    if verified_profile_sha256 == PUBLIC_ELF_SHA256_0167:
        w.boundary('anonymous_bool_0167')
        anonymous_bool_0167 = w.boolean()
    w.boundary('switches')
    for _ in range(w.count('switches', 16)):
        w.boolean()
        w.boolean()
        w.bounded(8)
    w.boundary('mpvar_booleans')
    for _ in range(w.count('mpvar_booleans', 128)):
        w.boolean()
    w.boundary('mpvar_floats')
    for _ in range(w.count('mpvar_floats', 64)):
        w.raw_float(64)
    w.boundary('mpvar_integers')
    for group in range(8):
        maximum = struct.unpack('<H', w.raw(2))[0]
        count = w.count('mpvar_integer_group_'+str(group), 64)
        if count and maximum == 0:
            # 3190cd0/3190940 reject min>=max, even when value is zero.
            raise ValueError('scene_native_invalid_integer_bounds')
        for _ in range(count):
            w.bounded(maximum, storage_bits=16)
    w.boundary('trailer')
    w.raw(4)  # main+63b4
    w.boolean()  # main+63b8; native tail call.
    bit_length = w.reader.bit_position
    # Occupied-byte equality matches the original record's checked length.
    # Padding values are otherwise ignored by native readers.
    if w.reader.remaining_bits >= 8:
        raise ValueError('scene_unexpected_wire_trailer')
    if check_zero_padding:
        w.reader.require_end(allow_zero_padding=True)
    return SceneWireObservation(tuple(w.tokens), body, tuple(w.counts),
                                bit_length, tuple(w.boundaries),
                                verified_profile_sha256,
                                home_owner_end_token_index, anonymous_bool_0167)


def reencode_scene_wire(observation, *, verified_profile_sha256=None,
                        anonymous_bool_0167=_UNSET_ANONYMOUS_BOOL):
    """Reencode opaque tokens with an explicitly selected or decoded layout.

    No bytes-based schema guessing occurs. Omitting the profile retains only
    the profile explicitly supplied to inspect_scene_wire. New-layout encoding
    requires either an explicitly supplied bool or a bool retained by a new-
    layout decode. In particular, converting an old observation to the new
    profile never defaults the unknown field to false. This is an offline
    typed codec, not a fresh Scene builder or proof of online compatibility.
    """
    if type(observation) is not SceneWireObservation:
        raise ValueError('scene_observation_required')
    source_profile = observation._profile_sha256
    target_profile = (source_profile if verified_profile_sha256 is None
                      else verified_profile_sha256)
    if (type(source_profile) is not str or source_profile not in _PROFILE_HASHES
            or type(target_profile) is not str or target_profile not in _PROFILE_HASHES):
        raise ValueError('scene_profile_unverified')
    tokens = list(observation._tokens)
    insertion = observation._home_owner_end_token_index
    source_new = source_profile == PUBLIC_ELF_SHA256_0167
    target_new = target_profile == PUBLIC_ELF_SHA256_0167
    if source_new:
        if type(observation._anonymous_bool_0167) is not bool:
            raise ValueError('scene_missing_anonymous_bool_0167')
        if (type(insertion) is not int or not 0 <= insertion < len(tokens)
                or tokens[insertion] != _Token('bits', 1, int(observation._anonymous_bool_0167))):
            raise ValueError('scene_observation_layout_mismatch')
    elif observation._anonymous_bool_0167 is not None:
        raise ValueError('scene_observation_layout_mismatch')
    if not target_new:
        if source_new:
            raise ValueError('scene_layout_downgrade_unsupported')
        if anonymous_bool_0167 is not _UNSET_ANONYMOUS_BOOL:
            raise ValueError('scene_anonymous_bool_0167_not_applicable')
    else:
        if anonymous_bool_0167 is _UNSET_ANONYMOUS_BOOL:
            if not source_new:
                raise ValueError('scene_missing_anonymous_bool_0167')
            flag = observation._anonymous_bool_0167
        else:
            if type(anonymous_bool_0167) is not bool:
                raise ValueError('scene_invalid_anonymous_bool_0167')
            flag = anonymous_bool_0167
        if (type(insertion) is not int or not 1 <= insertion <= len(tokens)
                or tokens[insertion - 1].kind != 'raw'
                or tokens[insertion - 1].extent != 16):
            raise ValueError('scene_observation_layout_mismatch')
        token = _Token('bits', 1, int(flag))
        if source_new:
            tokens[insertion] = token
        else:
            tokens.insert(insertion, token)
    writer = BitWriter(max_bytes=MAX_SCENE_WIRE_BYTES)
    for token in tokens:
        if token.kind == 'raw':
            if type(token.value) is not bytes or len(token.value) != token.extent:
                raise ValueError('scene_invalid_private_token')
            writer.write_raw(token.value)
        elif token.kind == 'bits':
            writer.write_bits(token.value, token.extent)
        else:
            raise ValueError('scene_invalid_private_token')
    if source_profile == target_profile and writer.bit_position != observation.bit_length:
        raise ValueError('scene_private_token_length_mismatch')
    result = writer.to_bytes()
    if source_profile != target_profile:
        # Cross-profile generation is explicit and must parse under its exact
        # requested layout. Never silently retry the other layout on failure.
        inspect_scene_wire(result, verified_profile_sha256=target_profile,
                           check_zero_padding=True)
    return result
