"""Offline-only cold factory binding a scene to bounded, hash-verified local assets.

No process is started here. Pass this callable to TerrainSession(..., mode='offline').
foot_offset is mandatory caller evidence/test input, NOT a calibration assertion.
Default behavior is unchanged. The explicit preserving_imported_nav profile also
binds a staged_navigation report and imported binary; missing pins never fall back.
Only cold load/prepare paths perform filesystem verification.
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path,PurePosixPath
from collections.abc import Mapping
from numbers import Real
import copy,hashlib,json,math,os,re,stat
from .backend import JsonlWorkerTransport,WorkerBackend,WorkerError,WORKER,STAGED_WORKER,STAGED_PROFILE
from .integration import Decision
from .catalog import scene_hash
from .session import SceneScope

class BindingError(ValueError):
    pass

@dataclass(frozen=True)
class BindingLimits:
    manifest_bytes:int=1024*1024
    positions_bytes:int=24_000_000
    indices_bytes:int=36_000_000
    evidence_bytes:int=32*1024*1024
    total_bytes:int=96*1024*1024
    chunk_bytes:int=1024*1024
    navigation_report_bytes:int=1024*1024
    navigation_bytes:int=128*1024*1024
    def __post_init__(self):
        caps={'manifest_bytes':1024*1024,'positions_bytes':24_000_000,'indices_bytes':36_000_000,
              'evidence_bytes':32*1024*1024,'total_bytes':96*1024*1024,'chunk_bytes':1024*1024,
              'navigation_report_bytes':1024*1024,'navigation_bytes':128*1024*1024}
        for key,cap in caps.items():
            if type(getattr(self,key)) is not int or not 0<getattr(self,key)<=cap:raise BindingError('binding_budget')

def _relative(value,*,basename=False):
    if not isinstance(value,str) or not 0<len(value)<=256 or '\\' in value or ':' in value or any(ord(c)<32 for c in value):raise BindingError('asset_path')
    p=PurePosixPath(value)
    if p.is_absolute() or value!=p.as_posix() or any(part in ('','.','..') for part in value.split('/')):raise BindingError('asset_path')
    if basename and len(p.parts)!=1:raise BindingError('asset_child_path')
    return value

def _sha(value):
    if not isinstance(value,str) or re.fullmatch('[0-9a-f]{64}',value) is None:raise BindingError('asset_hash_format')
    return value

def _json(raw):
    def pairs(items):
        out={}
        for key,value in items:
            if key in out:raise BindingError('duplicate_json_key')
            out[key]=value
        return out
    def constant(_):raise BindingError('nonfinite_json')
    try:value=json.loads(raw,object_pairs_hook=pairs,parse_constant=constant)
    except BindingError:raise
    except (ValueError,RecursionError,UnicodeError) as exc:raise BindingError('asset_json') from exc
    if not isinstance(value,dict):raise BindingError('asset_json_object')
    return value

_ON_DEMAND_DEFAULTS={'radius': 0.25, 'height': 1.5, 'skin': 0.015, 'stepHeight': 0.3, 'slopeDegrees': 42, 'snapGround': 0.25, 'gravity': 18, 'maxFallSpeed': 30, 'fixedDt': 0.016666666666666666, 'maxElapsed': 0.25, 'speed': 3.5, 'goalTolerance': 0.12, 'maxProjection': 0.4, 'cellSize': 0.15, 'cellHeight': 0.075}

_WINDOW_KEYS=('minX','maxX','minZ','maxZ')
def _window(value):
    """Snapshot the explicit worker window; same numeric bounds as core.mjs."""
    if value is None:return None
    if not isinstance(value,Mapping) or set(value)!=set(_WINDOW_KEYS):raise BindingError('navigation_window_invalid')
    values=tuple(value[key] for key in _WINDOW_KEYS)
    if any(isinstance(x,bool) or not isinstance(x,Real) or abs(x)>=1e6 or not math.isfinite(x) for x in values):raise BindingError('navigation_window_invalid')
    minx,maxx,minz,maxz=values
    if minx>=maxx or minz>=maxz or maxx-minx>512 or maxz-minz>512:raise BindingError('navigation_window_invalid')
    return tuple(float(x) for x in values)

def _window_dict(value):
    return None if value is None else dict(zip(_WINDOW_KEYS,value))

class _Reader:
    def __init__(self,root,limits):self.root,self.limits,self.used=root,limits,0
    def resolve(self,path):
        try:
            resolved=path.resolve(strict=True)
            relative=resolved.relative_to(self.root)
        except ValueError as exc:raise BindingError('asset_root_escape') from exc
        except OSError as exc:raise BindingError('asset_io') from exc
        if not relative.parts:raise BindingError('asset_is_root')
        return resolved
    def read(self,path,limit,*,expected,retain=False):
        resolved=self.resolve(path);expected=_sha(expected)
        try:
            if not stat.S_ISREG(resolved.stat().st_mode):raise BindingError('asset_not_file')
            with resolved.open('rb') as stream:
                before=stream.fileno()
                initial=os.fstat(before)
                if not stat.S_ISREG(initial.st_mode):raise BindingError('asset_not_file')
                if initial.st_size>limit or self.used+initial.st_size>self.limits.total_bytes:raise BindingError('asset_byte_budget')
                sha=hashlib.sha256();parts=[];count=0
                while block:=stream.read(min(self.limits.chunk_bytes,limit-count+1)):
                    count+=len(block);self.used+=len(block)
                    if count>limit or self.used>self.limits.total_bytes:raise BindingError('asset_byte_budget')
                    sha.update(block)
                    if retain:parts.append(block)
                final=os.fstat(before)
            after=self.resolve(path)
            current=after.stat()
            sig=lambda st:(st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns)
            if after!=resolved or count!=initial.st_size or sig(initial)!=sig(final) or sig(final)!=sig(current):raise BindingError('asset_changed_during_read')
        except BindingError:raise
        except OSError as exc:raise BindingError('asset_io') from exc
        if sha.hexdigest()!=expected:raise BindingError('asset_hash_mismatch')
        return b''.join(parts) if retain else count

@dataclass(frozen=True)
class _Binding:
    root:Path
    row:dict
    scope:SceneScope
    limits:BindingLimits
    navigation_window:tuple|None=None
    worker_profile:str='default'
    def verify(self):
        row=self.row;scope=self.scope
        if type(row.get('scene_hash')) is not int or row['scene_hash']!=scope.scene_hash or row.get('scene_name')!=scope.scene_name or scene_hash(scope.scene_name)!=scope.scene_hash:
            raise BindingError('scene_identity_mismatch')
        if any(row.get(k)!='ready' for k in ('read_status','collision_status','nav_status')):raise BindingError('asset_not_ready')
        coverage=row.get('navigation_coverage')
        row_window=_window(row.get('navigation_window'))
        if self.navigation_window is None:
            if coverage not in (None,'full_static_candidate','legacy_static_candidate','on_demand_static_candidate') or row_window is not None:raise BindingError('navigation_window_binding_mismatch')
            if coverage=='on_demand_static_candidate' and self.worker_profile!=STAGED_PROFILE:raise BindingError('on_demand_staged_profile_required')
        elif coverage!='bounded_window' or row_window!=self.navigation_window:
            raise BindingError('navigation_window_binding_mismatch')
        for key in ('native_collision_verified','calibration_verified','runtime_ready'):
            if key in row and type(row[key]) is not bool:raise BindingError('verification_flag')
        manifest=_relative(row.get('asset_relpath'));pin=_sha(row.get('asset_manifest_sha256'))
        reader=_Reader(self.root,self.limits);file=self.root/manifest
        raw=reader.read(file,self.limits.manifest_bytes,expected=pin,retain=True);m=_json(raw)
        if m.get('format')!='skyterrain-static-v1' or m.get('mapName')!=scope.scene_name:raise BindingError('manifest_scene_mismatch')
        if m.get('geometryKind') not in ('initial_static_collision_candidates','synthetic_fixture','verified_static_collision','offline_beamo_augmented_candidates'):raise BindingError('manifest_geometry_kind')
        if type(m.get('nativeCollisionVerified')) is not bool or type(m.get('calibrationVerified',False)) is not bool:raise BindingError('manifest_verification_flag')
        if m.get('geometryKind')=='offline_beamo_augmented_candidates' and (
            m.get('nativeCollisionVerified') is not False or m.get('calibrationVerified') is not False):
            raise BindingError('beamo_candidate_requires_unverified_flags')
        for field,key,cap in (('vertexCount','positions',self.limits.positions_bytes),('triangleCount','indices',self.limits.indices_bytes)):
            count=m.get(field)
            if type(count) is not int or not 0<count<=cap//12:raise BindingError('geometry_count_budget')
            filename=_relative(m.get(key),basename=True)
            hashes=m.get('sha256')
            if not isinstance(hashes,dict):raise BindingError('geometry_hashes')
            length=reader.read(file.parent/filename,cap,expected=hashes.get(key))
            if length!=count*12:raise BindingError('geometry_count_mismatch')
        evidence=m.get('evidenceManifest')
        if not isinstance(evidence,dict):raise BindingError('source_evidence_required')
        ep=file.parent/_relative(evidence.get('file'),basename=True)
        e=_json(reader.read(ep,self.limits.evidence_bytes,expected=evidence.get('sha256'),retain=True))
        # A self-consistent hash of another map is not evidence for this map.
        for key in ('format','mapName','geometryKind','nativeCollisionVerified','positions','indices','vertexCount','triangleCount','sha256'):
            if e.get(key)!=m.get(key):raise BindingError('source_evidence_identity_mismatch')
        result={'scene_name':scope.scene_name,'scene_hash':scope.scene_hash,'asset_relpath':manifest,
                'asset_manifest_sha256':pin,'asset_digests':{'manifest':pin,'positions':m['sha256']['positions'],'indices':m['sha256']['indices']},'verified_bytes':reader.used,'offline_only':True,
                'navigation_coverage':'bounded_window' if self.navigation_window is not None else coverage or 'full_static_candidate',
                'navigation_window':_window_dict(self.navigation_window)}
        if self.worker_profile==STAGED_PROFILE:
            descriptor=row.get('staged_navigation')
            if not isinstance(descriptor,Mapping) or set(descriptor)!= {'report_relpath','report_sha256','navigation_sha256'}:
                raise BindingError('staged_navigation_required')
            report_path=_relative(descriptor['report_relpath'])
            report_sha=_sha(descriptor['report_sha256']);navigation_sha=_sha(descriptor['navigation_sha256'])
            nav_file=self.root/report_path
            nav=_json(reader.read(nav_file,self.limits.navigation_report_bytes,expected=report_sha,retain=True))
            if nav.get('format')=='skyterrain-navigation-on-demand-v1':
                if coverage!='on_demand_static_candidate' or self.navigation_window is not None:
                    raise BindingError('on_demand_coverage_binding')
                if nav.get('mode')!='offline' or nav.get('status')!='on_demand_static_candidate':
                    raise BindingError('on_demand_schema')
                if (nav.get('mapName')!=scope.scene_name or nav.get('geometryKind')!=m['geometryKind']
                        or m['geometryKind'] not in ('offline_beamo_augmented_candidates','initial_static_collision_candidates')):
                    raise BindingError('on_demand_identity')
                if nav.get('navigationCoverage')!='on_demand_static_candidate' or nav.get('navigationWindow','missing') is not None:
                    raise BindingError('on_demand_window')
                if any(nav.get(k) is not False for k in ('nativeCollisionVerified','calibrationVerified','runtimeReady')):
                    raise BindingError('on_demand_unverified_flags')
                if nav.get('inputHashes')!=result['asset_digests']:
                    raise BindingError('on_demand_asset_binding')
                if any(type(nav.get(k)) is not int or nav[k]!=v for k,v in
                       (('inputVertices',m['vertexCount']),('inputTriangles',m['triangleCount']))):
                    raise BindingError('on_demand_geometry_count')
                if nav.get('policy')!={'windowMeters':64,'gridMeters':32,'cacheEntries':3,'maxRouteMeters':8}:
                    raise BindingError('on_demand_policy')
                if not isinstance(nav.get('config'),dict) or nav['config']!=_ON_DEMAND_DEFAULTS or any(
                        isinstance(v,bool) or not isinstance(v,Real) or not math.isfinite(v) for v in nav['config'].values()):
                    raise BindingError('on_demand_config')
                # This pin deliberately identifies the descriptor, NOT a built nav binary.
                if navigation_sha!=report_sha:raise BindingError('on_demand_descriptor_pin')
                result.update(worker_profile=STAGED_PROFILE,navigation_report_relpath=report_path,
                              navigation_report_sha256=report_sha,navigation_sha256=report_sha,
                              navigation_kind='on_demand',navigation_identity_kind='descriptor_sha256',
                              verified_bytes=reader.used)
                return result
            if coverage=='on_demand_static_candidate':raise BindingError('on_demand_descriptor_required')
            if (nav.get('format')!='skyterrain-navigation-build-v1' or nav.get('mode')!='offline'
                    or nav.get('status')!='built_offline'):raise BindingError('staged_navigation_schema')
            if nav.get('mapName')!=scope.scene_name or nav.get('geometryKind')!=m['geometryKind']:
                raise BindingError('staged_navigation_identity')
            if any(nav.get(key) is not False for key in ('nativeCollisionVerified','calibrationVerified','runtimeReady')):
                raise BindingError('staged_navigation_offline_flags')
            if _window(nav.get('navigationWindow'))!=self.navigation_window:
                raise BindingError('staged_navigation_window_mismatch')
            if self.navigation_window is not None:
                if nav.get('navigationCoverage')!='bounded_window':raise BindingError('staged_navigation_coverage')
            elif nav.get('navigationCoverage') not in (None,'full_static_candidate','legacy_static_candidate'):
                raise BindingError('staged_navigation_coverage')
            if not isinstance(nav.get('inputHashes'),dict) or any(nav['inputHashes'].get(k)!=v for k,v in result['asset_digests'].items()):
                raise BindingError('staged_navigation_asset_binding')
            for key,expected in (('inputVertices',m['vertexCount']),('inputTriangles',m['triangleCount'])):
                if type(nav.get(key)) is not int or nav[key]!=expected:raise BindingError('staged_navigation_geometry_count')
            for key in ('navVertices','navTriangles'):
                if type(nav.get(key)) is not int or nav[key]<=0:raise BindingError('staged_navigation_count')
            if nav.get('navigationHash')!=navigation_sha:raise BindingError('staged_navigation_hash_binding')
            count=nav.get('navigationBytes')
            if type(count) is not int or not 0<count<=self.limits.navigation_bytes:raise BindingError('staged_navigation_byte_budget')
            navigation_file=nav_file.parent/_relative(nav.get('navigationFile'),basename=True)
            actual=reader.read(navigation_file,self.limits.navigation_bytes,expected=navigation_sha)
            if actual!=count:raise BindingError('staged_navigation_size_mismatch')
            result.update(worker_profile=STAGED_PROFILE,navigation_report_relpath=report_path,
                          navigation_report_sha256=report_sha,navigation_sha256=navigation_sha,
                          verified_bytes=reader.used)
        return result

class _BoundTransport(JsonlWorkerTransport):
    def __init__(self,binding,*,node_executable,hop_enabled=False,hop_proof_directory=None):
        super().__init__(binding.root,node_executable=node_executable,worker_profile=binding.worker_profile,hop_enabled=hop_enabled,hop_proof_directory=hop_proof_directory);self._binding=binding
    def request(self,op,**payload):
        if op!='load':return super().request(op,**payload)
        try:
            if payload.get('manifest')!=self._binding.row['asset_relpath']:raise BindingError('load_identity_mismatch')
            if 'navigationWindow' in payload and _window(payload['navigationWindow'])!=self._binding.navigation_window:raise BindingError('load_window_mismatch')
            if self._binding.navigation_window is not None:payload['navigationWindow']=_window_dict(self._binding.navigation_window)
            snapshot=self._binding.verify()
            if 'expectedManifestSHA' in payload and payload['expectedManifestSHA']!=snapshot['asset_manifest_sha256']:raise BindingError('load_digest_mismatch')
            payload['expectedManifestSHA']=snapshot['asset_manifest_sha256']
            staged_keys={'navigationReport':snapshot.get('navigation_report_relpath'),
                         'expectedNavigationReportSHA':snapshot.get('navigation_report_sha256'),
                         'expectedNavigationSHA':snapshot.get('navigation_sha256')}
            if self._binding.worker_profile==STAGED_PROFILE:
                for key,value in staged_keys.items():
                    if key in payload and payload[key]!=value:raise BindingError('staged_load_identity_mismatch')
                    payload[key]=value
            elif any(key in payload for key in staged_keys):
                raise BindingError('staged_profile_required')
        except BindingError as exc:
            self._poison('asset_binding_failed');raise WorkerError('asset_binding_failed') from exc
        reply=super().request(op,**payload)
        if reply.get('status')=='ready':
            try:
                if reply.get('mapName')!=self._binding.scope.scene_name:raise BindingError('worker_scene_mismatch')
                expected_coverage=('on_demand_static_candidate' if snapshot.get('navigation_kind')=='on_demand' else
                                   'bounded_window' if self._binding.navigation_window is not None else 'full_static_candidate')
                if reply.get('navigationCoverage')!=expected_coverage or 'navigationWindow' not in reply or _window(reply['navigationWindow'])!=self._binding.navigation_window:raise BindingError('worker_window_mismatch')
                # Bind to the actual buffers consumed by the worker, not merely
                # to disk snapshots which a transient swap could restore.
                actual=reply.get('assetDigests')
                if not isinstance(actual,dict) or set(actual)!=set(snapshot['asset_digests']):raise BindingError('worker_asset_digests_missing')
                for key,value in snapshot['asset_digests'].items():
                    if _sha(actual[key])!=value:raise BindingError('worker_asset_digest_mismatch')
                if self._binding.worker_profile==STAGED_PROFILE:
                    expected={'workerProfile':STAGED_PROFILE,'navigationSource':'imported','navigationRebuilt':False,
                              'geometryPolicy':'zero-facing-32-retain-short-v1',
                              'navigationReportSHA256':snapshot['navigation_report_sha256'],
                              'navigationSHA256':snapshot['navigation_sha256'],'offline':True,
                              'nativeCollisionVerified':False,'calibrationVerified':False,'runtimeReady':False,
                              'routePreparationRequired':True}
                    if snapshot.get('navigation_kind')=='on_demand':
                        expected.update(navigationSource='on_demand',navigationIdentityKind='descriptor_sha256')
                    for key,value in expected.items():
                        if type(reply.get(key)) is not type(value) or reply[key]!=value:
                            raise BindingError('staged_worker_provenance_mismatch')
                self._binding.verify()
            except BindingError as exc:
                self._poison('asset_binding_failed');raise WorkerError('asset_binding_failed') from exc
        return reply

class BoundWorkerBackend(WorkerBackend):
    """Factory-created backend; rechecks snapshots only on explicit cold prepare."""
    def __init__(self,binding,*,node_executable,foot_offset,hop_enabled=False,hop_proof_directory=None):
        self._binding=binding
        super().__init__(_BoundTransport(binding,node_executable=node_executable,hop_enabled=hop_enabled,hop_proof_directory=hop_proof_directory),binding.row['asset_relpath'],origin_offset=foot_offset)
        # 同一份走路几何在 Python 侧再做一份只读地面查询；冷路径加载，失败就是 None。
        # 不另开资产/解码链：manifest 和两个数组正是 worker load 用的文件。
        self._ground_surface=None
        try:
            from .ground import GroundSurface
            manifest=self._binding.root/self._binding.row['asset_relpath']
            self._ground_surface=GroundSurface.from_manifest(
                manifest,expected_sha256=self._binding.row.get('asset_manifest_sha256'),
                map_name=self._binding.scope.scene_name)
        except Exception:
            self._ground_surface=None

    def terrain_ground(self,x,z,near_y=None):
        """按 (x,z) 查当前场景真实地面；结果已换算成 Actor 原点坐标。

        near_y 是 Actor 原点坐标，用来在双层地面里选层。几何没加载、
        点在网格外或查询出错返回 None（调用方用参考玩家的 y 兜底）。
        """
        surface=getattr(self,'_ground_surface',None)
        if surface is None:return None
        try:
            ox,oy,oz=self.offset
            raw=surface.height(float(x)-ox,float(z)-oz,
                None if near_y is None else float(near_y)-oy)
        except Exception:
            return None
        return None if raw is None else raw+oy
    def configure_map_cold_timeout(self):
        """Cold-only map loading budget; hot operations retain their 50ms cap."""
        snapshot=self._binding.verify()
        if self.transport.worker_profile!=STAGED_PROFILE:raise BindingError('staged_profile_required')
        demand=snapshot.get('navigation_kind')=='on_demand'
        seconds=10. if demand else 5.
        with self.transport._state:
            if self.transport._proc is not None or self.transport._closed or self.transport._failure is not None:
                raise BindingError('map_cold_timeout_requires_unstarted_transport')
            self.transport.timeout=seconds
            self.transport._on_demand_cold_budget=demand
        return seconds
    def binding_snapshot(self):
        """Explicit cold-only verification; never call from the Actor hot path."""
        return copy.deepcopy(self._binding.verify())
    @property
    def bound_scene(self):return self._binding.scope
    @property
    def bound_navigation_window(self):return _window_dict(self._binding.navigation_window)
    def prepare_route(self,scope,start,goal,*,speed,lease=None,follow_intent=None,hop_scope=None,continuation_key=None):
        if self._route_owner_epoch(lease) is None:return Decision('unavailable',reason='route_scheduler_owned')
        if not isinstance(scope,SceneScope) or scope!=self._binding.scope:
            with self._hot_lock() as acquired:
                if not acquired:return Decision('unavailable',reason='backend_busy')
                self._epoch+=1;self._token=None;self._prepared_route=None
            return Decision('unavailable',reason='scene_scope_mismatch')
        return super().prepare_route(scope,start,goal,speed=speed,lease=lease,follow_intent=follow_intent,hop_scope=hop_scope,continuation_key=continuation_key)
    def plan_move(self,scope,start,goal,reason,limits):
        if not isinstance(scope,SceneScope) or scope!=self._binding.scope:
            # Cheap identity rejection only: no disk, process or transport work.
            with self._hot_lock() as acquired:
                if not acquired:return Decision('unavailable',reason='backend_busy')
                self._epoch+=1;self._token=None;self._prepared_route=None
            return Decision('unavailable',reason='scene_scope_mismatch')
        return super().plan_move(scope,start,goal,reason,limits)
    def prepare(self):
        try:self._binding.verify()
        except BindingError:
            self.close();return Decision('unavailable',reason='asset_binding_failed')
        return super().prepare()

class BoundBackendFactory:
    """Callable ``factory(row, SceneScope)`` for offline TerrainSession only.

    Engine must be the frozen backend's fixed engine directory; no alternate
    worker/code injection. Constructor/call validates but never spawns a process.
    A nonzero foot_offset is no more a verification claim than a zero test value.
    Explicit navigation_window requires an already-ready matching local catalog
    row; neither the factory nor the worker promotes a global unavailable row.
    worker_profile='preserving_imported_nav' additionally requires row key
    staged_navigation with report_relpath/report_sha256/navigation_sha256. Its
    imported navigation and preserving geometry remain OFFLINE candidates.
    """
    offline_only=True
    def __init__(self,asset_root,*,engine_root,node_executable,foot_offset,limits=None,navigation_window=None,worker_profile='default',hop_enabled=False,hop_proof_directory=None):
        if worker_profile not in ('default',STAGED_PROFILE):raise BindingError('worker_profile')
        for name,value in (('asset_root',asset_root),('engine_root',engine_root),('node_executable',node_executable)):
            if not Path(value).is_absolute():raise BindingError('absolute_'+name+'_required')
        try:
            root=Path(asset_root).resolve(strict=True);engine=Path(engine_root).resolve(strict=True);node=Path(node_executable).resolve(strict=True)
            fixed=(STAGED_WORKER if worker_profile==STAGED_PROFILE else WORKER).resolve(strict=True)
        except OSError as exc:raise BindingError('binding_path_missing') from exc
        if not root.is_dir():raise BindingError('asset_root_directory')
        if not engine.is_dir() or engine!=fixed.parent or not fixed.is_file():raise BindingError('fixed_engine_required')
        if not node.is_file() or node.name.lower() not in ('node','node.exe'):raise BindingError('node_executable_required')
        if not isinstance(foot_offset,(tuple,list)) or len(foot_offset)!=3 or any(isinstance(x,bool) or not isinstance(x,Real) or not math.isfinite(x) or abs(x)>=1e6 for x in foot_offset):raise BindingError('explicit_finite_foot_offset_required')
        if limits is not None and not isinstance(limits,BindingLimits):raise BindingError('binding_limits_type')
        self.navigation_window=_window(navigation_window)
        self.worker_profile=worker_profile
        self.root,self.engine,self.node,self.offset,self.limits=root,engine,node,tuple(float(x) for x in foot_offset),limits or BindingLimits()
        if type(hop_enabled) is not bool:raise BindingError("hop_enabled_boolean")
        self.hop_enabled,self.hop_proof_directory=hop_enabled,hop_proof_directory
    def __call__(self,row,scope):
        if not isinstance(scope,SceneScope) or not isinstance(row,Mapping):raise BindingError('explicit_scene_identity_required')
        binding=_Binding(self.root,copy.deepcopy(dict(row)),scope,self.limits,self.navigation_window,self.worker_profile)
        binding.verify()
        return BoundWorkerBackend(binding,node_executable=str(self.node),foot_offset=self.offset,hop_enabled=self.hop_enabled,hop_proof_directory=self.hop_proof_directory)
