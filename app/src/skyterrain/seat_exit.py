"""有界、请求级座位出口规划；不移除 worker 或共享地形中的碰撞体。

坐姿原点不是已站立脚点。实测曲线保留为首选，当前 Actor 的脚偏移、
静态地形支撑投影和近邻搜索属于明确的工程适配，不冒充原生轨迹。
"""
from __future__ import annotations
import copy
import math
import numpy as np
from .props import _rotation

MAX_PATH_POINTS = 768
MAX_SEARCH_CANDIDATES = 512
SEGMENT_STEP = .05
EXIT_SPEED = 1.5


def _point(p):
    if not isinstance(p,(list,tuple)) or len(p)!=3 or any(type(v) not in (int,float)
            or not math.isfinite(v) or abs(v)>5000 for v in p):
        raise ValueError('seat_exit_point')
    return tuple(float(v) for v in p)


def _geometry(terrain):
    import stuck_recovery
    return stuck_recovery._Geometry(terrain,stuck_recovery.RecoveryPolicy())


def _provenance(surface,prop_id):
    rows=[r for r in getattr(surface,'collider_ranges',()) if r.get('id')==prop_id]
    return rows[0] if len(rows)==1 else None


def _exclusion(terrain,prop,marker,start):
    """只豁免这个 marker 的外侧接触带及起点已占据的本实例接触体积。

    不知道附件 mesh 分组时不猜测全件禁碰撞。三角必须整体位于已知侧座
    小邻域内；跨桌长三角被裁剪而非整片丢弃。接触区外的桌体、所有静态/
    其它道具面仍参与检查；身体离开本实例后该豁免不可再启用。
    """
    row=_provenance(terrain.surface,prop.get('id'))
    if row is None:raise ValueError('seat_exit_prop_not_in_surface')
    if prop.get('name')!='TableXmas' or marker not in (0,3):
        raise ValueError('seat_exit_marker_unverified')
    scale=prop.get('scale')
    if type(scale) not in (int,float) or not math.isfinite(scale) or scale<=0:
        raise ValueError('seat_exit_prop_scale')
    R=_rotation(prop['rotation']);origin=np.asarray(_point(prop['position']))
    side=-1 if marker==0 else 1
    marker_x=side*14.51*.075
    seat_local=(np.asarray(start)-origin)@R/scale
    if abs(seat_local[0]-side)>.25 or abs(seat_local[2])>.35 or not .3<=seat_local[1]<=1.3:
        raise ValueError('seat_exit_start_marker_mismatch')
    initial=sorted(_geometry(terrain).intersecting(start))
    if any(i<getattr(terrain.surface,'static_surface',terrain.surface).triangle_count for i in initial):
        raise ValueError('seat_exit_initial_static_overlap')
    if any(not row['start']<=i<row['start']+row['count'] for i in initial):
        raise ValueError('seat_exit_initial_other_prop_overlap')
    faces=[]
    for face in range(row['start'],row['start']+row['count']):
        local=(terrain.surface._tri[face]-origin)@R/scale
        if (np.any(local[:,0]*side >= abs(marker_x)-.45)
                and np.all(np.abs(local[:,2])<=.6)
                and np.all((-0.2<=local[:,1])&(local[:,1]<=1.6))):
            faces.append(face-row['start'])
    if len(faces)>2048:raise ValueError('seat_exit_contact_budget')
    normal=R[:,0]*side
    plane=float(normal@origin+scale*(abs(marker_x)-.45))
    offset=np.asarray(terrain.offset)
    box_min=(np.asarray(start)-offset+np.asarray([-.28,.14,-.28])).tolist()
    box_max=(np.asarray(start)-offset+np.asarray([.28,1.81,.28])).tolist()
    # 身体最初已占据的自接触体积，不把横跨整桌的长三角整片豁免。
    faces=sorted(set(faces)|{i-row['start'] for i in initial if row['start']<=i<row['start']+row['count']})
    return dict(prop_id=prop['id'],marker=marker,geometry_sha256=row['geometry_sha256'],
        local_faces=faces,clip_plane=dict(normal=normal.tolist(),constant=plane),
        initial_contact_box=dict(min=box_min,max=box_max),
        initial_self_contacts=len([i for i in initial if row['start']<=i<row['start']+row['count']]),
        scope='same_instance_marker_outer_contact_strip_clipped_not_whole_triangles',
        expires='exit_complete_or_collision_revision_changed')


