"""地面 motor 同尺寸的双面三角网胶囊查询（纯内存，无 worker/IO）。

Actor 坐标先减 TerrainView.offset 得到 feet；半径 .25、高 1.5、skin .015，
胶囊轴端为 feet.y + .265 / 1.265。距离算法为轴段对三角形的精确最短距，
不是身体 AABB。Node/Rapier 是交叉验收方；这里不创建导航可达性证明。
"""
from __future__ import annotations

import math
import numpy as np

RADIUS = .25
HEIGHT = 1.5
SKIN = .015
CONTACT_EPSILON = 1e-6


def _closest_points(points, tri):
    """批量点到三角形最近点：平面投影内部及三个闭合边的最小值。"""
    a, b, c = tri[:, 0], tri[:, 1], tri[:, 2]
    ab, ac = b-a, c-a
    n = np.cross(ab, ac)
    nn = np.einsum('ij,ij->i', n, n)
    delta = points-a
    scale = np.divide(np.einsum('ij,ij->i', delta, n), nn,
                      out=np.zeros(len(tri)), where=nn>1e-20)
    projected = points-scale[:, None]*n
    v = projected-a
    aa = np.einsum('ij,ij->i', ab, ab)
    bb = np.einsum('ij,ij->i', ab, ac)
    cc = np.einsum('ij,ij->i', ac, ac)
    d = np.einsum('ij,ij->i', v, ab)
    e = np.einsum('ij,ij->i', v, ac)
    det = aa*cc-bb*bb
    u = np.divide(cc*d-bb*e, det, out=np.zeros(len(tri)), where=abs(det)>1e-20)
    w = np.divide(aa*e-bb*d, det, out=np.zeros(len(tri)), where=abs(det)>1e-20)
    inside = (nn>1e-20) & (u>=0) & (w>=0) & (u+w<=1)
    candidates = [projected]
    distances = [np.where(inside, np.sum((points-projected)**2, axis=1), np.inf)]
    for p, q in ((a,b),(b,c),(c,a)):
        edge = q-p; ee = np.einsum('ij,ij->i', edge,edge)
        t = np.clip(np.divide(np.einsum('ij,ij->i',points-p,edge),ee,
                             out=np.zeros(len(tri)),where=ee>1e-20),0,1)
        cp = p+t[:,None]*edge
        candidates.append(cp);distances.append(np.sum((points-cp)**2,axis=1))
    k=np.argmin(np.stack(distances),axis=0)
    return np.stack(candidates)[k,np.arange(len(tri))]


def _segment_triangle(a,b,tri):
    count=len(tri); aa=np.broadcast_to(a,(count,3)); bb=np.broadcast_to(b,(count,3))
    left=[];right=[]
    for point in (aa,bb):
        left.append(point);right.append(_closest_points(point,tri))
    direction=b-a
    dd=float(direction@direction)
    for j,k in ((0,1),(1,2),(2,0)):
        p,q=tri[:,j],tri[:,k]; e=q-p; w=aa-p
        ee=np.einsum('ij,ij->i',e,e); de=e@direction
        dw=w@direction; ew=np.einsum('ij,ij->i',e,w)
        det=dd*ee-de*de
        s=np.divide(de*ew-ee*dw,det,out=np.zeros(count),where=abs(det)>1e-20)
        t=np.divide(dd*ew-de*dw,det,out=np.zeros(count),where=abs(det)>1e-20)
        valid=(abs(det)>1e-20)&(s>=0)&(s<=1)&(t>=0)&(t<=1)
        l=aa+s[:,None]*direction; r=p+t[:,None]*e
        # 无约束内部极小值不存在时用闭区间端点候选，避免假接触。
        l=np.where(valid[:,None],l,aa);r=np.where(valid[:,None],r,_closest_points(aa,tri))
        left.append(l);right.append(r)
        for endpoint in (p,q):
            v=np.clip(((endpoint-aa)@direction)/dd,0,1)
            left.append(aa+v[:,None]*direction);right.append(endpoint)
    # 轴段穿过三角形内部；端/边距离本身不会覆盖这个零距离解。
    n=np.cross(tri[:,1]-tri[:,0],tri[:,2]-tri[:,0]); den=n@direction
    fraction=np.divide(np.einsum('ij,ij->i',tri[:,0]-aa,n),den,
                       out=np.zeros(count),where=abs(den)>1e-20)
    ip=aa+fraction[:,None]*direction; cp=_closest_points(ip,tri)
    inside=(abs(den)>1e-20)&(fraction>=0)&(fraction<=1)&(np.sum((ip-cp)**2,axis=1)<1e-16)
    left.append(np.where(inside[:,None],ip,aa));right.append(np.where(inside[:,None],ip,_closest_points(aa,tri)))
    d=np.stack([np.sum((l-r)**2,axis=1) for l,r in zip(left,right)])
    pick=np.argmin(d,axis=0); ids=np.arange(count)
    return np.sqrt(d[pick,ids]),np.stack(left)[pick,ids],np.stack(right)[pick,ids]


