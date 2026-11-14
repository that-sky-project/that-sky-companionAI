// Room-local geometry only. No filesystem, persistence, mesh-name guessing or network.
export const COLLIDER_LIMITS=Object.freeze({count:64,vertices:32768,triangles:16384,perVertices:2048,perTriangles:2560,coordinate:10000,extent:32});
const need=(yes,why)=>{if(!yes)throw Error('dynamic_colliders:'+why);};
const string=(s,limit=128)=>typeof s==='string'&&s.length>0&&s.length<=limit&&!/[\x00-\x1f]/.test(s);
const plain=o=>o&&typeof o==='object'&&!Array.isArray(o)&&Object.getPrototypeOf(o)===Object.prototype;
function checked(raw){
 need(plain(raw)&&Object.keys(raw).every(k=>['id','name','positions','triangles','source_kind','standable'].includes(k)),'fields');
 need(string(raw.id,192)&&string(raw.name)&&string(raw.source_kind),'identity');
 need(raw.standable===true,'standable_required');
 const p=raw.positions,t=raw.triangles,L=COLLIDER_LIMITS;
 need(Array.isArray(p)&&p.length>=9&&p.length%3===0&&p.length<=L.perVertices*3,'vertex_budget');
 need(Array.isArray(t)&&t.length>=3&&t.length%3===0&&t.length<=L.perTriangles*3,'triangle_budget');
 need(p.every(x=>typeof x==='number'&&Number.isFinite(x)&&Math.abs(x)<=L.coordinate),'coordinate');
 need(t.every(x=>Number.isSafeInteger(x)&&x>=0&&x<p.length/3),'index');
 const lo=[Infinity,Infinity,Infinity],hi=[-Infinity,-Infinity,-Infinity];
 for(let i=0;i<p.length;i++){lo[i%3]=Math.min(lo[i%3],p[i]);hi[i%3]=Math.max(hi[i%3],p[i]);}
 need(hi.every((v,k)=>v-lo[k]<=L.extent),'extent');
 // Reject degeneracy after Float32 conversion, which is the physics representation.
 const fp=Array.from(new Float32Array(p));
 for(let i=0;i<t.length;i+=3){
  const a=t[i]*3,b=t[i+1]*3,c=t[i+2]*3;
  const u=[0,1,2].map(k=>fp[b+k]-fp[a+k]),v=[0,1,2].map(k=>fp[c+k]-fp[a+k]);
  need(Math.hypot(u[1]*v[2]-u[2]*v[1],u[2]*v[0]-u[0]*v[2],u[0]*v[1]-u[1]*v[0])>1e-8,'degenerate_triangle');
 }
 return Object.freeze({id:raw.id,name:raw.name,positions:Object.freeze(fp),triangles:Object.freeze([...t]),source_kind:raw.source_kind,standable:true});
}
function signature(map){return JSON.stringify([...map.values()].sort((a,b)=>a.id.localeCompare(b.id)));}
export class ColliderSet{
 #items;#revision;
 constructor(items=new Map(),revision=0){this.#items=items;this.#revision=revision;}
 get size(){return this.#items.size;}
 audit(){return {colliderRevision:this.#revision,colliderCount:this.size,colliderTriangles:[...this.#items.values()].reduce((s,c)=>s+c.triangles.length/3,0),colliderVertices:[...this.#items.values()].reduce((s,c)=>s+c.positions.length/3,0)};}
 propose(op,colliders,ids,revision){
  need(revision===undefined||Number.isSafeInteger(revision)&&revision>=0,'revision');
  const next=op==='setColliders'?new Map():new Map(this.#items),L=COLLIDER_LIMITS;
  if(op==='removeColliders'){
   need(colliders===undefined&&Array.isArray(ids)&&ids.length<=L.count&&ids.every(id=>string(id,192))&&new Set(ids).size===ids.length,'remove_ids');
   for(const id of ids)next.delete(id);
  }else{
   need(ids===undefined&&Array.isArray(colliders)&&colliders.length<=L.count,'collider_count');
   const seen=new Set();for(const raw of colliders){const c=checked(raw);need(!seen.has(c.id),'duplicate_id');seen.add(c.id);next.set(c.id,c);}
  }
  need(next.size<=L.count,'collider_count');
  const nextRevision=revision??this.#revision+1;
  need(Number.isSafeInteger(nextRevision)&&nextRevision>=0,'revision');
  const set=new ColliderSet(next,nextRevision),audit=set.audit();
  need(audit.colliderVertices<=L.vertices&&audit.colliderTriangles<=L.triangles,'aggregate_geometry_budget');
  const changed=signature(next)!==signature(this.#items);
  need(revision===undefined||revision>=this.#revision,'stale_revision');
  need(!changed||revision===undefined||revision>this.#revision,'revision_reuse');
  return {set,changed};
 }
 merge(asset){
  const all=[...this.#items.values()],p=new Float32Array(asset.positions.length+all.reduce((s,c)=>s+c.positions.length,0)),t=new Uint32Array(asset.indices.length+all.reduce((s,c)=>s+c.triangles.length,0));
  p.set(asset.positions);t.set(asset.indices);let po=asset.positions.length,to=asset.indices.length;
  for(const c of all){p.set(c.positions,po);for(const i of c.triangles)t[to++]=i+po/3;po+=c.positions.length;}
  return {positions:p,indices:t};
 }
}
