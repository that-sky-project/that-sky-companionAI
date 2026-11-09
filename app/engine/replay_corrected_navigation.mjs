// Replays exactly the previously recorded complete routes with offline motion corrections.
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {createHash,randomUUID} from 'node:crypto';
import {importNavMesh,NavMeshQuery,getNavMeshPositionsAndIndices} from 'recast-navigation';
import {init,DEFAULTS,navigationWindow} from './core.mjs';
import {loadAsset} from './assets.mjs';
import {completePath,replayCompletePath} from './review_beamo_navigation.mjs';
import {OfflineMotionWorld,routeCollisionWindow,PRESERVING_ORIGIN_POLICY} from './offline_motion_world.mjs';

const need=(value,reason)=>{if(!value)throw Error(reason);};
const HASH=/^[0-9a-f]{64}$/;
const FLAGS=Object.freeze({nativeCollisionVerified:false,calibrationVerified:false,runtimeReady:false,
  footOffsetCalibrated:false,calibrationInstalled:false,navigationRebuilt:false,live_enabled:false,server_modified:false});
const keys=['schema','mode','mapName','assetFile','expectedAssetManifestSHA256','baselineFile','expectedBaselineSHA256',
  'navigationReportFile','expectedNavigationReportSHA256','originalNavigationHash','navigationWindow',
  'nativeCollisionVerified','calibrationVerified','runtimeReady'];
