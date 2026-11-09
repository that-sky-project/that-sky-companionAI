import struct
from .bit_stream import BitWriter
from .delta_sync import MAX_SNAPSHOT_BYTES
from .level_authority import LevelRecord

def _uint(value, width, message):
    if type(value) is not int or not 0 <= value < (1 << width):
        raise ValueError(message)
    return value

def serialize_cached_level_header(record, *, own_map, received_version):
    """Native1ed9abc C->S owner0/map/version/include=true, intact record.

    The record must have been validated by CachedSceneAuthoritySender. This
    pure helper serializes supplied slots without interpreting their content.
    It deliberately does not replace slot4 with map or change body values.
    """
    if not isinstance(record, LevelRecord):
        raise ValueError('cached_scene_record_missing')
    _uint(own_map, 32, 'cached_scene_map_invalid')
    _uint(received_version, 16, 'cached_scene_version_invalid')
    if not own_map or type(record.data) is not bytes or not 1 <= len(record.data) <= 0x2000:
        raise ValueError('cached_scene_record_invalid')
    writer = BitWriter(max_bytes=MAX_SNAPSHOT_BYTES)
    writer.write_raw(bytes((0,)))  # Native outgoing owner is server sentinel0.
    writer.write_raw(struct.pack('<I', own_map))
    writer.write_raw(struct.pack('<H', received_version))
    writer.write_bool(True)
    writer.write_raw(struct.pack('<H', _uint(record.slot_0, 16, 'cached_scene_slot_invalid')))
    writer.write_raw(bytes((_uint(record.slot_2, 8, 'cached_scene_slot_invalid'),)))
    writer.write_raw(struct.pack('<I', _uint(record.slot_4, 32, 'cached_scene_slot_invalid')))
    writer.write_u8(_uint(record.slot_8, 8, 'cached_scene_slot_invalid'))
    writer.write_raw(struct.pack('<H', _uint(record.slot_a, 16, 'cached_scene_slot_invalid')))
    writer.write_raw(struct.pack('<H', _uint(record.slot_c, 16, 'cached_scene_slot_invalid')))
    writer.write_raw(struct.pack('<H', len(record.data)))
    writer.write_raw(record.data)
    return writer.to_bytes()
