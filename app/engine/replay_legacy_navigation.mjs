// Separate legacy-kind pipeline. Baseline planning never runs a movement motor.
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {createHash,randomUUID} from 'node:crypto';
import {importNavMesh,NavMeshQuery,getNavMeshPositionsAndIndices} from 'recast-navigation';
import {init,TerrainWorld,DEFAULTS,navigationWindow} from './core.mjs';
import {loadAsset} from './assets.mjs';
import {routeCandidates,completePath,replayCompletePath} from './review_beamo_navigation.mjs';
import {OfflineMotionWorld,routeCollisionWindow,PRESERVING_ORIGIN_POLICY} from './offline_motion_world.mjs';
const KIND='initial_static_collision_candidates',PROFILE='netease-android-0.16.7-421589';
const HASH=/^[0-9a-f]{64}$/;
const FLAGS=Object.freeze({nativeCollisionVerified:false,calibrationVerified:false,runtimeReady:false,footOffsetCalibrated:false,calibrationInstalled:false,navigationRebuilt:false,live_enabled:false,server_modified:false});
const KEYS=['schema','mode','phase','mapName','assetFile','expectedAssetManifestSHA256','navigationReportFile','expectedNavigationReportSHA256','originalNavigationHash','navigationWindow','baselineFile','expectedBaselineSHA256','nativeCollisionVerified','calibrationVerified','runtimeReady'];
const need=(ok,why)=>{if(!ok)throw Error('legacy_replay:'+why);};
const sha=b=>createHash('sha256').update(b).digest('hex');
const canon=v=>Array.isArray(v)?'['+v.map(canon).join(',')+']':v&&typeof v==='object'?'{'+Object.keys(v).sort().map(k=>JSON.stringify(k)+':'+canon(v[k])).join(',')+'}':JSON.stringify(v);
const falseFlags=o=>{for(const k of ['nativeCollisionVerified','calibrationVerified','runtimeReady'])need(o?.[k]===false,'offline_flags:'+k);};
const finitePoint=p=>Array.isArray(p)&&p.length===3&&p.every(x=>typeof x==='number'&&Number.isFinite(x)&&Math.abs(x)<1e6);
const inWindow=(w,p)=>!w||(p[0]>=w.minX&&p[0]<=w.maxX&&p[2]>=w.minZ&&p[2]<=w.maxZ);
export function validateLegacyRequest(value){
 need(value&&typeof value==='object'&&!Array.isArray(value)&&[Object.prototype,null].includes(Object.getPrototypeOf(value)),'request_object');
 const d=Object.getOwnPropertyDescriptors(value);
 need(Reflect.ownKeys(d).length===KEYS.length&&KEYS.every(k=>d[k]&&'value'in d[k]),'request_fields');
 need(value.schema==='offline-legacy-navigation-request-v1'&&value.mode==='offline','offline_schema');falseFlags(value);
 need(['baseline','preserving'].includes(value.phase),'phase');
 need(typeof value.mapName==='string'&&/^[A-Za-z0-9_]{1,128}$/.test(value.mapName),'map_name');
 for(const k of ['assetFile','navigationReportFile'])need(typeof value[k]==='string'&&path.isAbsolute(value[k]),'absolute_input_path');
 for(const k of ['expectedAssetManifestSHA256','expectedNavigationReportSHA256','originalNavigationHash'])need(typeof value[k]==='string'&&HASH.test(value[k]),'input_hash');
 need(value.navigationWindow===null||value.navigationWindow&&typeof value.navigationWindow==='object','explicit_window_or_null');
 const window=navigationWindow(value.navigationWindow);
 if(value.phase==='baseline')need(value.baselineFile===null&&value.expectedBaselineSHA256===null,'baseline_phase_no_prior_report');
 else{need(typeof value.baselineFile==='string'&&path.isAbsolute(value.baselineFile),'absolute_baseline_path');need(typeof value.expectedBaselineSHA256==='string'&&HASH.test(value.expectedBaselineSHA256),'baseline_hash');}
 return Object.freeze({...value,navigationWindow:window});
}
function bytes(file,budget){
 const fd=fs.openSync(file,'r');try{const st=fs.fstatSync(fd);need(st.isFile()&&st.size>0&&st.size<=budget,'input_budget');const b=Buffer.alloc(st.size+1);let n=0;while(n<b.length){const k=fs.readSync(fd,b,n,b.length-n,null);if(!k)break;n+=k;}need(n===st.size,'input_changed_during_read');return b.subarray(0,n);}finally{fs.closeSync(fd);}
}
function json(file,budget,expected){const raw=bytes(file,budget);need(!expected||sha(raw)===expected,'input_hash_mismatch');return {doc:JSON.parse(raw),sha256:sha(raw)};}
function child(parent,name){need(typeof name==='string'&&name.length>0&&!/[\\/:\0]/.test(name)&&!['.','..'].includes(name),'navigation_child');const root=fs.realpathSync(parent),file=fs.realpathSync(path.join(root,name));need(path.dirname(file)===root,'navigation_child_escape');return file;}
function saveNew(file,value){
 const out=path.resolve(file);need(path.extname(out)==='.json'&&!fs.existsSync(out),'new_json_output_required');fs.mkdirSync(path.dirname(out),{recursive:true});const tmp=out+'.'+randomUUID()+'.pending';
 try{fs.writeFileSync(tmp,JSON.stringify(value,null,2)+'\n',{flag:'wx'});fs.linkSync(tmp,out);}finally{if(fs.existsSync(tmp))fs.unlinkSync(tmp);}
}
function validatePlan(plan,w){
 need(plan?.status==='complete'&&plan.endFlag===true&&plan.lastCorridorRef===plan.endRef,'complete_original_plan');
 need(finitePoint(plan.start)&&finitePoint(plan.goal)&&Array.isArray(plan.path)&&plan.path.length>0&&plan.path.length<=512&&plan.path.every(finitePoint),'plan_points');
 need([plan.start,plan.goal,...plan.path].every(p=>inWindow(w,p)),'plan_outside_original_window');
 need(typeof plan.length==='number'&&Number.isFinite(plan.length)&&plan.length>=0,'plan_length');
 need(Number.isSafeInteger(plan.startRef)&&plan.startRef>0&&Number.isSafeInteger(plan.endRef)&&plan.endRef>0,'plan_refs');
}
export function validateLegacyEvidence(e,asset,evidenceSHA256){
 const m=asset.manifest;
 need(typeof evidenceSHA256==='string'&&HASH.test(evidenceSHA256)&&m.evidenceManifest?.sha256===evidenceSHA256,'evidence_hash_binding');
 need(e?.schema_version==='sky-partial-static-world-v1'&&e.format==='skyterrain-static-v1'&&e.coordinate_space==='sky_native','evidence_schema');
 need(e.mapName===m.mapName&&e.geometryKind===KIND&&e.geometry_kind==='partial_static_world_candidate','evidence_identity');
 for(const k of ['nativeCollisionVerified','native_collision_verified','runtime_ready'])need(e[k]===false,'evidence_false_flag:'+k);
 for(const k of [...Object.keys(FLAGS),'calibration_verified','foot_offset_calibrated','calibration_installed','navigation_rebuilt'])if(Object.hasOwn(e,k))need(e[k]===false,'evidence_optional_false_flag:'+k);
 for(const [key,countKey,snakeCount,fileKey,dtype] of [['positions','vertexCount','vertex_count','positions','<f4'],['indices','triangleCount','triangle_count','triangles','<u4']]){
  const count=asset[key].length/3,f=e.files?.[fileKey];
  need(e[countKey]===count&&e[snakeCount]===count&&m[countKey]===count,'evidence_counts:'+key);
  need(e[key]===m[key]&&e.sha256?.[key]===asset.assetDigests[key],'evidence_array_binding:'+key);
  need(f?.file===m[key]&&f.sha256===asset.assetDigests[key]&&f.bytes===asset[key].byteLength&&f.dtype===dtype&&canon(f.shape)===canon([count,3]),'evidence_descriptor:'+key);
 }
 return e;
}
export function validateLegacyBaseline(baseline,request,assetDigests,evidenceManifestSHA256){
 need(baseline?.schema==='offline-legacy-route-baseline-v1'&&baseline.mode==='offline'&&baseline.phase==='baseline'&&baseline.status==='planned','baseline_schema');falseFlags(baseline);for(const k of Object.keys(FLAGS))need(baseline[k]===false,'baseline_false_flag:'+k);
 need(baseline.geometryKind===KIND&&baseline.mapName===request.mapName&&baseline.profile===PROFILE,'baseline_map_kind_profile');
 need(baseline.movementExecuted===false&&baseline.movementAttempted===false&&baseline.arrived===0&&baseline.blocked===0&&baseline.unavailable===0,'baseline_is_plan_only');
 need(baseline.navReportSHA256===request.expectedNavigationReportSHA256&&baseline.originalNavigationHash===request.originalNavigationHash,'baseline_navigation_binding');
 need(canon(baseline.navigationWindow)===canon(request.navigationWindow)&&canon(baseline.config)===canon(DEFAULTS),'baseline_window_or_defaults');
 need(canon(baseline.assetDigests)===canon(assetDigests),'baseline_asset_binding');
 need(typeof evidenceManifestSHA256==='string'&&HASH.test(evidenceManifestSHA256)&&baseline.evidenceManifestSHA256===evidenceManifestSHA256,'baseline_evidence_binding');
 need(Array.isArray(baseline.routes)&&baseline.routes.length>=1&&baseline.routes.length<=3&&baseline.routeCount===baseline.routes.length&&baseline.planned===baseline.routes.length,'baseline_route_count');
 const seen=new Set();
 for(const r of baseline.routes){need(r.status==='not_replayed'&&r.candidateKind==='interior_of_imported_detail_triangle'&&Number.isSafeInteger(r.triangle)&&r.triangle>=0&&!seen.has(r.triangle),'baseline_route_identity');seen.add(r.triangle);validatePlan(r.plan,request.navigationWindow);}
 return baseline;
}
export async function replayLegacyNavigation({requestFile,outFile}={}){
 let imported,query,referenceWorld;
 const report={schema:'offline-legacy-navigation-error-v1',mode:'offline',profile:PROFILE,status:'unavailable',reason:null,...FLAGS,
  geometryKind:KIND,maximumRoutes:3,parameters:'unchanged_defaults',collisionWorldNavigation:false,selection:'fixed_van_der_corput_order_over_filtered_imported_detail_triangles',
  movementExecuted:false,movementAttempted:false,routes:[],routeCount:0,planned:0,arrived:0,blocked:0,unavailable:0,attempts:[],
  limitations:['Initial static candidates remain distinct from Beamo and native collision verification.','Only fixed short sample paths, not exhaustive scene connectivity.','No nav rebuild, direct fallback, movement-based route reselection, or live deployment.']};
 try{
  if(outFile)need(path.extname(outFile)==='.json'&&!fs.existsSync(outFile),'new_json_output_required');
  const loaded=json(requestFile,65536),request=validateLegacyRequest(loaded.doc);
  Object.assign(report,{schema:request.phase==='baseline'?'offline-legacy-route-baseline-v1':'offline-legacy-preserving-navigation-review-v1',phase:request.phase,mapName:request.mapName,requestSHA256:loaded.sha256});
  if(outFile)need(![requestFile,request.assetFile,request.navigationReportFile,request.baselineFile].filter(Boolean).map(x=>path.resolve(x)).includes(path.resolve(outFile)),'output_overwrites_input');
  const nav=json(request.navigationReportFile,1024*1024,request.expectedNavigationReportSHA256).doc;
  need(nav.format==='skyterrain-navigation-build-v1'&&nav.mode==='offline'&&nav.status==='built_offline','nav_schema');falseFlags(nav);
  need(nav.geometryKind===KIND&&nav.mapName===request.mapName,'legacy_nav_kind_or_map');
  need(canon(nav.config)===canon(DEFAULTS),'nav_default_parameters');
  need(canon(nav.navigationWindow??null)===canon(request.navigationWindow),'nav_window_binding');
  const coverage=request.navigationWindow?'bounded_window':'legacy_static_candidate';
  if(request.navigationWindow)need(nav.navigationCoverage==='bounded_window','window_scope_required');
  else need(!Object.hasOwn(nav,'navigationCoverage')&&!Object.hasOwn(nav,'navigationWindow'),'legacy_full_fields_must_be_absent');
  const asset=loadAsset(path.dirname(request.assetFile),path.basename(request.assetFile),{offline:true,expectedManifestSHA:request.expectedAssetManifestSHA256});
  need(asset.manifest.geometryKind===KIND&&asset.manifest.mapName===request.mapName,'legacy_asset_kind_or_map');
  need(asset.manifest.nativeCollisionVerified===false&&asset.manifest.calibrationVerified===false,'asset_unverified_flags');
  const evidenceRef=asset.manifest.evidenceManifest;need(typeof evidenceRef?.sha256==='string'&&HASH.test(evidenceRef.sha256),'evidence_reference');
  const evidence=json(child(path.dirname(request.assetFile),evidenceRef.file),32*1024*1024,evidenceRef.sha256);
  validateLegacyEvidence(evidence.doc,asset,evidence.sha256);report.evidenceManifestSHA256=evidence.sha256;
  for(const [k,v] of Object.entries(asset.assetDigests))need(nav.inputHashes?.[k]===v,'nav_asset_hash:'+k);
  need(nav.inputVertices===asset.positions.length/3&&nav.inputTriangles===asset.indices.length/3,'nav_asset_counts');
  need(nav.navigationHash===request.originalNavigationHash&&HASH.test(nav.navigationHash)&&Number.isInteger(nav.navigationBytes)&&nav.navigationBytes>0&&nav.navigationBytes<=128*1024*1024,'navigation_descriptor');
  const raw=bytes(child(path.dirname(request.navigationReportFile),nav.navigationFile),128*1024*1024);
  need(raw.length===nav.navigationBytes&&sha(raw)===request.originalNavigationHash,'navigation_bytes_hash');
  Object.assign(report,{assetDigests:asset.assetDigests,navReportSHA256:request.expectedNavigationReportSHA256,originalNavigationHash:request.originalNavigationHash,
   navigationBytes:raw.length,navigationCoverage:coverage,navigationWindow:request.navigationWindow,navigationMode:nav.navigationMode??null,
   inputVertices:nav.inputVertices,inputTriangles:nav.inputTriangles,config:DEFAULTS});
  const baseline=request.phase==='preserving'?validateLegacyBaseline(json(request.baselineFile,2*1024*1024,request.expectedBaselineSHA256).doc,request,asset.assetDigests,evidence.sha256):null;
  await init();({navMesh:imported}=importNavMesh(new Uint8Array(raw)));query=new NavMeshQuery(imported,{maxNodes:2048});
  const [positions,indices]=getNavMeshPositionsAndIndices(imported);
  need(positions.length/3===nav.navVertices&&indices.length/3===nav.navTriangles,'imported_nav_counts');
  need(positions.length>0&&positions.length<=24000000&&indices.length>0&&indices.length<=24000000&&positions.every(Number.isFinite)&&indices.every(i=>Number.isInteger(i)&&i>=0&&i<positions.length/3),'imported_nav_geometry');
  Object.assign(report,{importedNavVertices:positions.length/3,importedNavTriangles:indices.length/3,navVertices:positions.length/3,navTriangles:indices.length/3});
  if(request.phase==='baseline'){
   const selected=routeCandidates(positions,indices);report.eligibleTriangles=selected.eligibleTriangles;
   // This reference world performs only vertical proximity rays. NO motor call.
   referenceWorld=new TerrainWorld(asset.positions,asset.indices,{navigation:false,navigationWindow:request.navigationWindow});
   for(const c of selected.candidates){
    if(report.routes.length>=3)break;
    const plan=completePath(query,c.start,c.goal);
    if(plan.status!=='complete'){report.attempts.push({triangle:c.triangle,status:'unavailable',reason:plan.reason});continue;}
    try{validatePlan(plan,request.navigationWindow);}catch(e){report.attempts.push({triangle:c.triangle,status:'unavailable',reason:e.message});continue;}
    const a=referenceWorld.ground([plan.start[0],plan.start[1]+.3,plan.start[2]],.6),b=referenceWorld.ground([plan.goal[0],plan.goal[1]+.3,plan.goal[2]],.6);
    if(!a||!b||Math.abs(a.position[1]-plan.start[1])>.3||Math.abs(b.position[1]-plan.goal[1])>.3){report.attempts.push({triangle:c.triangle,status:'unavailable',reason:'nav_collision_surface_height_boundary'});continue;}
    report.routes.push({triangle:c.triangle,candidateKind:c.kind,plan,status:'not_replayed'});
   }
   report.routeCount=report.planned=report.routes.length;
   report.status=report.routeCount?'planned':'unavailable';report.reason=report.routeCount?null:'no_complete_short_route';
  }else{
   report.baselineSHA256=request.expectedBaselineSHA256;
   for(const original of baseline.routes){
    const originalPlan=structuredClone(original.plan),before=canon(originalPlan);
    const row={triangle:original.triangle,candidateKind:original.candidateKind,originalPlan,requestedOriginPolicy:PRESERVING_ORIGIN_POLICY,frameOrigin:originalPlan.start,frameOriginUsed:false};let world;
    try{
     const recheck=completePath(query,originalPlan.start,originalPlan.goal);
     need(recheck.status==='complete'&&recheck.startRef===originalPlan.startRef&&recheck.endRef===originalPlan.endRef,'original_corridor_recheck');
     world=new OfflineMotionWorld(asset.positions,asset.indices,{offline:true,geometryPolicy:PRESERVING_ORIGIN_POLICY,navigationWindow:request.navigationWindow,collisionWindow:routeCollisionWindow(originalPlan.start,originalPlan.path)});
     Object.assign(row,{frameOrigin:world.frameOrigin,frameOriginUsed:true,originPolicy:PRESERVING_ORIGIN_POLICY});report.movementAttempted=true;
     const replay=replayCompletePath(world,originalPlan);report.movementExecuted ||= Number.isInteger(replay.frames)&&replay.frames>0;
     Object.assign(row,replay,{startSupportAudit:world.startSupportAudit,geometryMetadata:world.metadata,corridorRechecked:true});
     need(before===canon(originalPlan)&&before===canon(original.plan),'original_plan_mutated');
    }catch(e){Object.assign(row,{status:'blocked',reason:String(e?.message??e).slice(0,256),startSupportAudit:world?.startSupportAudit??{status:'not_started',applied:false,collisionConstructorAccepted:false,reason:String(e?.message??e).slice(0,256),nativeCollisionVerified:false},geometryMetadata:world?.metadata??null});}
    finally{world?.dispose();}report.routes.push(row);
   }
   report.routeCount=report.routes.length;report.arrived=report.routes.filter(r=>r.status==='arrived').length;report.blocked=report.routes.filter(r=>r.status==='blocked').length;report.unavailable=report.routes.filter(r=>r.status==='unavailable').length;
   need(report.routeCount===baseline.routeCount&&report.arrived+report.blocked+report.unavailable===report.routeCount,'route_count_mismatch');
   report.status=report.blocked?'blocked':report.unavailable?'unavailable':'passed';report.reason=report.status==='passed'?null:'one_or_more_original_routes_not_arrived';
  }
 }catch(e){report.status='unavailable';report.reason=String(e?.message??e).slice(0,512);}
 finally{query?.destroy();imported?.destroy();referenceWorld?.dispose();}
 if(outFile)saveNew(outFile,report);return report;
}
if(process.argv[1]&&path.resolve(process.argv[1])===fileURLToPath(import.meta.url)){
 const args=process.argv.slice(2),get=k=>{const i=args.indexOf(k);need(i>=0&&args[i+1],'missing_cli_'+k);return path.resolve(args[i+1]);};
 const r=await replayLegacyNavigation({requestFile:get('--request'),outFile:get('--out')});console.log(JSON.stringify({mapName:r.mapName,phase:r.phase,status:r.status,routeCount:r.routeCount,planned:r.planned,arrived:r.arrived,blocked:r.blocked,reason:r.reason}));if(!['planned','passed'].includes(r.status))process.exitCode=2;
}
