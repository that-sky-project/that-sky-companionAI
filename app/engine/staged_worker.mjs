// Explicit local/offline JSONL path: pinned imported or on-demand static navigation + preserving collision.
// No default worker, core, motor parameters, game transport or server is changed.
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {createHash,randomUUID} from 'node:crypto';
import {performance} from 'node:perf_hooks';
import {importNavMesh,NavMeshQuery,getNavMeshPositionsAndIndices} from 'recast-navigation';
import {init,DEFAULTS,navigationWindow} from './core.mjs';
import {loadAsset} from './assets.mjs';
import {createHopContinuation,prepareContinuationFollow} from './hop_continuation_v22.mjs';
import {prepareFollowRoute} from './follow_ground_v14.mjs';
import {OnDemandNavigation,validateOnDemandDescriptor} from './on_demand_nav.mjs';
import {calculateStartSupportProjection,START_SUPPORT_LIMITS} from './start_support.mjs';
import {completePath} from './review_beamo_navigation.mjs';
import {OfflineMotionWorld,routeCollisionWindow,PRESERVING_ORIGIN_POLICY} from './offline_motion_world.mjs';
export const WORKER_PROFILE='preserving_imported_nav';
const HASH=/^[0-9a-f]{64}$/;
const need=(ok,why)=>{if(!ok)throw Error('staged_worker:'+why);};
const sha=b=>createHash('sha256').update(b).digest('hex');
const canon=v=>Array.isArray(v)?'['+v.map(canon).join(',')+']':v&&typeof v==='object'?'{'+Object.keys(v).sort().map(k=>JSON.stringify(k)+':'+canon(v[k])).join(',')+'}':JSON.stringify(v);
const flags=Object.freeze({offline:true,workerProfile:WORKER_PROFILE,nativeCollisionVerified:false,calibrationVerified:false,runtimeReady:false,navigationRebuilt:false,navigationSource:'imported',geometryPolicy:PRESERVING_ORIGIN_POLICY});
const point=p=>Array.isArray(p)&&p.length===3&&p.every(x=>typeof x==='number'&&Number.isFinite(x)&&Math.abs(x)<1e6);
const inside=(w,p)=>!w||(p[0]>=w.minX&&p[0]<=w.maxX&&p[2]>=w.minZ&&p[2]<=w.maxZ);
const allowedKinds=new Set(['offline_beamo_augmented_candidates','initial_static_collision_candidates']);
function bounded(file,limit){const fd=fs.openSync(file,'r');try{const s=fs.fstatSync(fd);need(s.isFile()&&s.size>0&&s.size<=limit,'input_budget');const b=Buffer.alloc(s.size+1);let n=0;while(n<b.length){const k=fs.readSync(fd,b,n,b.length-n,null);if(!k)break;n+=k;}need(n===s.size,'input_changed');return b.subarray(0,n);}finally{fs.closeSync(fd);}}
export function resolveStagedInput(root,relative){
 need(typeof relative==='string'&&relative.length>0&&relative.length<=512&&!/[\\:\0-\x1f]/.test(relative)&&!path.isAbsolute(relative)&&relative.split('/').every(p=>p&&!['.','..'].includes(p)),'relative_input_path');
 const file=fs.realpathSync(path.resolve(root,relative)),rel=path.relative(root,file);need(rel&&!rel.startsWith('..'+path.sep)&&rel!=='..'&&!path.isAbsolute(rel),'input_root_escape');return file;
}
function child(parent,name){need(typeof name==='string'&&name.length>0&&!/[\\/:\0-\x1f]/.test(name)&&!['.','..'].includes(name),'child_name');const p=fs.realpathSync(parent),f=fs.realpathSync(path.join(p,name));need(path.dirname(f)===p,'child_escape');return f;}
function pinnedJSON(file,max,pin){need(HASH.test(pin??''),'hash_required');const b=bounded(file,max);need(sha(b)===pin,'input_hash_mismatch');return JSON.parse(b);}
function falseFlags(d){for(const k of ['nativeCollisionVerified','calibrationVerified','runtimeReady'])need(d?.[k]===false,'offline_flags:'+k);}
export function validateStagedNavigation(nav,asset,q){
 need(nav?.format==='skyterrain-navigation-build-v1'&&nav.mode==='offline'&&nav.status==='built_offline','navigation_schema');falseFlags(nav);
 need(allowedKinds.has(asset.manifest.geometryKind)&&nav.geometryKind===asset.manifest.geometryKind&&nav.mapName===asset.manifest.mapName,'navigation_map_or_kind');
 need(asset.manifest.nativeCollisionVerified===false&&asset.manifest.calibrationVerified===false,'asset_offline_flags');
 need(canon(nav.config)===canon(DEFAULTS)&&(!Object.hasOwn(q,'config')||canon(q.config)===canon(DEFAULTS)),'unchanged_defaults_required');
 const window=navigationWindow(q.navigationWindow??null);need(canon(nav.navigationWindow??null)===canon(window),'navigation_window_binding');
 if(window)need(nav.navigationCoverage==='bounded_window','bounded_navigation_required');
 else need(nav.navigationCoverage==='full_static_candidate'||(nav.geometryKind==='initial_static_collision_candidates'&&!Object.hasOwn(nav,'navigationCoverage')&&!Object.hasOwn(nav,'navigationWindow')),'full_navigation_scope');
 for(const [key,pin]of Object.entries(asset.assetDigests))need(nav.inputHashes?.[key]===pin,'navigation_asset_hash:'+key);
 need(nav.inputVertices===asset.positions.length/3&&nav.inputTriangles===asset.indices.length/3,'navigation_input_counts');
 need(HASH.test(q.expectedNavigationSHA??'')&&nav.navigationHash===q.expectedNavigationSHA,'navigation_hash_binding');
 need(Number.isSafeInteger(nav.navigationBytes)&&nav.navigationBytes>0&&nav.navigationBytes<=128*1024*1024,'navigation_byte_budget');
 need(Number.isSafeInteger(nav.navVertices)&&nav.navVertices>0&&nav.navVertices<=8_000_000&&Number.isSafeInteger(nav.navTriangles)&&nav.navTriangles>0&&nav.navTriangles<=8_000_000,'navigation_geometry_budget');return window;
}
function validateEvidence(asset,manifestFile){
 const m=asset.manifest,ref=m.evidenceManifest;need(ref&&HASH.test(ref.sha256??''),'evidence_required');const e=pinnedJSON(child(path.dirname(manifestFile),ref.file),32*1024*1024,ref.sha256);
 need(e.schema_version==='sky-partial-static-world-v1'&&e.format==='skyterrain-static-v1'&&e.coordinate_space==='sky_native'&&e.geometry_kind==='partial_static_world_candidate','evidence_schema');
 for(const k of ['format','mapName','geometryKind','nativeCollisionVerified','positions','indices','vertexCount','triangleCount','sha256'])need(canon(e[k])===canon(m[k]),'evidence_identity:'+k);
 need(e.nativeCollisionVerified===false&&e.native_collision_verified===false&&e.runtime_ready===false,'evidence_offline_flags');
 for(const [key,count,snake,descriptor,dtype]of [['positions','vertexCount','vertex_count','positions','<f4'],['indices','triangleCount','triangle_count','triangles','<u4']]){const n=asset[key].length/3,f=e.files?.[descriptor];need(e[count]===n&&e[snake]===n&&e.sha256?.[key]===asset.assetDigests[key]&&f?.file===m[key]&&f.sha256===asset.assetDigests[key]&&f.bytes===asset[key].byteLength&&f.dtype===dtype&&canon(f.shape)===canon([n,3]),'evidence_arrays:'+key);}
 return ref.sha256;
}
function fields(q,allowed){need(q&&typeof q==='object'&&!Array.isArray(q)&&Object.keys(q).every(k=>allowed.includes(k)||k==='op'||k==='id'),'request_fields');}
function hopScope(value){need(value&&typeof value==='object'&&!Array.isArray(value)&&Object.keys(value).sort().join(',')==='actorId,epoch,incarnation,mapId,roomId','hop_scope_fields');for(const k of ['epoch','incarnation','mapId'])need(Number.isSafeInteger(value[k])&&value[k]>=0,'hop_scope_number');for(const k of ['actorId','roomId'])need(typeof value[k]==='string'&&value[k].length>0&&value[k].length<=256,'hop_scope_id');return structuredClone(value);}
function geometryFailure(reason){if(['collision_blocked','small_step_blocked','incomplete_polygon_corridor','goal_support_slope'].includes(reason))return reason;if(!reason.startsWith('follow_ground:no_prevalidated_or_connected_follow_route:'))return null;if(reason.includes(':preflight_blocked:collision_blocked'))return 'collision_blocked';if(reason.includes(':goal_support_slope'))return 'goal_support_slope';if(reason.includes(':preflight_airborne')||reason.includes(':preflight_support'))return 'ground_contact_gap';if(reason.includes(':preflight_not_arrived'))return 'ground_route_incomplete';return null;}

