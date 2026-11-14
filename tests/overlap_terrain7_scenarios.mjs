// Real bundled Node/Rapier; only synthetic temporary terrain and packaged props.
import fs from 'node:fs';
import path from 'node:path';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
const [engine,folder,inputFile]=process.argv.slice(2);
const {init,DEFAULTS}=await import(engine+'core.mjs');
const {OfflineMotionWorld}=await import(engine+'offline_motion_world.mjs');
const {StagedWorkerState}=await import(engine+'staged_worker.mjs');
const {ON_DEMAND_POLICY}=await import(engine+'on_demand_nav.mjs');
await init();
const inputs=JSON.parse(fs.readFileSync(inputFile,'utf8'));
const results={};
function scenario(name,fn){try{results[name]={ok:true,...fn()};}catch(e){results[name]={ok:false,error:String(e.stack)};}}
function floor(){return {positions:[-20,0,-20,-20,0,20,20,0,20,20,0,-20],indices:[0,1,2,0,2,3]};}
function wall(x=0){return {positions:[x,-5,-5,x,5,-5,x,5,5,x,-5,5],indices:[0,1,2,0,2,3]};}
function combine(...parts){const positions=[],indices=[];for(const p of parts){const offset=positions.length/3;positions.push(...p.positions);indices.push(...p.indices.map(i=>i+offset));}return {positions,indices};}
function world(mesh,options={}){return new OfflineMotionWorld(new Float32Array(mesh.positions),new Uint32Array(mesh.indices),{offline:true,origin:[0,0,0],startSupport:true,...options});}
function withWorld(mesh,fn){const w=world(mesh);try{return fn(w);}finally{w.dispose();}}
scenario('no_props_golden',()=>withWorld(floor(),w=>{
 const motor=w.motor([0,0,0],[[0,0,4]],0,1.5),frames=[];
 for(let i=0;i<90;i++)frames.push(motor.sample(i*.051));
 assert.equal(frames.at(-1).arrived,true);return {frames,audit:motor.startSupportAudit};
}));
scenario('capsule_clear_and_raw',()=>withWorld(combine(floor(),wall()),w=>{
 const clear=w.capsuleProbe([2,0,0]),raw=w.capsuleProbe([.1,0,0],false);
 assert.equal(clear.status,'unchanged');assert.equal(clear.contacts.length,0);
 assert.equal(raw.status,'blocked');assert.equal(raw.adjustedStart,null);assert.ok(raw.contacts.length>0);
 assert.deepEqual(raw.config,{radius:.25,height:1.5,skin:.015});
 return {clear,raw};
}));
scenario('bounded_monotone_exit',()=>withWorld(combine(floor(),wall()),w=>{
 const start=[.1,0,0],probe=w.capsuleProbe(start);assert.equal(probe.status,'adjusted');
 assert.ok(probe.deltaLength>0&&probe.deltaLength<=.5);assert.ok(probe.adjustedStart[0]>=.25);
 const initial=new Map(probe.contacts.map(c=>[c.id,c.distance]));let previous=new Map(initial),samples=0;
 for(let n=1;n<=Math.round(probe.deltaLength/.01);n++){
  const p=start.map((v,i)=>v+probe.delta[i]*n*.01/probe.deltaLength),q=w.capsuleProbe(p,false);
  for(const hit of q.contacts){assert.ok(initial.has(hit.id)&&previous.has(hit.id));assert.ok(hit.distance>=initial.get(hit.id)-.00101);assert.ok(hit.distance>=previous.get(hit.id)-.00101);}
  previous=new Map(q.contacts.map(c=>[c.id,c.distance]));samples++;
 }
 assert.equal(w.capsuleProbe(probe.adjustedStart,false).contacts.length,0);
 return {probe,samples};
}));
scenario('new_obstacle_and_deep_reject',()=>{
 const corridor=withWorld(combine(floor(),wall(0),wall(.4)),w=>w.capsuleProbe([.1,0,0]));
 const deep=withWorld(combine(floor(),wall(-.1),wall(.1)),w=>w.capsuleProbe([0,0,0]));
 assert.equal(corridor.status,'blocked');assert.equal(deep.status,'blocked');
 assert.equal(corridor.adjustedStart,null);assert.equal(deep.adjustedStart,null);
 return {corridor,deep};
});
scenario('candidate_and_contact_budgets',()=>{
 const g=wall();g.indices=Array.from({length:257},()=>[0,1,2]).flat();
 const candidate=withWorld(g,w=>w.capsuleProbe([.1,0,0]));
 assert.equal(candidate.reason,'initial_contact_candidate_budget');assert.equal(candidate.collisionClearanceVerified,false);
 g.indices=Array.from({length:65},()=>[0,1,2]).flat();
 const contact=withWorld(g,w=>w.capsuleProbe([.1,0,0]));
 assert.equal(contact.status,'blocked');assert.equal(contact.contacts.length,65);return {candidate,contactCount:contact.contacts.length};
});
scenario('shallow_floor_and_ceiling',()=>{
 const foot=withWorld(floor(),w=>w.capsuleProbe([0,-.02,0]));
 assert.equal(foot.status,'adjusted');assert.ok(foot.deltaY>0&&foot.deltaLength<=.03);
 const ceiling={positions:[-.1,1.4,-.1,-.1,1.4,.1,.1,1.4,.1,.1,1.4,-.1],indices:[0,1,2,0,2,3]};
 const head=withWorld(combine(floor(),ceiling),w=>w.capsuleProbe([0,0,0]));
 assert.equal(head.status,'adjusted');assert.ok(head.deltaLength<=.5);assert.ok(Math.hypot(head.delta[0],head.delta[2])>.1);
 return {foot,head};
});
scenario('continuous_prefix',()=>withWorld(combine(floor(),wall()),w=>{
 const start=[.1,0,0],motor=w.motor(start,[[2,0,0]],0,1),audit=motor.startSupportAudit;
 assert.equal(audit.initialContactExit,true);assert.deepEqual(motor.snapshot().position,start);
 assert.throws(()=>motor.retarget(start,[[3,0,0]],null,1),/active_grounded_motor_required/);
 const frames=[motor.sample(0)];
 for(let i=1;i<70;i++){
  const next=motor.sample(i*.051),prev=frames.at(-1),distance=Math.hypot(...next.position.map((v,k)=>v-prev.position[k]));
  // The new prefix is continuous. After handoff the existing 60 Hz motor
  // carries time debt, so a 51 ms poll can publish four fixed steps. Do not
  // silently change that old motor contract into a wall-clock interpolation.
  const prefix=next.reason==='initial_contact_exit',bound=prefix?.051:.051+DEFAULTS.fixedDt;
  const traveled=prefix?distance:Math.hypot(next.position[0]-prev.position[0],next.position[2]-prev.position[2]);
  assert.ok(traveled<=bound+1e-5,JSON.stringify({i,previous:prev,next,traveled,bound}));
  // The unchanged ground motor can snap/fall vertically; its horizontal speed
  // is bounded separately. Only the new exit interpolation bounds 3-D speed.
  assert.ok((prefix?Math.hypot(...next.velocity):Math.hypot(next.velocity[0],next.velocity[2]))<=1.0001);
  assert.ok(Math.abs(next.velocity[1])<=DEFAULTS.maxFallSpeed);frames.push(next);
 }
 assert.equal(frames.at(-1).arrived,true);assert.ok(frames.some(f=>f.reason==='initial_contact_exit'));
 assert.equal(w.capsuleProbe(frames.at(-1).position,false).contacts.length,0);
 return {audit,frames};
}));
scenario('prefix_cancel_clock_gap',()=>{
 const cancelled=withWorld(combine(floor(),wall()),w=>{
  const motor=w.motor([.1,0,0],[[2,0,0]],0,1),before=motor.sample(.02),stop=motor.cancel('fixture_cancel');
  assert.deepEqual(stop.position,before.position);assert.deepEqual(motor.sample(.2),stop);return {before,stop};
 });
 const gap=withWorld(combine(floor(),wall()),w=>{const motor=w.motor([.1,0,0],[[2,0,0]],0,1),start=motor.snapshot(),stop=motor.sample(.5);assert.equal(stop.reason,'clock_gap');assert.equal(stop.blocked,true);assert.deepEqual(stop.position,start.position);return stop;});
 return {cancelled,gap};
});
function sha(value){return createHash('sha256').update(value).digest('hex');}
function write(name,value){const bytes=typeof value==='object'&&!Buffer.isBuffer(value)?Buffer.from(JSON.stringify(value)):value;fs.writeFileSync(path.join(folder,name),bytes);return sha(bytes);}
function makeAsset(){
 const mesh=floor(),p=Buffer.from(new Float32Array(mesh.positions).buffer),t=Buffer.from(new Uint32Array(mesh.indices).buffer);
 const ph=write('positions.bin',p),th=write('triangles.bin',t);
 const m={format:'skyterrain-static-v1',mapName:'OfflineSyntheticFloor',geometryKind:'initial_static_collision_candidates',nativeCollisionVerified:false,calibrationVerified:false,positions:'positions.bin',indices:'triangles.bin',vertexCount:4,triangleCount:2,sha256:{positions:ph,indices:th}};
 const evidence={...m,schema_version:'sky-partial-static-world-v1',coordinate_space:'sky_native',geometry_kind:'partial_static_world_candidate',native_collision_verified:false,runtime_ready:false,vertex_count:4,triangle_count:2,
  files:{positions:{file:m.positions,sha256:ph,bytes:p.length,dtype:'<f4',shape:[4,3]},triangles:{file:m.indices,sha256:th,bytes:t.length,dtype:'<u4',shape:[2,3]}}};
 m.evidenceManifest={file:'evidence.json',sha256:write('evidence.json',evidence)};
 const mh=write('runtime-manifest.json',m),descriptor={format:'skyterrain-navigation-on-demand-v1',mode:'offline',status:'on_demand_static_candidate',nativeCollisionVerified:false,calibrationVerified:false,runtimeReady:false,mapName:m.mapName,geometryKind:m.geometryKind,config:DEFAULTS,inputHashes:{manifest:mh,positions:ph,indices:th},inputVertices:4,inputTriangles:2,navigationCoverage:'on_demand_static_candidate',navigationWindow:null,policy:ON_DEMAND_POLICY};
 const nh=write('navigation.json',descriptor);
 return {op:'load',manifest:'runtime-manifest.json',expectedManifestSHA:mh,navigationReport:'navigation.json',expectedNavigationReportSHA:nh,expectedNavigationSHA:nh};
}
const load=makeAsset();
scenario('worker_probe_isolation_and_props',()=>{
 const left=new StagedWorkerState(folder,{offline:true}),right=new StagedWorkerState(folder,{offline:true});
 try{
  const lload=left.request(load),rload=right.request(load);assert.equal(lload.status,'ready');assert.equal(rload.status,'ready');
  const plan={start:[0,0,0],goal:[0,0,4],speed:1.5,scope:'fixture-scope'};
  const lp=left.request({op:'preparePlan',...plan}),rp=right.request({op:'preparePlan',...plan});assert.equal(lp.status,'ready');assert.equal(rp.status,'ready');
  const probes=[];for(const p of [[0,0,0],[12,0,12],[-12,0,-12],[100,0,100],[-100,0,-100]])probes.push(right.request({op:'capsuleProbe',point:p}));
  const invalid=right.request({op:'capsuleProbe',point:[0,0,0],resolve:1});assert.equal(invalid.status,'unavailable');
  const lm=left.request({op:'plan',...plan,now:0,preparedToken:lp.preparedToken}),rm=right.request({op:'plan',...plan,now:0,preparedToken:rp.preparedToken});assert.equal(lm.status,'ready');assert.equal(rm.status,'ready');assert.deepEqual(lm.collisionCache,rm.collisionCache);
  const clean=value=>Object.fromEntries(['status','position','velocity','grounded','arrived','blocked','reason'].map(k=>[k,value[k]]));
  for(let i=1;i<=70;i++){
   if(i%10===0)right.request({op:'capsuleProbe',point:[i*2,0,i*2]});
   const l=left.request({op:'sample',token:lm.token,scope:plan.scope,now:i*.051}),r=right.request({op:'sample',token:rm.token,scope:plan.scope,now:i*.051});
   assert.equal(l.status,'ready');assert.equal(r.status,'ready');assert.deepEqual(clean(l),clean(r));
   if(i===70){assert.equal(l.arrived,true);assert.equal(r.arrived,true);}
  }
  // Existing collider protocol requires standable=true even for a vertical
  // mesh. Triangle normals, not this profile flag, still control support.
  const table=inputs.table,mesh=wall(),blocker={id:'fixture-wall',name:'synthetic-wall',positions:mesh.positions,triangles:mesh.indices,source_kind:'render_mesh_candidate',standable:true};
  const changed=right.request({op:'setColliders',expectedLoadEpoch:rload.colliderEpoch,revision:1,colliders:[table,blocker]});assert.equal(changed.status,'ready',JSON.stringify(changed));assert.equal(changed.colliderCount,2);assert.equal(changed.motionInvalidated,true);
  const stale=right.request({op:'sample',token:rm.token,scope:plan.scope,now:4});assert.equal(stale.status,'unavailable');
  const blocked=right.request({op:'capsuleProbe',point:[.1,0,0],resolve:false});assert.equal(blocked.status,'ready');assert.ok(blocked.capsule.contacts.length>0);assert.equal(blocked.nativeCollisionVerified,false);assert.equal(blocked.motorStarted,false);
  const removed=right.request({op:'removeColliders',ids:['fixture-wall'],expectedLoadEpoch:rload.colliderEpoch,revision:2});assert.equal(removed.colliderCount,1);
  const clear=right.request({op:'capsuleProbe',point:[.1,0,0],resolve:false});assert.equal(clear.capsule.status,'unchanged');
  assert.equal(sha(fs.readFileSync(path.join(folder,'runtime-manifest.json'))),load.expectedManifestSHA);
  return {load:rload.status,probes:probes.map(p=>p.status),invalid:invalid.reason,framesCompared:70,tableTriangles:table.triangles.length/3,changed,stale:stale.reason,blocked,clear};
 }finally{left.dispose();right.dispose();}
});
scenario('cross_language_contacts',()=>{
 const out=[];
 for(const row of inputs.probes)out.push(withWorld(row.mesh,w=>w.capsuleProbe(row.feet,false)));
 return {probes:out};
});
console.log(JSON.stringify(results));
