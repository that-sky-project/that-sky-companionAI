// Imported, hash-bound offline navigation -> unchanged default capsule replay.
// No navigation regeneration, straight-line fallback, live IO, or calibration.
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {createHash,randomUUID} from 'node:crypto';
import {importNavMesh,NavMeshQuery,getNavMeshPositionsAndIndices,Detour,statusDetail} from 'recast-navigation';
import {init,TerrainWorld,DEFAULTS} from './core.mjs';
import {loadAsset} from './assets.mjs';

const HASH=/^[0-9a-f]{64}$/;
const KIND='offline_beamo_augmented_candidates';
const sha=raw=>createHash('sha256').update(raw).digest('hex');
const need=(condition,reason)=>{if(!condition)throw Error(reason);};
const xyz=p=>({x:p[0],y:p[1],z:p[2]});
const tuple=p=>[p.x,p.y,p.z];
const distance=(a,b)=>Math.hypot(a[0]-b[0],a[1]-b[1],a[2]-b[2]);
function bytes(file,budget){
  const fd=fs.openSync(file,'r');try{
    const st=fs.fstatSync(fd);need(st.isFile()&&st.size<=budget,'input_budget');
    const raw=Buffer.alloc(st.size+1);let count=0;
    while(count<raw.length){const n=fs.readSync(fd,raw,count,raw.length-count,null);if(!n)break;count+=n;}
    need(count===st.size,'input_changed_during_read');return raw.subarray(0,count);
  }finally{fs.closeSync(fd);}
}
function json(file,budget=1024*1024){const raw=bytes(file,budget);return {doc:JSON.parse(raw),hash:sha(raw)};}
function child(directory,name){
  need(typeof name==='string'&&name!==''&&!['.','..'].includes(name)&&!/[\\/:\0]/.test(name),'nav_child_path');
  const root=fs.realpathSync(directory),target=fs.realpathSync(path.join(root,name));
  need(path.dirname(target)===root,'nav_child_escape');return target;
}
function falseFlags(doc){
  for(const key of ['nativeCollisionVerified','calibrationVerified','runtimeReady'])need(doc[key]===false,'offline_flags_required:'+key);
}
const badStatus=status=>[Detour.DT_PARTIAL_RESULT,Detour.DT_OUT_OF_NODES,Detour.DT_BUFFER_TOO_SMALL].some(flag=>statusDetail(status,flag));

export function completePath(query,start,goal,{minLength=.6,segmentPolicy='review'}={}){
  if(!['review','collision_motor'].includes(segmentPolicy))return {status:'unavailable',reason:'invalid_path_segment_policy'};
  if(typeof minLength!=='number'||!Number.isFinite(minLength)||minLength<=0||minLength>8)return {status:'unavailable',reason:'invalid_minimum_path_length'};
  const options={halfExtents:{x:.2,y:.3,z:.2}};
  const a=query.findNearestPoly(xyz(start),options),b=query.findNearestPoly(xyz(goal),options);
  if(!a.success||!b.success||!a.nearestRef||!b.nearestRef)return {status:'unavailable',reason:'nearest_polygon_missing'};
  const projectedStart=tuple(a.nearestPoint),projectedGoal=tuple(b.nearestPoint);
  if(distance(start,projectedStart)>.3||distance(goal,projectedGoal)>.3)return {status:'unavailable',reason:'nav_endpoint_projection_boundary'};
  let corridor,straight;
  try{
    corridor=query.findPath(a.nearestRef,b.nearestRef,a.nearestPoint,b.nearestPoint,{maxPathPolys:512});
    if(!corridor.success||badStatus(corridor.status)||corridor.polys.size<1
      ||corridor.polys.get(corridor.polys.size-1)!==b.nearestRef){
      return {status:'unavailable',reason:'incomplete_polygon_corridor',detourStatus:corridor.status};
    }
    straight=query.findStraightPath(a.nearestPoint,b.nearestPoint,corridor.polys,
      {maxStraightPathPoints:128,straightPathOptions:Detour.DT_STRAIGHTPATH_ALL_CROSSINGS});
    if(!straight.success||badStatus(straight.status)||straight.straightPathCount<2||straight.straightPathCount>128)
      return {status:'unavailable',reason:'incomplete_straight_path',detourStatus:straight.status};
    const n=straight.straightPathCount,points=[],flags=[];
    for(let i=0;i<n;i++){
      points.push([straight.straightPath.get(i*3),straight.straightPath.get(i*3+1),straight.straightPath.get(i*3+2)]);
      flags.push(straight.straightPathFlags.get(i));
    }
    if(points.some(p=>!p.every(Number.isFinite))||!(flags.at(-1)&Detour.DT_STRAIGHTPATH_END)
      ||flags.some(f=>f&Detour.DT_STRAIGHTPATH_OFFMESH_CONNECTION)||distance(points.at(-1),projectedGoal)>.01)
      return {status:'unavailable',reason:'path_endpoint_or_offmesh_boundary'};
    let length=0;
    for(let i=1;i<n;i++){
      const horizontal=Math.hypot(points[i][0]-points[i-1][0],points[i][2]-points[i-1][2]);
      const dy=Math.abs(points[i][1]-points[i-1][1]);length+=distance(points[i],points[i-1]);
      // Recast fold-point heights can jump across overlapping detail surfaces.
      // Runtime still requires the complete nav corridor and unchanged capsule
      // slope/step/collision checks; reviewer retains the old geometric screen.
      if(dy>.6||(segmentPolicy==='review'&&horizontal>.02&&Math.atan2(dy,horizontal)*180/Math.PI>35))
        return {status:'unavailable',reason:'path_height_or_slope_boundary'};
    }
    if(length<minLength||length>8)return {status:'unavailable',reason:'path_length_boundary'};
    return {status:'complete',start:projectedStart,goal:projectedGoal,path:points,length,
      startRef:a.nearestRef,endRef:b.nearestRef,lastCorridorRef:corridor.polys.get(corridor.polys.size-1),
      corridorPolygons:corridor.polys.size,endFlag:true,detourStatus:corridor.status,straightStatus:straight.status};
  }finally{
    corridor?.polys.destroy();straight?.straightPath.destroy();
    straight?.straightPathFlags.destroy();straight?.straightPathRefs.destroy();
  }
}

