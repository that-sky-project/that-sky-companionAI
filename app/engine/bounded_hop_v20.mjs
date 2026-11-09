// Explicit opt-in engineering hop proof. No native-client equivalence claimed.
import {createHash,randomUUID} from 'node:crypto';
import {DEFAULTS} from './core.mjs';
import {queryPreflightSupport} from './follow_ground_v14.mjs';
const plans=new WeakMap(),used=new WeakSet();
const need=(ok,why)=>{if(!ok)throw Error('bounded_hop:'+why);};
const hash=x=>createHash('sha256').update(x).digest('hex');
const freeze=x=>{if(x&&typeof x==='object'){Object.values(x).forEach(freeze);Object.freeze(x);}return x;};
const point=p=>Array.isArray(p)&&p.length===3&&p.every(Number.isFinite);
const horiz=(a,b)=>Math.hypot(a[0]-b[0],a[2]-b[2]);
const arr=p=>[p.x,p.y,p.z];
function fingerprint(world){const v=world.terrain.vertices(),i=world.terrain.indices();need(v instanceof Float32Array&&i instanceof Uint32Array,'trimesh_required');return hash(Buffer.concat([Buffer.from(v.buffer,v.byteOffset,v.byteLength),Buffer.from(i.buffer,i.byteOffset,i.byteLength),Buffer.from(JSON.stringify(world.config))]));}
function checkWindow(window,p){const r=DEFAULTS.radius+DEFAULTS.skin;need(p[0]-r>=window.minX&&p[0]+r<=window.maxX&&p[2]-r>=window.minZ&&p[2]+r<=window.maxZ,'unknown_window');}
function checkSupport(world,p){const s=queryPreflightSupport(world,p);need(s.supported,'support_missing_or_steep');return s;}
function makeMotor(world,request,now=0){
 const m=world.motor(request.start,[request.start],now-world.config.fixedDt,request.speed);
 try{let settled;const contactTrace=[];for(let i=0;i<6;i++){settled=m.sample(now+i*world.config.fixedDt);contactTrace.push({step:i+1,position:[...settled.position],velocity:[...settled.velocity],grounded:settled.grounded});if(settled.blocked||horiz(settled.position,request.start)>2*DEFAULTS.skin||Math.abs(settled.position[1]-request.start[1])>.06){const e=Error('bounded_hop:start_contact_budget');e.diagnostic={start:request.start,settled,contactTrace};throw e;}if(settled.grounded)break;}
  need(settled.grounded,'grounded_start_required');checkSupport(world,settled.position);m.hopStartContactTrace=contactTrace;
  // Restart only the clock epoch after bounded real gravity settling, not its pose.
  m.now=now;m.debt=0;
  m.startBoundedHop({id:request.hopId,landing:request.landing,verticalSpeed:request.verticalSpeed,maxRise:request.maxRise,maxAirSeconds:request.maxAirSeconds,maxHorizontalDistance:request.maxHorizontalDistance});return m;
 }catch(e){m.dispose();throw e;}
}
function frame(m,s,step,settling=false){return {step,phase:settling?(s.grounded&&m.boundedHop.complete?'cancelled_landed':'settling'):s.hop.phase,position:[...s.position],velocity:[...s.velocity],grounded:s.grounded,stateVerticalSpeed:m.vy,landingContact:s.hop?.landingContact??null};}
function checkFrame(world,req,m,s){
  // Match the fixed v21 receiver budgets before choosing a publishable proof.
  const requestedSpeedLimit=Math.sqrt(req.verticalSpeed**2+2*world.config.gravity*.3)+world.config.gravity*world.config.fixedDt;
  const actualSpeedLimit=Math.sqrt(req.speed**2+req.verticalSpeed**2+2*world.config.gravity*.3)+world.config.gravity*world.config.fixedDt;
  need(Math.hypot(...s.velocity)<=actualSpeedLimit+1e-6&&Math.abs(s.velocity[1])<=6+1e-6&&Math.abs(m.vy)<=requestedSpeedLimit+1e-6&&Math.abs(s.hop.landingContact?.requestedVerticalSpeed??0)<=requestedSpeedLimit+1e-6,'velocity_budget');
 need(!s.blocked,'motor_'+s.reason);checkWindow(req.collisionWindow,s.position);need(s.hop.elapsed<=req.maxAirSeconds+1e-7,'airtime');
 for(let i=0;i<m.controller.numComputedCollisions();i++)need(m.controller.computedCollision(i).normal1.y>=-.05,'headroom_collision');
 need(s.position[1]-m.boundedHop.start.y<=req.maxRise&&s.position[1]>=m.boundedHop.start.y-.3,'height_budget');
 need(horiz(s.position,req.start)<=req.maxHorizontalDistance+1e-6,'horizontal_budget');
 const ground=world.ground([s.position[0],s.position[1]+.05,s.position[2]],req.maxRise+.4);
 need(ground&&ground.position[1]>=req.start[1]-.3-2*DEFAULTS.skin,'deep_gap_or_missing_floor');
 if(m.boundedHop.complete){const c=s.hop.landingContact;need(s.grounded&&c&&c.requestedVerticalSpeed<=0&&c.requestedDeltaY<0&&c.appliedDeltaY>c.requestedDeltaY+1e-6&&c.normal[1]>=Math.cos(world.config.slopeDegrees*Math.PI/180),'false_landing');checkSupport(world,s.position);}
}
function run(world,req,{cadence=world.config.fixedDt,cancelAfterStep=null}={}){
 const m=makeMotor(world,req),trace=[frame(m,m.cached,0)],samples=[],fixed=world.config.fixedDt;let steps=0;const step=m.step.bind(m);
 // Keep the original fixed step and all KCC sweeps; observe each result only.
 m.step=dt=>{const before=arr(m.p);step(dt);steps++;const velocity=arr(m.p).map((v,i)=>(v-before[i])/dt),s=m.result(velocity);checkFrame(world,req,m,s);trace.push(frame(m,s,steps,cancelAfterStep!==null&&steps>cancelAfterStep));if(steps===cancelAfterStep&&!m.boundedHop.complete)m.requestHopLanding();};
 try{
  if(cancelAfterStep===0)m.requestHopLanding();
  for(let n=1;n<=Math.ceil(req.maxAirSeconds/cadence)+2;n++){const s=m.sample(n*cadence);samples.push({time:n*cadence,step:steps,phase:s.hop.phase,position:[...s.position],velocity:[...s.velocity],grounded:s.grounded});if(m.boundedHop.complete)break;need(!s.blocked,'motor_'+s.reason);}
  need(m.boundedHop.complete&&m.boundedHop.leftGround,'no_completed_hop');const last=trace.at(-1);
  if(cancelAfterStep===null)need(horiz(last.position,req.landing)<=.25&&Math.abs(last.position[1]-req.landing[1])<=.2,'landing_target_missed');
  return {trace,samples,steps,seconds:steps*fixed,startContactTrace:m.hopStartContactTrace};
 }finally{m.dispose();}
}
export function prepareBoundedHop(world,input){
 need(world&&world.motors?.size===0,'idle_world_required');need(input&&typeof input==='object','request');
 const request=structuredClone(input);need(Object.keys(request).sort().join(',')==='collisionWindow,landing,scope,speed,start','request_fields');need(point(request.start)&&point(request.landing),'points');
 need(Number.isFinite(request.speed)&&request.speed>0&&request.speed<=3,'speed');need(request.scope&&typeof request.scope==='object'&&!Array.isArray(request.scope)&&JSON.stringify(request.scope).length<=8192,'scope');
 const w=request.collisionWindow;need(w&&Object.keys(w).sort().join(',')==='maxX,maxZ,minX,minZ'&&Object.values(w).every(Number.isFinite)&&w.minX<w.maxX&&w.minZ<w.maxZ,'window');
 const known=world.navigationWindow??{minX:world.bounds.lo[0],maxX:world.bounds.hi[0],minZ:world.bounds.lo[2],maxZ:world.bounds.hi[2]};need(w.minX>=known.minX&&w.maxX<=known.maxX&&w.minZ>=known.minZ&&w.maxZ<=known.maxZ,'window_not_in_world');
 need(world.config.fixedDt===DEFAULTS.fixedDt&&world.config.gravity===DEFAULTS.gravity&&world.config.radius===DEFAULTS.radius&&world.config.height===DEFAULTS.height&&world.config.skin===DEFAULTS.skin,'fixed_engineering_shape_required');
 Object.assign(request,{hopId:randomUUID(),maxRise:1,maxHorizontalDistance:2,verticalSpeed:Math.sqrt(2*world.config.gravity),maxAirSeconds:1.1});
 need(horiz(request.start,request.landing)>0.05&&horiz(request.start,request.landing)<=2&&request.landing[1]-request.start[1]<=.95&&request.start[1]-request.landing[1]<=.3,'short_supported_landing');
 checkWindow(w,request.start);checkWindow(w,request.landing);checkSupport(world,request.start);checkSupport(world,request.landing);
 const worldSHA256=fingerprint(world),scopeBindingSHA256=hash(JSON.stringify(request.scope)),main=run(world,request),cadences=[100,120,130].map(ms=>({cadenceMs:ms,...run(world,request,{cadence:ms/1000})}));
 // Every possible accepted fixed-step cancellation gets a real rerun of its prefix,
 // then zero horizontal input and bounded gravity/sweeps until contact. No interpolation.
 const settleTraces=[];for(let i=0;i<main.trace.length-1;i++){const branch=run(world,request,{cancelAfterStep:i});settleTraces.push({fromStep:i,frames:branch.trace.filter(s=>s.step>i)});}
 need(fingerprint(world)===worldSHA256,'world_changed_during_proof');
 const receipt=freeze({schema:'bounded-hop-proof-v21-contact',hopId:request.hopId,scopeBindingSHA256,worldSHA256,scope:request.scope,start:main.trace[0].position,requestedStart:request.start,startContactTrace:main.startContactTrace,requiresPublishedWarmup:true,warmupExecutorIntegrated:true,liveAdapterIntegrated:false,landing:request.landing,fixedDt:world.config.fixedDt,gravity:world.config.gravity,launchVerticalSpeed:request.verticalSpeed,maxRise:1,maxHorizontalDistance:2,maxAirSeconds:request.maxAirSeconds,theoreticalApex:request.verticalSpeed**2/(2*world.config.gravity),nominalReturnAirSeconds:2*request.verticalSpeed/world.config.gravity,collisionWindow:w,mainTrace:main.trace,settleTraces,cadences:cadences.map(x=>({cadenceMs:x.cadenceMs,steps:x.steps,seconds:x.seconds,samples:x.samples})),nativeParametersVerified:false});
 const prepared=Object.freeze({receipt});plans.set(prepared,{world,request,worldSHA256});return prepared;
}
export function startPreparedBoundedHop(prepared,{now,isCurrent}={}){
 const plan=plans.get(prepared);need(plan&&!used.has(prepared),'fresh_proof_required');need(Number.isFinite(now)&&typeof isCurrent==='function'&&isCurrent(),'current_owner_required');need(plan.world.motors.size===0&&fingerprint(plan.world)===plan.worldSHA256,'world_changed');used.add(prepared);
 // Unlike offline proof construction, execution never consumes a hidden settling step.
 const m=plan.world.motor(plan.request.start,[plan.request.start],now,plan.request.speed);
 let closed=false,stepIndex=0,warmupStep=0,totalStep=0,cancelFrom=null,last=m.cached,pendingFrames=[],pendingWarmup=[],grounding=true,warmupCancelled=false,warmupTerminal=false;
 const original=m.step.bind(m);
 const expected=step=>cancelFrom===null?prepared.receipt.mainTrace.find(x=>x.step===step):prepared.receipt.settleTraces.find(x=>x.fromStep===cancelFrom)?.frames.find(x=>x.step===step);
 const matches=(e,a)=>e&&e.grounded===a.grounded&&Math.hypot(...e.position.map((v,i)=>v-a.position[i]))<=1e-5&&Math.hypot(...e.velocity.map((v,i)=>v-a.velocity[i]))<=1e-3&&JSON.stringify(e.landingContact??null)===JSON.stringify(a.landingContact??null);
 m.step=d=>{
  need(!closed&&isCurrent(),'ownership_lost');const before=arr(m.p);original(d);totalStep++;const s=m.result(arr(m.p).map((v,i)=>(v-before[i])/d));
  if(grounding){
   warmupStep++;const e=prepared.receipt.startContactTrace[warmupStep-1],actual={warmupStep,totalStep,phase:'grounding',position:[...s.position],velocity:[...s.velocity],grounded:s.grounded};
   need(!s.blocked&&matches(e,actual),'grounding_trace_diverged');pendingWarmup.push(freeze(actual));
   if(warmupStep===prepared.receipt.startContactTrace.length){
    need(s.grounded,'grounding_not_complete');
    if(warmupCancelled){warmupTerminal=true;return;}
    grounding=false;const r=plan.request;m.startBoundedHop({id:r.hopId,landing:r.landing,verticalSpeed:r.verticalSpeed,maxRise:r.maxRise,maxAirSeconds:r.maxAirSeconds,maxHorizontalDistance:r.maxHorizontalDistance});
    const takeoff={...frame(m,m.cached,0),totalStep,warmupStep};need(matches(prepared.receipt.mainTrace[0],takeoff),'takeoff_trace_diverged');pendingFrames.push(freeze(takeoff));
   }
   return;
  }
  stepIndex++;checkFrame(plan.world,plan.request,m,s);const e=expected(stepIndex),actual={...frame(m,s,stepIndex,cancelFrom!==null),totalStep,warmupStep};need(matches(e,actual)&&e.phase===actual.phase,'proof_trace_diverged');pendingFrames.push(freeze(actual));
 };
 const close=reason=>{if(!closed){m.cancel(reason);m.dispose();closed=true;}};
 const output=()=>Object.freeze({...last,hopId:plan.request.hopId,step:stepIndex,warmupStep,totalStep,phase:warmupTerminal?'cancelled_landed':grounding?'grounding':last.hop.phase,terminal:warmupTerminal||!!m.boundedHop?.complete,acceptedWarmupFrames:Object.freeze([...pendingWarmup]),acceptedFrames:Object.freeze([...pendingFrames])});
 const requestLanding=()=>{need(!closed,'inactive');try{need(isCurrent(),'ownership_lost');if(warmupTerminal||m.boundedHop?.complete||cancelFrom!==null)return;if(grounding){warmupCancelled=true;return;}cancelFrom=stepIndex;need(prepared.receipt.settleTraces.some(x=>x.fromStep===cancelFrom),'settle_suffix_missing');m.requestHopLanding();}catch(e){close(e.message);throw e;}};
 const snapshot=()=>{need(!closed,'inactive');try{need(isCurrent(),'ownership_lost');return Object.freeze({...output(),acceptedWarmupFrames:Object.freeze([]),acceptedFrames:Object.freeze([])});}catch(e){close(e.message);throw e;}};
 return Object.freeze({receipt:prepared.receipt,snapshot,sample:time=>{need(!closed,'inactive');try{need(isCurrent(),'ownership_lost');pendingFrames=[];pendingWarmup=[];last=m.sample(time);need(!last.blocked,'motor_'+last.reason);return output();}catch(e){close(e.message);throw e;}},requestLanding,cancel:reason=>close(reason??'cancelled'),dispose:()=>close('disposed')});
}