def _masked(terrain,exclusion):
    row=_provenance(terrain.surface,exclusion.get('prop_id'))
    if row is None or row.get('geometry_sha256')!=exclusion.get('geometry_sha256'):
        raise ValueError('seat_exit_geometry_changed')
    local=exclusion.get('local_faces')
    if not isinstance(local,list) or len(local)>2048 or any(type(i) is not int
            or not 0<=i<row['count'] for i in local):raise ValueError('seat_exit_mask')
    if not local:return terrain
    key=(id(terrain.surface),exclusion['geometry_sha256'],exclusion['prop_id'],tuple(local),
         tuple(exclusion['clip_plane']['normal']),exclusion['clip_plane']['constant'],
         tuple(exclusion['initial_contact_box']['min']),tuple(exclusion['initial_contact_box']['max']))
    cached=getattr(terrain,'_seat_exit_mask_cache',None)
    if cached and cached[0]==key:return cached[1]
    normal=np.asarray(_point(exclusion['clip_plane']['normal']))
    constant=float(exclusion['clip_plane']['constant'])
    if not math.isfinite(constant) or abs(np.linalg.norm(normal)-1)>1e-6:raise ValueError('seat_exit_clip_plane')
    ignored=set(row['start']+i for i in local)
    def clip(polygon,n,c):
        result=[]
        for a,b in zip(polygon,polygon[1:]+polygon[:1]):
            da=float(a@n)-c;db=float(b@n)-c
            if da<=0:result.append(a)
            if (da<=0)!=(db<=0):result.append(a+(b-a)*da/(da-db))
        return result
    low=np.asarray(_point(exclusion['initial_contact_box']['min']))
    high=np.asarray(_point(exclusion['initial_contact_box']['max']))
    if np.any(high<=low) or np.any(high-low>[.561,1.671,.561]):raise ValueError('seat_exit_contact_box')
    def outside_box(polygon):
        pieces=[];inside=polygon
        for k in range(3):
            for side,bound in [(1,high[k]),(-1,-low[k])]:
                n=np.eye(3)[k]*side
                outside=clip(inside,-n,-bound)
                if len(outside)>=3:pieces.append(outside)
                inside=clip(inside,n,bound)
                if not inside:return pieces
        return pieces
    clipped=[];replacement={}
    for face in ignored:
        begin=len(clipped)
        polygon=list(terrain.surface._tri[face])
        result=clip(polygon,normal,constant)
        for piece in outside_box(result):
            for i in range(1,len(piece)-1):
                tri=np.asarray([piece[0],piece[i],piece[i+1]])
                if np.linalg.norm(np.cross(tri[1]-tri[0],tri[2]-tri[0]))>1e-12:clipped.append(tri)
        replacement[face]=list(range(begin,len(clipped)))
    surface=copy.copy(terrain.surface);original=terrain.surface._ids
    count=len(surface._tri)
    if clipped:
        tri=np.asarray(clipped,dtype=np.float64)
        cross=np.cross(tri[:,1]-tri[:,0],tri[:,2]-tri[:,0]);length=np.linalg.norm(cross,axis=1)
        fields=dict(_tri=tri,_lo=tri.min(axis=1),_hi=tri.max(axis=1),
            _ny=np.divide(cross[:,1],length,out=np.zeros_like(length),where=length>1e-12))
        for field,value in fields.items():setattr(surface,field,np.concatenate((getattr(surface,field),value)))
    # 裁剪片严格包含于源三角，复用源bucket即可；不经过GroundSurface的Float32重新量化。
    def ids(x,z):
        out=[]
        for i in original(x,z):
            if i in ignored:out.extend(count+j for j in replacement[i])
            else:out.append(i)
        return np.asarray(out,dtype=np.int32)
    surface._ids=ids
    result=copy.copy(terrain);result.surface=surface
    terrain._seat_exit_mask_cache=(key,result)
    return result


def _dense(points):
    out=[tuple(points[0])]
    for a,b in zip(points,points[1:]):
        n=max(1,math.ceil(math.dist(a,b)/SEGMENT_STEP))
        for j in range(1,n+1):
            p=tuple(a[k]+(b[k]-a[k])*j/n for k in range(3))
            if math.dist(out[-1],p)>1e-8:out.append(p)
        if len(out)>MAX_PATH_POINTS:raise ValueError('seat_exit_path_budget')
    return out


def _ground_rows(terrain,point,max_delta=1.0):
    return sorted({float(y) for _,y,ny in terrain.hits(point[0],point[2])
        if ny>=math.cos(math.radians(42)) and abs(y-point[1])<=max_delta},
        key=lambda y:(abs(y-point[1]),-y))


def _candidates(terrain,goal,max_search):
    # 精确实测优先；若不支持，支撑投影及环形点按真实三维距离排序。
    points=[goal];seen={tuple(round(v,7) for v in goal)}
    for radius in [0.]+[.25*i for i in range(1,int(max_search/.25)+1)]:
        count=1 if radius==0 else max(8,math.ceil(math.tau*radius/.25))
        for i in range(count):
            x=goal[0]+radius*math.cos(math.tau*i/count)
            z=goal[2]+radius*math.sin(math.tau*i/count)
            for y in _ground_rows(terrain,(x,goal[1],z)):
                p=(x,y,z);key=tuple(round(v,7) for v in p)
                if key not in seen:points.append(p);seen.add(key)
    return [points[0]]+sorted(points[1:],key=lambda p:(math.dist(goal,p),p))[:MAX_SEARCH_CANDIDATES-1]


