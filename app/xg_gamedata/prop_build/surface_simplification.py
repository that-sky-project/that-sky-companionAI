"""全表面有界边折叠；允许支撑面变化，不伪称顶面误差为零。"""
from __future__ import annotations
import math
import numpy as np
from .simplify_collision import _normals, _topology
ALGORITHM='support-boundary-locked-bounded-surface-v4'

def verify_certificate(positions,triangles,out_positions,out_triangles,certificate,*,max_error=.02,
                       min_up_normal_y=.65,max_normal_change_degrees=15.):
    """独立重验最终仿射对应，不信任生成器的统计字段。证书仅供离线审计。"""
    raw=np.asarray(positions,dtype=np.float64).reshape(-1,3)
    raw_t=np.asarray(triangles).reshape(-1,3)
    if (not math.isfinite(max_error) or not 0<max_error<=.02 or not .5<=min_up_normal_y<=1
            or not 0<max_normal_change_degrees<=20 or not np.isfinite(raw).all()
            or raw_t.dtype.kind not in 'iu' or not len(raw_t) or raw_t.min()<0 or raw_t.max()>=len(raw)):
        raise ValueError('certificate_source')
    source,remap=np.unique(raw,axis=0,return_inverse=True);source_t=remap[raw_t]
    p=np.asarray(out_positions,dtype=np.float64).reshape(-1,3)
    t=np.asarray(out_triangles).reshape(-1,3)
    mapping=np.asarray(certificate['source_vertex_to_output_vertex'])
    face_mapping=np.asarray(certificate['output_face_to_source_face'])
    if any(v.dtype.kind not in 'iu' for v in (t,mapping,face_mapping)) or not len(t):raise ValueError('certificate_integer_indices')
    if mapping.shape!=(len(source),) or face_mapping.shape!=(len(t),):raise ValueError('certificate_shape')
    used=np.unique(source_t)
    if (not np.isfinite(p).all() or t.min()<0 or t.max()>=len(p)
            or mapping[used].min()<0 or mapping[used].max()>=len(p)
            or face_mapping.min()<0 or face_mapping.max()>=len(source_t)
            or len(np.unique(face_mapping))!=len(face_mapping)):
        raise ValueError('certificate_index')
    mapped=mapping[source_t]
    if not np.array_equal(t,mapped[face_mapping]):raise ValueError('certificate_oriented_face_correspondence')
    final_faces={tuple(sorted(map(int,row))) for row in t}
    final_edges={tuple(sorted((int(a),int(b)))) for a,b,c in t for a,b in ((a,b),(b,c),(c,a))}
    final_vertices=set(map(int,np.unique(t)))
    if len(final_faces)!=len(t) or len(final_vertices)!=len(p):raise ValueError('certificate_extra_geometry')
    for row in mapped:
        unique=tuple(sorted(set(map(int,row))))
        if (len(unique)==3 and unique not in final_faces or len(unique)==2 and unique not in final_edges
                or len(unique)==1 and unique[0] not in final_vertices):
            raise ValueError('certificate_removed_surface_uncovered')
    source_n,source_area=_normals(source,source_t);normals,areas=_normals(p,t)
    protected=source_n[:,1]>=min_up_normal_y
    for threshold in (min_up_normal_y,math.cos(math.radians(42))):
        old_support=source_n[:,1]>=threshold;new_support=normals[:,1]>=threshold
        if not np.array_equal(old_support[face_mapping],new_support):raise ValueError('certificate_support_class_changed')
        support_edges,_,_=_topology(source_t[old_support])
        support_boundary=sorted({v for e,ids in support_edges.items() if len(ids)!=2 for v in e})
        if support_boundary and not np.array_equal(source[support_boundary],p[mapping[support_boundary]]):
            raise ValueError('certificate_support_boundary_changed')
        output=t[new_support]
        support_faces={tuple(sorted(map(int,row))) for row in output}
        support_edges={tuple(sorted((int(a),int(b)))) for a,b,c in output for a,b in ((a,b),(b,c),(c,a))}
        support_vertices=set(map(int,np.unique(output)))
        for row in mapped[old_support]:
            unique=tuple(sorted(set(map(int,row))))
            if (len(unique)==3 and unique not in support_faces or len(unique)==2 and unique not in support_edges
                    or len(unique)==1 and unique[0] not in support_vertices):
                raise ValueError('certificate_support_region_uncovered')
    edges,_,_=_topology(source_t)
    boundary=sorted({v for e,ids in edges.items() if len(ids)!=2 for v in e})
    if boundary and float(np.linalg.norm(source[boundary]-p[mapping[boundary]],axis=1).max())>max_error+1e-12:raise ValueError('certificate_boundary_error')
    angle=np.degrees(np.arccos(np.clip(np.sum(normals*source_n[face_mapping],axis=1),-1,1)))
    if np.any(areas<=1e-9) or np.any(source_area<=1e-9) or float(angle.max())>max_normal_change_degrees+1e-8:
        raise ValueError('certificate_normal_or_area')
    displacement=np.linalg.norm(source[used]-p[mapping[used]],axis=1)
    bound=float(displacement.max())
    if bound>max_error+1e-12:raise ValueError('certificate_error_exceeded')
    return dict(certified_bidirectional_surface_distance_bound=bound,support_height_error=None,
                support_outline_error=None,protected_support_triangles=int(protected.sum()),
                max_normal_change_degrees=float(angle.max()),verified=True)


