"""Independent bounded LZ4 block reader, based on the public LZ4 block format.
No frame headers or implicit size prefix; callers provide exact/maximum output size.
https://github.com/lz4/lz4/blob/dev/doc/lz4_Block_format.md
"""
from ._types import FormatError, DEFAULT_LIMITS

def decompress_lz4_block(data, expected_size=None, *, max_output=DEFAULT_LIMITS.max_uncompressed_bytes):
    if type(max_output) is not int or max_output < 0:
        raise ValueError('invalid_output_limit')
    if expected_size is not None and (type(expected_size) is not int or expected_size < 0 or expected_size > max_output):
        raise FormatError('lz4_declared_size_limit')
    cap = min(max_output, expected_size) if expected_size is not None else max_output
    src = memoryview(data).cast('B')
    out = bytearray()
    pos = 0
    def length(base):
        nonlocal pos
        value = base
        if base == 15:
            while True:
                if pos >= len(src):
                    raise FormatError('lz4_truncated_length')
                part = src[pos]; pos += 1; value += part
                if value > cap:
                    raise FormatError('lz4_output_limit')
                if part != 255:
                    break
        return value
    if not src:
        raise FormatError('lz4_missing_terminal_token')
    terminated = False
    while pos < len(src):
        token = src[pos]; pos += 1
        count = length(token >> 4)
        if count > len(src)-pos:
            raise FormatError('lz4_truncated_literals')
        if count > cap-len(out):
            raise FormatError('lz4_output_limit')
        out.extend(src[pos:pos+count]); pos += count
        if pos == len(src):
            terminated = True
            break
        if len(src)-pos < 2:
            raise FormatError('lz4_truncated_offset')
        offset = src[pos] | src[pos+1] << 8; pos += 2
        if not 0 < offset <= len(out):
            raise FormatError('lz4_invalid_match_offset')
        count = length(token & 15)+4
        if count > cap-len(out):
            raise FormatError('lz4_output_limit')
        # Repeated overlap is part of the block format; bound before allocating.
        seed = bytes(out[-offset:])
        out.extend(seed * (count//offset))
        out.extend(seed[:count % offset])
    if not terminated:
        raise FormatError('lz4_missing_terminal_sequence')
    if expected_size is not None and len(out) != expected_size:
        raise FormatError('lz4_expanded_size_mismatch')
    return bytes(out)
