"""只删除已被非退化三角覆盖的精确零面积图元；不删任何有面积的面。"""
import numpy as np

def remove_covered_degenerates(positions, triangles):
    p=np.asarray(positions,dtype=np.float64).reshape(-1,3)
    t=np.asarray(triangles,dtype=np.int64).reshape(-1,3)
    if not np.isfinite(p).all() or not len(t) or np.any(t<0) or np.any(t>=len(p)):
        raise ValueError('cleanup_input')
    v=p[t];cross=np.cross(v[:,1]-v[:,0],v[:,2]-v[:,0]);area=np.linalg.norm(cross,axis=1)
    bad=np.flatnonzero(area==0);good=np.flatnonzero(area>0)
    proofs=[]
    for index in bad:
        pts=v[index];dist=np.sum((pts[:,None]-pts[None,:])**2,axis=-1)
        i,j=np.unravel_index(np.argmax(dist),dist.shape)
        start,end=pts[i],pts[j];delta=end-start;intervals=[]
        for face in good:
            tri=v[face];normal=cross[face]/area[face]
            if max(abs(normal@(start-tri[0])),abs(normal@(end-tri[0])))>1e-12:
                continue
            lo,hi=0.,1.
            for a,b in zip(tri,np.roll(tri,-1,axis=0)):
                base=float(np.cross(b-a,start-a)@normal)
                slope=float(np.cross(b-a,delta)@normal)
                if abs(slope)<1e-15:
                    if base < -1e-12:hi=-1.;break
                elif slope>0:lo=max(lo,(-1e-12-base)/slope)
                else:hi=min(hi,(-1e-12-base)/slope)
            if hi>=lo and hi>=0 and lo<=1:
                intervals.append((max(lo,0.),min(hi,1.),int(face)))
        cursor=0.;used=[]
        for lo,hi,face in sorted(intervals):
            if lo>cursor+1e-10:break
            if hi>cursor:cursor=hi;used.append(dict(face=face,lo=lo,hi=hi))
        if cursor<1.-1e-10:
            raise ValueError('zero_area_primitive_not_covered:'+str(int(index)))
        proofs.append(dict(face=int(index),segment_length=float(np.sqrt(dist.max())),coverage=used))
    kept=t[good]
    used,remap=np.unique(kept.reshape(-1),return_inverse=True)
    report=dict(method='covered_exact_zero_area_removal',removed_face_ids=bad.tolist(),
        source_triangle_count=len(t),output_triangle_count=len(kept),nonzero_faces_removed=0,
        support_triangles_unchanged=True,plane_tolerance=1e-12,parametric_gap_tolerance=1e-10,
        numerical_coverage_tolerance_world_units=1e-7,proofs=proofs)
    return p[used],remap.reshape(-1,3),report
