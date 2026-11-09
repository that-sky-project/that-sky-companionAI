"""Pure RAM Pose clock extension for a retained canonical Actor candidate.

This is an explicit clock experiment, not native physics or local sampling.
Call against the ORIGINAL last complete body with total elapsed time; do not
feed the previous result back in and accumulate quantization drift. If current
and history Pose counts are both zero, the original body returns unchanged.
No I/O, account access, logging or persistence operation exists here.
"""
import math

from .actor_codec.legacy_actor.actor_wire import inspect_actor_wire, reencode_actor_wire
from .float_quantization import (decode_float64, quantize_float64,
                                 NEAREST_EVEN_GRADUAL, _op)


def _timestamp_tokens(observed, starts):
    bit_position = 0
    found = {}
    for token in observed._tokens:
        if token.kind == 'raw':
            bit_position = (bit_position + 7) & ~7
            extent = token.extent * 8
        elif token.kind == 'bits':
            extent = token.extent
        else:
            raise ValueError('pose_clock_extension_unknown_token_kind')
        if bit_position in starts:
            if token.kind != 'bits' or extent != 12:
                raise ValueError('pose_clock_extension_timestamp_token_unverified')
            found[bit_position] = token.value
        bit_position += extent
    if bit_position != observed.bit_length or set(found) != starts:
        raise ValueError('pose_clock_extension_timestamp_extent_mismatch')
    return found


def extend_actor_pose_clocks(body, *, elapsed_seconds, map_id,
                             verified_profile_sha256):
    """Advance every current/history 12-bit phase by one common elapsed delta.

    Decode through the retained native FP64 quantizer, add nearest-even FP64,
    reduce modulo three seconds, and encode with its actual FP64/FP32 writer.
    Only timestamp bits can change. This returns private bytes, not diagnostics.
    Caller builds a fresh Snapshot/FNV8 frame after this operation.
    """
    if (type(elapsed_seconds) not in (int, float)
            or not 0 <= elapsed_seconds <= 1200
            or not math.isfinite(elapsed_seconds)):
        raise ValueError('pose_clock_extension_elapsed_invalid')
    if type(map_id) is not int or not 1 <= map_id <= 0xffffffff:
        raise ValueError('pose_clock_extension_map_invalid')
    elapsed = float(elapsed_seconds)
    observed = inspect_actor_wire(body,
        verified_profile_sha256=verified_profile_sha256,
        check_zero_padding=True, require_canonical_writer=True)
    if not observed.matches_map_association(map_id):
        raise ValueError('pose_clock_extension_map_mismatch')
    if reencode_actor_wire(observed) != body:
        raise ValueError('pose_clock_extension_input_not_canonical')
    counts = dict(observed._counts)
    expected = counts.get('pose_records', 0) + counts.get('history_pose_records', 0)
    ordered = [position for label, position in observed._boundaries
               if label == '0x169df88']
    starts = set(ordered)
    if (not 0 <= expected <= 8 or len(ordered) != expected
            or len(starts) != expected
            or any(start < 0 or start + 12 > observed.bit_length for start in starts)):
        raise ValueError('pose_clock_extension_timestamp_boundaries_unverified')
    if any(left + 12 > right for left, right in zip(sorted(starts), sorted(starts)[1:])):
        raise ValueError('pose_clock_extension_timestamp_ranges_overlap')
    codes = _timestamp_tokens(observed, starts)
    if expected == 0:
        # Explicit no-Pose path: never invent animation samples or timestamps.
        return body
    updated = bytearray(body)
    allowed_masks = bytearray(len(body))
    for start in sorted(starts):
        phase = decode_float64(codes[start], 0, 3, 0, 12,
                               profile=NEAREST_EVEN_GRADUAL)
        if not math.isfinite(phase) or not 0 <= phase <= 3:
            raise ValueError('pose_clock_extension_decoded_phase_invalid')
        shifted = math.fmod(_op(phase, elapsed, 'add', 53), 3.0)
        if not math.isfinite(shifted) or not 0 <= shifted < 3:
            raise ValueError('pose_clock_extension_shifted_phase_invalid')
        code = quantize_float64(shifted, 0, 3, 0, 12,
                                profile=NEAREST_EVEN_GRADUAL).code
        for bit in range(12):
            index, lane = divmod(start + bit, 8)
            mask = 1 << lane
            allowed_masks[index] |= mask
            updated[index] = ((updated[index] & ~mask)
                              | (((code >> bit) & 1) << lane))
    result = bytes(updated)
    if (len(result) != len(body)
            or any((old ^ new) & ~mask
                   for old, new, mask in zip(body, result, allowed_masks))):
        raise ValueError('pose_clock_extension_non_timestamp_change')
    checked = inspect_actor_wire(result,
        verified_profile_sha256=verified_profile_sha256,
        check_zero_padding=True, require_canonical_writer=True)
    if (not checked.matches_map_association(map_id)
            or checked.bit_length != observed.bit_length
            or checked._boundaries != observed._boundaries
            or checked._counts != observed._counts
            or reencode_actor_wire(checked) != result):
        raise ValueError('pose_clock_extension_output_roundtrip_failed')
    return result
