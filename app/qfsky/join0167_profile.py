"""Strict private, account-bound cache loader for APK build 421589.
This prepares a statically recovered JoinGame; it does not establish an online
session. No old-template fallback, no network, no automatic account rebinding.
"""
import hashlib
import hmac
import json
import re
import struct
from dataclasses import dataclass
from pathlib import Path
from .join0167_codec import encode, octets, uint

BUILD = 421589
NATIVE_SHA256 = '0b81f1da54d9c6f28c08746b2ec19519d8eb78c4f1b3c5e4e420ff726dc25321'
ACCOUNT_DOMAIN = b'sky-cn-421589-level-cache-account-v1\x00'
BINDING_ALGORITHM = 'sha256-domain-sky-cn-421589-level-cache-account-v1'


def uuid_raw(value):
    # Native 0x33e8d50 parses %2hhx into byte offsets0..15; formatter0x33e8cac
    # emits offsets0..15 in sequence. This is NOT Windows GUID bytes_le order.
    if isinstance(value, bytes):
        return octets(value, 16, 'account uuid')
    if not isinstance(value, str) or not re.fullmatch(r'(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})', value):
        raise ValueError('account_uuid must be a canonical UUID or 32 hex digits')
    return bytes.fromhex(value.replace('-', ''))


def account_binding(value):
    return hashlib.sha256(ACCOUNT_DOMAIN + uuid_raw(value)).hexdigest()


def inside(root, relative, *, base=None):
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ValueError('private record/cache paths must be explicit relative paths')
    p = ((base or root) / relative).resolve(strict=True)
    if not p.is_relative_to(root) or not p.is_file():
        raise ValueError('private path escapes configured root or is not a file')
    return p


@dataclass(frozen=True, repr=False)
class LoadedProfile:
    record: dict
    cache: bytes
    blob: bytes
    binding: str

    def __repr__(self):
        return '<LoadedProfile private account-bound cache; contents redacted>'

    @property
    def player_fields(self):
        v = self.record['player_fields']
        return {'spawn': bytes.fromhex(v['spawn_hex']), 'matrix': bytes.fromhex(v['matrix_hex']),
                'level_flag': v['level_flag']}


def validate_record(record, cache, *, scene, resource_revision, account_uuid, apk_build=BUILD):
    if apk_build != BUILD or record.get('apk_build') != BUILD:
        raise ValueError('cache profile is not for APK build 421589')
    if record.get('schema') != 1 or record.get('kind') != 'sky-native-level-cache':
        raise ValueError('unrecognized cache profile schema')
    if record.get('scene') != scene or record.get('resource_revision') != resource_revision:
        raise ValueError('cache scene/resource revision mismatch; no fallback is permitted')
    if record.get('native_sha256') != NATIVE_SHA256:
        raise ValueError('native build identity mismatch')
    if record.get('classification') != 'private-current-account-session-state':
        raise ValueError('cache must be marked private/current-account')
    obs = record.get('scene_observation', {})
    if obs.get('binding') != 'adjacent-native-init-lines-exact-timestamp-pid-tid':
        raise ValueError('missing exact native scene-log binding')
    expected = account_binding(account_uuid)
    binding = record.get('account_binding', {})
    if binding.get('algorithm') != BINDING_ALGORITHM or binding.get('source') != 'native-join-builder-uid16':
        raise ValueError('missing verified native account binding')
    if not hmac.compare_digest(str(binding.get('value','')), expected):
        raise ValueError('cache belongs to a different account; automatic rebinding is prohibited')
    if len(cache) != 0x2010 or not hmac.compare_digest(hashlib.sha256(cache).hexdigest(),str(record['cache']['sha256'])):
        raise ValueError('cache length/SHA256 mismatch')
    nv = record['netversion_u16']
    if not isinstance(nv,list) or len(nv) != 6:
        raise ValueError('explicit six-element NetVersion is required')
    for v in nv: uint(v,16,'NetVersion')
    if nv[2:] != [23536,0,0,25536]:
        raise ValueError('unexpected build-specific NetVersion invariant')
    uint(record['level_id'],32,'level_id')
    size=struct.unpack_from('<H',cache,0x200e)[0]
    if not 4<=size<=8192: raise ValueError('invalid native cache payload length')
    blob=cache[0xe:0xe+size]
    if struct.unpack_from('<H',cache)[0]!=nv[5] or struct.unpack_from('<I',blob)[0]!=nv[0]:
        raise ValueError('cache size/levelHash does not match selected NetVersion')
    player=record['player_fields']
    octets(bytes.fromhex(player['spawn_hex']),16,'spawn')
    octets(bytes.fromhex(player['matrix_hex']),64,'matrix')
    uint(player['level_flag'],8,'level_flag')
    if obs.get('scene') != scene or obs.get('level_id') != record['level_id'] or obs.get('netversion_u16') != nv:
        raise ValueError('scene observation and cache profile disagree')
    return LoadedProfile(record, bytes(cache), blob, expected)


def load_cache_profile(private_root, record_relative, *, scene, resource_revision, account_uuid, apk_build=BUILD):
    root=Path(private_root).resolve(strict=True)
    if not root.is_dir(): raise ValueError('private_root must be a directory')
    p=inside(root,record_relative)
    if p.stat().st_size>65536: raise ValueError('cache record exceeds size bound')
    record=json.loads(p.read_text(encoding='utf-8-sig'))
    c=inside(root,record['cache']['file'],base=p.parent)
    if c.stat().st_size!=0x2010: raise ValueError('cache file is not 0x2010 bytes')
    return validate_record(record,c.read_bytes(),scene=scene,resource_revision=resource_revision,
                           account_uuid=account_uuid,apk_build=apk_build)


def build_from_cache(profile, explicit_fields, *, frame=True):
    if not isinstance(profile,LoadedProfile): raise TypeError('a validated LoadedProfile is required')
    # uid comes from the current authenticated session, not from the cache record.
    if not hmac.compare_digest(account_binding(explicit_fields['uid']),profile.binding):
        raise ValueError('JoinGame uid differs from cache account binding')
    fields=dict(explicit_fields)
    native={'build':BUILD,'netversion':profile.record['netversion_u16'],
            'level_id':profile.record['level_id'],'blob':profile.blob,
            'state_extra_u32_a':profile.cache[2],
            'state_extra_u32_b':struct.unpack_from('<I',profile.cache,4)[0],
            'tail_u8':profile.cache[8]}
    native.update(profile.player_fields)
    for key,value in native.items():
        if key in fields and fields[key]!=value:
            raise ValueError(f'explicit {key} conflicts with selected native cache profile')
        fields[key]=value
    if fields.get('server_version')!=442 or fields.get('client_version')!=5127:
        raise ValueError('current native wire versions must be explicit 442/5127')
    return encode(fields,frame=frame)