def _route(terrain,start,preferred,goal,measured_goal):
    # 保留实测横向曲线；需要向上修正时贴当前 Actor 支撑面而不是穿地。
    points=[start]+preferred
    if math.dist(points[-1],goal)>1e-8:points.append(goal)
    raw=_dense(points);projected=[start]
    for p in raw[1:-1]:
        rows=_ground_rows(terrain,p)
        projected.append((p[0],max(p[1],rows[0]) if rows else p[1],p[2]))
    projected.append(goal)
    return _dense(projected)


def validate_exit_segment(terrain,plan,a,b):
    """每帧用当前完整地形复核；仅应用 plan 已绑定的临时初始接触豁免。"""
    try:
        a,b=_point(a),_point(b)
        if plan.get('status')!='ready' or math.dist(a,b)>8:return False
        geom=_geometry(_masked(terrain,plan['exclusion']));full=_geometry(terrain)
        released=bool(plan.get('_contact_cleared'))
        points=_dense([a,b])
        if (full if released else geom).intersecting(a):return False
        for x,y in zip(points,points[1:]):
            if (full if released else geom).intersecting(x,y):return False
            if not released and not full.intersecting(y):released=True
        if released or not full.intersecting(b):plan['_contact_cleared']=True
        return True
    except (ValueError,TypeError,KeyError,AttributeError,OverflowError):return False


def plan_exit(terrain,prop,marker,start,preferred_path,*,max_search=1.5):
    """返回可连续平移的 path；失败显式交上层现有脱困，不生成传送点。"""
    try:
        if terrain is None or not getattr(terrain,'has_hits',False):raise ValueError('seat_exit_geometry_missing')
        start=_point(start)
        if type(max_search) not in (int,float) or not 0<=max_search<=1.5:raise ValueError('seat_exit_search_budget')
        if not isinstance(preferred_path,(tuple,list)) or not 1<=len(preferred_path)<=128:raise ValueError('seat_exit_profile_shape')
        preferred=[_point(row['point']) for row in preferred_path]
        elapsed=[row['elapsed'] for row in preferred_path]
        if (any(type(t) not in (int,float) or not math.isfinite(t) or t<0 or t>3 for t in elapsed)
                or any(a>b for a,b in zip(elapsed,elapsed[1:]))):raise ValueError('seat_exit_profile_time')
        if any(math.dist(start,p)>8 for p in preferred):raise ValueError('seat_exit_profile_extent')
        exclusion=_exclusion(terrain,prop,marker,start);masked=_masked(terrain,exclusion)
        geom=_geometry(masked);full=_geometry(terrain);goal=preferred[-1]
        collisions={};geometry_calls=0;geometry_cache_hits=0
        def collides(complete,a,b=None):
            nonlocal geometry_calls,geometry_cache_hits
            key=(complete,tuple(a),None if b is None else tuple(b))
            if key in collisions:geometry_cache_hits+=1;return collisions[key]
            result=bool((full if complete else geom).intersecting(a,b));geometry_calls+=1
            if len(collisions)<8192:collisions[key]=result
            return result
        checked=0;support_rejected=0;path_rejected=0
        for candidate in _candidates(terrain,goal,max_search):
            checked+=1
            if not full.standable(candidate):support_rejected+=1;continue
            routes=[('measured_curve_projected',_route(terrain,start,preferred,candidate,goal)),
                    ('engineering_direct_support_route',_route(terrain,start,[],candidate,goal))]
            selected=None
            for route_kind,points in routes:
                released=False;safe=not collides(False,start)
                for a,b in zip(points,points[1:]):
                    if collides(released,a,b):safe=False;break
                    if not released and not collides(True,b):released=True
                if safe:selected=(route_kind,points);break
            if selected is None:path_rejected+=1;continue
            route_kind,points=selected
            distance=0.;times=[0.]
            for a,b in zip(points,points[1:]):distance+=math.dist(a,b);times.append(distance/EXIT_SPEED)
            duration=max(float(elapsed[-1]),times[-1],1.)
            if times[-1]>0:times=[t*duration/times[-1] for t in times]
            result=dict(status='ready',points=[list(p) for p in points],times=times,
                exclusion=exclusion,measured_goal=list(goal),selected_goal=list(candidate),
                projection_error=dict(vector=[candidate[k]-goal[k] for k in range(3)],
                    meters=math.dist(candidate,goal),horizontal=math.hypot(candidate[0]-goal[0],candidate[2]-goal[2])),
                reason='measured_exit' if math.dist(candidate,goal)<1e-6 else 'measured_exit_support_search',
                route_kind=route_kind,
                checked=checked,support_rejected=support_rejected,path_rejected=path_rejected,
                geometry_calls=geometry_calls,geometry_cache_hits=geometry_cache_hits,
                full_geometry_endpoint=True,original_capture_replay=False)
            return result
        return dict(status='blocked',reason='seat_exit_no_safe_path',checked=checked,
            support_rejected=support_rejected,path_rejected=path_rejected,exclusion=exclusion,
            measured_goal=list(goal),fallback='stuck_recovery')
    except (ValueError,TypeError,KeyError,AttributeError,OverflowError) as exc:
        return dict(status='blocked',reason=str(exc),fallback='stuck_recovery')
