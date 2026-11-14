"""确定性有界短边折叠；不填凸包、不制造包围盒或可站面。

输入已换成定义世界单位的静态三角网格。此模块只证明对给定数组的几何
近似误差；调用者仍须验证资源、静态部分、缩放与碰撞声明。
"""
from __future__ import annotations
import math
import numpy as np

ALGORITHM = 'protected-support-short-edge-v1'


def _normals(p,t):
    v=p[t];cross=np.cross(v[:,1]-v[:,0],v[:,2]-v[:,0]);length=np.linalg.norm(cross,axis=1)
    return cross/np.maximum(length[:,None],1e-300),length


def _topology(t):
    edges={};faces={};neighbors={}
    for i,(a,b,c) in enumerate(t):
        a,b,c=int(a),int(b),int(c)
        for v in (a,b,c):faces.setdefault(v,set()).add(i)
        for x,y in ((a,b),(b,c),(c,a)):
            edges.setdefault((min(x,y),max(x,y)),[]).append(i)
            neighbors.setdefault(x,set()).add(y);neighbors.setdefault(y,set()).add(x)
    return edges,faces,neighbors


def _sliver_coverage(p,t,face,kept,*,max_error):
    v=p[t[face]]
    pairs=((0,1),(1,2),(2,0));a,b=max(pairs,key=lambda pair:float(np.linalg.norm(v[pair[1]]-v[pair[0]])))
    start,end=v[a],v[b];direction=end-start;length=float(np.linalg.norm(direction))
    if length<=1e-12:raise ValueError('sliver_point_needs_separate_proof')
    k=3-a-b
    along=float(np.clip(np.dot(v[k]-start,direction)/(length*length),0.,1.))
    width=float(np.linalg.norm(v[k]-(start+along*direction)))
    if width>=max_error:raise ValueError('sliver_width_exceeds_error')
    intervals=[]
    for i in kept:
        tri=p[t[i]];e1,e2=tri[1]-tri[0],tri[2]-tri[0]
        normal=np.cross(e1,e2);normal/=np.linalg.norm(normal)
        gram=np.array([[np.dot(e1,e1),np.dot(e1,e2)],[np.dot(e1,e2),np.dot(e2,e2)]])
        try:
            uv0=np.linalg.solve(gram,np.array([np.dot(start-tri[0],e1),np.dot(start-tri[0],e2)]))
            uvd=np.linalg.solve(gram,np.array([np.dot(direction,e1),np.dot(direction,e2)]))
        except np.linalg.LinAlgError:continue
        d0,dd=float(np.dot(start-tri[0],normal)),float(np.dot(direction,normal))
        lo,hi=0.,1.;plane_tolerance=(max_error-width)*.5
        for c,slope in ((uv0[0],uvd[0]),(uv0[1],uvd[1]),(1-uv0.sum(),-uvd.sum()),
                        (plane_tolerance-d0,-dd),(plane_tolerance+d0,dd)):
            if abs(slope)<1e-20:
                if c<0:lo,hi=1.,0.;break
            elif slope>0:lo=max(lo,float(-c/slope))
            else:hi=min(hi,float(-c/slope))
        if lo>hi or hi<0 or lo>1:continue
        lo,hi=max(0.,lo),min(1.,hi)
        # Endpoint barycentrics bound the full affine interval, not merely samples.
        bary=np.array([uv0+lo*uvd,uv0+hi*uvd]);extra=max(0.,float(-bary.min()),float(bary.sum(axis=1).max()-1))
        roundoff=2*extra*float(np.linalg.norm(e1)+np.linalg.norm(e2))+1e-10
        line=np.array([start+lo*direction,start+hi*direction])
        projected=tri[0]+bary[:,0,None]*e1+bary[:,1,None]*e2
        dist=float(np.linalg.norm(line-projected,axis=1).max())+roundoff
        intervals.append(dict(face=int(i),start=lo,end=hi,plane_distance_bound=dist))
    intervals.sort(key=lambda row:(row['start'],row['end'],row['face']))
    cover=[];cursor=0.;gap=0.;surface_error=0.
    for row in intervals:
        if row['end']<=cursor:continue
        gap=max(gap,row['start']-cursor);cursor=max(cursor,row['end']);cover.append(row)
        surface_error=max(surface_error,row['plane_distance_bound'])
        if cursor>=1:break
    gap=max(gap,1-cursor)
    bound=width+surface_error+max(0.,gap)*length
    if not cover or bound>max_error:raise ValueError('sliver_not_covered_within_error')
    return dict(face=int(face),longest_edge_vertices=[int(t[face,a]),int(t[face,b])],
                triangle_to_segment_bound=width,segment_length=length,
                coverage_gap_parameter=max(0.,gap),coverage_surface_distance_bound=surface_error,
                whole_triangle_distance_bound=bound,intervals=cover)


