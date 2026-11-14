"""Reconstruct reviewed budget candidates from this run's APK-decoded arrays."""
from pathlib import Path
import json
import numpy as np
from .coverage_cleanup import remove_covered_degenerates
from .simplify_collision import remove_covered_slivers, verify_sliver_cleanup
from .planar_simplification import simplify_planar, verify_planar
from .surface_simplification import simplify, verify_certificate
from .budget_geometry_verify import verify_budget_geometry
from .reviewed_geometry import scale_xyz
from .rules import BUDGET_RECIPES
from .extract import atomic

def reduce_geometry(positions, triangles, *, target, max_error):
    source_p=np.asarray(positions,dtype=np.float64).reshape(-1,3)
    source_t=np.asarray(triangles,dtype=np.int64).reshape(-1,3)
    p,t,zero=remove_covered_degenerates(source_p,source_t)
    q,u,sliver=remove_covered_slivers(p,t)
    verify_sliver_cleanup(p,t,q,u,sliver)
    p,t,planar=simplify_planar(q,u,target=2048)
    plane_check=verify_planar(q,u,p,t,planar)
    error=sliver['certified_bidirectional_surface_distance_bound']+plane_check['bidirectional_surface_distance_bound']
    proof=None
    if len(p)>2048 or len(t)>target:
        if not max_error:raise ValueError('budget_exact_recipe_unreachable')
        before_p,before_t=p,t
        p,t,proof=simplify(p,t,max_triangles=target,max_error=max_error)
        check=verify_certificate(before_p,before_t,p,t,proof['certificate'],max_error=max_error)
        error+=check['certified_bidirectional_surface_distance_bound']
        proof.update(input_positions=before_p.reshape(-1).tolist(),input_triangles=before_t.reshape(-1).tolist())
    audit=dict(source_positions=source_p.reshape(-1).tolist(),source_triangles=source_t.reshape(-1).tolist(),
        positions=p.reshape(-1).tolist(),triangles=t.reshape(-1).tolist(),
        preprocessing=dict(degenerate=zero,sliver=sliver,planar=planar),simplification=proof,
        certified_combined_local_error=error)
    verify_budget_geometry(audit,source_p,source_t)
    return p,t,audit

def extend(scratch,index,progress=None):
    scratch=Path(scratch)
    manifest=json.loads((scratch/'all/manifest.json').read_text(encoding='utf8'))
    records=manifest['records'];runtime=index['props']
    for number,(name,(target,limit)) in enumerate(BUDGET_RECIPES.items(),1):
        if progress:progress(dict(stage='budget',current=number,total=len(BUDGET_RECIPES),name=name))
        group=[r for r in records if r['name']==name]
        if not group:raise ValueError('budget_definition_missing:'+name)
        row=group[0]
        if runtime[name]['status']=='ready':raise ValueError('budget_recipe_already_ready:'+name)
        remaining=set(row['runtime_reasons'])-{'per_prop_geometry_budget','degenerate_render_triangles_require_review'}
        if remaining:raise ValueError('budget_eligibility_rejected:'+name+':'+','.join(sorted(remaining)))
        raw=json.loads((scratch/'all'/row['resources'][0]['raw_geometry']['path']).read_text(encoding='utf8'))
        if raw['animated']:raise ValueError('budget_animated_rejected:'+name)
        d=row['definition'];p=np.asarray(raw['positions']).reshape(-1,3)
        p,remap=np.unique(p,axis=0,return_inverse=True);p=p.astype(np.float64)
        p[:,1]+=float(d.get('yOffset',0));p*=scale_xyz(d)
        source_t=remap[np.asarray(raw['triangles'],dtype=np.int64).reshape(-1,3)]
        q,u,audit=reduce_geometry(p,source_t,target=target,max_error=limit)
        atomic(scratch/'all/budget-review'/(name+'.json'),audit)
        res=row['resources'][0]
        fields=row['coverage_review'].get('profile_fields',{})
        profile=dict(source_kind='render_mesh_candidate',native_collision_verified=False,
            resource_binding_verified=True,collision_registration_declared=True,
            resource_path=res['resource_path'],source_sha256=res['source_sha256'],
            definition_member=row['definition_member'],definition_index=row['definition_index'],
            mesh_name=d['mesh'],definition_scale=d['scale'][0],raw_vertex_count=raw['vertex_count'],
            vertex_count=len(q),triangle_count=len(u),mesh_version=raw['mesh_version'],
            positions=q.reshape(-1).tolist(),triangles=u.reshape(-1).tolist(),coordinate_space='sky_native',
            bounds_min=q.min(0).tolist(),bounds_max=q.max(0).tolist(),extra_code_geometry_verified=False,
            transform_evidence=dict(axis_conversion='none',definition_scale_applied=True,
                rpc_scale_applied_in_runtime=True,quaternion_order='xyzw_assumed_not_native_verified',
                native_scale_semantics_verified=False,forward_offset_applied=False,
                matrix='world = RPC.translation + normalized_xyzw_rotation * (RPC.scale * definition.xyz * (mesh.xyz+[0,yOffset,0]))'))
        profile.update(fields)
        profile.update(collision_representation='bounded_edge_collapse',
            approximation=dict(method='topology_preserving_edge_collapse',max_local_error=audit['certified_combined_local_error'],max_world_error=.02),
            geometry_budget_review=dict(algorithm='support-boundary-locked-bounded-surface-v4' if audit['simplification'] else 'exact_planar_retriangulation',
                original_triangles=len(source_t),final_triangles=len(u),per_prop_triangle_limit=2560,
                certificate_verified=True,scope='render surface only; native collision not verified'))
        shard='all/runtime/'+name+'.json'
        ref=atomic(scratch/shard,dict(schema='skyterrain-props-v1',coordinate_space='sky_native',props={name:profile}))
        runtime[name].update(status='ready',runtime_eligible=True,reasons=[],shard=shard,**ref,
            vertex_count=len(q),triangle_count=len(u),source_kind='render_mesh_candidate')
    return index
