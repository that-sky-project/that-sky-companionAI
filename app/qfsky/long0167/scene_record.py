"""Current Home Scene contract; no transport or schema guessing."""
from .codec import scene_wire as wire
from .level_authority import LevelRecord
PUBLIC_ELF_SHA256='0b81f1da54d9c6f28c08746b2ec19519d8eb78c4f1b3c5e4e420ff726dc25321'
def _check_record(record,*,expected_resource_tag,verified_profile_sha256):
 if type(record)is not LevelRecord:raise ValueError('room0167_scene_record_type')
 if expected_resource_tag!=36851 or verified_profile_sha256!=PUBLIC_ELF_SHA256:raise ValueError('room0167_scene_profile')
 for value,width in ((record.slot_0,16),(record.slot_2,8),(record.slot_4,32),(record.slot_8,8),(record.slot_a,16),(record.slot_c,16)):
  if type(value)is not int or not 0<=value<1<<width:raise ValueError('room0167_scene_slot')
 if record.slot_0!=0x63c0:raise ValueError('room0167_scene_native_size_uncovered')
 obs=wire.inspect_scene_wire(record.data,verified_profile_sha256=PUBLIC_ELF_SHA256,check_zero_padding=True)
 if not obs.matches_resource_tag(36851)or obs._anonymous_bool_0167 is not False:raise ValueError('room0167_scene_profile_state_uncovered')
 if wire.reencode_scene_wire(obs)!=record.data:raise ValueError('room0167_scene_not_canonical')
 return record