def remove_covered_slivers(positions,triangles,*,max_twice_area=1e-8,max_error=1e-7,min_up_normal_y=.65):
    """只删已证明整片接近其它保留面的非支撑薄片；不是按面积盲删。

    全薄片先投到最长边（距离界=最大高），再证明边的完整参数区间落在
    保留三角形投影中。所有点而非仅三个顶点/中点都在误差界内。
    """
    p=np.asarray(positions,dtype=float).reshape(-1,3);t=np.asarray(triangles)
    if t.ndim==1:t=t.reshape(-1,3)
    if (not np.isfinite(p).all() or t.dtype.kind not in 'iu' or not len(t) or t.min()<0 or t.max()>=len(p)
            or not 0<max_twice_area<=1e-8 or not 0<max_error<=1e-7 or not .5<=min_up_normal_y<=1):
        raise ValueError('sliver_input')
    normals,areas=_normals(p,t);small=np.flatnonzero(areas<=max_twice_area);kept=np.flatnonzero(areas>max_twice_area)
    proofs=[]
    for i in small:
        if normals[i,1]>=min_up_normal_y:raise ValueError('sliver_protected_support_face:'+str(int(i)))
        proofs.append(_sliver_coverage(p,t,int(i),kept,max_error=max_error))
    bound=max((row['whole_triangle_distance_bound'] for row in proofs),default=0.)
    return p,t[kept],dict(method='non_support_sliver_whole_triangle_coverage_v1',removed_faces=[int(i) for i in small],
        kept_source_faces=kept.tolist(),max_twice_area=max_twice_area,max_error_requested=max_error,min_up_normal_y=min_up_normal_y,
        certified_bidirectional_surface_distance_bound=bound,protected_support_faces_removed=0,
        proof_scope='whole triangle -> longest segment -> union of retained triangle intervals; no vertex-only shortcut',proofs=proofs)


def verify_sliver_cleanup(positions,triangles,out_positions,out_triangles,report):
    p,t,proof=remove_covered_slivers(positions,triangles,max_twice_area=report['max_twice_area'],
                                   max_error=report['max_error_requested'],min_up_normal_y=report['min_up_normal_y'])
    if not np.array_equal(p,np.asarray(out_positions).reshape(-1,3)) or not np.array_equal(t,np.asarray(out_triangles).reshape(-1,3)):
        raise ValueError('sliver_output_not_certified_deletion')
    if proof!=report:raise ValueError('sliver_certificate_changed')
    return dict(verified=True,certified_bidirectional_surface_distance_bound=proof['certified_bidirectional_surface_distance_bound'])


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
    if (not np.array_equal(source[source_t[protected]],p[mapped[protected]])
            or int(protected.sum())!=int(np.count_nonzero(normals[:,1]>=min_up_normal_y))):
        raise ValueError('certificate_support_changed')
    edges,_,_=_topology(source_t)
    boundary=sorted({v for e,ids in edges.items() if len(ids)!=2 for v in e})
    if boundary and not np.array_equal(source[boundary],p[mapping[boundary]]):raise ValueError('certificate_boundary_changed')
    angle=np.degrees(np.arccos(np.clip(np.sum(normals*source_n[face_mapping],axis=1),-1,1)))
    if np.any(areas<=1e-9) or np.any(source_area<=1e-9) or float(angle.max())>max_normal_change_degrees+1e-8:
        raise ValueError('certificate_normal_or_area')
    displacement=np.linalg.norm(source[used]-p[mapping[used]],axis=1)
    bound=float(displacement.max())
    if bound>max_error+1e-12:raise ValueError('certificate_error_exceeded')
    return dict(certified_bidirectional_surface_distance_bound=bound,support_height_error=0.,
                support_outline_error=0.,protected_support_triangles=int(protected.sum()),
                max_normal_change_degrees=float(angle.max()),verified=True)


