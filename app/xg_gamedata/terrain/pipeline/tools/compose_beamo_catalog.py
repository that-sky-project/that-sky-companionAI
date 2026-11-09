"""Compose a NEW offline catalog with navigation bound to the actual new geometry.

Never changes input assets, marks native parity, or reuses stale navigation on a
changed mesh. All paths remain under the explicit common asset root.
"""
from pathlib import Path,PurePosixPath
from collections import Counter
import argparse,hashlib,json,re,sys
import numpy as np
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from skyterrain.catalog import Catalog,scene_hash
from skyterrain.cli import save_json
from skyterrain.mesh_resource_paths import PROFILE

FLAGS=('nativeCollisionVerified','calibrationVerified','runtimeReady',
       'native_collision_verified','calibration_verified','runtime_ready','live_enabled')
def need(value,reason):
    if not value:raise ValueError(reason)
def _abs(path):
    import os
    return Path(os.path.abspath(os.fspath(path)))
def sha(raw):return hashlib.sha256(raw).hexdigest()
def blob(path,maximum):
    path=Path(path);need(path.is_file() and path.stat().st_size<=maximum,'file_budget')
    with path.open('rb') as stream:raw=stream.read(maximum+1)
    need(len(raw)<=maximum,'file_budget');return raw
def _unique(pairs):
    out={}
    for key,value in pairs:
        need(key not in out,'duplicate_json_key');out[key]=value
    return out
def doc(path,maximum=8*1024*1024):
    raw=blob(path,maximum)
    return json.loads(raw,object_pairs_hook=_unique,parse_constant=lambda _:(_ for _ in ()).throw(ValueError('nonfinite_json'))),sha(raw)
def safe_path(root,parent,relative):
    need(type(relative) is str and 0<len(relative)<=512 and '\\' not in relative and ':' not in relative,'relative_path')
    p=PurePosixPath(relative)
    need(not p.is_absolute() and '..' not in p.parts and p.as_posix()==relative,'relative_path')
    # W5：不要 resolve（8.3 短路径会被展开，跟短 root 比较时误判越界）。
    import os
    root=Path(os.path.abspath(os.fspath(root)))
    target=Path(os.path.abspath(os.fspath(Path(parent)/relative)))
    need(target.is_relative_to(root) and target!=root,'asset_path_escape');return target
def false_flags(value):
    for key in FLAGS:
        if key in value:need(value[key] is False,'unverified_flag:'+key)
def scene_rows(rows,key):
    need(type(rows) is list and len(rows)<=256,'scene_rows')
    out={}
    for row in rows:
        name=row.get(key)
        need(type(name) is str and re.fullmatch(r'[A-Za-z0-9_-]{1,80}',name) and name not in out,'scene_identity')
        out[name]=row
    return out


def asset_proof(root,path,name,expected=None):
    m,digest=doc(path,1024*1024);false_flags(m)
    need(m.get('format')=='skyterrain-static-v1' and m.get('mapName')==name,'asset_identity')
    need(m.get('geometryKind') in ('initial_static_collision_candidates','offline_beamo_augmented_candidates'),'asset_geometry_kind')
    if expected is not None:need(digest==expected,'catalog_asset_hash')
    evidence=m.get('evidenceManifest',{})
    p=safe_path(root,path.parent,evidence.get('file'))
    e,eh=doc(p,32*1024*1024);need(eh==evidence.get('sha256'),'evidence_hash');false_flags(e)
    need(e.get('mapName')==name,'evidence_map')
    arrays={};hashes={'manifest':digest}
    for key,count_key,dtype in (('positions','vertexCount','<f4'),('indices','triangleCount','<u4')):
        count=m.get(count_key);need(type(count) is int and 0<count<=4_000_000,'geometry_count')
        child=m.get(key);need(type(child) is str and PurePosixPath(child).name==child,'asset_child_path')
        raw=blob(safe_path(root,path.parent,child),48*1024*1024)
        need(len(raw)==count*12,'geometry_length');h=sha(raw)
        need(h==m.get('sha256',{}).get(key)==e.get('sha256',{}).get(key),'geometry_hash')
        arrays[key]=np.frombuffer(raw,dtype=dtype);hashes[key]=h
    need(np.isfinite(arrays['positions']).all() and np.max(np.abs(arrays['positions']))<2**23,'position_range')
    need(int(arrays['indices'].max())<m['vertexCount'],'index_range')
    return {'hashes':hashes,'mapName':name,'path':path,'vertices':m['vertexCount'],'triangles':m['triangleCount']}


