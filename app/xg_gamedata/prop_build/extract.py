"""APK resource extraction. Raw audits exist only in a private staging directory."""
from pathlib import Path
import hashlib, json, os, re
from collections import Counter, defaultdict
import numpy as np
from skyterrain.formats import read_mesh
from skyterrain.resource_tables import read_resource_tables, PROFILE
from skyterrain.mesh_resource_paths import project_mesh_path
from .reviewed_geometry import load_policy, review as review_geometry, collision_evidence

def within(value):
    return Path(value).resolve()

ALL_SCHEMA = 'skyterrain-props-index-v1'
MANIFEST_SCHEMA = 'skyterrain-props-manifest-v1'
SAFE_RUNTIME_NAME = re.compile(r'[A-Za-z][A-Za-z0-9_]{0,95}\Z')
RUNTIME_CODE_TYPES = ('Placeable', 'TeaSet', 'XmasTable')
MIN_UP_NORMAL_Y = 0.65
GEOMETRY_CONTRACT_KEYS = ('mesh','scale','code_type_name','has_collision','yOffset','surface_offset',
    'hang_offset','forward_offset','placement_rot_offset','play_default_animation',
    'allow_roll','face_down','face_away','force_floor','requires_water')


def atomic(path,value):
    p=within(path);p.parent.mkdir(parents=True,exist_ok=True)
    t=p.with_suffix(p.suffix+'.tmp')
    payload=(json.dumps(value,ensure_ascii=False,separators=(',',':'),allow_nan=False)+'\n').encode('utf-8')
    t.write_bytes(payload)
    os.replace(t,p)
    return {'sha256':digest(payload),'byte_length':len(payload)}


def digest(raw):return hashlib.sha256(raw).hexdigest()


def member(z,path):
    info=z.getinfo(path)
    if not 0<info.file_size<=32*1024*1024:raise ValueError('member_budget')
    return z.read(info)


def clean_geometry(mesh,scale):
    if mesh.animated:raise ValueError('animated_mesh_needs_native_pose')
    # 精确相等坐标去重；不合并近点、不重排/删除三角形、不简化网格。
    p,remap=np.unique(mesh.positions,axis=0,return_inverse=True)
    t=remap[mesh.triangles]
    if len(p)>2048 or len(t)>2048:raise ValueError('per_prop_geometry_budget')
    p=p.astype(np.float64)*scale
    if not np.isfinite(p).all() or np.max(np.abs(p))>50:raise ValueError('local_coordinate_budget')
    tri=p[t];area=np.linalg.norm(np.cross(tri[:,1]-tri[:,0],tri[:,2]-tri[:,0]),axis=1)
    if np.any(area<1e-9):raise ValueError('degenerate_render_triangles_require_review')
    return p,t


def definition_equivalence(group):
    if len(group)==1:return {'state':'unique','geometry_conflict':False,'different_keys':[]}
    defs=[r['definition'] for r in group]
    keys=sorted(set().union(*(d.keys() for d in defs)))
    changed=[k for k in keys if any(d.get(k)!=defs[0].get(k) for d in defs[1:])]
    geometry=[k for k in changed if k in GEOMETRY_CONTRACT_KEYS]
    return {'state':'geometry_conflict' if geometry else ('non_geometry_only' if changed else 'exact'),
            'geometry_conflict':bool(geometry),'different_keys':changed,'geometry_different_keys':geometry}


def discover_definitions(z):
    paths=sorted(n for n in z.namelist()
                 if Path(n).name.lower().startswith('placeabledefs') and n.lower().endswith('.json'))
    rows=[];sources=[];hashes={}
    for path in paths:
        raw=member(z,path);items=json.loads(raw)
        if not isinstance(items,list):raise ValueError('definition_table_not_list:'+path)
        hashes[path]=digest(raw);sources.append({'path':path,'sha256':digest(raw),'records':len(items)})
        for index,d in enumerate(items):
            if not isinstance(d,dict) or not isinstance(d.get('name'),str):
                raise ValueError('invalid_definition_record:'+path+':'+str(index))
            rows.append({'definition_key':path+'#'+str(index),'definition_member':path,
                         'definition_index':index,'name':d['name'],'definition':d})
    if not paths:raise ValueError('no_placeable_definition_tables')
    return rows,sources,hashes


