"""Bounded binary I/O helpers; no installation, network, axis conversion or fallback plane."""
from pathlib import Path
import hashlib
import struct
import numpy as np
from ._types import FormatError

class Cursor:
    def __init__(self, data, offset=0, label='asset'):
        self.data = memoryview(data)
        self.pos = offset
        self.label = label
        self.require(0)

    def require(self, size):
        if type(size) is not int or size < 0 or self.pos < 0 or size > len(self.data)-self.pos:
            raise FormatError(f'{self.label}: truncated_at_{self.pos}_need_{size}')

    def take(self, size):
        self.require(size)
        result = self.data[self.pos:self.pos+size].tobytes()
        self.pos += size
        return result

    def unpack(self, fmt):
        size = struct.calcsize('<'+fmt)
        self.require(size)
        result = struct.unpack_from('<'+fmt, self.data, self.pos)
        self.pos += size
        return result

    def u32(self):
        return self.unpack('I')[0]

    @property
    def remaining(self):
        return len(self.data)-self.pos


def load_input(value, limits):
    if isinstance(value, (bytes, bytearray, memoryview)):
        if len(value) > limits.max_file_bytes:
            raise FormatError('file_size_limit')
        data, source = bytes(value), '<bytes>'
    elif isinstance(value, (str, Path)):
        path = Path(value)
        if path.stat().st_size > limits.max_file_bytes:
            raise FormatError('file_size_limit')
        with path.open('rb') as stream:
            data = stream.read(limits.max_file_bytes+1)
        if len(data) > limits.max_file_bytes:
            raise FormatError('file_size_limit')
        source = str(path.resolve())
    else:
        raise TypeError('expected_path_or_bytes')
    return data, source, hashlib.sha256(data).hexdigest()


def checked_count(value, limit, name):
    if type(value) is not int or value < 0 or value > limit:
        raise FormatError(name+'_limit')
    return value


def readonly(array, dtype=None, shape=None):
    array = np.array(array, dtype=dtype, copy=True, order='C')
    if shape is not None:
        array = array.reshape(shape)
    array.setflags(write=False)
    return array


def validate_positions(positions):
    if positions.ndim != 2 or positions.shape[1] != 3 or not np.isfinite(positions).all():
        raise FormatError('nonfinite_or_invalid_positions')


def validate_indices(indices, vertices):
    if indices.size and (np.any(indices < 0) or np.any(indices >= vertices)):
        raise FormatError('index_out_of_bounds')


def bounds(positions):
    if not len(positions):
        return None, None
    return tuple(float(v) for v in positions.min(axis=0)), tuple(float(v) for v in positions.max(axis=0))