export class StagedWorkerState{
 #cache=[];#cacheTick=0;#cacheHits=0;#cacheMisses=0;
 #onDemand=null;#root;#asset=null;#nav=null;#query=null;#window=null;#world=null;#motor=null;#token=null;#scope=null;#prepared=null;#closed=false;
 #continuation;#hopContinuationContext=null;#hopProofRoot=null;#hopClock;#groundFailure=null;#hopSession=null;#hopOwner=null;#hopGroundScope=null;#manifestPin=null;#loadEpoch=0;
 constructor(root,{offline=false,hopProofRoot=null,hopClock=()=>performance.now()/1000}={}){need(offline===true,'offline_flag_required');this.#root=fs.realpathSync(root);need(fs.statSync(this.#root).isDirectory(),'asset_root_directory');need(typeof hopClock==='function','hop_clock');this.#hopClock=hopClock;this.#continuation=createHopContinuation({clock:hopClock});if(hopProofRoot!==null){need(typeof hopProofRoot==='string'&&path.isAbsolute(hopProofRoot),'absolute_hop_proof_root');fs.mkdirSync(hopProofRoot,{recursive:true});this.#hopProofRoot=fs.realpathSync(hopProofRoot);}}
 #stop(){this.#hopContinuationContext=null;this.#hopSession?.close('worker_motion_invalidated');this.#hopSession=null;this.#hopOwner=null;this.#hopGroundScope=null;this.#groundFailure=null;this.#prepared=null;this.#motor?.dispose();this.#motor=null;this.#world=null;this.#token=null;this.#scope=null;}
 #unload(){this.#continuation.clear();this.#stop();this.#manifestPin=null;this.#loadEpoch++;for(const e of this.#cache)e.world.dispose();this.#cache=[];this.#cacheTick=this.#cacheHits=this.#cacheMisses=0;if(this.#onDemand){this.#onDemand.dispose();this.#onDemand=null;}else this.#query?.destroy();this.#query=null;this.#nav?.destroy();this.#nav=null;this.#asset=null;this.#window=null;}
 #failure(q,result){if(!this.#hopProofRoot||q.op!=='preparePlan'||result.status==='ready')return result;const reason=String(result.reason??'');let cause=geometryFailure(reason),supportedDropGoal=null;if(!cause&&reason.startsWith('follow_ground:no_prevalidated_or_connected_follow_route:')&&reason.includes(':goal_support_missing')&&point(q.start)&&point(q.followIntent?.goal)){try{const p=q.followIntent.goal,world=this.#cachedWorld({minX:p[0]-.25,maxX:p[0]+.25,minZ:p[2]-.25,maxZ:p[2]+.25}),hit=world.ground([p[0],p[1]+.75,p[2]],1.5);if(hit&&hit.normal[1]>=Math.cos(DEFAULTS.slopeDegrees*Math.PI/180)&&q.start[1]-hit.position[1]>.35&&q.start[1]-hit.position[1]<=6.35){cause='ground_route_incomplete';supportedDropGoal=hit.position;}}catch{}}if(!cause)return result;try{const owner=hopScope(q.hopScope);need(this.#asset&&typeof q.scope==='string'&&q.scope.length>0&&point(q.start)&&point(q.goal),'failure_binding');const failure={id:randomUUID(),cause,originalReason:reason.slice(0,2048),scope:q.scope,hopScope:owner,start:[...q.start],goal:[...q.goal],speed:q.speed??DEFAULTS.speed,loadEpoch:this.#loadEpoch,continuationKey:q.continuationKey??null,followGoal:q.followIntent?.goal??null,followPeer:q.followIntent?.peer??null,...(supportedDropGoal?{supportedDropGoal}: {})};this.#groundFailure=failure;return {...result,groundFailure:structuredClone(failure)};}catch{return result;}}
 #cachedWorld(required,motorPolicy=undefined){
  const contains=(a,b)=>a.minX<=b.minX&&a.maxX>=b.maxX&&a.minZ<=b.minZ&&a.maxZ>=b.maxZ;
  const candidates=this.#cache.filter(e=>e.motorPolicy===motorPolicy&&canon(e.navigationWindow)===canon(this.#window)&&contains(e.window,required));
  if(candidates.length){candidates.sort((a,b)=>(a.window.maxX-a.window.minX)*(a.window.maxZ-a.window.minZ)-(b.window.maxX-b.window.minX)*(b.window.maxZ-b.window.minZ));const e=candidates[0];e.used=++this.#cacheTick;this.#cacheHits++;return e.world;}
  // Fixed grid changes only cached STATIC geometry coverage. Nav permissions
  // and the motion's original requested route window remain unchanged.
  const grid=16,window=Object.freeze({minX:Math.floor(required.minX/grid)*grid,maxX:Math.ceil(required.maxX/grid)*grid,minZ:Math.floor(required.minZ/grid)*grid,maxZ:Math.ceil(required.maxZ/grid)*grid});
  const world=new OfflineMotionWorld(this.#asset.positions,this.#asset.indices,{offline:true,geometryPolicy:PRESERVING_ORIGIN_POLICY,navigationWindow:this.#window,collisionWindow:window,...(motorPolicy?{motorPolicy}:{})});
  if(this.#cache.length>=3){const oldest=this.#cache.reduce((a,b)=>a.used<b.used?a:b);oldest.world.dispose();this.#cache.splice(this.#cache.indexOf(oldest),1);}
  this.#cache.push({world,window,navigationWindow:this.#window,motorPolicy,used:++this.#cacheTick});this.#cacheMisses++;return world;
 }
 #cacheAudit(){return {kind:'same_load_static_world_lru',entries:this.#cache.length,capacity:3,hits:this.#cacheHits,misses:this.#cacheMisses,gridMeters:16,routeCoverageUnchanged:true};}
 dispose(){if(!this.#closed){this.#unload();this.#closed=true;}}
 request(q){try{need(!this.#closed,'worker_disposed');need(q&&typeof q==='object'&&!Array.isArray(q),'request_object');const result=this.#failure(q,this.#handle(q));return {...flags,...(this.#onDemand?{navigationSource:'on_demand',navigationIdentityKind:'descriptor_sha256'}:{}),...result};}catch(e){if(q?.op?.endsWith('Hop')){this.#continuation.clear();this.#hopContinuationContext=null;this.#hopSession?.close('hop_rpc_rejected');this.#hopSession=null;this.#hopOwner=null;this.#hopGroundScope=null;this.#groundFailure=null;}return {...flags,...this.#failure(q??{},{status:'unavailable',reason:String(e.message).slice(0,2048)}),reason:String(e.message).slice(0,240)};}}
 #handle(q){switch(q.op){
  case 'ping':fields(q,[]);return {status:'ready',protocol:1};
  case 'load':{
   this.#unload();fields(q,['manifest','expectedManifestSHA','navigationReport','expectedNavigationReportSHA','expectedNavigationSHA','navigationWindow','config']);
   need(HASH.test(q.expectedManifestSHA??''),'asset_pin_required');const manifest=resolveStagedInput(this.#root,q.manifest),report=resolveStagedInput(this.#root,q.navigationReport);
   const asset=loadAsset(this.#root,q.manifest,{offline:true,expectedManifestSHA:q.expectedManifestSHA});const nav=pinnedJSON(report,1024*1024,q.expectedNavigationReportSHA);const evidenceSHA256=validateEvidence(asset,manifest);
   if(nav.format==='skyterrain-navigation-on-demand-v1'){
    validateOnDemandDescriptor(nav,asset,q);this.#onDemand=new OnDemandNavigation(asset);this.#asset=asset;this.#window=null;this.#manifestPin=q.expectedManifestSHA;
    return {status:'ready',assetDigests:asset.assetDigests,mapName:asset.manifest.mapName,geometryKind:asset.manifest.geometryKind,navigationReady:true,routePreparationRequired:true,navError:null,navigationCoverage:'on_demand_static_candidate',navigationWindow:null,navigationReportSHA256:q.expectedNavigationReportSHA,navigationSHA256:q.expectedNavigationSHA,navigationIdentityKind:'descriptor_sha256',evidenceManifestSHA256:evidenceSHA256,navigationCache:this.#onDemand.audit(),navigationRebuilt:false,fullMapBuilt:false};
   }
   const window=validateStagedNavigation(nav,asset,q);
   const raw=bounded(child(path.dirname(report),nav.navigationFile),128*1024*1024);need(raw.length===nav.navigationBytes&&sha(raw)===q.expectedNavigationSHA,'navigation_binary_pin');
   let imported,query;try{({navMesh:imported}=importNavMesh(new Uint8Array(raw)));query=new NavMeshQuery(imported,{maxNodes:2048});const [ps,is]=getNavMeshPositionsAndIndices(imported);need(ps.length===nav.navVertices*3&&is.length===nav.navTriangles*3&&ps.every(Number.isFinite)&&is.every(i=>Number.isInteger(i)&&i>=0&&i<ps.length/3),'imported_navigation_counts');this.#nav=imported;this.#query=query;}catch(e){query?.destroy();imported?.destroy();throw e;}
   this.#asset=asset;this.#window=window;this.#manifestPin=q.expectedManifestSHA;
   return {status:'ready',assetDigests:asset.assetDigests,mapName:asset.manifest.mapName,geometryKind:asset.manifest.geometryKind,navigationReady:true,routePreparationRequired:true,navError:null,navigationCoverage:window?'bounded_window':'full_static_candidate',navigationWindow:window,navigationReportSHA256:q.expectedNavigationReportSHA,navigationSHA256:q.expectedNavigationSHA,evidenceManifestSHA256:evidenceSHA256};
  }
  case 'preparePlan':{
   // Explicit cold path: select/build bounded navigation and the fixed collision domain.
   // No motor/time state is advanced until the one-use hot plan consumes the pin.
   const continuation=this.#continuation.take({scope:q.scope,hopScope:q.hopScope,continuationKey:q.continuationKey,loadEpoch:this.#loadEpoch,start:q.start,goal:q.followIntent?.goal,peer:q.followIntent?.peer});this.#stop();fields(q,['start','goal','speed','scope','followIntent','hopScope','continuationKey']);if(q.continuationKey!==undefined)need(HASH.test(q.continuationKey),'continuation_key');if(q.hopScope!==undefined){need(this.#hopProofRoot,'hop_disabled');hopScope(q.hopScope);}need(this.#asset&&(this.#query||this.#onDemand),'asset_not_loaded');need(typeof q.scope==='string'&&q.scope.length>0&&q.scope.length<=256,'invalid_scope');need(point(q.start)&&point(q.goal),'finite_points_required');const speed=q.speed??DEFAULTS.speed;need(typeof speed==='number'&&Number.isFinite(speed)&&speed>0&&speed<=10,'speed');let navigationBuild=null;
   if(this.#onDemand){navigationBuild=this.#onDemand.acquire(q.start,q.goal,{cold:true});this.#query=navigationBuild.entry.query;this.#window=navigationBuild.entry.window;}
   need(inside(this.#window,q.start)&&inside(this.#window,q.goal),'outside_navigation_window');
   if(q.followIntent!==undefined){
    const args={query:this.#query,start:q.start,intent:q.followIntent,speed,inside:p=>inside(this.#window,p),
     groundAt:(p,d)=>this.#cachedWorld({minX:p[0]-.25,maxX:p[0]+.25,minZ:p[2]-.25,maxZ:p[2]+.25}).ground(p,d),
     worldFor:(w,policy)=>this.#cachedWorld(w,policy)},quick=prepareContinuationFollow({...args,continuation});
    if(quick.kind==='blocked')return quick.result;const choice=quick.kind==='ready'?quick.choice:prepareFollowRoute(args);
    const {world,collisionWindow,plan,goalSupport}=choice;
    const supportOrigin=[q.start[0],q.start[1]+START_SUPPORT_LIMITS.queryAbove,q.start[2]],supportHit=world.ground(supportOrigin,START_SUPPORT_LIMITS.queryDistance),supportProposal=calculateStartSupportProjection(q.start,supportHit,DEFAULTS);
    const motorStart=supportProposal.status==='adjusted'?supportProposal.adjustedStart:[...q.start],preparedToken=randomUUID();this.#world=world;
    this.#prepared={preparedToken,scope:q.scope,start:[...q.start],goal:[...q.goal],speed,plan,collisionWindow,motorStart,supportProposal,startSupportPolicy:supportProposal.status==='blocked'?'original_capsule_constructor_decides':'bounded_positive_support_proposal',goalSupport,navigationGoal:plan.goal,followIntentAudit:choice.followIntentAudit,motorPolicy:choice.motorPolicy,motorPolicyAudit:choice.motorPolicyAudit,motorSpeed:choice.motorSpeed};
    return {...plan,status:'ready',preparedToken,scope:q.scope,estimatedSeconds:plan.length/(choice.motorSpeed??speed),frameOrigin:world.frameOrigin,collisionWindow,geometryMetadata:world.metadata,preparation:'explicit_cold',motorStarted:false,collisionCache:this.#cacheAudit(),goalSupport,navigationGoal:plan.goal,requestedGoal:[...q.goal],followIntentAudit:choice.followIntentAudit,...(choice.motorPolicy?{motorPolicy:choice.motorPolicy,motorPolicyAudit:choice.motorPolicyAudit}:{}),...(navigationBuild?{navigationRebuilt:navigationBuild.built,navigationWindow:this.#window,navigationBuildMs:navigationBuild.built?navigationBuild.entry.buildMs:0,navigationCache:this.#onDemand.audit()}: {})};
   }
   // Goal XZ is a standing location, not necessarily the peer's XZ. Its
   // supplied Y is only a reference; query this location's actual support before
   // selecting a navigation layer. This NEVER changes requested motor start.
   let goalProbe,goalSupport,navigationGoal;
   try{
    goalProbe=this.#cachedWorld({minX:q.goal[0]-.25,maxX:q.goal[0]+.25,minZ:q.goal[2]-.25,maxZ:q.goal[2]+.25});
    goalSupport=goalProbe.ground([q.goal[0],q.goal[1]+START_SUPPORT_LIMITS.queryAbove,q.goal[2]],START_SUPPORT_LIMITS.queryDistance);
    need(goalSupport&&point(goalSupport.position)&&point(goalSupport.normal),'goal_support_missing');
    need(Math.abs(goalSupport.position[1]-q.goal[1])<=START_SUPPORT_LIMITS.queryAbove+1e-6,'goal_support_height_band');
    need(goalSupport.normal[1]>=Math.cos(DEFAULTS.slopeDegrees*Math.PI/180)&&Math.abs(Math.hypot(...goalSupport.normal)-1)<=START_SUPPORT_LIMITS.normalLengthTolerance,'goal_support_slope');
    navigationGoal=[q.goal[0],goalSupport.position[1],q.goal[2]];
   }finally{goalProbe=null;}
   // Existing .3 m projection, XZ/search extent, corridor/slope/path gates remain.
   // The final collision world is rebuilt around the ACTUAL complete route.
   const plan=completePath(this.#query,q.start,navigationGoal,{minLength:.1,segmentPolicy:'collision_motor'});if(plan.status!=='complete')return plan;
   need(inside(this.#window,plan.start)&&inside(this.#window,plan.goal)&&plan.path.every(p=>inside(this.#window,p)),'path_outside_original_window');
   let world;try{const collisionWindow=routeCollisionWindow(q.start,plan.path);world=this.#cachedWorld(collisionWindow);const supportOrigin=[q.start[0],q.start[1]+START_SUPPORT_LIMITS.queryAbove,q.start[2]],supportHit=world.ground(supportOrigin,START_SUPPORT_LIMITS.queryDistance),supportProposal=calculateStartSupportProjection(q.start,supportHit,DEFAULTS);const startSupportPolicy=supportProposal.status==='blocked'?'original_capsule_constructor_decides':'bounded_positive_support_proposal';const motorStart=supportProposal.status==='adjusted'?supportProposal.adjustedStart:[...q.start];const preparedToken=randomUUID();this.#world=world;this.#prepared={preparedToken,scope:q.scope,start:[...q.start],goal:[...q.goal],speed,plan,collisionWindow,motorStart,supportProposal,startSupportPolicy,goalSupport,navigationGoal};
    return {...plan,status:'ready',preparedToken,scope:q.scope,estimatedSeconds:plan.length/speed,frameOrigin:world.frameOrigin,collisionWindow,geometryMetadata:world.metadata,preparation:'explicit_cold',motorStarted:false,...(navigationBuild?{navigationRebuilt:navigationBuild.built,navigationWindow:this.#window,navigationBuildMs:navigationBuild.built?navigationBuild.entry.buildMs:0,navigationCache:this.#onDemand.audit()}:{}),collisionCache:this.#cacheAudit(),goalSupport,navigationGoal,requestedGoal:[...q.goal]};
   }catch(e){this.#stop();return {status:'blocked',reason:String(e.message).slice(0,240)};}
  }
  case 'plan':{
   const prepared=this.#prepared;this.#prepared=null;
   try{fields(q,['start','goal','now','speed','scope','preparedToken']);need(prepared&&this.#world&&!this.#motor,'prepared_plan_required');need(q.preparedToken===prepared.preparedToken,'prepared_token_mismatch');need(q.scope===prepared.scope&&canon(q.start)===canon(prepared.start)&&canon(q.goal)===canon(prepared.goal)&&(q.speed??DEFAULTS.speed)===prepared.speed,'prepared_input_mismatch');need(typeof q.now==='number'&&Number.isFinite(q.now),'finite_time_required');
    const {plan,speed,collisionWindow}=prepared;this.#motor=this.#world.motor(prepared.motorStart,plan.path,q.now,prepared.motorSpeed??speed,collisionWindow);this.#scope=q.scope;this.#token=randomUUID();
    return {...(prepared.followIntentAudit?{followIntentAudit:prepared.followIntentAudit}:{}),...(prepared.motorPolicy?{motorPolicy:prepared.motorPolicy,motorPolicyAudit:prepared.motorPolicyAudit}:{}),...plan,status:'ready',token:this.#token,scope:this.#scope,estimatedSeconds:plan.length/(prepared.motorSpeed??speed),frameOrigin:this.#world.frameOrigin,collisionWindow,startSupportAudit:this.#world.startSupportAudit,geometryMetadata:this.#world.metadata,preparation:'consumed_exact',collisionCache:this.#cacheAudit(),collisionConstructedOnHotPath:false,motorStartSource:'requested_feet_with_collision_support_check',requestedStart:[...prepared.start],navigationProjectedStart:[...plan.start],startSupportProposal:prepared.supportProposal,startSupportPolicy:prepared.startSupportPolicy,goalSupport:prepared.goalSupport,navigationGoal:prepared.navigationGoal,requestedGoal:[...prepared.goal]};
   }catch(e){this.#stop();throw e;}
  }
  case 'prepareHop':{
   fields(q,['scope','hopScope','start','goal','speed','ttlSeconds','artifactDirectory','groundFailureId','aerialProfiles']);const aerialProfiles=q.aerialProfiles??[];need(Array.isArray(aerialProfiles)&&aerialProfiles.length<=3&&new Set(aerialProfiles).size===aerialProfiles.length&&aerialProfiles.every(x=>['drop_v22','flap_v22','jump_drop_v23'].includes(x)),'aerial_profiles');need(this.#hopProofRoot&&this.#asset&&this.#manifestPin,'hop_disabled_or_not_loaded');need(!this.#motor&&!this.#hopSession,'hop_requires_idle_worker');const owner=hopScope(q.hopScope),failure=this.#groundFailure;
   need(failure&&failure.id===q.groundFailureId&&failure.loadEpoch===this.#loadEpoch&&failure.scope===q.scope&&canon(failure.hopScope)===canon(owner),'confirmed_ground_failure_required');
   need(canon(q.start)===canon(failure.start)&&canon(q.goal)===canon(failure.goal)&&q.speed===failure.speed,'ground_failure_input_mismatch');need(typeof q.artifactDirectory==='string'&&path.isAbsolute(q.artifactDirectory)&&fs.realpathSync(q.artifactDirectory)===this.#hopProofRoot,'hop_proof_directory_mismatch');
   this.#groundFailure=null;this.#hopContinuationContext={scope:failure.scope,hopScope:owner,continuationKey:failure.continuationKey,loadEpoch:failure.loadEpoch,goal:failure.followGoal,peer:failure.followPeer};this.#hopOwner=owner;this.#hopGroundScope=q.scope;const collisionWindow=routeCollisionWindow(q.start,[q.goal]),world=this.#cachedWorld(collisionWindow);this.#world=world;
   const result=world.prepareHopSession({groundResult:{status:'blocked',reason:failure.cause},start:q.start,target:q.goal,speed:q.speed,scope:owner,collisionWindow,assetManifestSHA256:this.#manifestPin,artifactDirectory:this.#hopProofRoot,clock:this.#hopClock,currentScope:()=>this.#hopOwner,ttlSeconds:q.ttlSeconds,aerialProfiles});
   if(result.status!=='ready'){this.#hopOwner=null;this.#hopGroundScope=null;return result;}this.#hopSession=result.session;
   return {status:'ready',...result.session.prepared,scope:q.scope,hopScope:owner,groundFailureId:failure.id,groundFailureCause:failure.cause,hopRPCEnabled:true,productionPublicationEnabled:false};
  }
  case 'startHop':case 'sampleHop':case 'softCancelHop':case 'hardCancelHop':{
   fields(q,['scope','hopScope','token','reason']);need(this.#hopProofRoot&&this.#hopSession,'hop_session_required');const owner=hopScope(q.hopScope);need(q.scope===this.#hopGroundScope&&canon(owner)===canon(this.#hopOwner),'hop_ownership_changed');
   const operation={startHop:'start',sampleHop:'sample',softCancelHop:'softCancel',hardCancelHop:'hardCancel'}[q.op],reply=this.#hopSession.invoke(operation,{scope:owner,token:q.token,...(q.op==='hardCancelHop'?{reason:q.reason??'cancelled'}:{})});
   if(q.op==='softCancelHop'||q.op==='hardCancelHop')this.#continuation.clear();if(q.op==='sampleHop'&&reply.phase==='landed'&&this.#hopContinuationContext)this.#continuation.record({...this.#hopContinuationContext,reply});if(reply.state==='landed'||reply.state==='closed'){this.#hopContinuationContext=null;this.#hopSession=null;this.#hopOwner=null;this.#hopGroundScope=null;}
   return {status:'ready',...reply,scope:q.scope,hopScope:owner};
  }
  case 'retarget':{
   fields(q,['token','scope','start','goal','speed']);
   if(!this.#motor||!this.#world||q.token!==this.#token||q.scope!==this.#scope)return {status:'unavailable',reason:'stale_motion'};
   // All rejection branches leave the old motor, token and cached sample intact.
   const cold=reason=>({status:'needs_cold_replan',reason,token:this.#token,scope:this.#scope});
   if(this.#world.metadata.motorPolicy)return cold('bounded_step_retarget_requires_preflight');
   try{
    need(point(q.start)&&point(q.goal),'finite_points_required');
    const speed=q.speed;need(typeof speed==='number'&&Number.isFinite(speed)&&speed>0&&speed<=10,'speed');
    const current=this.#motor.snapshot();
    if(current.grounded!==true||current.arrived||current.blocked)return cold('active_grounded_motor_required');
    if(Math.hypot(...current.position.map((v,i)=>v-q.start[i]))>1e-5)return cold('retarget_start_mismatch');
    if(!inside(this.#window,q.start)||!inside(this.#window,q.goal))return cold(this.#onDemand?'navigation_window_requires_cold_replan':'outside_navigation_window');
    const outer=this.#world.metadata.coverageWindow,contains=w=>w.minX>=outer.minX&&w.maxX<=outer.maxX&&w.minZ>=outer.minZ&&w.maxZ<=outer.maxZ;
    if(!contains({minX:q.goal[0]-.25,maxX:q.goal[0]+.25,minZ:q.goal[2]-.25,maxZ:q.goal[2]+.25}))return cold('goal_outside_cached_world');
    const support=this.#world.ground([q.goal[0],q.goal[1]+START_SUPPORT_LIMITS.queryAbove,q.goal[2]],START_SUPPORT_LIMITS.queryDistance);
    if(!support||!point(support.position)||!point(support.normal))return cold('goal_support_missing');
    if(Math.abs(support.position[1]-q.goal[1])>START_SUPPORT_LIMITS.queryAbove+1e-6)return cold('goal_support_height_band');
    if(support.normal[1]<Math.cos(DEFAULTS.slopeDegrees*Math.PI/180)||Math.abs(Math.hypot(...support.normal)-1)>START_SUPPORT_LIMITS.normalLengthTolerance)return cold('goal_support_slope');
    const navigationGoal=[q.goal[0],support.position[1],q.goal[2]],plan=completePath(this.#query,current.position,navigationGoal,{minLength:.1,segmentPolicy:'collision_motor'});
    if(plan.status!=='complete')return cold(plan.reason);
    if(!inside(this.#window,plan.start)||!inside(this.#window,plan.goal)||!plan.path.every(p=>inside(this.#window,p)))return cold('path_outside_original_window');
    const collisionWindow=routeCollisionWindow(current.position,plan.path);if(!contains(collisionWindow))return cold('route_outside_cached_world');
    const snapshot=this.#motor.retarget(q.start,plan.path,collisionWindow,speed);
    return {status:'ready',token:this.#token,scope:this.#scope,start:[...snapshot.position],goal:plan.goal,path:plan.path,pathLength:plan.length,estimatedSeconds:plan.length/speed,grounded:snapshot.grounded,velocity:snapshot.velocity,heading:snapshot.heading,collisionWindow,retargeted:true,physicsAdvanced:false,collisionConstructedOnHotPath:false,collisionCache:this.#cacheAudit()};
   }catch(e){return cold(String(e.message).slice(0,240));}
  }
  case 'sample':{
   fields(q,['token','scope','now']);if(!this.#motor||q.token!==this.#token||q.scope!==this.#scope)return {status:'unavailable',reason:'stale_motion'};
   try{return {status:'ready',...this.#motor.sample(q.now)};}catch(e){this.#stop();throw e;}
  }
  case 'cancel':this.#continuation.clear();fields(q,['token']);if(q.token!==undefined&&q.token!==this.#token)return {status:'unavailable',reason:'stale_motion'};this.#stop();return {status:'ready'};
  case 'invalidate':this.#continuation.clear();fields(q,[]);this.#stop();return {status:'ready'};
  case 'ground':{
   fields(q,['point','maxDistance']);need(this.#asset,'asset_not_loaded');need(point(q.point)&&inside(this.#window,q.point),'ground_point_outside_navigation_window');const max=q.maxDistance??10;need(typeof max==='number'&&Number.isFinite(max)&&max>0&&max<=256,'ground_distance');
   // No cold geometry construction in a query or motion callback.
   need(this.#world,'cold_plan_preparation_required');return {status:'ready',ground:this.#world.ground(q.point,max)};
  }
  default:throw Error('unknown_operation');
 }}
}
function usageError(message){
 process.stderr.write('staged_worker: '+message+'\n');
 process.stderr.write('usage: node staged_worker.mjs --assets <地形资产目录> --offline [--hop-proofs <目录>]\n');
 process.exit(3);
}
async function main(){
 const args=process.argv.slice(2),i=args.indexOf('--assets');
 if(i<0||!args[i+1]||!args.includes('--offline'))usageError('缺资产（assets missing）：需要 --assets <目录> --offline');
 if(!((args.length===3||args.length===5&&args[3]==='--hop-proofs')&&i===0&&args[2]==='--offline'))usageError('参数形式不对（cli_fields）');
 const assetRoot=path.resolve(args[1]);
 if(!fs.existsSync(assetRoot)||!fs.statSync(assetRoot).isDirectory())usageError('缺资产目录：'+assetRoot+'（W2 在用户机上生成地形/导航资产后再启动）');
 try{await init();}catch(e){usageError('引擎初始化失败：'+e.message);}
 let state;try{state=new StagedWorkerState(assetRoot,{offline:true,...(args.length===5?{hopProofRoot:args[4]}:{})});}catch(e){usageError('缺资产（assets unusable）：'+e.message);}
 let pending='';process.stdin.setEncoding('utf8');
 process.stdin.on('data',chunk=>{pending+=chunk;if(Buffer.byteLength(pending)>65536){state.dispose();process.stderr.write('request_budget\n');process.exit(2);return;}let end;while((end=pending.indexOf('\n'))>=0){const line=pending.slice(0,end);pending=pending.slice(end+1);let q=null,r;try{q=JSON.parse(line);r=state.request(q);}catch(e){r={...flags,status:'unavailable',reason:String(e.message).slice(0,240)};}let reply=JSON.stringify({id:q?.id??null,...r});if(Buffer.byteLength(reply)>65536){state.dispose();reply=JSON.stringify({id:q?.id??null,...flags,status:'unavailable',reason:'reply_budget'});}process.stdout.write(reply+'\n');}});process.stdin.on('end',()=>state.dispose());
}
if(process.argv[1]&&path.resolve(process.argv[1])===fileURLToPath(import.meta.url))await main();