export function triangleRoute(positions,indices,triangle){
  const p=[0,1,2].map(i=>{const n=indices[triangle*3+i]*3;return [positions[n],positions[n+1],positions[n+2]];});
  if(p.some(a=>!a.every(Number.isFinite)))return null;
  const u=p[1].map((v,i)=>v-p[0][i]),v=p[2].map((x,i)=>x-p[0][i]);
  const normal=[u[1]*v[2]-u[2]*v[1],u[2]*v[0]-u[0]*v[2],u[0]*v[1]-u[1]*v[0]],area=Math.hypot(...normal);
  if(area<1e-8||Math.acos(Math.min(1,Math.abs(normal[1])/area))*180/Math.PI>30
    ||Math.max(...p.map(a=>a[1]))-Math.min(...p.map(a=>a[1]))>.6)return null;
  const pairs=[[0,1],[1,2],[2,0]].map(([a,b])=>({a,b,len:Math.hypot(p[a][0]-p[b][0],p[a][2]-p[b][2])}));
  pairs.sort((a,b)=>b.len-a.len);const edge=pairs[0];if(edge.len<1.875)return null;
  const center=[0,1,2].map(i=>(p[0][i]+p[1][i]+p[2][i])/3);
  // Stay within the imported detail triangle, away from skinny XZ boundaries.
  for(const {a,b,len} of pairs){
    if(len<1e-8)return null;
    const margin=Math.abs((p[b][0]-p[a][0])*(center[2]-p[a][2])-(p[b][2]-p[a][2])*(center[0]-p[a][0]))/len;
    if(margin<.08)return null;
  }
  const factor=Math.min(.4,2/edge.len),delta=p[edge.a].map((x,i)=>(x-p[edge.b][i])*factor/2);
  return {triangle,start:center.map((x,i)=>x-delta[i]),goal:center.map((x,i)=>x+delta[i]),
    kind:'interior_of_imported_detail_triangle',horizontalLength:factor*edge.len};
}

export function routeCandidates(positions,indices){
  const eligible=[];
  for(let i=0;i<indices.length/3;i++)if(triangleRoute(positions,indices,i))eligible.push(i);
  const selected=[],seen=new Set();
  // Fixed van-der-Corput order, no process/global random state.
  for(let i=1;i<=96&&selected.length<48&&selected.length<eligible.length;i++){
    let n=i,f=.5,value=0;while(n){value+=(n&1)*f;n>>=1;f*=.5;}
    const triangle=eligible[Math.min(eligible.length-1,Math.floor(value*eligible.length))];
    if(triangle===undefined||seen.has(triangle))continue;
    seen.add(triangle);selected.push(triangleRoute(positions,indices,triangle));
  }
  return {eligibleTriangles:eligible.length,candidates:selected};
}

