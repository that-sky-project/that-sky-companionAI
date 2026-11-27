"""Strict private, account-bound cache loader for APK build 421629.
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

BUILD = 421629
NATIVE_SHA256 = '0b18ecc44e873f6caa181e6bc49f14b7e07058888523e47db52a14b32077a7ed'
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
        raise ValueError('cache profile is not for APK build 421629')
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


# T94（11-22）：中央神坛门开/门关两份场景数据自动选择。
# 变体出生点 rainbowday(彩虹日)/oreo(奥利奥) 在门里，默认“门开”；主条目默认“门关”(小光原缓存)；
# 配对换房(只在门开后出现)强制“门开”；加入被 36 拒绝就翻到另一份，之后沿用直到再被拒。
_T94_OPEN_VARIANTS = frozenset({'rainbowday', 'oreo'})
_T94 = {'pref': None, 'last': None}

def t94_set_dayhub_pref(value):
    if value in ('open', 'closed', None):
        _T94['pref'] = value

def t94_flip_dayhub():
    cur = _T94['pref'] or _T94['last'] or 'closed'
    _T94['pref'] = 'closed' if cur == 'open' else 'open'
    return _T94['pref']

def t94_state():
    return dict(_T94)

def _t94_variant(profile):
    try:
        return object.__getattribute__(profile, '_asset_inputs')._context.variant
    except Exception:
        return None


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
    # T84（11-21）：官方客户端加入中央神坛 DayHubCave 时状态值 a 每次都是 1（六份抓包 13 次），
    # 晨岛等为 0；小光 10-31 录的神坛缓存是 0。仅神坛改 1，SKY_T84_DAYHUB_STATE_A=0 回退。
    if (native['level_id'] == 2394719185 and native['state_extra_u32_a'] == 0
            and __import__('os').environ.get('SKY_T84_DAYHUB_STATE_A', '1') != '0'):
        native['state_extra_u32_a'] = 1
        if fields.get('state_extra_u32_a') == 0:
            fields.pop('state_extra_u32_a')
    # T90（11-21 实验）：小光 10-31 录的神坛场景数据停在“门未开”（时间戳位全 0、开关位缺最高位），
    # 门开后的神坛有人房间一律 36。改用主人号当前神坛场景数据（不含任何身份/会话字节，已核对）。
    # 文件缺失或头 4 字节(nv0)不符则不替换。SKY_T90_DAYHUB_BLOB=0 回退。
    # T93（11-22）：通用化——按地图号查 runtime/private/scene_blobs/<level_id>.bin（官方客户端当前
    # 场景数据，不含身份字节）；神坛另保留旧文件路径兜底。SKY_T90_DAYHUB_BLOB=0 全部关闭。
    _os = __import__('os')
    _dir = _os.environ.get('SKY_T93_SCENE_BLOB_DIR') or _os.path.join(_os.environ.get('XG_DATA_DIR') or _os.environ.get('SKY_AI_ROOT') or '.', 'runtime', 'private', 'scene_blobs')
    _cand = _os.path.join(_dir, '%d.bin' % native['level_id'])
    if native['level_id'] == 2394719185 and not _os.path.exists(_cand):
        _cand = _os.environ.get('SKY_T90_DAYHUB_BLOB_FILE',
            _os.path.join(_os.environ.get('XG_DATA_DIR') or _os.environ.get('SKY_AI_ROOT') or '.', 'runtime', 'private', 't90_dayhub_blob.bin'))
    _t94_want = 'open'
    if (native['level_id'] == 2394719185
            and _os.environ.get('SKY_T94_DAYHUB_AUTO', '1') != '0'):
        _t94_want = _T94['pref'] or ('open' if _t94_variant(profile) in _T94_OPEN_VARIANTS else 'closed')
        _T94['last'] = _t94_want
        if _t94_want == 'closed':
            # 门关：用小光自己的原缓存；显式字段里若带着门开那份/旧值，一律让位原缓存
            if 'blob' in fields:
                fields.pop('blob')
            if 'state_extra_u32_a' in fields and fields['state_extra_u32_a'] in (0, 1):
                fields.pop('state_extra_u32_a')
    if (_t94_want == 'open' and _os.path.exists(_cand)
            and _os.environ.get('SKY_T90_DAYHUB_BLOB', '1') != '0'):
        try:
            _b = open(_cand, 'rb').read()
            if len(_b) >= 4 and _b[:4] == bytes(native['blob'][:4]):
                # T90b：换图时上一轮字段里可能带着旧缓存数据或替换后数据，两者都认，
                # 统一按替换后数据发，避免“explicit blob conflicts”(ValueError)。
                if 'blob' in fields and bytes(fields['blob']) in (bytes(native['blob']), _b):
                    fields.pop('blob')
                if 'state_extra_u32_a' in fields and fields['state_extra_u32_a'] in (0, 1):
                    fields.pop('state_extra_u32_a')
                native['blob'] = _b
                # T97（11-22）：官方客户端门开后每次进房这 3 字节都不同（首字节低 5 位恒 0x1f，
                # 其余位随机；门关时为 df ff 1f）。小光原来每次都发同一值，疑被当重复拒绝。
                if (len(_b) > 267 and _b[264:267] != bytes.fromhex('dfff1f')
                        and _os.environ.get('SKY_T97_RANDOM_264', '0') != '0'
                        and (_os.environ.get('SKY_T97_AB', '1') == '0' or _os.urandom(1)[0] < 128)):  # T97 对照：一半随机一半固定
                    _r = bytearray(_os.urandom(3))
                    _r[0] = (_r[0] & 0xc0) | 0x1f
                    _bb = bytearray(_b); _bb[264:267] = _r
                    native['blob'] = bytes(_bb)
                    if 'blob' in fields:
                        fields.pop('blob')
        except OSError:
            pass
    # T106（11-22）：跑图模式炸过花的房间，场景里暗石已清零；小光拿“花还在”的缓存进房会被 36 拒。
    # 进房前对缓存场景块做同样的清零（scene_edit.apply_zero 只在跑图模式开着且该图有登记时改动）。
    # SKY_T106_JOIN_ZERO=0 回退。
    try:
        if _os.environ.get('SKY_T106_JOIN_ZERO', '1') != '0':
            from qfsky.long0167.scene_edit import apply_zero as _az
            from qfsky.long0167.scene_record import PUBLIC_ELF_SHA256 as _psha
            _old = bytes(native['blob'])
            _nb = _az(_old, map_id=native['level_id'], profile_sha=_psha)
            _changed = _nb is not None and bytes(_nb) != _old
            # T106b：房间里花炸没炸事先不知道——同一张图每次进房交替：先原样，被拒后下一次再用清过花的
            if _changed:
                _tog = globals().setdefault('_T106_TOGGLE', {})
                _n = _tog.get(native['level_id'], 0)
                _tog[native['level_id']] = _n + 1
                if _n % 2 == 0:
                    _changed = False
            try:
                import json as _j, time as _t
                with open(_os.path.join(_os.environ.get('XG_DATA_DIR') or _os.environ.get('SKY_AI_ROOT') or '.', 'logs', 't106_join_zero.log'), 'a', encoding='utf-8') as _f:
                    _f.write(_j.dumps({'ts': _t.strftime('%H:%M:%S'), 'map': native['level_id'], 'changed': _changed, 'size': len(_old)}) + chr(10))
            except Exception:
                pass
            if _changed:
                if 'blob' in fields and bytes(fields['blob']) in (_old, bytes(_nb)):
                    fields.pop('blob')
                native['blob'] = bytes(_nb)
    except Exception:
        pass
    for key,value in native.items():
        if key in fields and fields[key]!=value:
            raise ValueError(f'explicit {key} conflicts with selected native cache profile')
        fields[key]=value
    if fields.get('server_version')!=442 or fields.get('client_version')!=5127:
        raise ValueError('current native wire versions must be explicit 442/5127')
    return encode(fields,frame=frame)
