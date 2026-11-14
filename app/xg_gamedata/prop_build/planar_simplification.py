"""同向共面顶点星形重三角化：保留投影边界，不以包围盒填孔。"""
from __future__ import annotations
import math
import numpy as np
from .simplify_collision import _normals, _topology


def _candidate(p,t,drop,keep,faces,neighbors,edges,tolerance):
    incident=np.asarray(sorted(faces[drop]),dtype=np.int64)
    if len(incident)<3:return None
    if any(len(edges[tuple(sorted((drop,v)))])!=2 for v in neighbors[drop]):return None
    shared=edges[tuple(sorted((drop,keep)))]
    if len(shared)!=2:return None
    opposite={int(v) for v in t[shared].ravel() if v not in (keep,drop)}
    if neighbors[keep]&neighbors[drop]!=opposite:return None
    old=t[incident];normal,area=_normals(p,old)
    if np.any(area<=1e-9):return None
    anchor=p[old[0,0]];axis=normal[np.argmax(area)]
    if np.any(normal@axis<1-1e-8):return None
    used=np.unique(old);delta=float(np.abs((p[used]-anchor)@axis).max())
    if delta>tolerance:return None
    new=old.copy();new[new==drop]=keep
    good=(new[:,0]!=new[:,1])&(new[:,1]!=new[:,2])&(new[:,2]!=new[:,0])
    new=new[good]
    normals,areas=_normals(p,new)
    if len(new)!=len(old)-2 or np.any(areas<=1e-8) or np.any(normals@axis<1-1e-8):return None
    # Oriented boundary chains cancel the same internal edges. With positive
    # projected triangle orientation this proves equal winding/covered regions.
    def boundary(tris):
        result={}
        for a,b,c in tris:
            for x,y in ((a,b),(b,c),(c,a)):
                x,y=int(x),int(y);key=(min(x,y),max(x,y))
                result[key]=result.get(key,0)+(1 if x<y else -1)
        return {k:v for k,v in result.items() if v}
    if boundary(old)!=boundary(new):return None
    return incident,new,dict(drop=int(drop),keep=int(keep),plane_distance=delta,
                            surface_distance_bound=2*delta)


def simplify_planar(positions,triangles,*,target=2048,tolerance=2e-8,max_steps=20000):
    raw=np.asarray(positions,dtype=np.float64).reshape(-1,3)
    raw_t=np.asarray(triangles,dtype=np.int64).reshape(-1,3)
    p,remap=np.unique(raw,axis=0,return_inverse=True);t=remap[raw_t]
    if not np.isfinite(p).all() or raw_t.min()<0 or raw_t.max()>=len(raw):raise ValueError('planar_source')
    changes=[]
    while len(t)>target and len(changes)<max_steps:
        edges,faces,neighbors=_topology(t);changed=False
        for drop in sorted(faces):
            # Try nearest neighbour first, but the bound is plane distance,
            # not a false assertion that the displaced vertex barely moved.
            for keep in sorted(neighbors[drop],key=lambda x:(np.linalg.norm(p[x]-p[drop]),x)):
                result=_candidate(p,t,drop,keep,faces,neighbors,edges,tolerance)
                if result is None:continue
                incident,new,proof=result
                live=np.ones(len(t),dtype=bool);live[incident]=False
                trial=np.concatenate((t[live],new))
                if len(np.unique(np.sort(trial,axis=1),axis=0))!=len(trial):continue
                t=trial;changes.append(proof);changed=True;break
            if changed:break
        if not changed:break
    used=np.unique(t);indices=np.full(len(p),-1,dtype=np.int64);indices[used]=np.arange(len(used))
    bound=sum(row['surface_distance_bound'] for row in changes)
    return p[used],indices[t],dict(method='positive_coplanar_star_retriangulation_v1',
        input_vertices=len(raw),input_triangles=len(raw_t),vertices=len(used),triangles=len(t),
        tolerance=tolerance,steps=changes,certified_bidirectional_surface_distance_bound=bound,
        max_single_plane_distance=max((row['plane_distance'] for row in changes),default=0.),
        support_surface_distance_bound=bound,support_outline_projected_error=0.,
        proof='same oriented boundary chain and positive coplanar triangulations; plane residual accumulated',
        native_collision_verified=False)


def verify_planar(positions,triangles,out_positions,out_triangles,certificate):
    raw=np.asarray(positions,dtype=np.float64).reshape(-1,3)
    p,remap=np.unique(raw,axis=0,return_inverse=True)
    t=remap[np.asarray(triangles,dtype=np.int64).reshape(-1,3)]
    bound=0.
    for step in certificate['steps']:
        edges,faces,neighbors=_topology(t)
        result=_candidate(p,t,step['drop'],step['keep'],faces,neighbors,edges,certificate['tolerance'])
        if result is None:raise ValueError('planar_step_unverified')
        incident,new,proof=result
        if proof!=step:raise ValueError('planar_certificate_changed')
        live=np.ones(len(t),dtype=bool);live[incident]=False;t=np.concatenate((t[live],new))
        if len(np.unique(np.sort(t,axis=1),axis=0))!=len(t):raise ValueError('planar_duplicate_face')
        bound+=proof['surface_distance_bound']
    used=np.unique(t);indices=np.full(len(p),-1,dtype=np.int64);indices[used]=np.arange(len(used))
    if not np.array_equal(p[used],np.asarray(out_positions).reshape(-1,3)) or not np.array_equal(indices[t],np.asarray(out_triangles).reshape(-1,3)):
        raise ValueError('planar_output_changed')
    # Match Python 3.12 sum's compensated floating summation used by builder.
    bound=sum(step['surface_distance_bound'] for step in certificate['steps'])
    if bound!=certificate['certified_bidirectional_surface_distance_bound']:raise ValueError('planar_bound_changed')
    return dict(verified=True,bidirectional_surface_distance_bound=bound,steps=len(certificate['steps']))