export function replayCompletePath(world,plan){
  if(plan.status!=='complete')return {status:'unavailable',reason:'complete_path_required'};
  let motor;
  try{
    // Motor receives the complete imported-nav path; no world.plan or direct substitute.
    motor=world.motor(plan.start,plan.path,0);let last=motor.sample(0),frames=0,groundedFrames=0,contacts=0,lateralContacts=0;
    const trace=[],deadline=Math.min(12,plan.length/DEFAULTS.speed*3+3);
    for(let frame=1;frame<=Math.ceil(deadline*60);frame++){
      last=motor.sample(frame/60);frames=frame;if(last.grounded)groundedFrames++;
      const count=motor.controller.numComputedCollisions();contacts+=count;
      for(let i=0;i<count;i++){const c=motor.controller.computedCollision(i);if(c&&Math.abs(c.normal1.y)<.7)lateralContacts++;}
      if(frame%6===0||last.arrived||last.blocked)trace.push({time:frame/60,...last,contacts:count});
      if(last.arrived||last.blocked)break;
    }
    const finalXZ=Math.hypot(last.position[0]-plan.goal[0],last.position[2]-plan.goal[2]);
    const finalY=Math.abs(last.position[1]-plan.goal[1]);
    const physicallyArrived=last.arrived===true&&!last.blocked&&last.grounded===true&&finalXZ<=DEFAULTS.goalTolerance+.02&&finalY<=.35;
    return {status:physicallyArrived?'arrived':'blocked',reason:physicallyArrived?null:last.reason??(last.arrived?(last.grounded?'arrival_endpoint_mismatch':'arrival_not_grounded'):'replay_timeout'),
      frames,elapsedSeconds:frames/60,groundedFrames,sampledContactObservations:contacts,lateralContactObservations:lateralContacts,
      finalDistanceXZ:finalXZ,finalDistanceY:finalY,last,trace};
  }catch(error){return {status:'blocked',reason:error.message};}
  finally{motor?.dispose();}
}

