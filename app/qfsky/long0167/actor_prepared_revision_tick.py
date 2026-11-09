"""RAM-only wire equivalent of the two native prepared-row U8 tick writes.

This does not reproduce ordinary fill, physics, loaded Avatar creation or
cache commits. It changes exactly two already-present revision bytes and does
not assign authority, identity, entities, positions or actions.
"""
from .actor_codec.legacy_actor.actor_wire import inspect_actor_wire, reencode_actor_wire


def _raw_tokens(observed):
    bit_position = 0
    raw = {}
    for token in observed._tokens:
        if token.kind == 'raw':
            bit_position = (bit_position + 7) & ~7
            raw[bit_position // 8] = (token.extent, token.value)
            bit_position += token.extent * 8
        elif token.kind == 'bits':
            bit_position += token.extent
        else:
            raise ValueError('prepared_revision_unknown_token_kind')
    if bit_position != observed.bit_length:
        raise ValueError('prepared_revision_token_extent_mismatch')
    return raw


def advance_prepared_actor_revisions(body, *, tick_steps, map_id,
                                    verified_profile_sha256):
    """Apply N independent increments to both ORIGINAL prepared revisions.

    Caller supplies the original held Actor after its clock-only extension,
    and successful extension count +1. Zero is a valid wrapped U8 revision;
    it must not be skipped. Never feed back an already advanced output.
    """
    if type(tick_steps) is not int or not 1 <= tick_steps <= 12000:
        raise ValueError('prepared_revision_tick_steps_invalid')
    if type(map_id) is not int or not 1 <= map_id <= 0xffffffff:
        raise ValueError('prepared_revision_map_invalid')
    observed = inspect_actor_wire(body,
        verified_profile_sha256=verified_profile_sha256,
        check_zero_padding=True, require_canonical_writer=True)
    if (not observed.matches_map_association(map_id)
            or reencode_actor_wire(observed) != body):
        raise ValueError('prepared_revision_input_unverified')
    current = [position for label, position in observed._boundaries
               if label == '0x2144600']
    history = [position for label, position in observed._boundaries
               if label == '0x2144670']
    if len(current) != 1 or len(history) != 1:
        raise ValueError('prepared_revision_boundaries_unverified')
    offsets = tuple((position + 7) // 8 for position in (current[0], history[0]))
    if offsets[0] == offsets[1]:
        raise ValueError('prepared_revision_fields_overlap')
    raw = _raw_tokens(observed)
    for offset in offsets:
        token = raw.get(offset)
        if (token is None or token[0] != 1 or type(token[1]) is not bytes
                or len(token[1]) != 1 or offset >= len(body)
                or token[1][0] != body[offset]):
            raise ValueError('prepared_revision_raw_u8_unverified')
    updated = bytearray(body)
    for offset in offsets:
        updated[offset] = (body[offset] + tick_steps) & 255
    result = bytes(updated)
    if (len(result) != len(body)
            or any(old != new and index not in offsets
                   for index, (old, new) in enumerate(zip(body, result)))):
        raise ValueError('prepared_revision_non_revision_change')
    checked = inspect_actor_wire(result,
        verified_profile_sha256=verified_profile_sha256,
        check_zero_padding=True, require_canonical_writer=True)
    if (not checked.matches_map_association(map_id)
            or checked.bit_length != observed.bit_length
            or checked._boundaries != observed._boundaries
            or checked._counts != observed._counts
            or reencode_actor_wire(checked) != result):
        raise ValueError('prepared_revision_output_roundtrip_failed')
    return result
