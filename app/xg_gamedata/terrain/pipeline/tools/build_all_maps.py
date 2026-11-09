"""Version-bound, resumable offline scene builds; no server or device operations."""
from __future__ import annotations
import argparse, atexit, collections, hashlib, json, os, re, subprocess, sys, time, zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
for _p in Path(__file__).resolve().parents:
    if (_p/'xg_exec.py').is_file(): sys.path.insert(0,str(_p)); break
import xg_exec
from skyterrain.cli import atomic,save_json
from skyterrain.catalog import Catalog,scene_hash
from skyterrain.baked_terrain import PROFILE

EMPTY_HASH='fed40a4aa38016626f1a288eb95948e401805973c6fd2e98b08fb80ebe9955f9'

def fingerprint():
    files=sorted((ROOT/'src/skyterrain').rglob('*.py'))+sorted((ROOT/'engine').glob('*.mjs'))+sorted((ROOT/'engine/vendor').glob('*.mjs'))
    return hashlib.sha256(''.join(str(p.relative_to(ROOT))+':'+hashlib.sha256(p.read_bytes()).hexdigest()+'\n' for p in files if not p.name.startswith('test_')).encode()).hexdigest()

def run(cmd,log,timeout,env):
    start=time.monotonic()
    try:
        p=xg_exec.run(cmd,cwd=ROOT,env=env,capture_output=True,timeout=timeout)
        code=p.returncode;data=p.stdout+p.stderr
    except subprocess.TimeoutExpired as e:
        code=124;data=(e.stdout or b'')+(e.stderr or b'')+b'\nTIMEOUT'
    atomic(log,data)
    return {'exit_code':code,'seconds':time.monotonic()-start,'log':str(log.relative_to(log.parent.parent))}

def inventory(apk):
    with zipfile.ZipFile(apk) as z:
        names=z.namelist()
        if len(names)!=len(set(names)):raise ValueError('duplicate_archive_members')
        members=[];rows=[]
        for n in names:
            m=re.fullmatch(r'assets/Data/Levels/([A-Za-z0-9_-]{1,80})/BstBaked\.meshes',n)
            if not m:continue
            info=z.getinfo(n);name=m[1];parent=n.rsplit('/',1)[0]
            for suffix in ('Objects.level.bin','Resources.lua.luac'):
                if parent+'/'+suffix not in names:raise ValueError('missing_scene_member:'+name+':'+suffix)
            row={'scene_name':name,'scene_hash':scene_hash(name),'asset_relpath':None,
                'read_status':'pending','collision_status':'pending','nav_status':'pending',
                'collision_coverage':'initial_static_candidates','native_collision_verified':False,
                'calibration_verified':False,'runtime_ready':False,
                'source_member':n,'source_bytes':info.file_size,'source_crc32':f'{info.CRC:08x}'}
            if name=='WorldEmpty' and info.file_size==149:
                digest=hashlib.sha256(z.read(n)).hexdigest()
                if digest==EMPTY_HASH:
                    row.update(read_status='empty_scene',collision_status='empty_scene',nav_status='empty_scene',
                        reason='hash_bound_LOD_only_empty_scene_no_initial_geometry',source_sha256=digest)
            rows.append(row)
        for info in z.infolist():members.append((info.filename,info.file_size,info.CRC))
    return sorted(rows,key=lambda r:r['scene_name']),hashlib.sha256(json.dumps(sorted(members),separators=(',',':')).encode()).hexdigest()

def valid_ready_files(out,row):
    try:
        paths=[]
        for key in ('asset_relpath','navigation_relpath'):
            value=row[key]
            if not isinstance(value,str) or '\\' in value or ':' in value:return False
            p=Path(os.path.abspath(os.fspath(out/value)))
            if not p.is_relative_to(out) or not p.is_file():return False
            paths.append(p)
        asset=json.loads(paths[0].read_text(encoding='utf-8'))
        if asset.get('mapName')!=row['scene_name'] or asset.get('format')!='skyterrain-static-v1':return False
        for key in ('positions','indices'):
            child=asset[key]
            if not isinstance(child,str) or Path(child).name!=child:return False
            p=Path(os.path.abspath(os.fspath(paths[0].parent/child)))
            if not p.is_relative_to(out) or not p.is_file() or p.stat().st_size>64*1024*1024:return False
            if hashlib.sha256(p.read_bytes()).hexdigest()!=asset['sha256'][key]:return False
        if paths[1].stat().st_size>128*1024*1024:return False
        return hashlib.sha256(paths[1].read_bytes()).hexdigest()==row['navigation_sha256']
    except (OSError,ValueError,KeyError,TypeError):return False