def support_info(p,t,animated=False):
    if not len(t):return {'status':'no_local_candidate','candidate_count':0,'min_up_normal_y':MIN_UP_NORMAL_Y,
                         'runtime_pose_required':True,'native_standing_verified':False}
    tri=np.asarray(p,dtype=np.float64)[t]
    cross=np.cross(tri[:,1]-tri[:,0],tri[:,2]-tri[:,0]);length=np.linalg.norm(cross,axis=1)
    ny=np.divide(cross[:,1],length,out=np.zeros(len(length)),where=length>1e-12)
    count=int(np.count_nonzero(ny>=MIN_UP_NORMAL_Y))
    return {'status':'animated_bind_candidates_only' if animated else ('local_candidates' if count else 'no_local_candidate'),
            'candidate_count':count,'min_up_normal_y':MIN_UP_NORMAL_Y,
            'slope_42deg_candidate_count':int(np.count_nonzero(ny>=np.cos(np.deg2rad(42)))),
            'final_slope_limit_degrees':42,
            'local_y_up_assumed':True,'runtime_pose_required':True,
            'world_rotation_slope_clearance_collision_required':True,'native_standing_verified':False}


def seat_info(d):
    markers=d.get('marker_params')
    tap=d.get('on_tap_play_anim_params') or {}
    if not isinstance(tap,dict):tap={}
    anim=tap.get('anim',''); sit=any(k.lower().startswith('sit') for k in tap) or (isinstance(anim,str) and 'sit' in anim.lower())
    state='configured' if sit and isinstance(markers,list) and markers else ('unknown' if markers or sit else 'not_declared')
    return {'state':state,'marker_count':len(markers) if isinstance(markers,list) else 0,
            'sit_animation_declared':bool(sit),'live_seat_binding_verified':False,
            'extra_seat_geometry_verified':False,
            'meaning':'configured means marker plus sit animation evidence; not_declared is not proof of no native seats'}


def runtime_reasons(row,geometry_conflict,mesh,decl):
    d=row['definition'];reasons=[]
    if not SAFE_RUNTIME_NAME.fullmatch(row['name']):reasons.append('invalid_runtime_name')
    if geometry_conflict:reasons.append('ambiguous_geometry_contract_across_definitions')
    if d.get('has_collision') is not True:reasons.append('has_collision_not_true')
    if d.get('code_type_name') not in RUNTIME_CODE_TYPES:reasons.append('specialized_motion_or_subobjects_unverified')
    scale=d.get('scale')
    if not isinstance(scale,list) or len(scale)!=1 or type(scale[0]) not in (int,float) or not np.isfinite(scale[0]) or not 0<scale[0]<=20:
        reasons.append('definition_scale_selection_unverified')
    if any(d.get(k,0)!=0 for k in ('yOffset','surface_offset','hang_offset')):
        reasons.append('nonzero_definition_offset_semantics_unverified')
    rotation=d.get('placement_rot_offset')
    if rotation is not None and rotation!=0 and rotation!=[0,0,0]:
        reasons.append('placement_rotation_offset_semantics_unverified')
    if d.get('play_default_animation') not in (None,False,0,''):
        reasons.append('default_animation_behavior_unverified')
    if mesh is None:reasons.append('no_decoded_main_mesh')
    elif mesh.animated:reasons.append('animated_mesh_needs_native_pose')
    if decl is None:reasons.append('no_unique_mesh_declaration')
    elif decl.get('properties',{}).get('registerCollision') is not True:
        reasons.append('mesh_collision_registration_not_declared')
    return reasons


