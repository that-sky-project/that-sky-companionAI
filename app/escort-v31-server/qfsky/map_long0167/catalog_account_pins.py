"""按账号的目录 pin 注册表（catalog_account_pins.json）。

键 = account_binding(uuid)；值 = 这个号的
  source_actor_sha256 / source_metadata_sha256 / entries_sha256 / variants

5b12 的内置 pin 不写进注册表：DEFAULT_BINDING 直接走资产目录里的原常量，
保证默认账号逐字节不变，也让注册表损坏时默认账号不受影响。其它绑定如果
注册表缺失/没有该键，同样回落到原常量；文件存在但形状不对则直接报
ValueError('catalog_pin_registry_shape')，不静默放宽。
"""
from __future__ import annotations
import json
import os
import re
from pathlib import Path

PINS_FILENAME = 'catalog_account_pins.json'
# 默认账号的绑定由安装时写入环境变量 XG_DEFAULT_ACCOUNT_BINDING；代码里不写死任何号的 hash。
# 为空时所有绑定都走注册表/原常量，和原来默认账号的回落路径一致。
DEFAULT_BINDING = os.environ.get('XG_DEFAULT_ACCOUNT_BINDING') or ''
_ROW_KEYS = frozenset(('source_actor_sha256', 'source_metadata_sha256', 'entries_sha256', 'variants'))
_VARIANT_KEYS = frozenset(('scene', 'label', 'actor_sha256', 'placement_sha256', 'annotation_sha256'))
_VARIANT_SHAS = ('actor_sha256', 'placement_sha256', 'annotation_sha256')
_HEX = re.compile('[0-9a-f]{64}')
_VARIANT_NAME = re.compile('[a-z0-9_]{1,32}')
_MAP_ID = re.compile('[1-9][0-9]{0,9}')
_SCENE = re.compile('[A-Za-z0-9_]+')
_MAX_BYTES = 4 * 1024 * 1024


def registry_path():
    override = os.environ.get("XG_CATALOG_PINS_PATH")
    if override:
        return Path(override)
    data = os.environ.get("XG_DATA_DIR")
    if data:
        return Path(data) / "runtime/private/catalog_account_pins.json"
    return Path(__file__).with_name(PINS_FILENAME)


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def _fail():
    raise ValueError('catalog_pin_registry_shape')


def _pin(value):
    if type(value) is not str or _HEX.fullmatch(value) is None:
        _fail()
    return value


def _pairs(rows):
    out = {}
    for key, value in rows:
        if key in out:
            _fail()
        out[key] = value
    return out


def validate_registry(doc):
    """严格校验注册表形状；返回值原样，不修补。"""
    if type(doc) is not dict:
        _fail()
    for binding, row in doc.items():
        if type(binding) is not str or _HEX.fullmatch(binding) is None:
            _fail()
        if type(row) is not dict or set(row) != _ROW_KEYS:
            _fail()
        for key in ('source_actor_sha256', 'source_metadata_sha256', 'entries_sha256'):
            _pin(row.get(key))
        variants = row.get('variants')
        if type(variants) is not dict:
            _fail()
        for map_id, variant_row in variants.items():
            if (type(map_id) is not str or _MAP_ID.fullmatch(map_id) is None
                    or not 0 < int(map_id) <= 0xffffffff):
                _fail()
            if type(variant_row) is not dict or not variant_row:
                _fail()
            for name, value in variant_row.items():
                if type(name) is not str or _VARIANT_NAME.fullmatch(name) is None:
                    _fail()
                if type(value) is not dict or set(value) != _VARIANT_KEYS:
                    _fail()
                if type(value.get('scene')) is not str or _SCENE.fullmatch(value['scene']) is None:
                    _fail()
                if type(value.get('label')) is not str or not value['label']:
                    _fail()
                for key in _VARIANT_SHAS:
                    _pin(value.get(key))
    return doc


def load_registry(path=None):
    """读注册表。文件不存在返回 None；存在但坏了抛 catalog_pin_registry_shape。"""
    path = Path(path) if path is not None else registry_path()
    if path.is_symlink():
        _fail()
    if not path.exists():
        return None
    try:
        if not path.is_file() or path.stat().st_size > _MAX_BYTES:
            _fail()
        raw = path.read_bytes()
    except OSError:
        _fail()
    try:
        doc = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs,
                         parse_constant=lambda _: _fail())
    except (UnicodeError, json.JSONDecodeError):
        _fail()
    return validate_registry(doc)


def load_for_binding(binding):
    """按绑定取 pin；默认账号或没有该键返回 None（由调用方用原常量）。"""
    if type(binding) is not str or _HEX.fullmatch(binding) is None:
        raise ValueError('catalog_pin_registry_binding')
    if binding == DEFAULT_BINDING:
        return None
    doc = load_registry()
    if doc is None:
        return None
    return doc.get(binding)