def navigation_proof(root,report_path,asset,expected=None,*,legacy_base=False):
    report,rh=doc(report_path,1024*1024);false_flags(report)
    if expected is not None:need(rh==expected,'navigation_report_hash')
    need(report.get('status')=='built_offline' and report.get('mode')=='offline','navigation_not_built')
    need(report.get('mapName')==asset['mapName'] and report.get('inputHashes')==asset['hashes'],'navigation_geometry_binding')
    need(report.get('inputVertices')==asset['vertices'] and report.get('inputTriangles')==asset['triangles'],'navigation_input_counts')
    for key in ('nativeCollisionVerified','calibrationVerified','runtimeReady'):need(report.get(key) is False,'navigation_flags')
    for key in ('navVertices','navTriangles','navigationBytes'):need(type(report.get(key)) is int and report[key]>0,'navigation_counts')
    filename=report.get('navigationFile');need(type(filename) is str and PurePosixPath(filename).name==filename,'navigation_child_path')
    path=safe_path(root,report_path.parent,filename);raw=blob(path,128*1024*1024)
    need(len(raw)==report['navigationBytes'] and sha(raw)==report.get('navigationHash'),'navigation_bytes_hash')
    window=report.get('navigationWindow')
    if window is not None:
        need(type(window) is dict and set(window)=={'minX','maxX','minZ','maxZ'},'window_keys')
        need(all(type(v) in (int,float) and np.isfinite(v) and abs(v)<1e6 for v in window.values()),'window_values')
        need(0<window['maxX']-window['minX']<=512 and 0<window['maxZ']-window['minZ']<=512,'window_range')
        need(report.get('navigationCoverage')=='bounded_window','window_coverage')
    else:need(report.get('navigationCoverage')=='full_static_candidate' or
              (legacy_base and 'navigationCoverage' not in report and 'navigationWindow' not in report),
              'navigation_coverage')
    return {'nav_status':'ready','navigation_relpath':path.relative_to(root).as_posix(),
        'navigation_sha256':sha(raw),'navigation_report_relpath':report_path.relative_to(root).as_posix(),
        'navigation_report_sha256':rh,'navigation_coverage':report.get('navigationCoverage','legacy_static_candidate'),
        'navigation_window':window,'nav_vertices':report['navVertices'],'nav_triangles':report['navTriangles'],
        'reason':None,'native_collision_verified':False,'calibration_verified':False,'runtime_ready':False}