const sha=raw=>createHash('sha256').update(raw).digest('hex');
function canon(value){
  if(Array.isArray(value))return '['+value.map(canon).join(',')+']';
  if(value&&typeof value==='object')return '{'+Object.keys(value).sort().map(k=>JSON.stringify(k)+':'+canon(value[k])).join(',')+'}';
  return JSON.stringify(value);
}
function bytes(file,budget){
  const fd=fs.openSync(file,'r');try{
    const stat=fs.fstatSync(fd);need(stat.isFile()&&stat.size<=budget,'input_budget');
    const raw=Buffer.alloc(stat.size+1);let count=0;
    while(count<raw.length){const n=fs.readSync(fd,raw,count,raw.length-count,null);if(!n)break;count+=n;}
    need(count===stat.size,'input_changed_during_read');return raw.subarray(0,count);
  }finally{fs.closeSync(fd);}
}
function json(file,budget,expected){const raw=bytes(file,budget);if(expected)need(sha(raw)===expected,'input_hash_mismatch');return {doc:JSON.parse(raw),hash:sha(raw)};}
function falseFlags(value){for(const key of ['nativeCollisionVerified','calibrationVerified','runtimeReady'])need(value?.[key]===false,'offline_flags_required:'+key);}
export function validateReplayRequest(value){
  need(value&&typeof value==='object'&&!Array.isArray(value),'request_object');
  need(Object.keys(value).length===keys.length&&keys.every(k=>Object.hasOwn(value,k)),'request_fields');
  need(value.schema==='offline-corrected-replay-request-v1'&&value.mode==='offline','offline_request_required');
  falseFlags(value);
  need(typeof value.mapName==='string'&&value.mapName.length>0&&value.mapName.length<=128,'map_name');
  for(const key of ['assetFile','baselineFile','navigationReportFile'])need(typeof value[key]==='string'&&path.isAbsolute(value[key]),'absolute_input_path_required');
  for(const key of ['expectedAssetManifestSHA256','expectedBaselineSHA256','expectedNavigationReportSHA256','originalNavigationHash'])need(typeof value[key]==='string'&&HASH.test(value[key]),'input_hash_required');
  navigationWindow(value.navigationWindow);
  return value;
}
function navChild(parent,name){
  need(typeof name==='string'&&name.length>0&&!/[\\/:\0]/.test(name)&&!['.','..'].includes(name),'navigation_child_name');
  const directory=fs.realpathSync(parent),file=fs.realpathSync(path.join(directory,name));
  need(path.dirname(file)===directory,'navigation_child_escape');return file;
}
function saveNew(file,report){
  const output=path.resolve(file);need(path.extname(output)==='.json'&&!fs.existsSync(output),'new_json_report_required');
  fs.mkdirSync(path.dirname(output),{recursive:true});const temporary=output+'.'+randomUUID()+'.pending';
  try{fs.writeFileSync(temporary,JSON.stringify(report,null,2)+'\n',{flag:'wx'});fs.linkSync(temporary,output);}
  finally{if(fs.existsSync(temporary))fs.unlinkSync(temporary);}
}
export async function reviewCorrectedNavigation({requestFile,outFile}={}){
  let imported,query;
  const report={schema:'offline-corrected-navigation-review-v1',mode:'offline',status:'unavailable',reason:null,...FLAGS,
    routes:[],routeCount:0,arrived:0,blocked:0,unavailable:0,
    limitations:['Only the fixed original short routes are replayed; not full map connectivity.',
      'Local origin translation and bounded support projection are offline corrections, not native physics calibration.',
      'The original path, goal, collision parameters and navigation are not replaced or relaxed.']};
  try{
    if(outFile)need(!fs.existsSync(path.resolve(outFile))&&path.extname(outFile)==='.json','new_json_report_required');
    const loaded=json(requestFile,65536),request=validateReplayRequest(loaded.doc);report.requestSHA256=loaded.hash;
    if(outFile)need(![requestFile,request.assetFile,request.baselineFile,request.navigationReportFile].map(p=>path.resolve(p)).includes(path.resolve(outFile)),'report_overwrites_input');
    const baseline=json(request.baselineFile,2*1024*1024,request.expectedBaselineSHA256).doc;
    const nav=json(request.navigationReportFile,1024*1024,request.expectedNavigationReportSHA256).doc;
    need(baseline.schema==='offline-beamo-imported-navigation-review-v1'&&baseline.mode==='offline','baseline_schema');
    need(nav.format==='skyterrain-navigation-build-v1'&&nav.mode==='offline'&&nav.status==='built_offline','nav_schema');
    falseFlags(baseline);falseFlags(nav);
    need(baseline.mapName===request.mapName&&nav.mapName===request.mapName,'map_mismatch');
    need(baseline.navReportSHA256===request.expectedNavigationReportSHA256&&baseline.navigationHash===request.originalNavigationHash&&nav.navigationHash===request.originalNavigationHash,'navigation_provenance_mismatch');
    need(canon(baseline.navigationWindow??null)===canon(request.navigationWindow)&&canon(nav.navigationWindow??null)===canon(request.navigationWindow),'navigation_window_mismatch');
    need(canon(baseline.config)===canon(DEFAULTS)&&canon(nav.config)===canon(DEFAULTS),'default_parameters_required');
    need(Array.isArray(baseline.routes)&&baseline.routes.length>0&&baseline.routes.length<=3&&baseline.routeCount===baseline.routes.length,'baseline_route_count');
    const asset=loadAsset(path.dirname(request.assetFile),path.basename(request.assetFile),{offline:true,expectedManifestSHA:request.expectedAssetManifestSHA256});
    need(asset.manifest.mapName===request.mapName&&asset.manifest.geometryKind==='offline_beamo_augmented_candidates','asset_map_or_kind');
    need(asset.manifest.nativeCollisionVerified===false&&asset.manifest.calibrationVerified===false,'asset_unverified_flags');
    for(const [key,value] of Object.entries(asset.assetDigests))need(baseline.assetDigests?.[key]===value&&nav.inputHashes?.[key]===value,'geometry_provenance_mismatch:'+key);
    need(nav.inputVertices===asset.positions.length/3&&nav.inputTriangles===asset.indices.length/3,'input_geometry_count');
    const navBytes=bytes(navChild(path.dirname(request.navigationReportFile),nav.navigationFile),128*1024*1024);
    need(sha(navBytes)===request.originalNavigationHash&&navBytes.length===nav.navigationBytes,'navigation_binary_mismatch');
    Object.assign(report,{profile:baseline.profile,mapName:request.mapName,baselineSHA256:request.expectedBaselineSHA256,
      assetDigests:asset.assetDigests,navReportSHA256:request.expectedNavigationReportSHA256,
      originalNavigationHash:request.originalNavigationHash,navigationWindow:request.navigationWindow});
    await init();({navMesh:imported}=importNavMesh(new Uint8Array(navBytes)));query=new NavMeshQuery(imported,{maxNodes:2048});
    const [positions,indices]=getNavMeshPositionsAndIndices(imported);
    need(positions.length/3===nav.navVertices&&indices.length/3===nav.navTriangles,'imported_nav_count');
    for(const previous of baseline.routes){
      const originalPlan=structuredClone(previous.plan),before=canon(originalPlan);
      let world,replay;
      const row={triangle:previous.triangle,candidateKind:previous.candidateKind,originalPlan,
        baselineStatus:previous.status,baselineReason:previous.reason,frameOrigin:originalPlan?.start??null,frameOriginUsed:false,requestedOriginPolicy:PRESERVING_ORIGIN_POLICY};
      try{
        need(originalPlan?.status==='complete'&&originalPlan.endFlag===true&&originalPlan.lastCorridorRef===originalPlan.endRef,'original_complete_plan_required');
        const rechecked=completePath(query,originalPlan.start,originalPlan.goal);
        need(rechecked.status==='complete'&&rechecked.startRef===originalPlan.startRef&&rechecked.endRef===originalPlan.endRef,'original_corridor_recheck_failed');
        // New proof is diagnostic only; motor still consumes the exact original path.
        world=new OfflineMotionWorld(asset.positions,asset.indices,{offline:true,geometryPolicy:PRESERVING_ORIGIN_POLICY,navigationWindow:request.navigationWindow,
          collisionWindow:routeCollisionWindow(originalPlan.start,originalPlan.path)});
        Object.assign(row,{frameOrigin:world.frameOrigin,frameOriginUsed:true,originPolicy:PRESERVING_ORIGIN_POLICY});
        replay=replayCompletePath(world,originalPlan);
        Object.assign(row,replay,{startSupportAudit:world.startSupportAudit,geometryMetadata:world.metadata,corridorRechecked:true});
        need(canon(originalPlan)===before&&canon(previous.plan)===before,'original_plan_mutated');
      }catch(error){Object.assign(row,{status:'blocked',reason:String(error?.message??error).slice(0,256),startSupportAudit:world?.startSupportAudit??{status:'not_started',applied:false,collisionConstructorAccepted:false,reason:String(error?.message??error).slice(0,256),nativeCollisionVerified:false},geometryMetadata:world?.metadata??null});}
      finally{world?.dispose();}
      report.routes.push(row);
    }
    report.routeCount=report.routes.length;
    for(const key of ['arrived','blocked','unavailable'])report[key]=report.routes.filter(r=>r.status===key).length;
    need(report.routeCount===baseline.routeCount&&report.arrived+report.blocked+report.unavailable===report.routeCount,'corrected_route_count');
    report.status=report.blocked?'blocked':report.unavailable?'unavailable':'passed';
    report.reason=report.status==='passed'?null:'one_or_more_corrected_routes_not_arrived';
  }catch(error){report.status='unavailable';report.reason=String(error?.message??error).slice(0,512);}
  finally{query?.destroy();imported?.destroy();}
  if(outFile)saveNew(outFile,report);
  return report;
}
if(process.argv[1]&&path.resolve(process.argv[1])===fileURLToPath(import.meta.url)){
  const args=process.argv.slice(2),get=key=>{const i=args.indexOf(key);need(i>=0&&args[i+1],'missing '+key);return path.resolve(args[i+1]);};
  const report=await reviewCorrectedNavigation({requestFile:get('--request'),outFile:get('--out')});
  console.log(JSON.stringify({mapName:report.mapName,status:report.status,reason:report.reason,routeCount:report.routeCount,arrived:report.arrived,blocked:report.blocked,...FLAGS}));
  if(report.status!=='passed')process.exitCode=2;
}
