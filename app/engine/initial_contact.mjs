// Cold, bounded capsule separation. Never removes a collider or ignores a mesh.
import RAPIER from '@dimforge/rapier3d-compat';
import {DEFAULTS} from './core.mjs';
const ROT={x:0,y:0,z:0,w:1},ZERO={x:0,y:0,z:0};
const vec=p=>({x:p[0],y:p[1],z:p[2]}),arr=p=>[p.x,p.y,p.z];
const distance=(a,b)=>Math.hypot(...a.map((v,i)=>v-b[i]));
export const INITIAL_CONTACT_LIMITS=Object.freeze({maximumDistance:.5,step:.01,maxContacts:64,maxCandidates:256,maxDirections:18,contactEpsilon:.00002});

export class InitialContactQuery {
 constructor(world,positions,indices){this.world=world;this.positions=positions;this.indices=indices;}
 shape(){const c=this.world.config;return new RAPIER.Capsule((c.height-2*c.radius)/2,c.radius);}
 center(feet){const c=this.world.config;return {x:feet[0],y:feet[1]+c.height/2+c.skin,z:feet[2]};}
 overlap(feet){return !!this.world.world.intersectionWithShape(this.center(feet),ROT,this.shape(),undefined,undefined,undefined,undefined,c=>c===this.world.terrain);}
 candidates(feet){
  const c=this.world.config,extent=c.radius+INITIAL_CONTACT_LIMITS.maximumDistance+.03,result=[];
  for(let i=0;i<this.indices.length;i+=3){
   const vs=[0,1,2].map(j=>Array.from(this.positions.subarray(this.indices[i+j]*3,this.indices[i+j]*3+3)));
   if([0,1,2].some(d=>Math.min(...vs.map(v=>v[d]))>feet[d]+(d===1?c.height+.55:extent)||Math.max(...vs.map(v=>v[d]))<feet[d]-(d===1?.55:extent)))continue;
   result.push({id:i/3,vertices:vs,shape:new RAPIER.Triangle(...vs.map(vec))});
   if(result.length>INITIAL_CONTACT_LIMITS.maxCandidates)return null;
  }
  return result;
 }
 contacts(feet,candidates){
  const shape=this.shape(),center=this.center(feet),out=[];
  for(const t of candidates??[]){const h=t.shape.contactShape(ZERO,ROT,shape,center,ROT,0);
   if(h&&h.distance< -INITIAL_CONTACT_LIMITS.contactEpsilon)out.push({id:t.id,distance:h.distance,normal:arr(h.normal1),point:arr(h.point1),vertices:t.vertices});}
  return out.sort((a,b)=>a.distance-b.distance);
 }
 probe(feet,{resolve=true}={}){
  const c=this.world.config,base={rawStart:[...feet],adjustedStart:[...feet],status:'unchanged',reason:'capsule_clear',config:{radius:c.radius,height:c.height,skin:c.skin},limits:INITIAL_CONTACT_LIMITS,collisionClearanceVerified:true};
  if(!this.overlap(feet))return {...base,contacts:[]};
  const candidates=this.candidates(feet);
  if(!candidates)return {...base,status:'blocked',reason:'initial_contact_candidate_budget',collisionClearanceVerified:false};
  const contacts=this.contacts(feet,candidates),blocked={...base,status:'blocked',adjustedStart:null,reason:'initial_contact_no_bounded_exit',collisionClearanceVerified:false,contacts};
  if(!resolve||!contacts.length||contacts.length>INITIAL_CONTACT_LIMITS.maxContacts)return blocked;
  const initial=new Map(contacts.map(t=>[t.id,t])),directions=[[0,1,0]];
  const add=d=>{const n=Math.hypot(...d);if(n<.01)return;const p=d.map(v=>v/n);if(p[1]<-.05||directions.some(a=>distance(a,p)<.04))return;directions.push(p);};
  for(const t of contacts){add(t.normal);add([t.normal[0],Math.max(.35,t.normal[1]),t.normal[2]]);}
  add(contacts.reduce((a,t)=>a.map((v,i)=>v+t.normal[i]*(-t.distance)),[0,0,0]));
  // A shallow head/side overlap may separate sideways, not upward. It still
  // follows the same per-face monotonic/no-new-contact proof and .5m cap.
  for(let n=0;n<8;n++)add([Math.cos(n*Math.PI/4),0,Math.sin(n*Math.PI/4)]);
  let best=null;
  for(const dir of directions.slice(0,INITIAL_CONTACT_LIMITS.maxDirections)){
   let previous=new Map(contacts.map(t=>[t.id,t.distance])),samples=0;
   for(let n=1;n<=50;n++){
    const delta=n*INITIAL_CONTACT_LIMITS.step;if(best&&delta>best.deltaLength+.00001)break;
    const p=feet.map((v,i)=>v+dir[i]*delta),hits=this.contacts(p,candidates);samples++;
    // Only initially touching faces may remain, with non-increasing penetration.
    // Once a face clears it cannot be re-entered. This rejects wall traversal.
    if(hits.some(h=>!previous.has(h.id)||!initial.has(h.id)||h.distance<(previous.get(h.id)??0)-.001||h.distance<initial.get(h.id).distance-.001))break;
    if(!hits.length&&!this.overlap(p)){
     best={...base,status:'adjusted',reason:'bounded_initial_contact_exit',adjustedStart:p,delta:p.map((v,i)=>v-feet[i]),deltaLength:delta,deltaY:p[1]-feet[1],contacts,
      proof:{initialFaceIds:[...initial.keys()],samples,allOtherFacesChecked:true,penetrationMonotone:true,clearedFacesCannotReenter:true,maximumDistance:.5,normal:dir}};break;
    }
    previous=new Map(hits.map(h=>[h.id,h.distance]));
   }
  }
  return best??blocked;
 }
}