def archive_mesh(z,path,out_dir,mesh_cache,raw_assets):
    if path not in mesh_cache:
        try:
            raw=member(z,path);decoded=read_mesh(raw)
            if not len(decoded.positions) or not len(decoded.triangles):raise ValueError('empty_mesh_geometry')
            rawhash=digest(raw);raw_name='raw/'+rawhash+'.json'
            raw_asset={'schema':'skyterrain-prop-raw-mesh-v1','source_kind':'render_mesh_candidate',
                'native_collision_verified':False,'source_sha256':rawhash,'source_bytes':len(raw),
                'mesh_version':decoded.version,'animated':bool(decoded.animated),
                'coordinate_space':'mesh_raw_unscaled','axis_conversion':'none',
                'vertex_count':len(decoded.positions),'triangle_count':len(decoded.triangles),
                'positions':decoded.positions.reshape(-1).tolist(),'triangles':decoded.triangles.reshape(-1).tolist(),
                'bounds_min':decoded.positions.min(0).tolist(),'bounds_max':decoded.positions.max(0).tolist(),
                'marker_count':decoded.metadata.get('marker_count',0),'skeleton_applied':False,
                'scale_applied':False,'winding_changed':False,'triangles_removed':0}
            if rawhash not in raw_assets:
                ref=atomic(out_dir/raw_name,raw_asset)
                raw_assets[rawhash]=dict(ref,path=raw_name,source_sha256=rawhash,
                    vertex_count=len(decoded.positions),triangle_count=len(decoded.triangles),animated=bool(decoded.animated))
            mesh_cache[path]=(decoded,raw_assets[rawhash],None)
        except (ValueError,KeyError,TypeError,OverflowError,IndexError) as error:
            mesh_cache[path]=(None,None,type(error).__name__+':'+str(error))
    return mesh_cache[path]


def unverified_candidates(z,mesh_name,declarations):
    candidates={}
    for ds in declarations.values():
        for d in ds:
            source=d.get('properties',{}).get('source')
            if isinstance(source,str) and Path(source).stem==mesh_name and not d.get('has_symbolic_properties'):
                projected=project_mesh_path(d['properties'],profile=PROFILE)
                if projected.path:
                    candidates.setdefault(projected.path,{'resource_path':projected.path,
                        'binding_status':'source_stem_match_declared_name_differs','binding_verified':False,
                        'declaration_names':[]})['declaration_names'].append(d['name'])
    if not candidates:
        exact='assets/Data/Meshes/Bin/'+mesh_name+'.mesh'
        if exact in z.namelist():candidates[exact]={'resource_path':exact,'binding_status':'exact_filename_only','binding_verified':False}
        else:
            # 只报告完全相同 stem 加已知编译选项后缀，不做任意模糊资源匹配。
            suffixes={'CompOcc','ZipPos','ZipUvs','StripUv13','StripNorm','CopyFrameDelay','Persistent'}
            for path in z.namelist():
                stem=Path(path).stem
                if path.endswith('.mesh') and stem.startswith(mesh_name+'_'):
                    tokens=stem[len(mesh_name)+1:].split('_')
                    if tokens and all(token in suffixes for token in tokens):
                        candidates[path]={'resource_path':path,'binding_status':'filename_with_compile_suffix_only','binding_verified':False}
    return list(candidates.values())


