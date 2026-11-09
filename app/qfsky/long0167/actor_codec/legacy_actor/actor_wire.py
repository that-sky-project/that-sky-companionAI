"""LEGACY-ONLY Actor codec baseline. NEW profile is not supported.
Old map bit376/byte47 remains specific to the old schema.

Complete offline normal-path Actor wire inspection for the pinned build.

Main21444f4, its nested helpers, and all dynamic branches were audited in
retained public ELF reports. Only caller-supplied bytes in RAM are consumed.
Quantizers retain their wire codes, contextual entities remain opaque mode2
dictionary indices, and strings remain bytes (never decoded/rebuilt by name).

This is not a fresh Actor producer, native PlayerState deserializer, entity
dictionary, spawn implementation or live send path. A successful roundtrip
proves layout, not legitimate own gameplay state or continuous room presence.
Native replacement dispatch handlers and invalid/truncated streams are not
shorter valid formats. All of them must fail or use a separately audited path.
"""
from dataclasses import dataclass, field
import math
import struct

from .actor_wire_schema import PUBLIC_ELF_SHA256, MAIN, FIXED_HELPERS, POSE
from ..bit_stream import BitReader, BitWriter

MAX_ACTOR_WIRE_BYTES = 0x528


@dataclass(frozen=True, repr=False)
class _Token:
    kind: str
    extent: int
    value: int | bytes = field(repr=False)


@dataclass(frozen=True, repr=False)
class ActorWireObservation:
    _tokens: tuple[_Token, ...] = field(repr=False)
    _wire_bytes: bytes = field(repr=False)
    _counts: tuple[tuple[str, int], ...] = field(repr=False)
    _boundaries: tuple[tuple[str, int], ...] = field(repr=False)
    bit_length: int
    canonical_writer_strings: bool
    canonical_writer_boolean_tail: bool

    def matches_map_association(self, expected_map):
        """Compare parsed main+51b0 with exact current Level+9a10 U32.

        Native own OnLevelLoad sets component+b8c4 from NetWorld+30 via
        1edb680, and fill214679c writes it to state+51b0. This is separate
        from Scene's folded resource tag, room tokens and a spawn position.
        An equality check is not proof of the full own Actor producer.
        """
        if type(expected_map) is not int or not 0 <= expected_map <= 0xffffffff:
            raise ValueError('actor_invalid_expected_map')
        positions = [position for label, position in self._boundaries
                     if label == '0x2144620']
        if positions != [376] or type(self._wire_bytes) is not bytes or len(self._wire_bytes) < 51:
            raise ValueError('actor_map_association_unverified')
        return struct.unpack_from('<I', self._wire_bytes, 47)[0] == expected_map

    def metadata(self):
        """Only structural counts; never identity, labels, pose or raw bytes."""
        return {'actor_layout_checked': True,
                'actor_bytes': len(self._wire_bytes),
                'actor_bits': self.bit_length,
                'wire_entity_context': 2,
                'entity_dictionary_resolved': False,
                'variable_group_counts': dict(self._counts),
                'canonical_writer_strings': self.canonical_writer_strings,
                'canonical_writer_boolean_tail': self.canonical_writer_boolean_tail,
                'fresh_actor_initialized': False,
                'actor_authority_verified': False,
                'stable_online_verified': False,
                'game_requests_sent': False}