def _pid_alive(pid):
    try:
        if os.name=='nt':
            import ctypes
            from ctypes import wintypes
            handle=ctypes.windll.kernel32.OpenProcess(0x1000,False,int(pid))
            if not handle:return False
            code=wintypes.DWORD()
            try:return bool(ctypes.windll.kernel32.GetExitCodeProcess(handle,ctypes.byref(code))) and code.value==259
            finally:ctypes.windll.kernel32.CloseHandle(handle)
        os.kill(int(pid),0);return True
    except (OSError,ValueError,TypeError):
        return False

def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--apk',required=True,type=Path);ap.add_argument('--out',required=True,type=Path)
    ap.add_argument('--node',required=True);ap.add_argument('--maps',nargs='+')
    ap.add_argument('--timeout',type=int,default=90);ap.add_argument('--resume',action='store_true')
    ap.add_argument('--retry-failed',action='store_true')
    ap.add_argument('--refresh-navigation',action='store_true',help='Rebuild navigation; retain hash-checked existing geometry and its old builder identity')
    ap.add_argument('--navigation-dir',default='navigation')
    a=ap.parse_args(argv)
    if not 10<=a.timeout<=600:raise ValueError('timeout_budget')
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}',a.navigation_dir):raise ValueError('navigation_dir')
    # W5：不要 resolve（会把 8.3 短路径展开成 MAX_PATH 以外的长路径）。
    out=Path(os.path.abspath(os.fspath(a.out)));out.mkdir(parents=True,exist_ok=True)
    lockpath=out/'.build.lock'
    if lockpath.exists():
        try: prior=int(lockpath.read_text(encoding='ascii').strip())
        except (OSError,ValueError): prior=None
        if prior is None or not _pid_alive(prior):
            # A force-killed run can leave the lock behind; only reclaim it when
            # the recorded process is provably gone.
            lockpath.unlink()
        else:
            raise ValueError('batch_lock_present_verify_previous_process_before_recovery')
    try:lock=lockpath.open('x',encoding='ascii')
    except FileExistsError:raise ValueError('batch_lock_present_verify_previous_process_before_recovery')
    lock.write(str(os.getpid()));lock.flush()
    def unlock():
        lock.close()
        if lockpath.exists() and lockpath.read_text(encoding='ascii')==str(os.getpid()):lockpath.unlink()
    atexit.register(unlock)
    rows,apk_id=inventory(a.apk);sig=fingerprint();catalog_file=out/'catalog.json'
    data={'format':'skyterrain-catalog-v1','profile':PROFILE,'archive_directory_sha256':apk_id,
        'implementation_sha256':sig,'asset_root':str(out),'scene_count':len(rows),'entries':rows,
        'coverage_meaning':'Ready means offline construction only. Dynamic state and client calibration remain unverified.'}
    if catalog_file.exists():
        if not a.resume:raise ValueError('existing_catalog_requires_resume')
        old=json.loads(catalog_file.read_text(encoding='utf-8'))
        if old.get('archive_directory_sha256')!=apk_id or (old.get('implementation_sha256')!=sig and not a.refresh_navigation):
            raise ValueError('resume_source_changed_use_new_output')
        previous=Catalog.from_file(catalog_file)
        for i,row in enumerate(rows):
            saved=previous.resolve(row['scene_hash'],row['scene_name'])
            if saved is None:raise ValueError('resume_inventory_changed')
            saved.setdefault('build_implementation_sha256',old.get('implementation_sha256'))
            if a.refresh_navigation and saved['collision_status']=='ready':
                saved.update(nav_status='pending',reason='explicit_navigation_refresh')
                saved.pop('navigation_relpath',None);saved.pop('navigation_sha256',None)
            rows[i]=saved
    selected=set(a.maps or [r['scene_name'] for r in rows])
    if selected-{r['scene_name'] for r in rows}:raise ValueError('unknown_map_selection')
    # W5：research/ 是开发机证据，不随包；没有它时走完整现场解析（只是慢一点）。
    audit_path=ROOT/'research/formats/terrain-full-audit-v2.json'
    audit=json.loads(audit_path.read_text(encoding='utf-8')) if audit_path.is_file() else {'rows':[]}
    audited={r['member']:r for r in audit['rows']}
    env=dict(os.environ,PYTHONPATH=str(ROOT/'src'),PYTHONDONTWRITEBYTECODE='1')
    def checkpoint():
        Catalog(rows)
        data['summary']={axis:dict(collections.Counter(r[axis] for r in rows)) for axis in ('read_status','collision_status','nav_status')}
        save_json(catalog_file,data)
    checkpoint()
    for index,row in enumerate(rows):
        name=row['scene_name']
        if name not in selected or row['read_status']=='empty_scene':continue
        if row['nav_status']=='ready':
            if valid_ready_files(out,row):continue
            row.update(collision_status='pending',nav_status='pending',asset_relpath=None,reason='resume_artifact_missing_or_corrupt')
            row.pop('navigation_relpath',None);row.pop('navigation_sha256',None)
        if not a.retry_failed and row['nav_status'] in ('failed','unavailable','unsupported'):continue
        dest=out/name;dest.mkdir(exist_ok=True)
        if row['collision_status']!='ready':
            cached=audited.get(row['source_member'],{})
            if cached.get('status') in ('parsed','parsed_cached') and cached.get('bytes')==row['source_bytes'] and cached.get('crc32')==row['source_crc32']:
                row.update(read_status='ready',read_evidence='previous_complete_decoder_audit')
            result=run([sys.executable,'-X','utf8','-B',str(ROOT/'tools/build_map.py'),'--apk',str(a.apk.resolve()),
                '--map',name,'--out',str(dest),'--node',a.node,'--profile',PROFILE],dest/'build.log',a.timeout,env)
            row['build_run']=result
            if result['exit_code']!=0:
                row.update(collision_status='failed',nav_status='unavailable',reason='map_build_timeout' if result['exit_code']==124 else 'map_build_failed')
                checkpoint();print(json.dumps({'scene':name,'index':index+1,'total':len(rows),'status':row['reason']}),flush=True);continue
            try:
                report=json.loads((dest/'build.json').read_text(encoding='utf-8'))
                if not isinstance(report.get('excludedInstances'),int) or not isinstance(report.get('meshVersion'),int):raise ValueError('build_report_schema')
            except (OSError,ValueError,KeyError,TypeError):
                row.update(collision_status='failed',nav_status='unavailable',reason='map_build_report_invalid')
                checkpoint();print(json.dumps({'scene':name,'status':row['reason']}),flush=True);continue
            row.update(read_status='ready',read_evidence='current_map_build',collision_status='ready',
                build_implementation_sha256=sig,
                asset_relpath=f'{name}/static-world/runtime-manifest.json',
                excluded_instances=report['excludedInstances'],initial_exclusion_reasons=report.get('initialExclusionReasons',{}),
                scene_classes=report.get('sceneClassCounts',{}),mesh_version=report['meshVersion'])
            checkpoint()
        asset_file=out/row['asset_relpath']
        try:
            asset_raw=asset_file.read_bytes()
            if len(asset_raw)>1024*1024:raise ValueError('runtime_manifest_budget')
            manifest=json.loads(asset_raw)
            row['asset_manifest_sha256']=hashlib.sha256(asset_raw).hexdigest()
            if manifest.get('vertexCount')==0 and manifest.get('triangleCount')==0:
                row.update(collision_status='empty_scene',nav_status='empty_scene',reason='no_initial_static_collision_geometry')
                checkpoint();print(json.dumps({'scene':name,'status':row['reason']}),flush=True);continue
        except (OSError,ValueError,TypeError):pass # Worker independently rejects missing or malformed assets.
        nav_dir=dest/a.navigation_dir
        result=run([a.node,str(ROOT/'engine/build_nav_asset.mjs'),'--asset',str(asset_file),
            '--out',str(nav_dir)],dest/(a.navigation_dir+'.log'),a.timeout,env)
        row['nav_run']=result
        if result['exit_code']==0:
            try:
                report=json.loads((nav_dir/'nav-build.json').read_text(encoding='utf-8'))
                if report.get('status')!='built_offline':raise ValueError('nav_exit_status_mismatch')
                if report.get('mapName')!=name or report.get('inputHashes',{}).get('manifest')!=row.get('asset_manifest_sha256'):
                    raise ValueError('nav_scene_or_asset_identity_mismatch')
                for key in ('navigationFile','navigationHash','navVertices','navTriangles'):report[key]
            except (OSError,ValueError,KeyError,TypeError):
                row.update(nav_status='unavailable',reason='navigation_report_invalid')
                checkpoint();print(json.dumps({'scene':name,'status':row['reason']}),flush=True);continue
            row.update(nav_status='ready',reason=None,navigation_relpath=f"{name}/{a.navigation_dir}/{report['navigationFile']}",
                navigation_implementation_sha256=sig,
                navigation_sha256=report['navigationHash'],nav_vertices=report['navVertices'],nav_triangles=report['navTriangles'],tile_stats=report.get('tileStats'))
        else:
            report_file=nav_dir/'nav-build.json'
            try:report=json.loads(report_file.read_text(encoding='utf-8')) if report_file.exists() and result['exit_code']!=124 else {}
            except (OSError,ValueError,TypeError):report={'reason':'navigation_report_invalid'}
            row.update(nav_status='unavailable',reason=report.get('reason','navigation_timeout' if result['exit_code']==124 else 'navigation_build_failed'))
            row.pop('navigation_relpath',None);row.pop('navigation_sha256',None)
        checkpoint();print(json.dumps({'scene':name,'index':index+1,'total':len(rows),'read':row['read_status'],'collision':row['collision_status'],'nav':row['nav_status'],'reason':row['reason']}),flush=True)
    print(json.dumps({'catalog':str(catalog_file),'summary':data['summary']}),flush=True)

if __name__=='__main__':main()
