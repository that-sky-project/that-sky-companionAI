// Cold continuation hint only. It never grants a hop or reuses a failure ticket.
import {validateFollowIntent,supportedFollowGoal,preflightStepRoute,BOUNDED_STEP_POLICY} from './follow_ground_v14.mjs';
import {routeCollisionWindow} from './offline_motion_world.mjs';
const sealed=new WeakSet();
const point=p=>Array.isArray(p)&&p.length===3&&p.every(x=>typeof x==='number'&&Number.isFinite(x)&&Math.abs(x)<1e6);
const dist=(a,b)=>Math.hypot(...a.map((v,i)=>v-b[i]));
const canon=v=>Array.isArray(v)?'['+v.map(canon).join(',')+']':v&&typeof v==='object'?'{'+Object.keys(v).sort().map(k=>JSON.stringify(k)+':'+canon(v[k])).join(',')+'}':JSON.stringify(v);
const freeze=x=>{if(x&&typeof x==='object'){Object.values(x).forEach(freeze);Object.freeze(x);}return x;};
const owner=x=>x&&Object.keys(x).sort().join(',')==='actorId,epoch,incarnation,mapId,roomId'&&['epoch','incarnation','mapId'].every(k=>Number.isSafeInteger(x[k])&&x[k]>=0)&&['actorId','roomId'].every(k=>typeof x[k]==='string'&&x[k].length>0&&x[k].length<=256);
const key=x=>typeof x==='string'&&/^[a-f0-9]{64}$/.test(x);
const scope=x=>typeof x==='string'&&x.length>0&&x.length<=256;
export function createHopContinuation({clock=()=>performance.now()/1000,ttlSeconds=1.5,startTolerance=.04,targetTolerance=.75}={}){
 if(typeof clock!=='function'||![ttlSeconds,startTolerance,targetTolerance].every(Number.isFinite)||ttlSeconds<=0||ttlSeconds>2||startTolerance<0||startTolerance>.05||targetTolerance<0||targetTolerance>1)throw Error('continuation_config');
 let memo=null;
 return Object.freeze({
  clear(){memo=null;},
  record({scope:s,hopScope,continuationKey,loadEpoch,goal,peer,reply}){
   memo=null;const now=clock(),last=reply?.acceptedFrames?.at(-1),frame=reply?.coordinateFrame;
   if(!Number.isFinite(now)||!scope(s)||!owner(hopScope)||!key(continuationKey)||!Number.isSafeInteger(loadEpoch)||loadEpoch<0||!point(goal)||!point(peer))return false;
   if(reply?.state!=='landed'||reply.terminal!==true||reply.phase!=='landed'||reply.grounded!==true||!point(reply.worldFeetPosition)||last?.phase!=='landed'||last.grounded!==true||!last.landingContact||!point(last.position)||frame?.kind!=='translation_only'||!point(frame.origin))return false;
   if(dist(last.position.map((v,i)=>v+frame.origin[i]),reply.worldFeetPosition)>1e-5)return false;
   memo=freeze({scope:s,hopScope:structuredClone(hopScope),continuationKey,loadEpoch,goal:[...goal],peer:[...peer],landed:[...reply.worldFeetPosition],recorded:now,expires:now+ttlSeconds});return true;
  },
  take({scope:s,hopScope,continuationKey,loadEpoch,start,goal,peer}){
   const saved=memo;memo=null;const now=clock();
   if(!saved||!Number.isFinite(now)||now<saved.recorded||now>=saved.expires||!point(start)||!point(goal)||!point(peer)||!owner(hopScope)||!key(continuationKey))return null;
   if(s!==saved.scope||canon(hopScope)!==canon(saved.hopScope)||continuationKey!==saved.continuationKey||loadEpoch!==saved.loadEpoch||dist(start,saved.landed)>startTolerance||dist(goal,saved.goal)>targetTolerance||dist(peer,saved.peer)>targetTolerance)return null;
   const hint=freeze({start:[...start],goal:[...goal],peer:[...peer],loadEpoch,continuationKey,recorded:saved.recorded,consumed:now});sealed.add(hint);return hint;
  }
 });
}
export function prepareContinuationFollow({continuation,start,intent,groundAt,worldFor,inside,speed}){
 // Only the exact one-use, fresh real-landing hint may select this optimization.
 // Unknown support/validation failures return fallback: the full planner remains.
 if(!continuation||!sealed.has(continuation))return {kind:'fallback',reason:'continuation_not_sealed'};
 sealed.delete(continuation);
 try{
  validateFollowIntent(intent);
  if(!point(start)||canon(start)!==canon(continuation.start)||canon(intent.goal)!==canon(continuation.goal)||canon(intent.peer)!==canon(continuation.peer)||typeof speed!=='number'||!Number.isFinite(speed)||speed<=0||speed>3)return {kind:'fallback',reason:'continuation_request_changed'};
  const finalSupport=supportedFollowGoal(groundAt,intent.goal),selected=[...finalSupport.position],full=dist(start,selected);
  if(full<.1)return {kind:'fallback',reason:'continuation_too_near'};
  const maximum=Math.min(.8,intent.segmentLength-.05),ratio=Math.min(1,maximum/full),reference=start.map((v,i)=>v+(selected[i]-v)*ratio);
  const goalSupport=supportedFollowGoal(groundAt,reference),end=[...goalSupport.position],length=dist(start,end);
  if(length<.1||length>1.05||length>intent.segmentLength||![start,end].every(inside))return {kind:'fallback',reason:'continuation_prefix_bounds'};
  const points=[[...start],end],collisionWindow=routeCollisionWindow(start,points,BOUNDED_STEP_POLICY),world=worldFor(collisionWindow,BOUNDED_STEP_POLICY);
  let audit;
  try{audit=preflightStepRoute(world,start,points,speed);}catch(e){
   // This exact error comes from the NEW start's real capsule sweep. The staged
   // worker may issue its ordinary fresh failure ticket from this result only.
   if(e.message==='follow_ground:preflight_blocked:collision_blocked')return {kind:'blocked',result:{status:'blocked',reason:'follow_ground:no_prevalidated_or_connected_follow_route:'+e.message,continuationAudit:{shortProbeMeters:length,actualSweepRejected:true}}};
   return {kind:'fallback',reason:String(e.message).slice(0,240)};
  }
  const truncated=dist(end,selected)>1e-6;
  return {kind:'ready',choice:{world,collisionWindow,motorPolicy:BOUNDED_STEP_POLICY,motorPolicyAudit:audit,motorSpeed:audit.commandedSpeedMps,
   plan:{status:'complete',start:[...start],goal:end,path:points,length,pathLength:length},goalSupport,
   followIntentAudit:{peer:[...intent.peer],selectedGoal:selected,prefixLength:length,fullPathLength:Math.max(full,length),fullPathLengthKind:'supported_direct_reference',fullRouteVerified:!truncated,truncated,routeKind:'cold_collision_preflight',rejected:[],continuation:true}}};
 }catch(e){return {kind:'fallback',reason:String(e.message).slice(0,240)};}
}