class CapsuleProbe:
    def __init__(self,terrain):
        self.terrain=terrain;self.surface=getattr(terrain,'surface',None)
        if self.surface is None or not hasattr(self.surface,'_tri'):
            raise ValueError('capsule_triangle_geometry_required')
        self.offset=np.asarray(getattr(terrain,'offset',(0.,0.,0.)),dtype=float)
        self._cache={}

    def contacts(self,point):
        key=tuple(float(v) for v in point)
        if len(key)!=3 or not all(math.isfinite(v) and abs(v)<=1e6 for v in key):
            raise ValueError('capsule_point')
        if key not in self._cache:
            if len(self._cache)>=128:self._cache.pop(next(iter(self._cache)))
            self._cache[key]=self._contacts_uncached(key)
        return self._cache[key]

    def _contacts_uncached(self,point):
        p=np.asarray(point,dtype=float)-self.offset
        if p.shape!=(3,) or not np.isfinite(p).all():raise ValueError('capsule_point')
        s=self.surface; groups=[]
        # 复用 GroundSurface 的 XZ 桶及 AABB，空中每帧不构造 Python ID 大集合。
        for ix in range(math.floor((p[0]-RADIUS)/s.bucket),math.floor((p[0]+RADIUS)/s.bucket)+1):
            for iz in range(math.floor((p[2]-RADIUS)/s.bucket),math.floor((p[2]+RADIUS)/s.bucket)+1):
                ids=s._cells.get((ix,iz))
                if ids is not None and len(ids):groups.append(ids)
        if len(s._global):groups.append(s._global)
        if not groups:return []
        ids=groups[0] if len(groups)==1 else np.unique(np.concatenate(groups))
        lo=p+(-RADIUS,SKIN,-RADIUS); hi=p+(RADIUS,HEIGHT+SKIN,RADIUS)
        keep=np.all(s._hi[ids]>=lo,axis=1)&np.all(s._lo[ids]<=hi,axis=1)
        ids=ids[keep];tri=s._tri[ids]
        if not len(ids):return []
        a=p+(0,RADIUS+SKIN,0);b=p+(0,HEIGHT-RADIUS+SKIN,0)
        distances,left,right=_segment_triangle(a,b,tri)
        out=[]
        for k in np.flatnonzero(distances<RADIUS-CONTACT_EPSILON):
            d=float(distances[k]); delta=left[k]-right[k]
            if d>1e-10:normal=delta/d
            else:
                normal=np.cross(tri[k,1]-tri[k,0],tri[k,2]-tri[k,0]);norm=float(np.linalg.norm(normal))
                if norm<1e-12:continue
                normal/=norm
                if float(normal@((a+b)*.5-tri[k,0]))<0:normal=-normal
            out.append(dict(triangle_id=int(ids[k]),distance=d,depth=RADIUS-d,
                            normal=normal.tolist(),point=(right[k]+self.offset).tolist()))
        return out

    def intersecting(self,point):return {r['triangle_id'] for r in self.contacts(point)}

    def sweep(self,a,b,*,step=.025):
        if not math.isfinite(step) or not .005<=step<=.025 or math.dist(a,b)>256:
            raise ValueError('capsule_sweep_budget')
        count=max(1,int(math.ceil(math.dist(a,b)/step)));result=set()
        for i in range(count+1):
            result.update(self.intersecting(tuple(x+(y-x)*i/count for x,y in zip(a,b))))
        return result

    def standable(self,point):
        x,y,z=point
        if not any(ny>=math.cos(math.radians(42)) and abs(h-y)<=.04
                   for _,h,ny in self.terrain.hits(x,z)):return False
        return not self.intersecting(point)

    def resolve(self,point,*,max_distance=.5):
        """只从初始接触脱离；<=.5m、无新面、每 .025m 检查，不作墙后传送。"""
        if not math.isfinite(max_distance) or not 0<=max_distance<=.5:
            raise ValueError('capsule_resolve_budget')
        point=tuple(float(v) for v in point); contacts=self.contacts(point)
        if not contacts:return dict(point=point,distance=0.,initial_faces=[])
        initial={r['triangle_id'] for r in contacts};directions=[np.array((0.,1.,0.))]
        normals=[np.asarray(r['normal']) for r in contacts]
        total=sum((n*r['depth'] for n,r in zip(normals,contacts)),np.zeros(3))
        directions.extend([total]+normals)
        normalized=[]
        for d in directions:
            norm=float(np.linalg.norm(d))
            if norm>1e-9 and d[1]>=-.05:
                d=d/norm
                if not any(np.linalg.norm(d-e)<1e-5 for e in normalized):normalized.append(d)
        for distance in np.arange(.025,max_distance+.0001,.025):
            for d in normalized:
                q=tuple(np.asarray(point)+d*distance)
                if self.intersecting(q):continue
                if self.sweep(point,q)-initial:continue
                return dict(point=q,distance=float(distance),initial_faces=sorted(initial),normal=d.tolist())
        return None


def capsule_contacts(terrain,point):return CapsuleProbe(terrain).intersecting(point)
def capsule_standable(terrain,point):return CapsuleProbe(terrain).standable(point)
