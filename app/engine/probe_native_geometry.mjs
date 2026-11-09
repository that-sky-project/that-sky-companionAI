// Native numeric positions -> existing offline ray engine. No motors or offsets.
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {createHash} from 'node:crypto';
import RAPIER from '@dimforge/rapier3d-compat';
import {init,TerrainWorld} from './core.mjs';
import {loadAsset} from './assets.mjs';
import {preparePreservingGeometry} from './preserving_geometry.mjs';
import {assertPinnedCollisionRuntime} from './pinned_collision_runtime.mjs';
import {observationRays,observationCollisionWindow} from './probe_observation_geometry.mjs';
const need=(ok,m)=>{if(!ok)throw Error(m);};
const sha=b=>createHash('sha256').update(b).digest('hex');
export function validateNativePoints(d){
 need(d.schema==='native-geometry-numeric-input-v1'&&d.profile==='netease-android-0.16.7-421589','schema');
 need(d.mapAssumption==='user_context_CandleSpace'&&d.nativeMapIDVerified===false&&d.calibrationVerified===false&&d.runtimeReady===false,'unverified_map_and_calibration');
 need(Array.isArray(d.cases)&&d.cases.length>0&&d.cases.length<=8,'cases');
 let total=0;const ids=new Set();
 for(const c of d.cases){need(/^[a-z0-9_-]+$/.test(c.id)&&!ids.has(c.id),'case_id');ids.add(c.id);need(/^[0-9a-f]{64}$/.test(c.sourceSHA256),'source_hash');need(Array.isArray(c.points)&&c.points.length>0&&c.points.length<=600,'points');total+=c.points.length;let last=-Infinity;
  for(const [i,p]of c.points.entries()){need(p.index===i&&Number.isFinite(p.startMs)&&Number.isFinite(p.endMs)&&p.startMs>=last&&p.endMs>p.startMs,'time');last=p.endMs;for(const key of ['actorPosition','queryVector150'])need(Array.isArray(p[key])&&p[key].length===3&&p[key].every(x=>Number.isFinite(x)&&Math.abs(x)<999000),'vector');need(Number.isFinite(p.rawC0)&&[0,1].includes(p.diagnosticInAir)&&p.atomicSnapshot===false,'raw_diagnostics');}
 }need(total<=1200,'sample_budget');return d;
}
export async function runNativeGeometry(inputFile,outFile){
 need(!fs.existsSync(outFile),'new_output_required');const raw=fs.readFileSync(inputFile);need(raw.length<=2*1024*1024,'input_budget');const d=validateNativePoints(JSON.parse(raw));
 need(path.isAbsolute(d.assetFile)&&/^[0-9a-f]{64}$/.test(d.expectedAssetManifestSHA256),'asset_reference');
 const asset=loadAsset(path.dirname(d.assetFile),path.basename(d.assetFile),{offline:true,expectedManifestSHA:d.expectedAssetManifestSHA256});
 need(asset.manifest.mapName==='CandleSpace'&&asset.manifest.geometryKind==='offline_beamo_augmented_candidates'&&asset.manifest.nativeCollisionVerified===false,'asset_identity');
 await init();const queryRuntime=assertPinnedCollisionRuntime();const cases=[];let queries=0,hits=0;
 for(const c of d.cases){const collisionWindow=observationCollisionWindow(c.points.map(p=>p.actorPosition));const mesh=preparePreservingGeometry({positions:asset.positions,indices:asset.indices},collisionWindow);let world;
  try{world=new TerrainWorld(mesh.positions,mesh.indices,{navigation:false});const points=[];
   for(const p of c.points){mesh.coverage.assertWorldCapsule(p.actorPosition,{radius:0,skin:0});const rays=observationRays(p.actorPosition);
    for(const ray of Object.values(rays)){const origin=mesh.frame.toLocalPosition(ray.origin),dir=ray.direction;const hit=world.world.castRayAndGetNormal(new RAPIER.Ray({x:origin[0],y:origin[1],z:origin[2]},{x:dir[0],y:dir[1],z:dir[2]}),ray.maxDistance,true,undefined,undefined,undefined,undefined,col=>col===world.terrain);queries++;ray.hit=null;
     if(hit){const t=hit.timeOfImpact,n=[hit.normal.x,hit.normal.y,hit.normal.z];need(Number.isFinite(t)&&t>=0&&t<=ray.maxDistance&&n.every(Number.isFinite)&&Math.abs(Math.hypot(...n)-1)<.001,'invalid_hit');const pos=mesh.frame.toWorldPosition(origin.map((x,i)=>x+dir[i]*t));ray.hit={position:pos,normal:n,distance:t};hits++;}
    }points.push({...p,rays});
   }cases.push({id:c.id,sourceSHA256:c.sourceSHA256,sourceList:c.sourceList,collisionWindow,frameOrigin:mesh.frame.origin,geometryMetadata:mesh.metadata,points});
  }finally{world?.dispose();}
 }
 const report={schema:'native-geometry-ray-report-v1',status:'probed',inputSHA256:sha(raw),mapAssumption:d.mapAssumption,nativeMapIDVerified:false,assetDigests:asset.assetDigests,queryRuntime,sampleCount:cases.reduce((n,c)=>n+c.points.length,0),queryCount:queries,hitCount:hits,noHitCount:queries-hits,nativeCollisionVerified:false,calibrationVerified:false,runtimeReady:false,motorExecuted:false,navigationRebuilt:false,offsetFitted:false,offsetInstalled:false,nativeGroundedInferred:false,atomicSnapshot:false,cases};
 const temp=outFile+'.pending';fs.writeFileSync(temp,JSON.stringify(report,null,2)+'\n',{flag:'wx'});fs.renameSync(temp,outFile);return report;
}
if(process.argv[1]&&path.resolve(process.argv[1])===fileURLToPath(import.meta.url)){const r=await runNativeGeometry(path.resolve(process.argv[2]),path.resolve(process.argv[3]));console.log(JSON.stringify({status:r.status,sampleCount:r.sampleCount,queryCount:r.queryCount,hitCount:r.hitCount}));}
