"""Build-421629 NetMsg 0x04 body, recovered from native serializer 0x1f7f044.

Offline codec only. This does not establish a connection or verify the complete
registration chain. field00 is read from the active connection, not player ID.
The one-bit tail is sampled from connection-state +0x3b0 before native clears it.
"""
from __future__ import annotations
import struct

BUILD = 421629
SIZE = 83
_FORMAT = struct.Struct('<B6HBI64sB')

def _uint(value, bits, name):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < (1 << bits):
        raise ValueError(f'{name} must be uint{bits}')
    return value

def encode(*, build, field00, netversion, level_flag, level_id, matrix, tail_flag):
    if build != BUILD:
        raise ValueError('serializer is verified statically only for build 421629')
    if not isinstance(netversion, (tuple, list)) or len(netversion) != 6:
        raise ValueError('explicit six-value NetVersion required')
    nv = [_uint(v, 16, f'netversion[{i}]') for i, v in enumerate(netversion)]
    if nv[2] != 23536:
        raise ValueError('NetVersion constant does not match build 421629')
    if not isinstance(matrix, (bytes, bytearray)) or len(matrix) != 64:
        raise ValueError('matrix must be exactly 64 explicit bytes')
    if type(tail_flag) is not bool:
        raise ValueError('tail_flag must be an explicit bool from the active connection state')
    return _FORMAT.pack(_uint(field00, 8, 'field00'), *nv,
                        _uint(level_flag, 8, 'level_flag'), _uint(level_id, 32, 'level_id'),
                        bytes(matrix), int(tail_flag))

def decode(data, *, build, require_zero_padding=False):
    if build != BUILD or len(data) != SIZE:
        raise ValueError('expected the build-421629 83-byte registration body')
    fields = _FORMAT.unpack(data)
    padding = fields[-1] >> 1
    if require_zero_padding and padding:
        raise ValueError('nonzero unused bits in the last byte')
    result = {'build': build, 'field00': fields[0], 'netversion': list(fields[1:7]),
              'level_flag': fields[7], 'level_id': fields[8], 'matrix': fields[9],
              'tail_flag': bool(fields[10] & 1)}
    encode(**result)  # shared range/version validation; no network side effects
    result['unused_tail_bits'] = padding
    return result

def public_summary(data):
    value = decode(data, build=BUILD)
    return {k: value[k] for k in ['build', 'field00', 'netversion', 'level_flag', 'level_id', 'tail_flag', 'unused_tail_bits']} | {
        'payload_bytes': len(data), 'matrix_omitted': True,
        'native_layout_recovered': True, 'custom_registration_chain_verified': False}
