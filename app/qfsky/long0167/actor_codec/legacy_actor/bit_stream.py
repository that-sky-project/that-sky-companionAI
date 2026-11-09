"""Offline current-native bit primitives; no player state, sockets or actions.

Verified public AArch64 routines: 318e404/318e5b4 use LSB-first bits;
318e788/318ea64 and wrapper319040c align raw bytes upward; 3190940
stores value-minimum using bit_length(maximum-minimum), inclusive bounds.
The native writer is initialized on a zeroed buffer (318f230..318f298).

The local API deliberately fails before modifying the cursor or buffer on
invalid input/truncation, instead of partially consuming and flagging a native
stream invalid. It rejects values that would be silently masked by raw native
write_bits. Alignment normally ignores padding, as native does; zero-padding
validation and require_end are explicit local validation options. Float
quantizers are not implemented here: 3190f64 is a separate, piecewise FP32
algorithm whose exact arithmetic must not be replaced with Python doubles.
"""

_DEFAULT_MAX_BYTES = 65536


def _integer(value, minimum, maximum, label):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(label)
    return value


def _storage_bits(bits):
    if type(bits) is not int or bits not in (8, 16, 32):
        raise ValueError('invalid_bit_storage_width')
    return bits


def _bounds(minimum, maximum, storage_bits, signed=False):
    _storage_bits(storage_bits)
    low = -(1 << (storage_bits-1)) if signed else 0
    high = (1 << (storage_bits-1))-1 if signed else (1 << storage_bits)-1
    _integer(minimum, low, high, 'invalid_bit_integer_bounds')
    _integer(maximum, low, high, 'invalid_bit_integer_bounds')
    if minimum >= maximum:
        raise ValueError('invalid_bit_integer_bounds')
    return (maximum-minimum).bit_length()


def bounded_uint_width(minimum, maximum, *, storage_bits=32):
    """Native inclusive-range width; equal bounds are not a native encoding."""
    return _bounds(minimum, maximum, storage_bits)


