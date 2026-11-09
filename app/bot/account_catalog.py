"""按账号生成 115 张图的地图目录（asset-catalog），并维护按账号 pin / 源录制。

服务器用法（root 目录）：
  .venv/bin/python bot/account_catalog.py build <账号ID> [--donor <录制目录名>]
  .venv/bin/python bot/account_catalog.py status <账号ID>
  .venv/bin/python bot/account_catalog.py manifest-for <账号ID>

build 会：
  1. 用该号最近一次真录（优先遇境）当 donor，把 5b12 的遇境源 actor 换成该号身份，
     生成固定的合成源录制，登记到 runtime/private/catalog_sources.json；
  2. 以 5b12 的 asset-catalog-v32c 为模板，生成
     release-v20-hop-trial/asset-catalog-acct/<账号ID>/（actor 身份换成该号）；
  3. 在 map_long0167/catalog_account_pins.json 写该号的 pin；
  4. 逐张（含变体）实际调用 load_catalog_trial 自检，失败则整体回滚。

离线测试把 live 复制到临时目录，用 SKY_AI_ROOT 指过去即可（默认值不变）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

import xg_paths

ROOT = xg_paths.DATA
RELEASE = xg_paths.ASSET_RELEASE
SERVER = xg_paths.QFSKY_CODE.parent
BOT = xg_paths.BOT_CODE
CATALOG = RELEASE / 'asset-catalog-v32c'
ACCT_ROOT = RELEASE / 'asset-catalog-acct'
PROFILES = xg_paths.CURRENT_PROFILES
CONFIG = xg_paths.PROFILES_JSON
SOURCES = xg_paths.RUNTIME_PRIVATE / 'catalog_sources.json'
LOCK = xg_paths.CONTROL / 'profiles.lock'
DEFAULT_MANIFEST = CATALOG / 'manifest.json'
HOME = 3526133726
SOURCE_PREFIX = 'CandleSpace_catalog_src_'
_HEX = re.compile('[0-9a-f]{64}')
_UUID = re.compile('[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}|[0-9a-fA-F]{32}')
_NAME = re.compile('[A-Za-z0-9_-]+')
_ACCOUNT_ID = re.compile('[A-Za-z0-9_-]+')
_REQUIRED_FILES = ('cache.bin', 'scene.bin', 'placement.json', 'scene-provenance.json')
_HIDDEN = '0x21dee4c'


def _install_paths():
    # runner 的顺序：release overlay 在前，server 源码在后；bot 最后。
    paths = [str(xg_paths.OVERLAY), str(SERVER), str(BOT)]
    for path in paths:
        while path in sys.path:
            sys.path.remove(path)
    sys.path[:0] = paths


_install_paths()


class CatalogError(ValueError):
    pass


class _CheckFailure(CatalogError):
    def __init__(self, failures):
        self.failures = failures
        super().__init__('self_check_failed')


def _fail(code):
    raise CatalogError(code)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _canonical_bytes(value):
    return _canonical(value).encode('utf-8')


def _pairs(rows):
    out = {}
    for key, value in rows:
        if key in out:
            _fail('duplicate_json_key')
        out[key] = value
    return out


def _read_json(path, limit=8 * 1024 * 1024):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        _fail('json_file_scope')
    try:
        return json.loads(path.read_bytes().decode('utf-8'), object_pairs_hook=_pairs,
                          parse_constant=lambda _: _fail('nonfinite_json'))
    except (UnicodeError, json.JSONDecodeError):
        _fail('json_file_shape')


def _write_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _write_json(path, value):
    _write_bytes(path, _canonical_bytes(value))


class _locked:
    """control/profiles.lock；Linux 用 fcntl，Windows 测试用 msvcrt。"""

    def __enter__(self):
        LOCK.parent.mkdir(parents=True, exist_ok=True)
        self.f = open(LOCK, 'a+')
        try:
            import fcntl
            fcntl.flock(self.f, fcntl.LOCK_EX)
            self.kind = 'fcntl'
        except ImportError:
            import msvcrt
            self.f.seek(0)
            msvcrt.locking(self.f.fileno(), msvcrt.LK_LOCK, 1)
            self.kind = 'msvcrt'
        return self

    def __exit__(self, *exc):
        try:
            if self.kind == 'fcntl':
                import fcntl
                fcntl.flock(self.f, fcntl.LOCK_UN)
            else:
                import msvcrt
                self.f.seek(0)
                msvcrt.locking(self.f.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            self.f.close()


def _prepare_env():
    os.environ.setdefault('QFLY_JOIN_MODE', 'current')
    os.environ.setdefault('QFLY_JOIN_CONFIG', str(CONFIG))
    os.environ.setdefault('QFLY_DATA_DIR', str(xg_paths.RUNTIME_DATA))
    try:
        revision = _config()['resource_revision']
    except Exception:
        revision = 'V75885'
    os.environ.setdefault('QFLY_RESOURCE_REVISION', revision)


def _account(account_id):
    if type(account_id) is not str or _ACCOUNT_ID.fullmatch(account_id) is None:
        _fail('account_id_shape')
    data = _read_json(ROOT / 'runtime/runtime-data/accounts.json')
    if type(data) is not dict or type(data.get('accounts')) is not list:
        _fail('accounts_file_shape')
    for account in data['accounts']:
        if type(account) is not dict:
            continue
        if str(account.get('id')) == account_id:
            uuid = account.get('user_uuid')
            if type(uuid) is not str or _UUID.fullmatch(uuid) is None:
                _fail('account_uuid_shape')
            return {'id': account_id, 'uuid': uuid, 'alias': account.get('alias') or ''}
    _fail('account_not_found')


def _binding(uuid):
    from qfsky.join0167_profile import account_binding
    return account_binding(uuid)


def _config():
    doc = _read_json(CONFIG, 2 * 1024 * 1024)
    if type(doc) is not dict or type(doc.get('maps')) is not dict:
        _fail('join_config_shape')
    if type(doc.get('resource_revision')) is not str:
        _fail('join_config_revision')
    return doc


def _default_manifest():
    raw = DEFAULT_MANIFEST.read_bytes()
    manifest = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs,
                          parse_constant=lambda _: _fail('nonfinite_json'))
    for key in ('source_account_binding_hash', 'source_actor_sha256', 'source_metadata_sha256',
                'source_capture_record_sha256'):
        value = manifest.get(key)
        if type(value) is not str or _HEX.fullmatch(value) is None:
            _fail('default_manifest_pin')
    if type(manifest.get('entries')) is not dict:
        _fail('default_manifest_entries')
    return raw, manifest


def _record_path(name):
    if type(name) is not str or _NAME.fullmatch(name) is None:
        _fail('record_dir_name')
    path = PROFILES / name
    if path.is_symlink() or not path.is_dir():
        _fail('record_dir_missing')
    record = _read_json(path / 'record.json', 65536)
    return path, record


def _is_real(record):
    return type(record) is dict and 'synthesized' not in record


def _candidate_record(doc, name):
    path, record = _record_path(name)
    return {'record_dir': name, 'path': path, 'record': record,
            'captured_utc': str(record.get('captured_utc') or ''),
            'level_id': record.get('level_id'), 'scene': record.get('scene')}


def _template_record_dir(manifest, doc):
    entry = doc['maps'].get(str(HOME)) or {}
    cap = entry.get('current_capture') or {}
    names = []
    for row in (cap.get('by_account') or {}).values():
        if type(row) is dict and row.get('record_dir'):
            names.append(row['record_dir'])
    if cap.get('record_dir'):
        names.append(cap['record_dir'])
    for name in dict.fromkeys(names):
        try:
            item = _candidate_record(doc, name)
            record = item['record']
            path = item['path']
            if (_sha(path.joinpath('record.json').read_bytes()) == manifest['source_capture_record_sha256']
                    and _sha(path.joinpath('actor.bin').read_bytes()) == manifest['source_actor_sha256']
                    and (record.get('account_binding') or {}).get('value') == manifest['source_account_binding_hash']):
                return name
        except (OSError, ValueError, KeyError):
            continue
    _fail('default_source_capture_not_found')


def _real_candidates(doc, uuid, binding):
    found = {}
    for entry in doc['maps'].values():
        if type(entry) is not dict:
            continue
        cap = entry.get('current_capture') or {}
        rows = []
        own = (cap.get('by_account') or {}).get(uuid)
        if type(own) is dict and own.get('record_dir') and not own.get('synthesized'):
            rows.append(own['record_dir'])
        if cap.get('account') == uuid and cap.get('record_dir') and not cap.get('synthesized'):
            rows.append(cap['record_dir'])
        for name in rows:
            if name in found:
                continue
            try:
                item = _candidate_record(doc, name)
            except (OSError, ValueError, KeyError):
                continue
            record = item['record']
            if not _is_real(record):
                continue
            if (record.get('account_binding') or {}).get('value') != binding:
                continue
            found[name] = item
    return sorted(found.values(), key=lambda row: (row['captured_utc'], row['record_dir']), reverse=True)


def _choose_donor(doc, uuid, binding, donor=None):
    if donor is not None:
        if type(donor) is not str or _NAME.fullmatch(donor) is None:
            _fail('donor_name')
        item = _candidate_record(doc, donor)
        if not _is_real(item['record']):
            _fail('donor_not_real_capture')
        if (item['record'].get('account_binding') or {}).get('value') != binding:
            _fail('donor_account_mismatch')
        return item
    rows = _real_candidates(doc, uuid, binding)
    if not rows:
        raise CatalogError('这个号还没真录过图，先用 MuMu 录一张（任何图都行，遇境最好）')
    home = [row for row in rows if row['level_id'] == HOME]
    return (home or rows)[0]


def _source_doc():
    if not SOURCES.exists():
        return {}
    doc = _read_json(SOURCES, 65536)
    if type(doc) is not dict:
        _fail('catalog_sources_shape')
    for uuid, row in doc.items():
        if type(uuid) is not str or _UUID.fullmatch(uuid) is None:
            _fail('catalog_sources_shape')
        if type(row) is not dict or set(row) != {'record_dir', 'netversion_u16'}:
            _fail('catalog_sources_shape')
        if type(row.get('record_dir')) is not str or _NAME.fullmatch(row['record_dir']) is None:
            _fail('catalog_sources_shape')
        nv = row.get('netversion_u16')
        if (type(nv) is not list or len(nv) != 6 or
                any(type(v) is not int or not 0 <= v <= 65535 for v in nv)):
            _fail('catalog_sources_shape')
    return doc


def _registry_doc():
    from qfsky.map_long0167 import catalog_account_pins
    doc = catalog_account_pins.load_registry()
    return {} if doc is None else doc


def _write_registry(doc):
    from qfsky.map_long0167 import catalog_account_pins
    catalog_account_pins.validate_registry(doc)
    _write_bytes(catalog_account_pins.registry_path(), _canonical_bytes(doc))


def _safe_rmtree(path, parent):
    path = Path(path)
    parent = Path(parent).resolve()
    resolved = path.resolve()
    if resolved.parent != parent or not path.name or path.name in ('.', '..'):
        _fail('unsafe_delete')
    shutil.rmtree(path)


def _mkdir(path):
    if path.is_symlink():
        _fail('symlink_not_allowed')
    path.mkdir(parents=True, exist_ok=True)


def _copy_file(src, dst):
    src, dst = Path(src), Path(dst)
    if src.is_symlink() or not src.is_file():
        _fail('source_file_scope')
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    if _sha(src.read_bytes()) != _sha(dst.read_bytes()):
        _fail('copy_verify')


def _group_for(actor):
    from qfsky.join0167_profile import NATIVE_SHA256
    from qfsky.long0167.actor_codec.actor_wire_0167 import inspect_actor_wire
    obs = inspect_actor_wire(actor, verified_profile_sha256=NATIVE_SHA256,
                             check_zero_padding=False)
    starts = []
    bit = 0
    for token in obs._tokens:
        if token.kind == 'raw':
            bit = (bit + 7) & ~7
        starts.append(bit)
        bit += token.extent * 8 if token.kind == 'raw' else token.extent
    offsets = [offset for label, offset in obs._boundaries if label == _HIDDEN]
    if len(offsets) != 1:
        _fail('friend_header_boundary')
    index = next((i for i, start in enumerate(starts) if start >= offsets[0]), None)
    if index is None or index + 2 >= len(obs._tokens):
        _fail('friend_header_boundary')
    first = obs._tokens[index]
    if (first.kind, first.extent) != ('raw', 2):
        _fail('friend_header_shape')
    result = []
    for token in obs._tokens[index:index + 3]:
        result.append((token.kind, token.extent,
                       bytes(token.value) if token.kind == 'raw' else int(token.value)))
    return tuple(result)


def _build_tree(staging, default_manifest, donor_actor, binding, src_sha, src_size,
                record_sha, meta_sha):
    import synth_capture
    entries = {}
    variants = {}
    header_ok = True
    for map_id, row in default_manifest['entries'].items():
        row = dict(row)
        scene = row['scene']
        src_dir = CATALOG / scene
        dst_dir = staging / scene
        if src_dir.is_symlink() or not src_dir.is_dir():
            _fail('catalog_source_folder')
        _mkdir(dst_dir)
        for name in _REQUIRED_FILES:
            _copy_file(src_dir / name, dst_dir / name)
        old_actor = (src_dir / 'actor.bin').read_bytes()
        new_actor = synth_capture.actor_with_identity(old_actor, donor_actor)
        if _group_for(old_actor) != _group_for(new_actor):
            header_ok = False
        (dst_dir / 'actor.bin').write_bytes(new_actor)
        row['actor_sha256'] = _sha(new_actor)
        entries[map_id] = row
        variant_rows = (default_manifest.get('variants') or {}).get(map_id) or {}
        if variant_rows:
            src_variants = src_dir / 'variants'
            dst_variants = dst_dir / 'variants'
            _mkdir(dst_variants)
            new_variant_rows = {}
            for name, variant in variant_rows.items():
                variant = dict(variant)
                src_vdir = src_variants / name
                dst_vdir = dst_variants / name
                if src_vdir.is_symlink() or not src_vdir.is_dir():
                    _fail('catalog_variant_folder')
                _mkdir(dst_vdir)
                for child in sorted(src_vdir.iterdir()):
                    if child.name == 'actor.bin':
                        continue
                    if child.is_symlink() or not child.is_file():
                        _fail('catalog_variant_file')
                    _copy_file(child, dst_vdir / child.name)
                old_v_actor = (src_vdir / 'actor.bin').read_bytes()
                new_v_actor = synth_capture.actor_with_identity(old_v_actor, donor_actor)
                if _group_for(old_v_actor) != _group_for(new_v_actor):
                    header_ok = False
                (dst_vdir / 'actor.bin').write_bytes(new_v_actor)
                variant['actor_sha256'] = _sha(new_v_actor)
                new_variant_rows[name] = variant
            variants[map_id] = new_variant_rows
    meta = _read_json(CATALOG / 'source-metadata.json', 65536)
    meta = dict(meta)
    meta['actorSHA256'] = src_sha
    meta['bytes'] = src_size
    meta_bytes = _canonical_bytes(meta)
    if _sha(meta_bytes) != meta_sha:
        _fail('source_metadata_pin')
    _write_bytes(staging / 'source-metadata.json', meta_bytes)
    manifest = dict(default_manifest)
    manifest['source_actor_sha256'] = src_sha
    manifest['source_metadata_sha256'] = meta_sha
    manifest['source_account_binding_hash'] = binding
    manifest['source_capture_record_sha256'] = record_sha
    manifest['entries'] = entries
    manifest['variants'] = variants
    manifest_bytes = _canonical_bytes(manifest)
    _write_bytes(staging / 'manifest.json', manifest_bytes)
    return manifest, manifest_bytes, _sha(_canonical_bytes(entries)), _sha(_canonical_bytes(variants)), header_ok


def _verify_one(uuid, map_id, variant=None):
    from qfsky.asset_catalog_runtime_v32c import load_catalog_trial
    return load_catalog_trial(uuid, map_id, variant=variant)


def _verify_all(uuid, manifest_path, manifest, binding):
    saved = {}
    keys = ('QFSKY_ASSET_CATALOG_TRIAL', 'QFSKY_ASSET_CATALOG_MANIFEST',
            'QFSKY_ASSET_CATALOG_MANIFEST_SHA256', 'QFLY_JOIN_CONFIG',
            'QFLY_RESOURCE_REVISION', 'QFLY_DATA_DIR', 'QFLY_JOIN_MODE')
    for key in keys:
        saved[key] = os.environ.get(key)
    try:
        os.environ.update({
            'QFSKY_ASSET_CATALOG_TRIAL': '1',
            'QFSKY_ASSET_CATALOG_MANIFEST': str(manifest_path),
            'QFSKY_ASSET_CATALOG_MANIFEST_SHA256': _sha(Path(manifest_path).read_bytes()),
            'QFLY_JOIN_CONFIG': str(CONFIG),
            'QFLY_RESOURCE_REVISION': _config()['resource_revision'],
            'QFLY_DATA_DIR': str(ROOT / 'runtime/runtime-data'),
            'QFLY_JOIN_MODE': 'current',
        })
        checked = 0
        variant_checked = 0
        failures = []
        for map_id, entry in manifest['entries'].items():
            try:
                result = _verify_one(uuid, int(map_id))
                inputs, profile, row = result
                if profile.binding != binding:
                    _fail('profile_binding')
                if profile.record['actor_sha256'] != entry['actor_sha256']:
                    _fail('profile_actor_pin')
                if profile.record['source_actor_sha256'] != manifest['source_actor_sha256']:
                    _fail('profile_source_pin')
                if _sha(profile.cache) != entry['cache_sha256']:
                    _fail('profile_cache_pin')
                if _sha(profile.blob) != entry['scene_sha256']:
                    _fail('profile_scene_pin')
                checked += 1
            except Exception as exc:
                failures.append({'map_id': map_id, 'variant': None, 'error': str(exc)[:200]})
                continue
            for variant in sorted((manifest.get('variants') or {}).get(map_id) or {}):
                try:
                    result = _verify_one(uuid, int(map_id), variant)
                    inputs, profile, row = result
                    if profile.binding != binding:
                        _fail('profile_binding')
                    if profile.record['actor_sha256'] != manifest['variants'][map_id][variant]['actor_sha256']:
                        _fail('profile_variant_actor_pin')
                    variant_checked += 1
                except Exception as exc:
                    failures.append({'map_id': map_id, 'variant': variant, 'error': str(exc)[:200]})
        if failures:
            raise _CheckFailure(failures)
        return checked, variant_checked
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _source_name(uuid):
    stamp = time.strftime('%Y%m%d%H%M%S', time.gmtime())
    base = f'{SOURCE_PREFIX}{uuid[:8]}_{stamp}'
    name = base
    offset = 1
    while (PROFILES / name).exists():
        name = f'{base}_{offset}'
        offset += 1
    return name


def build(account_id, donor=None):
    _prepare_env()
    with _locked():
        account = _account(account_id)
        uuid = account['uuid']
        binding = _binding(uuid)
        from qfsky.map_long0167 import catalog_account_pins as _pin_mod
        if binding == _pin_mod.DEFAULT_BINDING:
            _fail('account_uses_builtin_catalog')
        doc = _config()
        _, default_manifest = _default_manifest()
        template = _template_record_dir(default_manifest, doc)
        donor_item = _choose_donor(doc, uuid, binding, donor)
        donor_name = donor_item['record_dir']
        donor_actor = (donor_item['path'] / 'actor.bin').read_bytes()

        sources_before = SOURCES.read_bytes() if SOURCES.exists() else None
        pins_before = _registry_path_bytes()
        sources_doc = _source_doc()
        registry_doc = _registry_doc()
        old_source = (sources_doc.get(uuid) or {}).get('record_dir')

        source_name = _source_name(uuid)
        stamp = time.time_ns()
        staging = ACCT_ROOT / f'.{account_id}.tmp-{os.getpid()}-{stamp}'
        backup = ACCT_ROOT / f'.{account_id}.bak-{os.getpid()}-{stamp}'
        final = ACCT_ROOT / account_id
        report = {'ok': False, 'account': account_id, 'uuid': uuid, 'binding': binding,
                  'donor': donor_name, 'template': template}
        moved_old = False
        published = False
        created_source = False
        try:
            import synth_capture
            created_source = True
            source_name = synth_capture.make(template, donor_name, uuid, source_name, stand=False).name
            source_path = PROFILES / source_name
            source_record_raw = (source_path / 'record.json').read_bytes()
            source_actor = (source_path / 'actor.bin').read_bytes()
            source_record = json.loads(source_record_raw)
            if (source_record.get('account_binding') or {}).get('value') != binding:
                _fail('source_binding')
            if source_record.get('level_id') != HOME:
                _fail('source_level')
            nv = source_record.get('netversion_u16')
            sources_doc[uuid] = {'record_dir': source_name, 'netversion_u16': nv}
            _write_json(SOURCES, sources_doc)

            src_sha = _sha(source_actor)
            meta = _read_json(CATALOG / 'source-metadata.json', 65536)
            meta = dict(meta, actorSHA256=src_sha, bytes=len(source_actor))
            meta_bytes = _canonical_bytes(meta)
            meta_sha = _sha(meta_bytes)

            _mkdir(staging)
            manifest, manifest_bytes, entries_sha, variants_sha, header_ok = _build_tree(
                staging, default_manifest, donor_actor, binding, src_sha, len(source_actor),
                _sha(source_record_raw), meta_sha)
            if not header_ok:
                _fail('friend_header_changed')

            if final.exists():
                if final.is_symlink() or not final.is_dir():
                    _fail('catalog_target_shape')
                final.rename(backup)
                moved_old = True
            staging.rename(final)
            published = True

            registry_doc[binding] = {
                'source_actor_sha256': src_sha,
                'source_metadata_sha256': meta_sha,
                'entries_sha256': entries_sha,
                'variants': manifest['variants'],
            }
            _write_registry(registry_doc)

            checked, variant_checked = _verify_all(uuid, final / 'manifest.json', manifest, binding)
            report.update({
                'ok': True,
                'source_record_dir': source_name,
                'source_actor_sha256': src_sha,
                'source_metadata_sha256': meta_sha,
                'source_capture_record_sha256': _sha(source_record_raw),
                'entries_sha256': entries_sha,
                'variants_sha256': variants_sha,
                'manifest_path': str((final / 'manifest.json').resolve()),
                'manifest_sha256': _sha(manifest_bytes),
                'catalog_dir': str(final.resolve()),
                'maps': checked,
                'variants': variant_checked,
                'checks': {
                    'all_maps_loaded': checked == len(manifest['entries']),
                    'all_variants_loaded': variant_checked == sum(len(row) for row in (manifest.get('variants') or {}).values()),
                    'friend_transfer_header_unchanged': True,
                    'registry_entry': True,
                    'catalog_sources_entry': True,
                    'no_symlinks': all(not p.is_symlink() for p in final.rglob('*')),
                },
            })
            warnings = []
            if moved_old and backup.exists():
                try:
                    _safe_rmtree(backup, ACCT_ROOT)
                except Exception as exc:
                    warnings.append(f'backup: {exc}')
            if old_source and old_source != source_name:
                referenced = any(row.get('record_dir') == old_source for row in sources_doc.values())
                if (not referenced and old_source.startswith(SOURCE_PREFIX)
                        and (PROFILES / old_source).is_dir()):
                    try:
                        _safe_rmtree(PROFILES / old_source, PROFILES)
                    except Exception as exc:
                        warnings.append(f'old_source: {exc}')
            if warnings:
                report['cleanup_warnings'] = warnings
            return report
        except Exception as exc:
            # 回滚：注册表、源清单、目录、合成源录制，全部恢复/删除。
            try:
                if moved_old and backup.exists():
                    if published and final.exists():
                        _safe_rmtree(final, ACCT_ROOT)
                    backup.rename(final)
                elif published and final.exists():
                    _safe_rmtree(final, ACCT_ROOT)
            except Exception:
                pass
            try:
                if staging.exists():
                    _safe_rmtree(staging, ACCT_ROOT)
            except Exception:
                pass
            try:
                if created_source and (PROFILES / source_name).exists():
                    _safe_rmtree(PROFILES / source_name, PROFILES)
            except Exception:
                pass
            try:
                _restore_bytes(SOURCES, sources_before)
            except Exception:
                pass
            try:
                _restore_bytes(_registry_path(), pins_before)
            except Exception:
                pass
            report.update({'ok': False, 'error': str(exc)[:240], 'rolled_back': True})
            if getattr(exc, 'failures', None):
                report['failures'] = exc.failures[:10]
            return report


def _registry_path():
    from qfsky.map_long0167 import catalog_account_pins
    return catalog_account_pins.registry_path()


def _registry_path_bytes():
    path = _registry_path()
    return path.read_bytes() if path.exists() else None


def _restore_bytes(path, data):
    path = Path(path)
    if data is None:
        if path.exists():
            path.unlink()
    else:
        _write_bytes(path, data)


def status(account_id):
    _prepare_env()
    account = _account(account_id)
    uuid = account['uuid']
    binding = _binding(uuid)
    doc = _config()
    rows = _real_candidates(doc, uuid, binding)
    donor = None
    if rows:
        donor = (([row for row in rows if row['level_id'] == HOME] or rows)[0])['record_dir']
    try:
        sources = _source_doc()
    except Exception as exc:
        sources = {'error': str(exc)[:120]}
    try:
        from qfsky.map_long0167 import catalog_account_pins
        pins = catalog_account_pins.load_for_binding(binding)
        registry_error = None
        pin_source = 'registry' if pins is not None else (
            'builtin' if binding == catalog_account_pins.DEFAULT_BINDING else None)
    except Exception as exc:
        pins = None
        registry_error = str(exc)[:120]
        pin_source = None
    if pins is None and pin_source == 'builtin':
        try:
            from qfsky.map_long0167 import asset_bootstrap_v32 as _bootstrap
            from qfsky.map_long0167 import asset_catalog_v32c as _catalog
            pins = {'source_actor_sha256': _bootstrap.SOURCE_SHA,
                    'source_metadata_sha256': _bootstrap.SOURCE_METADATA_SHA,
                    'entries_sha256': _catalog._EXPECTED_ENTRIES_SHA}
        except Exception:
            pins = None
    manifest_path = ACCT_ROOT / account_id / 'manifest.json'
    try:
        _, default_manifest = _default_manifest()
        if binding == default_manifest['source_account_binding_hash']:
            manifest_path = DEFAULT_MANIFEST
    except Exception:
        pass
    manifest_sha = _sha(manifest_path.read_bytes()) if manifest_path.is_file() else None
    synthesized = sorted(p.name for p in PROFILES.iterdir()
                         if p.is_dir() and not p.is_symlink()
                         and p.name.startswith(f'{SOURCE_PREFIX}{uuid[:8]}_'))
    return {
        'ok': True,
        'account': account_id,
        'uuid': uuid,
        'alias': account['alias'],
        'binding': binding,
        'real_recordings': [row['record_dir'] for row in rows],
        'real_recording_count': len(rows),
        'default_donor': donor,
        'synthesized_source_count': len(synthesized),
        'synthesized_sources': synthesized,
        'catalog_sources_entry': (sources.get(uuid) if isinstance(sources, dict) and 'error' not in sources else None),
        'catalog_sources_error': (sources.get('error') if isinstance(sources, dict) else None),
        'catalog_generated': manifest_path.is_file(),
        'catalog_dir': str(manifest_path.parent.resolve()) if manifest_path.is_file() else None,
        'manifest_path': str(manifest_path.resolve()) if manifest_path.is_file() else None,
        'manifest_sha256': manifest_sha,
        'pin_registered': pin_source is not None,
        'pin_source': pin_source,
        'pin_registry_error': registry_error,
        'pin': ({key: pins[key] for key in ('source_actor_sha256', 'source_metadata_sha256', 'entries_sha256')}
                if pins is not None else None),
    }


def manifest_for(account_id):
    _prepare_env()
    account = _account(account_id)
    binding = _binding(account['uuid'])
    # 安装包不带默认目录（asset-catalog-v32c 可能不存在）：缺了就只看本号专属目录，
    # 不能因为默认目录缺失把整个 manifest-for 打挂（否则首页只剩录制的图）。
    try:
        _, default_manifest = _default_manifest()
    except Exception:
        default_manifest = None
    if default_manifest is not None and binding == default_manifest['source_account_binding_hash']:
        return str(DEFAULT_MANIFEST.resolve())
    path = ACCT_ROOT / account_id / 'manifest.json'
    if path.is_file() and not path.is_symlink():
        try:
            manifest = json.loads(path.read_bytes().decode('utf-8'))
        except (UnicodeError, json.JSONDecodeError):
            manifest = None
        if type(manifest) is dict and manifest.get('source_account_binding_hash') == binding:
            return str(path.resolve())
    return ''


def main(argv=None):
    parser = argparse.ArgumentParser(description='按账号生成地图目录')
    sub = parser.add_subparsers(dest='command', required=True)
    p_build = sub.add_parser('build')
    p_build.add_argument('account')
    p_build.add_argument('--donor')
    p_status = sub.add_parser('status')
    p_status.add_argument('account')
    p_manifest = sub.add_parser('manifest-for')
    p_manifest.add_argument('account')
    args = parser.parse_args(argv)
    _prepare_env()
    try:
        if args.command == 'build':
            report = build(args.account, args.donor)
            print(json.dumps(report, ensure_ascii=False, sort_keys=True))
            return 0 if report.get('ok') else 1
        if args.command == 'status':
            print(json.dumps(status(args.account), ensure_ascii=False, sort_keys=True))
            return 0
        path = manifest_for(args.account)
        print(path)
        return 0 if path else 3
    except Exception as exc:
        print(json.dumps({'ok': False, 'error': str(exc)[:240]}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    sys.exit(main())
