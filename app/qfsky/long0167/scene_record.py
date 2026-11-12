"""Current Home Scene contract; no transport or schema guessing."""
from .codec import scene_wire as wire
from .level_authority import LevelRecord
from qfsky.version_profile import CURRENT_NATIVE_SHA256, profile_for_native_sha256

PUBLIC_ELF_SHA256 = CURRENT_NATIVE_SHA256


def expected_home_tag(verified_profile_sha256):
    """按 native_sha256 取该版本的遇境 resource tag。

    tag 全部来自 qfsky.version_profile 的版本档案；未知摘要 fail-closed。
    """
    profile = profile_for_native_sha256(verified_profile_sha256)
    if profile is None:
        raise ValueError('room0167_scene_profile')
    return profile['home_resource_tag']


def _check_record(record, *, expected_resource_tag=None, verified_profile_sha256):
    if type(record) is not LevelRecord:
        raise ValueError('room0167_scene_record_type')
    tag = expected_home_tag(verified_profile_sha256)
    if expected_resource_tag is not None and expected_resource_tag != tag:
        raise ValueError('room0167_scene_profile')
    for value, width in ((record.slot_0, 16), (record.slot_2, 8), (record.slot_4, 32),
                         (record.slot_8, 8), (record.slot_a, 16), (record.slot_c, 16)):
        if type(value) is not int or not 0 <= value < 1 << width:
            raise ValueError('room0167_scene_slot')
    if record.slot_0 != 0x63c0:
        raise ValueError('room0167_scene_native_size_uncovered')
    obs = wire.inspect_scene_wire(record.data,
                                  verified_profile_sha256=verified_profile_sha256,
                                  check_zero_padding=True)
    if (not obs.matches_resource_tag(tag)
            or obs._anonymous_bool_0167 is not False):
        raise ValueError('room0167_scene_profile_state_uncovered')
    if wire.reencode_scene_wire(obs) != record.data:
        raise ValueError('room0167_scene_not_canonical')
    return record