class _Walk:
    def __init__(self, body, padding, canonical):
        self.reader = BitReader(body, max_bytes=MAX_ACTOR_WIRE_BYTES)
        self.padding = padding
        self.canonical = canonical
        self.tokens = []
        self.counts = []
        self.boundaries = []
        self.canonical_strings = True
        self.canonical_boolean_tail = True

    def boundary(self, label):
        self.boundaries.append((label, self.reader.bit_position))

    def raw(self, n):
        value = self.reader.read_raw(n, check_zero_padding=self.padding)
        self.tokens.append(_Token('raw', n, value))
        return value

    def bits(self, width):
        value = self.reader.read_bits(width)
        self.tokens.append(_Token('bits', width, value))
        return value

    def boolean(self):
        return bool(self.bits(1))

    def bounded(self, minimum, maximum):
        value = self.bits((maximum-minimum).bit_length())
        if value > maximum-minimum:
            raise ValueError('actor_native_invalid_integer_code')
        return value + minimum

    def count(self, label, maximum):
        value = self.bounded(0, maximum)
        self.counts.append((label, value))
        return value

    def raw_float(self, width, components=1, *, origin=False):
        values = []
        for _ in range(components):
            data = self.raw(width//8)
            value = struct.unpack('<f' if width == 32 else '<d', data)[0]
            if math.isnan(value):
                raise ValueError('actor_native_invalid_raw_float')
            values.append(value)
        if origin and any(abs(value) > 10000 for value in values):
            raise ValueError('actor_native_invalid_origin')

    def operation(self, item):
        label, kind, *args = item
        self.boundary(label)
        if kind == 'raw':
            return self.raw(args[0])
        if kind == 'float':
            return self.raw_float(*args, origin=label == '0x214458c')
        if kind == 'bool':
            return self.boolean()
        if kind == 'range':
            return self.bounded(*args)
        if kind == 'quant':
            width, components = args
            for _ in range(components):
                self.bits(width)
            return None
        if kind == 'entity':
            return self.raw(1)  # mode2 dictionary index, never native handle.
        if kind == 'helper':
            return self.helper(args[0])
        raise ValueError('actor_unknown_schema_operation')

    def plan(self, plan):
        for item in plan:
            self.operation(item)

    def helper(self, address):
        if address in FIXED_HELPERS:
            self.plan(FIXED_HELPERS[address])
        elif address == '0x2142aa4':
            n = self.count('pose_records', 4)
            for _ in range(n):
                self.plan(POSE)  # 221 packed bits, no raw alignment.
        elif address == '0x2145220':
            self.history()
        elif address == '0x1eb530c':
            self.strings()
        elif address == '0x1ebb468':
            self.fixed_boolean_array()
        elif address == '0x21473dc':
            for _ in range(48):
                self.boolean()  # source gates force false; never omit fields.
            self.raw(4)
        elif address == '0x2144024':
            self.multipass_arrays()
        else:
            raise ValueError('actor_unknown_schema_helper')

    def history(self):
        self.raw(1)
        self.raw(4)
        self.raw_float(32, 3)
        self.bits(6)
        n = self.count('history_pose_records', 4)
        for _ in range(n):
            self.plan(POSE)
        self.raw(4)
        self.raw(8)
        self.bits(9)
        self.raw_float(32)
        for _ in range(4):
            self.raw(1)
        self.bounded(0, 8)

    def strings(self):
        # Ten records are fixed; labels are serialized through native lookup.
        # Reader accepts noncanonical NUL/oversize inputs, preserving bytes.
        for _ in range(10):
            self.raw(4)
            for _ in range(3):
                self.bounded(0, 3)
            n = int.from_bytes(self.raw(4), 'little')
            if n > MAX_ACTOR_WIRE_BYTES:
                raise ValueError('actor_invalid_string_wire_length')
            data = self.raw(n)
            canonical = (1 <= n <= 64 and data[-1:] == b'\0'
                         and b'\0' not in data[:-1])
            self.canonical_strings &= canonical
            if self.canonical and not canonical:
                raise ValueError('actor_noncanonical_writer_string')
        self.bits(8)
        self.bits(8)
        self.bounded(0, 15)
        self.bounded(0, 14)
        self.raw(4)
        self.raw(2)

    def fixed_boolean_array(self):
        self.raw(2)
        self.raw(4)
        self.raw(4)
        self.boolean()
        n = self.count('fixed_boolean_active', 256)
        for i in range(256):
            value = self.boolean()
            if i >= n and value:
                self.canonical_boolean_tail = False
                if self.canonical:
                    raise ValueError('actor_noncanonical_writer_boolean_tail')

    def multipass_arrays(self):
        # Seven packed header values, then two independent aligned raw counts.
        for maximum in (3, 161, 7, 161, 512, 512, 161):
            self.bounded(0, maximum)
        n_a, n_b = self.raw(1)[0], self.raw(1)[0]
        self.counts.extend((('array_a', n_a), ('array_b', n_b)))
        primary_a, condition_a = [], []
        self.boundary('array_a_primary')
        for _ in range(n_a):
            primary = self.boolean()
            primary_a.append(primary)
            condition_a.append(self.boolean() if primary else False)
        a, q = sum(primary_a), sum(condition_a)
        if a > 64 or q > 8:
            raise ValueError('actor_native_invalid_active_array_count')
        self.boundary('array_b_primary')
        primary_b = [self.boolean() for _ in range(n_b)]
        self.counts.extend((('array_a_active', a), ('array_b_active', sum(primary_b)),
                            ('array_a_position', q)))
        self.boundary('array_a_auxiliary')
        for primary in primary_a:
            if primary:
                self.boolean()
                self.boolean()
        self.boundary('array_b_auxiliary')
        for primary in primary_b:
            if primary:
                self.boolean()
        self.boundary('array_a_position')
        for condition in condition_a:
            if condition:
                self.boolean()
                self.boolean()
                for _ in range(3):
                    self.bits(6)
        self.boundary('array_a_terminal')
        for primary in primary_a:
            if primary:
                self.bounded(0, 127)
                self.bounded(0, 127)

    def main(self):
        optional = False
        n_raw = n_entity = 0
        for item in MAIN:
            label = item[0]
            if label in ('0x2144858', '0x2144890') and not optional:
                continue
            if label == '0x2144bac':
                for _ in range(n_raw):
                    self.operation(item)
                continue
            if label == '0x2144ca4':
                for _ in range(n_entity):
                    self.operation(item)
                continue
            value = self.operation(item)
            if label == '0x2144840':
                optional = value
            elif label == '0x2144b84':
                n_raw = value
                self.counts.append(('raw8_records', value))
            elif label == '0x2144c84':
                n_entity = value
                self.counts.append(('entity_records', value))


def inspect_actor_wire(body, *, verified_profile_sha256,
                       check_zero_padding=False, require_canonical_writer=False):
    """Consume normal mode2 layout; assertions never attest the native build.

    Lossless reader mode preserves strings and ignored inactive bools exactly.
    Explicit canonical-writer validation additionally requires writer-produced
    string shape and false inactive bool slots. It does not recover private
    lookup tables or prove all writer source masks. Zero-padding is a separate
    stricter local option; native reads normally skip internal padding.
    """
    if type(verified_profile_sha256) is not str or verified_profile_sha256 != PUBLIC_ELF_SHA256:
        raise ValueError('actor_profile_unverified')
    if type(check_zero_padding) is not bool or type(require_canonical_writer) is not bool:
        raise ValueError('actor_invalid_validation_option')
    if type(body) is not bytes or not 1 <= len(body) <= MAX_ACTOR_WIRE_BYTES:
        raise ValueError('actor_invalid_wire_length')
    w = _Walk(body, check_zero_padding, require_canonical_writer)
    w.main()
    bit_length = w.reader.bit_position
    if w.reader.remaining_bits >= 8:
        raise ValueError('actor_unexpected_wire_trailer')
    if check_zero_padding:
        w.reader.require_end(allow_zero_padding=True)
    return ActorWireObservation(tuple(w.tokens), body, tuple(w.counts),
                                tuple(w.boundaries), bit_length,
                                w.canonical_strings, w.canonical_boolean_tail)


def reencode_actor_wire(observation):
    """Fresh zero-padded typed wire tokens; no native semantic regeneration."""
    if type(observation) is not ActorWireObservation:
        raise ValueError('actor_observation_required')
    writer = BitWriter(max_bytes=MAX_ACTOR_WIRE_BYTES)
    for token in observation._tokens:
        if token.kind == 'raw':
            if type(token.value) is not bytes or len(token.value) != token.extent:
                raise ValueError('actor_invalid_private_token')
            writer.write_raw(token.value)
        elif token.kind == 'bits':
            writer.write_bits(token.value, token.extent)
        else:
            raise ValueError('actor_invalid_private_token')
    if writer.bit_position != observation.bit_length:
        raise ValueError('actor_private_token_length_mismatch')
    return writer.to_bytes()