def build_all_index(z,apk_identity,out_dir,*,primary_index=None,progress=None):
    out_dir=within(out_dir);out_dir.mkdir(parents=True,exist_ok=True)
    policy=load_policy(apk_identity)
    rows,sources,input_hashes=discover_definitions(z);counts=Counter(r['name'] for r in rows)
    definition_groups=defaultdict(list)
    for row in rows:definition_groups[row['name']].append(row)
    equivalences={name:definition_equivalence(group) for name,group in definition_groups.items()}
    resource_paths=sorted(n for n in z.namelist() if Path(n).name.lower().startswith('meshdefs') and n.endswith('.lua.luac'))
    declarations={};related=[]
    for path in resource_paths:
        raw=member(z,path);input_hashes[path]=digest(raw)
        parsed=read_resource_tables(raw,profile=PROFILE)
        related.append({'path':path,'sha256':digest(raw),'declaration_count':len(parsed['declarations'])})
        for d in parsed['declarations']:
            if d.get('family')=='Mesh':
                declarations.setdefault(d['name'],[]).append(dict(d,table_member=path))
    outfit_rows={};outfits_by_name=defaultdict(list)
    for path in sorted(n for n in z.namelist() if Path(n).name.lower().startswith(('outfitdefs','outfittransform')) and n.endswith('.json')):
        raw=member(z,path);items=json.loads(raw);input_hashes[path]=digest(raw)
        related.append({'path':path,'sha256':digest(raw),'records':len(items),'role':'outfit_reference_not_placed_mesh_binding'})
        for index,item in enumerate(items):
            if not isinstance(item,dict):continue
            name=item.get('name')
            if isinstance(name,str):
                key=path+'#'+str(index);outfit_rows[key]={'name':name,'mesh':item.get('mesh'),
                    'type':item.get('type'),'abilities':item.get('abilities'),'source':path,'index':index}
                outfits_by_name[name].append(key)
    used_declarations={};mesh_cache={};raw_assets={};records=[];runtime={};runtime_total=0
    for number,base in enumerate(rows,1):
        if progress:progress({"stage":"extract","current":number,"total":len(rows),"name":base["name"]})
        d=base['definition'];name=base['name'];mesh_name=d.get('mesh');decl=None;mesh=None;resource={};failure=[]
        if not isinstance(mesh_name,str) or not mesh_name:
            failure.append('definition_main_mesh_not_declared')
        else:
            ds=declarations.get(mesh_name,[]);used_declarations[mesh_name]=ds
            resource={'role':'main_mesh','mesh_name':mesh_name,'declaration_count':len(ds)}
            if len(ds)!=1:failure.append('mesh_declaration_missing_or_ambiguous')
            elif ds[0].get('has_symbolic_properties'):failure.append('symbolic_mesh_declaration_unverified')
            else:
                decl=ds[0];projected=project_mesh_path(decl['properties'],profile=PROFILE)
                if not projected.path:failure.append('mesh_path_unverified:'+str(projected.reasons))
                else:
                    path=projected.path;resource['resource_path']=path;resource['binding_status']='declaration_bound'
                    mesh,ref,error=archive_mesh(z,path,out_dir,mesh_cache,raw_assets)
                    if error:failure.append('mesh_decode_failed:'+error)
                    else:resource.update(source_sha256=ref['source_sha256'],raw_geometry=dict(ref),
                        raw_vertex_count=len(mesh.positions),raw_triangle_count=len(mesh.triangles),
                        animated=bool(mesh.animated),collision_registration_declared=decl['properties'].get('registerCollision') is True)
        candidate_geometry=False
        if mesh is None and isinstance(mesh_name,str) and mesh_name and failure:
            candidates=unverified_candidates(z,mesh_name,declarations);decoded_candidates=[]
            for candidate in candidates:
                candidate_mesh,candidate_ref,error=archive_mesh(z,candidate['resource_path'],out_dir,mesh_cache,raw_assets)
                if error:candidate['decode_error']=error
                else:
                    candidate['raw_geometry']=dict(candidate_ref)
                    candidate['standing_surface']=support_info(candidate_mesh.positions,candidate_mesh.triangles,candidate_mesh.animated)
                    decoded_candidates.append((candidate_mesh,candidate_ref,candidate))
            resource['unverified_candidates']=candidates
            if len(decoded_candidates)==1:
                mesh,ref,candidate=decoded_candidates[0];candidate_geometry=True
                resource.update(resource_path=candidate['resource_path'],binding_status=candidate['binding_status'],binding_verified=False,
                    raw_geometry=dict(ref),source_sha256=ref['source_sha256'],raw_vertex_count=len(mesh.positions),
                    raw_triangle_count=len(mesh.triangles),animated=bool(mesh.animated))
            elif decoded_candidates:
                candidate_geometry=True
        eligibility=runtime_reasons(base,equivalences[name]['geometry_conflict'],mesh,decl)
        if failure:eligibility.extend(failure)
        geometry=None
        if mesh is not None and 'definition_scale_selection_unverified' not in eligibility:
            try:
                p,t=clean_geometry(mesh,d['scale'][0]);geometry=(p,t)
            except (ValueError,TypeError,OverflowError) as error:eligibility.append(str(error))
        geometry,eligibility,reviewed=review_geometry(base,mesh,decl,geometry,eligibility,policy)
        audit=reviewed.pop('audit',{})
        if audit:
            audit_path='review/'+name+'-'+digest(base['definition_key'].encode())[:12]+'.json'
            audit_ref=atomic(out_dir/audit_path,dict(schema='prop-geometry-review-v1',name=name,
                definition_key=base['definition_key'],source_sha256=resource.get('source_sha256'),**audit))
            reviewed['audit']=dict(path=audit_path,**audit_ref)
        if not eligibility and geometry is not None and name not in runtime:
            p,t=geometry
            profile={'source_kind':'render_mesh_candidate','native_collision_verified':False,
                'resource_binding_verified':True,'collision_registration_declared':True,
                'resource_path':resource['resource_path'],'source_sha256':resource['source_sha256'],
                'definition_member':base['definition_member'],'definition_index':base['definition_index'],
                'mesh_name':mesh_name,'definition_scale':d['scale'][0],
                'raw_vertex_count':len(mesh.positions),'vertex_count':len(p),'triangle_count':len(t),
                'mesh_version':mesh.version,'positions':p.reshape(-1).tolist(),'triangles':t.reshape(-1).tolist(),
                'coordinate_space':'sky_native','bounds_min':p.min(0).tolist(),'bounds_max':p.max(0).tolist(),
                'extra_code_geometry_verified':False,
                'transform_evidence':{'axis_conversion':'none','definition_scale_applied':True,
                    'rpc_scale_applied_in_runtime':True,'quaternion_order':'xyzw_assumed_not_native_verified',
                    'native_scale_semantics_verified':False,'forward_offset_applied':False,
                    'matrix':'world = RPC.translation + normalized_xyzw_rotation * (RPC.scale * definition.scale * mesh.xyz)'}}
            profile.update(reviewed.get('profile_fields',{}))
            if profile.get('definition_scale_xyz') or profile.get('definition_y_offset'):
                profile['transform_evidence'].update(definition_xyz_semantics_verified=True,
                    definition_y_offset_order='pre_definition_scale',
                    matrix='world = RPC.translation + normalized_xyzw_rotation * (RPC.scale * definition.xyz * (mesh.xyz + [0,yOffset,0]))')
            shard='runtime/'+name+'.json'
            ref=atomic(out_dir/shard,{'schema':'skyterrain-props-v1','coordinate_space':'sky_native','props':{name:profile}})
            runtime[name]={'status':'ready','runtime_eligible':True,'reasons':[],
                'shard':shard,**ref,'vertex_count':len(p),'triangle_count':len(t),'source_kind':'render_mesh_candidate'}
            runtime_total+=ref['byte_length']
        standing=support_info(mesh.positions,mesh.triangles,mesh.animated) if mesh is not None else {
            'status':'unknown','candidate_count':None,'runtime_pose_required':True,'native_standing_verified':False}
        extraction=('unverified_binding_mesh_extracted' if candidate_geometry else
            ('static_mesh_extracted' if mesh is not None and not mesh.animated else
             ('animated_bind_geometry_extracted' if mesh is not None else 'no_geometry')))
        records.append(dict(base,outfit_references=outfits_by_name.get(d.get('outfit_name'),[]),
            resources=[resource] if resource else [],extraction_status=extraction,
            extraction_reasons=failure,runtime_eligible=not eligibility and geometry is not None,
            runtime_reasons=list(dict.fromkeys(eligibility)),source_kind='render_mesh_candidate' if mesh is not None or candidate_geometry else 'unavailable',
            standing_surface=standing,seat_evidence=seat_info(d),
            coverage_review=reviewed,collision_evidence=collision_evidence(d,decl),
            native_collision_verified=False,native_transform_verified=False,resource_binding_verified=mesh is not None and not candidate_geometry,
            transform_assumptions={'definition_scale_candidates':d.get('scale'),
                'definition_offsets':{k:d.get(k) for k in ('yOffset','surface_offset','hang_offset','forward_offset','placement_rot_offset')},
                'rpc_position_is_final':True,'forward_offset_not_reapplied':True,
                'quaternion_xyzw_assumed':True,'native_semantics_verified':False}))
    grouped=defaultdict(list)
    for r in records:grouped[r['name']].append(r)
    for name,group in grouped.items():
        if name not in runtime:
            status='ambiguous' if equivalences[name]['geometry_conflict'] else ('no_geometry' if group[0]['extraction_status']=='no_geometry' else 'ineligible')
            runtime[name]={'status':status,'runtime_eligible':False,
                'reasons':sorted({reason for r in group for reason in r['runtime_reasons']}),
                'shard':None,'sha256':None,'byte_length':0,'vertex_count':0,'triangle_count':0,
                'source_kind':'render_mesh_candidate' if any(r['source_kind']=='render_mesh_candidate' for r in group) else 'unavailable'}
        runtime[name]['definition_keys']=[r['definition_key'] for r in group]
        runtime[name]['definition_equivalence']=equivalences[name]['state']
    count_summary={'definition_records':len(records),'unique_names':len(grouped),
        'duplicate_names':sum(len(v)>1 for v in grouped.values()),
        'duplicate_equivalence':dict(Counter(e['state'] for name,e in equivalences.items() if len(grouped[name])>1)),
        'extraction_status':dict(Counter(r['extraction_status'] for r in records)),
        'runtime_status':dict(Counter(r['status'] for r in runtime.values())),
        'runtime_eligible_names':sum(r['runtime_eligible'] for r in runtime.values()),
        'runtime_shard_bytes':runtime_total,'runtime_vertices':sum(r['vertex_count'] for r in runtime.values()),
        'runtime_triangles':sum(r['triangle_count'] for r in runtime.values()),
        'raw_mesh_assets':len(raw_assets),'raw_geometry_bytes':sum(r['byte_length'] for r in raw_assets.values()),
        'raw_vertices':sum(r['vertex_count'] for r in raw_assets.values()),'raw_triangles':sum(r['triangle_count'] for r in raw_assets.values())}
    limitations=['范围是本 APK 所有发现的 PlaceableDefs JSON 定义，不等于服务器可放置道具全集已验证。',
        '资源源名相似或文件名匹配候选仅提取 raw，缺精确声明绑定时禁止运行时。',
        '配置名称重复全部保留；几何契约一致可复用主网格，冲突不猜测覆盖或区域选择。',
        '原始mesh可读不等于原生碰撞完整；新增native证据只解除明确XYZ/yOffset/静态主模型门，其余动画/资源别名/偏移保持分开记录。',
        '所有网格均为 render_mesh_candidate，native collision/transform verified 均为 false。',
        'TableXmas抓包已验证xyzw/.075，native定义XYZ与缩放前yOffset另有静态及144组原生矩阵仿真；全局运行时因子、其它道具真机、surface/hang等仍未完整确认。forward_offset不重复叠加。',
        '部分复杂家具为带证书的有界短边折叠，保留原局部支持面，不用凸包填空；世界误差含RPC.scale和Float32量化须≤.02，超出忽略。',
        'standing_surface 是局部正Y绕序候选，仍需世界旋转、坡度、胶囊净空和路径碰撞验证。',
        'seat configured 只表示marker与sit配置；没有创建新座位或伴生椅子。']
    manifest={'schema':MANIFEST_SCHEMA,'apk':apk_identity,'definition_sources':sources,
        'related_resource_tables':related,'input_hashes':input_hashes,'counts':count_summary,
        'extractor_sha256':digest(Path(__file__).read_bytes()),
        'mesh_decoder_sha256':digest(Path(__import__('skyterrain.formats._mesh',fromlist=['__file__']).__file__).read_bytes()),
        'server_placeable_completeness_verified':False,'records':records,
        'runtime_eligibility_scope':'render-main geometry candidates; active64/32768 vertices/16384 triangles/500000 collider bytes; bounded simplification world error <=.02',
        'coverage_policy_sha256':policy['sha256'],
        'coverage_code_sha256':{name:digest((Path(__file__).parent/name).read_bytes()) for name in
            ('reviewed_geometry.py','coverage_cleanup.py','simplify_collision.py')},
        'mesh_declarations':used_declarations,'raw_assets':raw_assets,
        'definition_equivalence':equivalences,'geometry_contract_keys':list(GEOMETRY_CONTRACT_KEYS),
        'outfit_references':{key:outfit_rows[key] for key in sorted({key for row in records for key in row['outfit_references']})},
        'limitations':limitations}
    manifest_ref=atomic(out_dir/'manifest.json',manifest)
    index={'schema':ALL_SCHEMA,'coordinate_space':'sky_native','apk':apk_identity,
        'native_collision_verified':False,'server_placeable_completeness_verified':False,
        'counts':count_summary,'props':dict(sorted(runtime.items())),
        'manifest':dict(path='manifest.json',**manifest_ref),'limitations':limitations}
    atomic(out_dir/'index.json',index)
    if primary_index:
        primary=within(primary_index);prefix=out_dir.relative_to(primary.parent).as_posix()
        public=dict(index,props={name:dict(row,shard=prefix+'/'+row['shard'] if row['shard'] else None) for name,row in index['props'].items()},
                    manifest=dict(index['manifest'],path=prefix+'/manifest.json'))
        atomic(primary,public)
    atomic(out_dir/'names.json',{'schema':'skyterrain-prop-names-v1','count':len(grouped),'names':sorted(grouped)})
    return index


