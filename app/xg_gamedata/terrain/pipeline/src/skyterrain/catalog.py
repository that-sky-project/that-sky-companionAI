"""Exact scene identity registry. Ready means built offline, never native parity."""
from __future__ import annotations
import copy
import json
import re
from pathlib import Path, PurePosixPath

STATUSES = frozenset({'ready','pending','unsupported','failed','unavailable','empty_scene'})

def scene_hash(name: str) -> int:
    value = 2166136261
    for byte in name.encode('utf-8'):
        value = ((value ^ byte) * 16777619) & 0xffffffff
    return value

def _relative(value):
    if value is None:
        return None
    if not isinstance(value,str) or not value or len(value)>512 or '\\' in value or ':' in value:
        raise ValueError('asset_relpath')
    p=PurePosixPath(value)
    if p.is_absolute() or '..' in p.parts or value!=p.as_posix():
        raise ValueError('asset_relpath')
    return value

class Catalog:
    def __init__(self, entries):
        if not isinstance(entries,(list,tuple)) or len(entries)>4096:
            raise ValueError('catalog_entries_budget')
        self._rows={}; names=set(); hashes=set()
        for source in entries:
            if not isinstance(source,dict):raise ValueError('catalog_row')
            row=copy.deepcopy(source); name=row.get('scene_name'); hashed=row.get('scene_hash')
            if not isinstance(name,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',name):raise ValueError('scene_name')
            if type(hashed) is not int or not 0<=hashed<=0xffffffff:raise ValueError('scene_hash')
            if name in names or hashed in hashes:raise ValueError('ambiguous_scene_identity')
            names.add(name);hashes.add(hashed)
            for key in ('read_status','collision_status','nav_status'):
                if row.get(key) not in STATUSES:raise ValueError('catalog_status:'+key)
            row['asset_relpath']=_relative(row.get('asset_relpath'))
            if all(row[k]=='ready' for k in ('read_status','collision_status','nav_status')) and row['asset_relpath'] is None:
                raise ValueError('ready_without_asset')
            for key in ('native_collision_verified','calibration_verified','runtime_ready'):
                row.setdefault(key,False)
                if type(row[key]) is not bool:raise ValueError('catalog_verification_flag')
            if row['runtime_ready'] and not (row['native_collision_verified'] and row['calibration_verified']
                    and all(row[k]=='ready' for k in ('read_status','collision_status','nav_status')) and row['asset_relpath']):
                raise ValueError('unverified_runtime_ready')
            row.setdefault('collision_coverage','initial_static_candidates')
            self._rows[(hashed,name)]=row

    @classmethod
    def from_file(cls, path):
        p=Path(path)
        if not p.is_file() or p.stat().st_size>8*1024*1024:raise ValueError('catalog_file_budget')
        with p.open('rb') as f:raw=f.read(8*1024*1024+1)
        if len(raw)>8*1024*1024:raise ValueError('catalog_file_budget')
        data=json.loads(raw)
        if not isinstance(data,dict) or data.get('format')!='skyterrain-catalog-v1':raise ValueError('catalog_format')
        catalog=cls(data.get('entries'))
        for row in catalog._rows.values():
            if scene_hash(row['scene_name'])!=row['scene_hash']:raise ValueError('catalog_scene_hash_mismatch')
        return catalog

    def resolve(self, scene_hash, scene_name):
        if type(scene_hash) is not int or not isinstance(scene_name,str):return None
        row=self._rows.get((scene_hash,scene_name))
        return copy.deepcopy(row) if row is not None else None

    def resolve_local(self, scene_hash, scene_name, window):
        """Explicitly select an exact prevalidated bounded-window row; never fallback."""
        row=self.resolve(scene_hash,scene_name)
        if row is None or not isinstance(window,dict):return None
        matches=[x for x in row.get('local_navigation_examples',[]) if isinstance(x,dict)
            and x.get('navigation_window')==window and x.get('navigation_coverage')=='bounded_window'
            and x.get('nav_status')=='ready']
        if len(matches)!=1:return None
        row.update(copy.deepcopy(matches[0]));return row

    @property
    def entries(self):
        return tuple(copy.deepcopy(row) for row in self._rows.values())

    def __len__(self):return len(self._rows)
