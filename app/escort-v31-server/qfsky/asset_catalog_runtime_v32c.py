"""Bounded catalogue loader; no network and no changes to native captures.

2026-11-07 T18：同一张图可以有多个命名落点（变体）。变体目录只放
actor.bin / placement.json，scene/cache/scene-provenance 必须沿用主条目；
variant 与 sha 由 manifest.variants（按账号 pin 校验）钉住。默认 variant=None 时
行为与 T17 完全一致。
"""
from pathlib import Path
import copy,os,re
from .asset_bootstrap_runtime_v32 import _read,_json,_source

ENV_PATH='QFSKY_ASSET_CATALOG_MANIFEST'
VARIANT_NAME=re.compile(r'[a-z0-9_]{1,32}')
FILES=('cache.bin','scene.bin','actor.bin','placement.json','scene-provenance.json')
VARIANT_FILES=('actor.bin','placement.json')

def _lp(path):
    """W5：Windows 长路径前缀；非 Windows / 没有 xg_paths 时原样。"""
    try:
        import xg_paths
        return xg_paths.long_path(path)
    except Exception:
        p=os.fspath(path)
        if os.name!='nt' or not p or p.startswith('\\\\?\\'):return p
        p=p.replace('/','\\')
        if not os.path.isabs(p):p=os.path.abspath(p)
        if len(p)<240:return p
        return ('\\\\?\\UNC\\'+p[2:]) if p.startswith('\\\\') else ('\\\\?\\'+p)

def _norm(path):
    """真实路径比较口径：realpath 后去掉 \\\\?\\ 前缀。

    Python 对短路径的 resolve 返回不带前缀，对 >260 的路径返回带前缀；
    不统一就会把同一个目录比成两个。
    """
    value=os.path.normcase(os.path.realpath(_lp(path)))
    if value.startswith('\\\\?\\'):value=value[4:]
    return value

def _scoped_file(folder,name,limit=1024*1024):
    child=folder/name
    try:
        child_l=Path(_lp(child))
        if child_l.is_symlink() or os.path.dirname(_norm(child_l))!=_norm(folder):
            raise ValueError('asset_catalog_file_scope')
    except OSError:raise ValueError('asset_catalog_file_scope')
    return _read(child,limit)

def _variant_folder(folder,variant):
    if type(variant)is not str or VARIANT_NAME.fullmatch(variant)is None:
        raise ValueError('asset_catalog_variant_name')
    base=folder/'variants'
    try:
        base_l=Path(_lp(base))
        if base_l.is_symlink() or os.path.dirname(_norm(base_l))!=_norm(folder):
            raise ValueError('asset_catalog_variant_scope')
        child=base/variant;child_l=Path(_lp(child))
        if child_l.is_symlink() or os.path.dirname(_norm(child_l))!=_norm(base_l):
            raise ValueError('asset_catalog_variant_scope')
    except OSError:raise ValueError('asset_catalog_variant_scope')
    return child

def select_catalog_parts(folder,manifest,map_id,variant=None):
    """Select the five catalog parts for the main entry or a named variant.

    Returns (parts, row, variant_row); row is the unchanged main manifest entry.
    The variant row currently pins actor/placement/annotation sha only.
    """
    folder=Path(folder)
    row=manifest['entries'].get(str(map_id))
    if row is None:return None
    parts={name:_scoped_file(folder,name) for name in FILES}
    vrow=None
    if variant is not None:
        table=manifest.get('variants') or {}
        vrow=(table.get(str(map_id)) or {}).get(variant)
        if type(vrow)is not dict:
            raise ValueError('asset_catalog_variant_not_allowlisted')
        vfolder=_variant_folder(folder,variant)
        for name in VARIANT_FILES:parts[name]=_scoped_file(vfolder,name)
    return parts,row,vrow

def load_catalog_trial(account_uuid,map_id,*,build=421589,variant=None):
    if os.environ.get('QFSKY_ASSET_CATALOG_TRIAL')!='1':
        return None
    from .map_long0167 import asset_catalog_v32c as adapter
    if type(map_id)is not int or type(build)is not int or build!=421589:
        raise ValueError('asset_catalog_map_or_build')
    if variant is not None and(type(variant)is not str or VARIANT_NAME.fullmatch(variant)is None):
        raise ValueError('asset_catalog_variant_name')
    name=os.environ.get(ENV_PATH,'');path=Path(name)
    if not name or not path.is_absolute() or path.is_symlink():
        raise ValueError('asset_catalog_manifest_path')
    path=Path(_lp(path)).resolve(strict=True);raw=_read(path,131072)
    manifest=adapter.validate_catalog_manifest(raw)
    row=manifest['entries'].get(str(map_id))
    if row is None:return None
    folder=path.parent/row['scene']
    folder_l=Path(_lp(folder));parent_l=Path(_lp(path.parent))
    if folder_l.is_symlink() or os.path.dirname(_norm(folder_l))!=_norm(parent_l):
        raise ValueError('asset_catalog_folder_scope')
    selected=select_catalog_parts(folder,manifest,map_id,variant)
    if selected is None:return None
    parts,row,vrow=selected
    meta=_json(_read(path.parent/'source-metadata.json',65536))
    inputs=adapter.build_catalog_inputs(manifest=raw,scene_cache=parts['cache.bin'],
        scene_wire=parts['scene.bin'],destination_actor=parts['actor.bin'],account_uuid=account_uuid,
        source_metadata=meta,placement=_json(parts['placement.json']),
        scene_provenance=_json(parts['scene-provenance.json']),verified_same_account_source=_source(account_uuid),
        map_id=map_id,enabled=True,variant=variant)
    profile=adapter.build_catalog_profile(inputs,account_uuid=account_uuid)
    return inputs,profile,row

def catalog_trial_available(account_uuid,map_id):
    try:return load_catalog_trial(account_uuid,map_id)is not None
    except(OSError,ValueError,KeyError,TypeError,UnicodeError):return False

def prepare_catalog_connection(account,build,map_id,*,decoder,variant=None):
    result=load_catalog_trial(account['user_uuid'],map_id,build=build,variant=variant)
    if result is None:return None
    from .connection0167 import Prepared
    inputs,profile,row=result
    return Prepared(copy.deepcopy(account),profile,decoder,row['scene'],map_id,
                    row['resource_revision'],current_inputs=inputs)
