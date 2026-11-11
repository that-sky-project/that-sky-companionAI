"""Opt-in asset trial, using the real private same-account stored loader.

Does not change capture configuration, spend resources, or open a connection.
Existing capture-backed destinations retain their original preparation path.
"""
from pathlib import Path
import hashlib
import json
import os
import re

MANIFEST_ENV = 'QFSKY_ASSET_BOOTSTRAP_MANIFEST'
FILES = ('cache.bin', 'scene.bin', 'actor.bin', 'source-metadata.json',
         'scene-provenance.json', 'placement.json')
HOME = 3526133726
NIGHT_ARCHIVE = 2518601
CATALOG_SOURCES = 'catalog_sources.json'


def _lp(path):
    """W5：Windows 长路径前缀；非 Windows / 没有 xg_paths 时原样。"""
    try:
        import xg_paths
        return xg_paths.long_path(path)
    except Exception:
        p = os.fspath(path)
        if os.name != 'nt' or not p or p.startswith('\\\\?\\'):
            return p
        p = p.replace('/', '\\')
        if not os.path.isabs(p):
            p = os.path.abspath(p)
        if len(p) < 240:
            return p
        return ('\\\\?\\UNC\\' + p[2:]) if p.startswith('\\\\') else ('\\\\?\\' + p)


def _read(path, limit):
    path = Path(_lp(path))
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise ValueError('asset_trial_file_extent')
    raw = path.read_bytes()
    if len(raw) > limit:
        raise ValueError('asset_trial_file_extent')
    return raw


def _json(raw):
    def pairs(rows):
        out = {}
        for k, v in rows:
            if k in out:
                raise ValueError('asset_trial_duplicate_key')
            out[k] = v
        return out
    def bad(_):
        raise ValueError('asset_trial_nonfinite_json')
    return json.loads(raw.decode('utf-8'), object_pairs_hook=pairs, parse_constant=bad)


def _catalog_source(root, account_uuid):
    """catalog_sources.json 只覆盖显式列出的 uuid；其余账号走原 by_account 逻辑。"""
    path = Path(root).parent / CATALOG_SOURCES
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
        raise ValueError('asset_trial_catalog_sources_path')
    try:
        doc = _json(path.read_bytes())
    except (OSError, UnicodeError):
        raise ValueError('asset_trial_catalog_sources_extent') from None
    if type(doc) is not dict:
        raise ValueError('asset_trial_catalog_sources_shape')
    row = doc.get(account_uuid)
    if row is None:
        return None
    if type(row) is not dict or set(row) != {'record_dir', 'netversion_u16'}:
        raise ValueError('asset_trial_catalog_sources_row')
    record_dir = row.get('record_dir')
    if type(record_dir) is not str or re.fullmatch('[A-Za-z0-9_-]+', record_dir) is None:
        raise ValueError('asset_trial_catalog_sources_row')
    nv = row.get('netversion_u16')
    if (type(nv) is not list or len(nv) != 6 or
            any(type(v) is not int or not 0 <= v <= 65535 for v in nv)):
        raise ValueError('asset_trial_catalog_sources_row')
    return record_dir, tuple(nv)


def _source(account_uuid):
    from qfsky import current_join
    from qfsky.map_long0167.prepare import prepare_current_capture
    doc, root, revision = current_join.config()
    entry = doc['maps'].get(str(HOME)) or {}
    override = _catalog_source(root, account_uuid)
    if override is not None:
        record_dir, netversion = override
        return prepare_current_capture(root, record_dir,
            expected_account_uuid=account_uuid, expected_scene=entry['scene'],
            expected_resource_revision=revision, expected_level_id=HOME,
            expected_netversion=netversion, apk_build=421629).inputs
    cap = entry.get('current_capture') or {}
    own = (cap.get('by_account') or {}).get(account_uuid)
    if isinstance(own, dict):
        cap = {**cap, **own}
    elif cap.get('account') != account_uuid:
        raise ValueError('asset_trial_source_not_owned')
    return prepare_current_capture(root, cap['record_dir'],
        expected_account_uuid=account_uuid, expected_scene=entry['scene'],
        expected_resource_revision=revision, expected_level_id=HOME,
        expected_netversion=cap['netversion_u16'], apk_build=421629).inputs


def load_asset_trial(account_uuid, map_id, *, build=421629):
    """Return None only when disabled/unlisted. Invalid enabled inputs fail."""
    if os.environ.get('QFSKY_ASSET_BOOTSTRAP_TRIAL') != '1' or map_id != NIGHT_ARCHIVE:
        return None
    if type(map_id) is not int or type(build) is not int or build != 421629:
        raise ValueError('asset_trial_build_or_map')
    from qfsky.map_long0167 import asset_bootstrap_v32 as adapter
    name = os.environ.get(MANIFEST_ENV, '')
    path = Path(name)
    if not name or not path.is_absolute() or path.is_symlink():
        raise ValueError('asset_trial_manifest_path')
    path = path.resolve(strict=True)
    raw = _read(path, 65536)
    # Verify the raw manifest/deadline before touching private source records.
    manifest = adapter._gate(raw)
    parts = {}
    for name in FILES:
        child = path.parent / name
        if child.resolve(strict=True).parent != path.parent:
            raise ValueError('asset_trial_file_scope')
        parts[name] = _read(child, 1024 * 1024)
    meta = _json(parts['source-metadata.json'])
    proof = _json(parts['scene-provenance.json'])
    placement = _json(parts['placement.json'])
    source = _source(account_uuid)
    inputs = adapter.build_asset_bootstrap_inputs(
        manifest=raw, scene_cache=parts['cache.bin'], scene_wire=parts['scene.bin'],
        destination_actor=parts['actor.bin'], account_uuid=account_uuid,
        source_actor_sha256=hashlib.sha256(source.actor_bytes()).hexdigest(),
        source_metadata=meta, placement=placement, scene_provenance=proof,
        verified_same_account_source=source, enabled=True)
    profile = adapter.build_asset_bootstrap_profile(inputs, account_uuid=account_uuid)
    return inputs, profile, manifest


def asset_trial_available(account_uuid, map_id):
    try:
        return load_asset_trial(account_uuid, map_id) is not None
    except (OSError, ValueError, KeyError, TypeError, UnicodeError):
        return False


def prepare_asset_trial_connection(account, build, map_id, *, decoder):
    loaded = load_asset_trial(account['user_uuid'], map_id, build=build)
    if loaded is None:
        return None
    import copy
    from qfsky.connection0167 import Prepared
    inputs, profile, manifest = loaded
    return Prepared(copy.deepcopy(account), profile, decoder, manifest['scene'],
                    map_id, manifest['resource_revision'], current_inputs=inputs)