def compose(base_catalog,world_summary,navigation_summary,assets_root,output,window_reports=()):
    root=_abs(assets_root);out=_abs(output)
    need(root.is_dir() and out.is_relative_to(root) and out!=root and not out.exists(),'new_output_under_assets_root_required')
    inputs=[_abs(x) for x in (base_catalog,world_summary,navigation_summary)]
    need(all(x.is_relative_to(root) for x in inputs),'input_outside_assets_root')
    base,bh=doc(inputs[0]);world,wh=doc(inputs[1]);batch,nh=doc(inputs[2])
    for value in (base,world,batch):need(value.get('profile')==PROFILE,'profile');false_flags(value)
    Catalog(base['entries'])
    base_root=_abs(base['asset_root']);need(base_root==inputs[0].parent,'base_asset_root')
    old=scene_rows(base['entries'],'scene_name');changed=scene_rows(world['rows'],'mapName');nav=scene_rows(batch['rows'],'mapName')
    need(all(row['scene_hash']==scene_hash(name) for name,row in old.items()),'scene_hash_name_mismatch')
    need(set(old)==set(changed) and set(nav)<=set(old),'scene_sets')
    need(batch.get('input_summary_sha256')==wh and batch.get('base_catalog_sha256')==bh,'batch_input_hashes')
    rows=[];proofs={}
    for name,original in old.items():
        source=changed[name];false_flags(source)
        reuse=source.get('status')=='no_beamo'
        need(reuse or source.get('status') in ('built_candidate','partial_candidate'),'invalid_world_build')
        row={'scene_name':name,'scene_hash':original['scene_hash'],'read_status':original['read_status'],
            'collision_status':original['collision_status'],'nav_status':'pending','asset_relpath':None,
            'collision_coverage':'initial_static_candidates' if reuse else 'beamo_augmented_static_candidates',
            'native_collision_verified':False,'calibration_verified':False,'runtime_ready':False,
            'geometry_source':'unchanged_base' if reuse else 'beamo_world',
            'beamo_build_status':source['status'],'beamo_included':source.get('beamo_included',0),
            'beamo_excluded':source.get('beamo_excluded',0),'reason':'navigation_not_rebuilt'}
        if reuse and original['collision_status']!='ready':
            row.update(nav_status=original['nav_status'],reason=original.get('reason','no_collision_geometry'))
            rows.append(row);continue
        path=safe_path(root,base_root,original['asset_relpath']) if reuse else safe_path(root,inputs[1].parent,f'{name}/static-world/runtime-manifest.json')
        asset=asset_proof(root,path,name,original.get('asset_manifest_sha256') if reuse else None);proofs[name]=asset
        row.update(asset_relpath=path.relative_to(root).as_posix(),asset_manifest_sha256=asset['hashes']['manifest'],
                   collision_status='ready',read_status='ready')
        if reuse and original['nav_status']=='ready':
            old_nav=safe_path(root,base_root,original['navigation_relpath'])
            report=safe_path(root,base_root,original['navigation_report_relpath']) if original.get('navigation_report_relpath') else old_nav.parent/'nav-build.json'
            patch=navigation_proof(root,report,asset,original.get('navigation_report_sha256'),legacy_base=True)
            need(patch['navigation_window'] is None,'base_full_build_has_window')
            need(patch['navigation_sha256']==original.get('navigation_sha256') and
                patch['navigation_relpath']==old_nav.relative_to(root).as_posix(),'old_navigation_binding')
            row.update(patch)
        elif reuse:
            row.update(nav_status=original['nav_status'],reason=original.get('reason'))
        elif name in nav:
            item=nav[name];false_flags(item)
            if item['status']=='built_offline':
                report=safe_path(root,inputs[2].parent,item['nav_report_relpath'])
                patch=navigation_proof(root,report,asset,item['nav_report_sha256'])
                need(patch['navigation_window'] is None,'full_build_has_window');row.update(patch)
            else:row.update(nav_status='unavailable',reason=item.get('reason') or item['status'])
        if reuse:
            for example in original.get('local_navigation_examples',[]):
                report=safe_path(root,base_root,example['navigation_report_relpath'])
                patch=navigation_proof(root,report,asset,example.get('navigation_report_sha256'))
                need(patch['navigation_window'] is not None and patch['navigation_sha256']==example['navigation_sha256'],
                     'old_window_binding')
                row.setdefault('local_navigation_examples',[]).append(patch)
        rows.append(row)
    by_name={r['scene_name']:r for r in rows}
    for p in window_reports:
        p=_abs(p);need(p.is_relative_to(root),'window_report_escape')
        report,_=doc(p);name=report.get('mapName');need(name in proofs,'unknown_window_scene')
        patch=navigation_proof(root,p,proofs[name]);need(patch['navigation_window'] is not None,'window_required')
        existing=by_name[name].setdefault('local_navigation_examples',[])
        need(all(x['navigation_window']!=patch['navigation_window'] for x in existing),'duplicate_window');existing.append(patch)
    Catalog(rows)
    result={'format':'skyterrain-catalog-v1','profile':PROFILE,'asset_root':str(root),'scene_count':len(rows),
        'entries':rows,'input_sha256':{'base_catalog':bh,'world_summary':wh,'navigation_summary':nh},
        'summary':{k:dict(Counter(r[k] for r in rows)) for k in ('read_status','collision_status','nav_status')},
        'local_navigation_examples':sum(len(r.get('local_navigation_examples',[])) for r in rows),
        'runtime_ready':False,'native_collision_verified':False,'calibration_verified':False,'live_enabled':False,
        'implementation_sha256':sha(Path(__file__).read_bytes())}
    save_json(out,result);return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for key in ('base-catalog','world-summary','navigation-summary','assets-root','out'):p.add_argument('--'+key,type=Path,required=True)
    p.add_argument('--window-reports',type=Path,nargs='*',default=[]);a=p.parse_args()
    result=compose(a.base_catalog,a.world_summary,a.navigation_summary,a.assets_root,a.out,a.window_reports)
    print(json.dumps({'scenes':result['scene_count'],'summary':result['summary'],'windows':result['local_navigation_examples'],'runtime_ready':False}))
