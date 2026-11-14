"""0.16.8 原生证据约束的主网格扩展；不猜测未声明资源/代码子对象。"""
from pathlib import Path
import json,re,hashlib
import numpy as np
from .coverage_cleanup import remove_covered_degenerates
from .simplify_collision import simplify,verify_certificate,remove_covered_slivers,verify_sliver_cleanup

APK_SHA256='c8017dc7faac95d3f4f706e8dd44dc4bd0319429d5213e255c98afb03fb6fc4e'
BASE=Path(__file__).resolve().parent

def load_policy(apk_identity):
    from .rules import POLICY
    if apk_identity.get('sha256') != APK_SHA256:
        raise ValueError('unsupported_apk_geometry_policy')
    policy = json.loads(json.dumps(POLICY))
    policy['sha256'] = hashlib.sha256(json.dumps(POLICY, sort_keys=True).encode()).hexdigest()
    return policy


def scale_xyz(definition):
    values=definition.get('scale')
    if not isinstance(values,list) or not 1<=len(values)<=3:
        raise ValueError('definition_scale_selection_unverified')
    if any(type(v) not in (int,float) or not np.isfinite(v) or v<0 or v>20 for v in values):
        raise ValueError('definition_scale_selection_unverified')
    values=list(map(float,values))+[0.]*(3-len(values))
    x,y,z=values
    if x<=0:raise ValueError('definition_scale_selection_unverified')
    return np.asarray([x,y or x,z or x],dtype=np.float64)

def priority_name(row):
    return bool(re.search('chair|table|bench|sofa|stool|seat|bed|couch',row['name'],re.I)
                or row['name'] in ('AP24Desk','AP23PlaceablePianoBasic'))

def collision_evidence(definition,decl):
    has=definition.get('has_collision');reg=(decl or {}).get('properties',{}).get('registerCollision')
    if has is False:state='definition_explicitly_disabled'
    elif has is not True:state='definition_collision_not_declared'
    elif decl is None:state='mesh_binding_unknown'
    elif reg is False:state='definition_enabled_but_mesh_registration_explicitly_disabled'
    elif reg is not True:state='mesh_registration_not_declared'
    else:state='definition_and_mesh_registration_enabled'
    return dict(state=state,definition_has_collision=has,mesh_register_collision=reg,
                native_pass_through_verified=False,native_collision_shape_verified=False)

def review(row,mesh,decl,geometry,reasons,policy):
    reasons=list(dict.fromkeys(reasons));removed=[];fields={};audit={};d=row['definition']
    def resolve(reason):
        if reason in reasons:reasons.remove(reason);removed.append(reason)
    code=d.get('code_type_name');contract=policy['static_main_classes'].get(code)
    if contract and contract['verified'] is True:
        resolve('specialized_motion_or_subobjects_unverified')
        fields.update(geometry_scope='static_main_mesh_only',whole_prop_collision_complete=False,
                      excluded_parts=contract['excluded_parts'],native_static_class=code,
                      native_static_evidence=contract['evidence'])
        if d.get('force_floor') is True:
            reasons.append('force_floor_index_offset_unverified')
    if mesh is None or mesh.animated or decl is None:
        return geometry,reasons,dict(resolved_reasons=removed,profile_fields=fields,audit=audit)
    try:xyz=scale_xyz(d)
    except ValueError:return geometry,reasons,dict(resolved_reasons=removed,profile_fields=fields,audit=audit)
    if len(d['scale'])!=1:
        resolve('definition_scale_selection_unverified')
        fields['definition_scale_xyz']=xyz.tolist()
        fields['definition_scale_interpretation']='native_xyz_zero_yz_fallback_to_x'
    y=d.get('yOffset',0.)
    verified_y=(type(y) in (int,float) and np.isfinite(y) and abs(y)<=50 and d.get('force_floor') is not True)
    if verified_y and y!=0 and all(d.get(k,0)==0 for k in ('surface_offset','hang_offset')):
        resolve('nonzero_definition_offset_semantics_unverified')
        fields.update(definition_y_offset=float(y),definition_y_offset_order='before_definition_xyz_scale',
                      definition_scale_xyz=xyz.tolist())
    p,remap=np.unique(mesh.positions,axis=0,return_inverse=True)
    p=p.astype(np.float64)
    if verified_y and y!=0:p[:,1]+=float(y)
    p*=xyz;t=remap[mesh.triangles]
    if not np.isfinite(p).all() or np.max(np.abs(p))>50:
        return None,reasons+['local_coordinate_budget'],dict(resolved_reasons=removed,profile_fields=fields,audit=audit)
    # 原始raw保持原样；仅优先家具进行可复验清理/简化，不改变其它旧ready数组。
    if priority_name(row):
        v=p[t];area=np.linalg.norm(np.cross(v[:,1]-v[:,0],v[:,2]-v[:,0]),axis=1)
        if np.any(area==0):
            try:
                p,t,proof=remove_covered_degenerates(p,t)
                audit['zero_area_cleanup']=proof
                fields['collision_representation']='covered_zero_area_removal'
            except ValueError as error:audit['zero_area_cleanup_failure']=str(error)
        if len(p)>2048 or len(t)>2048:
            attempts=[];original_p,original_t=p,t
            try:
                clean_p,clean_t,cleanup=remove_covered_slivers(p,t)
                verify_sliver_cleanup(p,t,clean_p,clean_t,cleanup)
                cleanup_error=cleanup['certified_bidirectional_surface_distance_bound']
                for limit in (.005,.01,.02):
                    try:
                        q,u,proof=simplify(clean_p,clean_t,max_error=limit)
                        verified=verify_certificate(clean_p,clean_t,q,u,proof['certificate'],max_error=limit)
                        error=cleanup_error+verified['certified_bidirectional_surface_distance_bound']
                        if error>.02:raise ValueError('combined_error_exceeds_2cm')
                        p,t=q,u;audit.update(sliver_cleanup=cleanup,simplification=proof,
                            certified_combined_local_error=error,source_positions=original_p.reshape(-1).tolist(),
                            source_triangles=original_t.reshape(-1).tolist())
                        fields.update(collision_representation='bounded_edge_collapse',
                            approximation=dict(method='topology_preserving_edge_collapse',max_local_error=error,max_world_error=.02))
                        attempts.append(dict(limit=limit,success=True));break
                    except ValueError as error:attempts.append(dict(limit=limit,error=str(error)))
            except ValueError as error:attempts.append(dict(error=str(error),phase='sliver_cleanup'))
            audit['simplification_attempts']=attempts
    v=p[t];area=np.linalg.norm(np.cross(v[:,1]-v[:,0],v[:,2]-v[:,0]),axis=1)
    if len(p)<=2048 and len(t)<=2048:resolve('per_prop_geometry_budget')
    elif 'per_prop_geometry_budget' not in reasons:reasons.append('per_prop_geometry_budget')
    if np.all(area>=1e-9):resolve('degenerate_render_triangles_require_review')
    elif 'degenerate_render_triangles_require_review' not in reasons:reasons.append('degenerate_render_triangles_require_review')
    if audit.get('simplification') or audit.get('zero_area_cleanup'):
        audit.update(positions=p.reshape(-1).tolist(),triangles=t.reshape(-1).tolist())
    info=dict(resolved_reasons=removed,profile_fields=fields,audit=audit,
        policy_sha256=policy['sha256'],definition_scale_xyz=xyz.tolist(),
        definition_y_offset_applied_before_scale=float(y) if verified_y else 0.,
        force_floor_jitter_recovered=False,native_transform_verified=False)
    return (p,t),reasons,info