def simplify(positions,triangles,*,max_vertices=2048,max_triangles=2048,
             max_error=.01,min_up_normal_y=.65,max_normal_change_degrees=15.):
    """返回(p,t,report)。预算无法达到时抛ValueError，不输出伪合格网格。

    支撑区域边界固定，非支撑边界可在误差内移动；每次边折叠
    验证link条件、重复面、翻面、法向变化和累计原顶点位移，允许阈值附近的
    局部支持分类变化。保留连通关系与孔洞；不是保证外包的凸碰撞近似。
    """
    if type(max_vertices) is not int or type(max_triangles) is not int or not 3<=max_vertices<=2048 or not 1<=max_triangles<=2560:
        raise ValueError('simplification_budget')
    if not isinstance(max_error,(float,int)) or not math.isfinite(max_error) or not 0<max_error<=.02:
        raise ValueError('simplification_error_limit')
    if not .5<=min_up_normal_y<=1 or not 0<max_normal_change_degrees<=20:
        raise ValueError('simplification_normal_limit')
    p=np.asarray(positions,dtype=np.float64)
    original_t=np.asarray(triangles)
    if p.ndim==1:p=p.reshape(-1,3)
    if original_t.ndim==1:original_t=original_t.reshape(-1,3)
    if p.ndim!=2 or p.shape[1]!=3 or original_t.ndim!=2 or original_t.shape[1]!=3 or original_t.dtype.kind not in 'iu':
        raise ValueError('simplification_shape')
    if not 3<=len(p)<=100000 or not 1<=len(original_t)<=100000 or not np.isfinite(p).all() or np.max(np.abs(p))>50:
        raise ValueError('simplification_source_budget')
    if original_t.min()<0 or original_t.max()>=len(p):raise ValueError('simplification_index')
    source,remap=np.unique(p,axis=0,return_inverse=True)
    source_t=remap[original_t]
    p=source.copy();t=source_t.copy()
    normals,areas=_normals(p,t)
    source_normals=normals.copy()
    if np.any(areas<=1e-9):raise ValueError('simplification_source_degenerate')
    protected_faces=normals[:,1]>=min_up_normal_y
    protected_vertices=set(map(int,t[protected_faces].ravel()))
    edges,faces,neighbors=_topology(t)
    boundary_vertices={v for e,ids in edges.items() if len(ids)!=2 for v in e}
    locked=set()
    # Preserve each local support-region boundary rather than every interior
    # triangle vertex. This avoids losing a high ledge at the slope threshold.
    for threshold in (min_up_normal_y,math.cos(math.radians(42))):
        support_edges,_,_=_topology(t[normals[:,1]>=threshold])
        locked.update(v for edge,ids in support_edges.items() if len(ids)!=2 for v in edge)
    clusters={i:[i] for i in range(len(p))}
    representatives=np.arange(len(p),dtype=np.int64)
    face_source=np.arange(len(t),dtype=np.int64)
    cos_normal=math.cos(math.radians(max_normal_change_degrees))
    collapses=[];rejections={}
    while len(t)>max_triangles or len(np.unique(t))>max_vertices:
        edges,faces,neighbors=_topology(t)
        normals,areas=_normals(p,t)
        candidates=[]
        for (a,b),ids in edges.items():
            if len(ids)!=2 or a in locked and b in locked:continue
            distance=float(np.linalg.norm(p[a]-p[b]))
            if distance>2*max_error:continue
            for drop,keep in ((b,a),(a,b)):
                if drop not in locked:
                    if keep not in locked:candidates.append((distance,0,keep,drop,ids,(p[keep]+p[drop])*.5))
                    candidates.append((distance,1,keep,drop,ids,p[keep]))
        candidates.sort(key=lambda x:x[:4])
        accepted=False
        for distance,mode,keep,drop,ids,target in candidates:
            opposite={int(v) for v in t[ids].ravel() if v not in (keep,drop)}
            if neighbors[keep]&neighbors[drop]!=opposite:
                rejections['link']=rejections.get('link',0)+1;continue
            cluster=clusters[keep]+clusters[drop]
            if float(np.linalg.norm(source[cluster]-target,axis=1).max())>max_error+1e-12:
                rejections['cumulative_error']=rejections.get('cumulative_error',0)+1;continue
            affected=np.asarray(sorted(faces[drop]|faces[keep]),dtype=np.int64)
            changed=t[affected].copy();changed[changed==drop]=keep
            good=(changed[:,0]!=changed[:,1])&(changed[:,1]!=changed[:,2])&(changed[:,2]!=changed[:,0])
            surviving=affected[good]
            trial_p=p.copy();trial_p[keep]=target
            new_n,new_area=_normals(trial_p,changed[good])
            if (np.any(new_area<=1e-8) or np.any(np.sum(new_n*source_normals[face_source[surviving]],axis=1)<cos_normal)):
                rejections['normal_or_area']=rejections.get('normal_or_area',0)+1;continue
            if any(np.any((new_n[:,1]>=threshold)!=(source_normals[face_source[surviving],1]>=threshold))
                   for threshold in (min_up_normal_y,math.cos(math.radians(42)))):
                rejections['support_class_change']=rejections.get('support_class_change',0)+1;continue
            candidate=t.copy();candidate[affected]=changed
            alive=np.ones(len(t),dtype=bool);alive[affected[~good]]=False
            candidate=candidate[alive]
            if len(np.unique(np.sort(candidate,axis=1),axis=0))!=len(candidate):
                rejections['duplicate_face']=rejections.get('duplicate_face',0)+1;continue
            p=trial_p;t=candidate;face_source=face_source[alive]
            representatives[clusters[drop]]=keep
            clusters[keep]=cluster;del clusters[drop]
            collapses.append((drop,keep,distance,mode));accepted=True
            break
        if not accepted:
            raise ValueError('simplification_budget_unreachable:'+str(len(t))+'tri/'+str(len(np.unique(t)))+'vertices')
    # A surviving triangle is the affine image of its original indexed triangle.
    # Every removed triangle maps to a final edge/point still contained in the
    # mesh. This provides a two-sided geometric distance bound, not just samples.
    active=np.unique(t);index=np.full(len(p),-1,dtype=np.int64);index[active]=np.arange(len(active))
    out_p=p[active];out_t=index[t];mapped=representatives[source_t]
    final_faces={tuple(sorted(map(int,row))) for row in t}
    final_edges={tuple(sorted((int(a),int(b)))) for a,b,c in t for a,b in ((a,b),(b,c),(c,a))}
    final_vertices=set(map(int,active))
    for row in mapped:
        unique=tuple(sorted(set(map(int,row))))
        if (len(unique)==3 and unique not in final_faces or len(unique)==2 and unique not in final_edges
                or len(unique)==1 and unique[0] not in final_vertices):
            raise ValueError('simplification_correspondence_not_covered')
    displacement=np.linalg.norm(source-p[representatives],axis=1)
    changed_values=displacement[displacement>0]
    output_normals,_=_normals(out_p,out_t)
    bound=float(displacement.max())
    report=dict(algorithm=ALGORITHM,source_vertex_count=len(positions)//3 if np.asarray(positions).ndim==1 else len(positions),
        source_unique_vertices=len(source),source_triangles=len(source_t),vertex_count=len(out_p),triangle_count=len(out_t),
        collapses=len(collapses),max_error_requested=float(max_error),certified_bidirectional_surface_distance_bound=bound,
        max_vertex_displacement=bound,mean_vertex_displacement=float(displacement.mean()),
        p95_vertex_displacement=float(np.quantile(displacement,.95)),
        changed_vertex_count=int(len(changed_values)),
        protected_support_triangles=int(protected_faces.sum()),support_triangles_preserved_exactly=False,min_up_normal_y=min_up_normal_y,
        support_height_error=None,support_outline_error=None,new_local_support_triangles=None,
        boundary_vertices_locked=len(locked),normal_change_limit_degrees=float(max_normal_change_degrees),
        max_normal_change_degrees=float(np.degrees(np.arccos(np.clip(np.sum(output_normals*source_normals[face_source],axis=1),-1,1))).max()),
        source_bounds_min=source.min(0).tolist(),source_bounds_max=source.max(0).tolist(),
        bounds_min=out_p.min(0).tolist(),bounds_max=out_p.max(0).tolist(),
        rejected_candidates=rejections,correspondence_verified=True,native_collision_verified=False,
        certificate=dict(source_vertex_to_output_vertex=index[representatives].tolist(),output_face_to_source_face=face_source.tolist()),
        guarantee_scope='input render surface, definition units; world error = bound * abs(RPC.scale)',
        limitations=['not a native collider','not a guaranteed outer envelope; inward and outward deviation both bounded',
                    'all support faces may differ within the global surface bound; height/outline audited separately',
                    'resource/transform/static-part eligibility must be checked by caller'])
    return out_p,out_t,report