export async function reviewNavigation({assetFile,navReportFile,outFile}={}){
  if(outFile){
    const output=path.resolve(outFile);
    need(path.extname(output)==='.json'&&!fs.existsSync(output),'new_json_report_required');
    need(![assetFile,navReportFile].map(p=>path.resolve(p)).includes(output),'report_overwrites_input');
  }
  let world,navMesh,query;
  const report={schema:'offline-beamo-imported-navigation-review-v1',mode:'offline',profile:'netease-android-0.16.7-421589',
    status:'unavailable',reason:null,nativeCollisionVerified:false,calibrationVerified:false,runtimeReady:false,
    footOffsetCalibrated:false,calibrationInstalled:false,parameters:'unchanged_defaults',navigationRebuilt:false,
    maximumRoutes:3,selection:'fixed_van_der_corput_order_over_filtered_imported_detail_triangles',
    routes:[],routeCount:0,arrived:0,blocked:0,unavailable:0,attempts:[],limitations:['At most three short interior-triangle routes do not establish complete map connectivity.',
      'Arrival is offline default-capsule replay, not native-client parity or live verification.',
      'No navigation or incomplete path means unavailable, never a straight-line substitute.',
      'A merged static-registration observation may be inherited from old base; it does not verify Beamo.']};
  try{
    const loaded=json(navReportFile),nav=loaded.doc;report.navReportSHA256=loaded.hash;
    need(nav.format==='skyterrain-navigation-build-v1'&&nav.mode==='offline','nav_report_format');falseFlags(nav);
    need(nav.status==='built_offline','navigation_unavailable:'+(nav.reason??nav.status));
    need(nav.geometryKind===KIND,'old_geometry_navigation_kind');
    report.navigationCoverage=nav.navigationCoverage??null;
    report.navigationWindow=nav.navigationWindow??null;report.navigationMode=nav.navigationMode??null;
    need(HASH.test(nav.inputHashes?.manifest??''),'nav_manifest_pin_missing');
    const asset=loadAsset(path.dirname(assetFile),path.basename(assetFile),{offline:true,expectedManifestSHA:nav.inputHashes.manifest});
    need(asset.manifest.geometryKind===KIND,'new_merged_geometry_required');
    need(asset.manifest.nativeCollisionVerified===false&&asset.manifest.calibrationVerified===false,'asset_offline_flags');
    need(nav.mapName===asset.manifest.mapName,'nav_asset_map_mismatch');report.mapName=nav.mapName;
    for(const key of ['positions','indices'])need(nav.inputHashes[key]===asset.assetDigests[key],'nav_geometry_hash_mismatch:'+key);
    need(nav.inputVertices===asset.positions.length/3&&nav.inputTriangles===asset.indices.length/3,'nav_geometry_count_mismatch');
    need(nav.config&&Object.keys(DEFAULTS).every(k=>nav.config[k]===DEFAULTS[k])
      &&Object.keys(nav.config).every(k=>k in DEFAULTS),'nav_default_config_mismatch');
    need(HASH.test(nav.navigationHash??'')&&Number.isInteger(nav.navigationBytes)&&nav.navigationBytes>0&&nav.navigationBytes<=128*1024*1024,'navigation_descriptor');
    const navFile=child(path.dirname(navReportFile),nav.navigationFile),raw=bytes(navFile,128*1024*1024);
    need(raw.length===nav.navigationBytes&&sha(raw)===nav.navigationHash,'navigation_binary_hash_or_size');
    report.assetDigests=asset.assetDigests;report.navigationHash=nav.navigationHash;
    report.navigationBytes=raw.length;report.inputVertices=nav.inputVertices;report.inputTriangles=nav.inputTriangles;
    await init();({navMesh}=importNavMesh(new Uint8Array(raw)));query=new NavMeshQuery(navMesh,{maxNodes:2048});
    const [positions,indices]=getNavMeshPositionsAndIndices(navMesh);
    need(positions.length/3===nav.navVertices&&indices.length/3===nav.navTriangles,'imported_nav_geometry_count_mismatch');
    need(positions.length>0&&positions.length<=24000000&&indices.length>0&&indices.length<=24000000,'imported_nav_geometry_budget');
    need(positions.every(Number.isFinite)&&indices.every(i=>Number.isInteger(i)&&i>=0&&i<positions.length/3),'imported_nav_geometry_invalid');
    report.importedNavVertices=positions.length/3;report.importedNavTriangles=indices.length/3;
    const selected=routeCandidates(positions,indices);report.eligibleTriangles=selected.eligibleTriangles;
    need(selected.candidates.length>0,'no_short_triangle_routes');
    world=new TerrainWorld(asset.positions,asset.indices,{navigation:false});need(!world.nav,'unexpected_navigation_regeneration');
    report.config=world.config;report.collisionWorldNavigation=false;
    for(const candidate of selected.candidates){
      if(report.routes.length>=3)break;
      const plan=completePath(query,candidate.start,candidate.goal);
      if(plan.status!=='complete'){report.attempts.push({triangle:candidate.triangle,status:'unavailable',reason:plan.reason});continue;}
      const startGround=world.ground([plan.start[0],plan.start[1]+.3,plan.start[2]],.6);
      const goalGround=world.ground([plan.goal[0],plan.goal[1]+.3,plan.goal[2]],.6);
      if(!startGround||!goalGround||Math.abs(startGround.position[1]-plan.start[1])>.3||Math.abs(goalGround.position[1]-plan.goal[1])>.3){
        report.attempts.push({triangle:candidate.triangle,status:'unavailable',reason:'nav_collision_surface_height_boundary'});continue;
      }
      // Preserve nav path Y. Ground rays only validate layer proximity, never install offsets.
      const replay=replayCompletePath(world,plan);
      report.routes.push({triangle:candidate.triangle,candidateKind:candidate.kind,plan,...replay});
    }
    report.arrived=report.routes.filter(r=>r.status==='arrived').length;
    report.blocked=report.routes.filter(r=>r.status==='blocked').length;
    report.routeCount=report.routes.length;
    if(!report.routes.length){report.reason='no_complete_short_route';}
    else{report.status=report.blocked?'blocked':'passed';report.reason=report.blocked?'one_or_more_replays_blocked':null;}
  }catch(error){report.reason=String(error?.message??error).slice(0,512);report.status='unavailable';}
  finally{query?.destroy();navMesh?.destroy();world?.dispose();}
  if(outFile){
    const output=path.resolve(outFile);
    need(path.extname(output)==='.json'&&!fs.existsSync(output),'new_json_report_required');
    need(![assetFile,navReportFile].map(p=>path.resolve(p)).includes(output),'report_overwrites_input');
    fs.mkdirSync(path.dirname(output),{recursive:true});const temp=output+'.'+randomUUID()+'.tmp';
    try{fs.writeFileSync(temp,JSON.stringify(report,null,2),{flag:'wx'});fs.renameSync(temp,output);}
    finally{if(fs.existsSync(temp))fs.unlinkSync(temp);}
  }
  return report;
}

if(process.argv[1]&&path.resolve(process.argv[1])===fileURLToPath(import.meta.url)){
  const args=process.argv.slice(2),get=key=>{const i=args.indexOf(key);if(i<0||!args[i+1])throw Error('missing '+key);return path.resolve(args[i+1]);};
  const report=await reviewNavigation({assetFile:get('--asset'),navReportFile:get('--nav-report'),outFile:get('--out')});
  console.log(JSON.stringify({mapName:report.mapName,status:report.status,reason:report.reason,
    routes:report.routes.length,arrived:report.arrived??0,blocked:report.blocked??0,attempts:report.attempts.length,
    runtimeReady:false,nativeCollisionVerified:false}));
  if(report.status!=='passed')process.exitCode=2;
}