def simplify(positions,triangles,*,max_vertices=2048,max_triangles=2048,
             max_error=.01,min_up_normal_y=.65,max_normal_change_degrees=15.):
    """返回(p,t,report)。预算无法达到时抛ValueError，不输出伪合格网格。

    所有原始向上候选三角逐顶点保留。边界/非流形顶点不折叠；每次边折叠
    验证link条件、重复面、翻面、法向变化和累计原顶点位移，禁止产生新的
    局部支持面。保留连通关系与孔洞；不是保证外包的凸碰撞近似。
    """
    if type(max_vertices) is not int or type(max_triangles) is not int or not 3<=max_vertices<=2048 or not 1<=max_triangles<=2048:
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
    locked=protected_vertices|boundary_vertices
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
            if np.any((new_n[:,1]>=min_up_normal_y)&(normals[surviving,1]<min_up_normal_y)):
                rejections['new_support']=rejections.get('new_support',0)+1;continue
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
    if (not np.array_equal(representatives[source_t[protected_faces]],source_t[protected_faces])
            or not np.array_equal(source[source_t[protected_faces]],p[representatives[source_t[protected_faces]]])):
        raise ValueError('simplification_support_changed')
    displacement=np.linalg.norm(source-p[representatives],axis=1)
    changed_values=displacement[displacement>0]
    output_normals,_=_normals(out_p,out_t)
    if np.count_nonzero(output_normals[:,1]>=min_up_normal_y)!=int(np.count_nonzero(protected_faces)):
        raise ValueError('simplification_support_count_changed')
    bound=float(displacement.max())
    report=dict(algorithm=ALGORITHM,source_vertex_count=len(positions)//3 if np.asarray(positions).ndim==1 else len(positions),
        source_unique_vertices=len(source),source_triangles=len(source_t),vertex_count=len(out_p),triangle_count=len(out_t),
        collapses=len(collapses),max_error_requested=float(max_error),certified_bidirectional_surface_distance_bound=bound,
        max_vertex_displacement=bound,mean_vertex_displacement=float(displacement.mean()),
        p95_vertex_displacement=float(np.quantile(displacement,.95)),
        changed_vertex_count=int(len(changed_values)),
        protected_support_triangles=int(protected_faces.sum()),support_triangles_preserved_exactly=True,min_up_normal_y=min_up_normal_y,
        support_height_error=0.,support_outline_error=0.,new_local_support_triangles=0,
        boundary_vertices_locked=len(boundary_vertices),normal_change_limit_degrees=float(max_normal_change_degrees),
        max_normal_change_degrees=float(np.degrees(np.arccos(np.clip(np.sum(output_normals*source_normals[face_source],axis=1),-1,1))).max()),
        source_bounds_min=source.min(0).tolist(),source_bounds_max=source.max(0).tolist(),
        bounds_min=out_p.min(0).tolist(),bounds_max=out_p.max(0).tolist(),
        rejected_candidates=rejections,correspondence_verified=True,native_collision_verified=False,
        certificate=dict(source_vertex_to_output_vertex=index[representatives].tolist(),output_face_to_source_face=face_source.tolist()),
        guarantee_scope='input render surface, definition units; world error = bound * abs(RPC.scale)',
        limitations=['not a native collider','not a guaranteed outer envelope; inward and outward deviation both bounded',
                    'local support faces exact; tilted support may differ within the global bound',
                    'resource/transform/static-part eligibility must be checked by caller'])
    return out_p,out_t,report