class BitWriter:
    """Fresh zero-backed LSB-first writer with an explicit allocation limit."""

    __slots__ = ('_buffer', '_position', '_max_bytes')

    def __init__(self, *, max_bytes=_DEFAULT_MAX_BYTES):
        _integer(max_bytes, 0, 1 << 24, 'invalid_bit_capacity')
        self._buffer = bytearray()
        self._position = 0
        self._max_bytes = max_bytes

    def __repr__(self):
        return f'<BitWriter position={self._position}>'

    @property
    def bit_position(self):
        return self._position

    def _reserve(self, position):
        count = (position+7)//8
        if count > self._max_bytes:
            raise ValueError('bit_stream_capacity_exceeded')
        if count > len(self._buffer):
            self._buffer.extend(bytes(count-len(self._buffer)))

    def write_bits(self, value, width):
        """Write 1..32 low-order bits; reject a value that does not fit."""
        _integer(width, 1, 32, 'invalid_bit_width')
        _integer(value, 0, (1 << width)-1, 'invalid_bit_value')
        self._reserve(self._position+width)
        remaining = width
        while remaining:
            index, offset = divmod(self._position, 8)
            take = min(8-offset, remaining)
            self._buffer[index] |= (value & ((1 << take)-1)) << offset
            self._position += take
            value >>= take
            remaining -= take

    def write_bool(self, value):
        """3190564 consumes one LSB; accept canonical bool inputs only."""
        if type(value) is not bool:
            raise ValueError('invalid_bit_bool')
        self.write_bits(int(value), 1)

    def align(self):
        """Skip to the next byte boundary, retaining zero padding."""
        position = (self._position+7) & ~7
        self._reserve(position)
        self._position = position

    def write_raw(self, data):
        """319040c raw bytes align even for an empty byte sequence."""
        if type(data) is not bytes:
            raise ValueError('invalid_bit_raw_bytes')
        position = (self._position+7) & ~7
        end = position+8*len(data)
        self._reserve(end)
        self._buffer[position//8:end//8] = data
        self._position = end

    def write_uint(self, value, minimum, maximum, *, storage_bits=32):
        """3190940 /3190bec/3190cd0/3190dac inclusive unsigned range."""
        width = _bounds(minimum, maximum, storage_bits)
        _integer(value, minimum, maximum, 'invalid_bit_integer_value')
        self.write_bits(value-minimum, width)

    def write_sint(self, value, minimum, maximum, *, storage_bits=32):
        """Signed wrappers bias the domain; encoded delta is value-minimum."""
        width = _bounds(minimum, maximum, storage_bits, signed=True)
        _integer(value, minimum, maximum, 'invalid_bit_integer_value')
        self.write_bits(value-minimum, width)

    def write_u8(self, value, minimum=0, maximum=255):
        self.write_uint(value, minimum, maximum, storage_bits=8)

    def write_u16(self, value, minimum=0, maximum=65535):
        self.write_uint(value, minimum, maximum, storage_bits=16)

    def write_u32(self, value, minimum=0, maximum=4294967295):
        self.write_uint(value, minimum, maximum, storage_bits=32)

    def write_i8(self, value, minimum=-128, maximum=127):
        self.write_sint(value, minimum, maximum, storage_bits=8)

    def write_i16(self, value, minimum=-32768, maximum=32767):
        self.write_sint(value, minimum, maximum, storage_bits=16)

    def write_i32(self, value, minimum=-2147483648, maximum=2147483647):
        self.write_sint(value, minimum, maximum, storage_bits=32)

    def to_bytes(self):
        """Return occupied bytes without consuming final zero padding."""
        return bytes(self._buffer)


class BitReader:
    """LSB-first reader. Raw reads align; bit reads stay packed."""

    __slots__ = ('_data', '_position', '_limit')

    def __init__(self, data, *, bit_length=None, max_bytes=_DEFAULT_MAX_BYTES):
        _integer(max_bytes, 0, 1 << 24, 'invalid_bit_capacity')
        if type(data) is not bytes or len(data) > max_bytes:
            raise ValueError('invalid_bit_raw_bytes')
        if bit_length is None:
            bit_length = len(data)*8
        _integer(bit_length, 0, len(data)*8, 'invalid_bit_length')
        self._data = data
        self._position = 0
        self._limit = bit_length

    def __repr__(self):
        return f'<BitReader position={self._position} remaining={self.remaining_bits}>'

    @property
    def bit_position(self):
        return self._position

    @property
    def remaining_bits(self):
        return self._limit-self._position

    def _require(self, position):
        if position > self._limit:
            raise ValueError('truncated_bit_stream')

    def _value_at(self, position, width):
        value = 0
        shift = 0
        while width:
            index, offset = divmod(position, 8)
            take = min(8-offset, width)
            value |= ((self._data[index] >> offset) & ((1 << take)-1)) << shift
            position += take
            shift += take
            width -= take
        return value

    def read_bits(self, width):
        _integer(width, 1, 32, 'invalid_bit_width')
        self._require(self._position+width)
        value = self._value_at(self._position, width)
        self._position += width
        return value

    def read_bool(self):
        return bool(self.read_bits(1))

    def _aligned_position(self, check_zero_padding):
        if type(check_zero_padding) is not bool:
            raise ValueError('invalid_bit_padding_option')
        position = (self._position+7) & ~7
        self._require(position)
        if check_zero_padding and self._value_at(self._position, position-self._position):
            raise ValueError('nonzero_bit_padding')
        return position

    def align(self, *, check_zero_padding=False):
        """Native skips padding; optional canonical-zero check is local only."""
        self._position = self._aligned_position(check_zero_padding)

    def read_raw(self, length, *, check_zero_padding=False):
        _integer(length, 0, 1 << 24, 'invalid_bit_raw_length')
        position = self._aligned_position(check_zero_padding)
        end = position+8*length
        self._require(end)
        data = self._data[position//8:end//8]
        self._position = end
        return data

    def _read_integer(self, minimum, maximum, storage_bits, signed):
        width = _bounds(minimum, maximum, storage_bits, signed)
        self._require(self._position+width)
        value = minimum+self._value_at(self._position, width)
        if value > maximum:
            raise ValueError('invalid_bit_integer_code')
        self._position += width
        return value

    def read_uint(self, minimum, maximum, *, storage_bits=32):
        return self._read_integer(minimum, maximum, storage_bits, False)

    def read_sint(self, minimum, maximum, *, storage_bits=32):
        return self._read_integer(minimum, maximum, storage_bits, True)

    def read_u8(self, minimum=0, maximum=255):
        return self.read_uint(minimum, maximum, storage_bits=8)

    def read_u16(self, minimum=0, maximum=65535):
        return self.read_uint(minimum, maximum, storage_bits=16)

    def read_u32(self, minimum=0, maximum=4294967295):
        return self.read_uint(minimum, maximum, storage_bits=32)

    def read_i8(self, minimum=-128, maximum=127):
        return self.read_sint(minimum, maximum, storage_bits=8)

    def read_i16(self, minimum=-32768, maximum=32767):
        return self.read_sint(minimum, maximum, storage_bits=16)

    def read_i32(self, minimum=-2147483648, maximum=2147483647):
        return self.read_sint(minimum, maximum, storage_bits=32)

    def require_end(self, *, allow_zero_padding=False):
        """Local validation: allow at most 7 final zero bits, never extra bytes."""
        if type(allow_zero_padding) is not bool:
            raise ValueError('invalid_bit_padding_option')
        remaining = self.remaining_bits
        if remaining and not (allow_zero_padding and remaining < 8
                              and not self._value_at(self._position, remaining)):
            raise ValueError('unexpected_bit_stream_trailer')

