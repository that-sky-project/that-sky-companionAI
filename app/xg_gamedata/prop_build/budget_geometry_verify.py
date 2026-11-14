"""独立重验TASK-j简化链；源数组由调用者从APK/raw读取。"""
import numpy as np
from .coverage_cleanup import remove_covered_degenerates
from .simplify_collision import remove_covered_slivers,verify_sliver_cleanup
from .planar_simplification import verify_planar
from .surface_simplification import verify_certificate


def verify_budget_geometry(doc,source_positions,source_triangles):
    p=np.asarray(source_positions,dtype=float).reshape(-1,3)
    t=np.asarray(source_triangles,dtype=np.int64).reshape(-1,3)
    # Exact source-array identity, not a tolerant shape or sampled hash check.
    def canonical(p,t):
        p,remap=np.unique(p,axis=0,return_inverse=True);return p,remap[t]
    a,b=canonical(p,t);c,d=canonical(np.asarray(doc['source_positions']).reshape(-1,3),np.asarray(doc['source_triangles']).reshape(-1,3))
    if not np.array_equal(a,c) or not np.array_equal(b,d):raise ValueError('budget_source_changed')
    pre=doc['preprocessing'];p,t,zero=remove_covered_degenerates(p,t)
    if zero!=pre['degenerate']:raise ValueError('budget_zero_cleanup_changed')
    q,u,sliver=remove_covered_slivers(p,t)
    if sliver!=pre['sliver']:raise ValueError('budget_sliver_cleanup_changed')
    verify_sliver_cleanup(p,t,q,u,sliver);p,t=q,u
    out_p=np.asarray(doc['positions']).reshape(-1,3);out_t=np.asarray(doc['triangles']).reshape(-1,3)
    simplify=doc.get('simplification')
    plane_p=np.asarray(simplify['input_positions']).reshape(-1,3) if simplify else out_p
    plane_t=np.asarray(simplify['input_triangles']).reshape(-1,3) if simplify else out_t
    planar=verify_planar(p,t,plane_p,plane_t,pre['planar'])
    error=sliver['certified_bidirectional_surface_distance_bound']+planar['bidirectional_surface_distance_bound']
    if simplify:
        proof=verify_certificate(plane_p,plane_t,out_p,out_t,simplify['certificate'],
            max_error=simplify['max_error_requested'],min_up_normal_y=simplify['min_up_normal_y'],
            max_normal_change_degrees=simplify['normal_change_limit_degrees'])
        error+=proof['certified_bidirectional_surface_distance_bound']
    if error!=doc['certified_combined_local_error'] or error>.02:raise ValueError('budget_error_changed')
    if len(out_p)>2048 or len(out_t)>2560:raise ValueError('budget_output_cap')
    return dict(verified=True,error=error,vertices=len(out_p),triangles=len(out_t))
