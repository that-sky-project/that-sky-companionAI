// Query existing observations against offline candidate geometry; no motor or fitting.
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {createHash,randomUUID} from 'node:crypto';
import RAPIER from '@dimforge/rapier3d-compat';
import {init,TerrainWorld,navigationWindow} from './core.mjs';
import {loadAsset} from './assets.mjs';
import {preparePreservingGeometry,PRESERVING_ORIGIN_POLICY} from './preserving_geometry.mjs';
import {assertPinnedCollisionRuntime} from './pinned_collision_runtime.mjs';
const PROFILE='netease-android-0.16.7-421589',SCENE='CandleSpace',MAP_HASH=3526133726,KIND='offline_beamo_augmented_candidates';
const HASH=/^[0-9a-f]{64}$/;
const FLAGS=Object.freeze({nativeCollisionVerified:false,calibrationVerified:false,runtimeReady:false,motorExecuted:false,navigationRebuilt:false,parameterFittingAllowed:false,groundedInferred:false,nativeModeInferred:false,footOffsetInstalled:false,server_modified:false,live_enabled:false});
const need=(ok,msg)=>{if(!ok)throw Error('observation_probe:'+msg);};
const sha=b=>createHash('sha256').update(b).digest('hex');
const same=(a,b)=>JSON.stringify(a)===JSON.stringify(b);
function record(o,keys,label){
 need(o&&typeof o==='object'&&!Array.isArray(o)&&[Object.prototype,null].includes(Object.getPrototypeOf(o)),label+'_object');
 const d=Object.getOwnPropertyDescriptors(o);need(Reflect.ownKeys(d).length===keys.length&&keys.every(k=>d[k]&&'value'in d[k]),label+'_fields');
 return Object.fromEntries(keys.map(k=>[k,d[k].value]));
}
function sequence(a,min,max,label){
 need(Array.isArray(a)&&Object.getPrototypeOf(a)===Array.prototype,label+'_array');const d=Object.getOwnPropertyDescriptors(a),n=d.length?.value;
 need(Number.isInteger(n)&&n>=min&&n<=max&&Reflect.ownKeys(d).length===n+1,label+'_count');
 const out=[];for(let i=0;i<n;i++){need(d[i]&&'value'in d[i],label+'_data');out.push(d[i].value);}return out;
}
function point(v){return sequence(v,3,3,'point').map(x=>{need(typeof x==='number'&&Number.isFinite(x)&&Math.abs(x)<999000,'point_range');return x;});}
function falseFlags(v,keys=['nativeCollisionVerified','calibrationVerified','runtimeReady']){for(const k of keys)need(v[k]===false,'false_flag:'+k);}
function hash(v,label){need(typeof v==='string'&&HASH.test(v),'hash:'+label);return v;}
function freeze(o){if(o&&typeof o==='object'){for(const x of Object.values(o))freeze(x);Object.freeze(o);}return o;}
export function validateProbeRequest(value){
 const r=record(value,['schema','mode','mapName','assetFile','expectedAssetManifestSHA256','numericFile','expectedNumericSHA256','expectedManifestSHA256','nativeCollisionVerified','calibrationVerified','runtimeReady'],'request');
 need(r.schema==='observation-geometry-probe-request-v1'&&r.mode==='offline'&&r.mapName===SCENE,'request_identity');falseFlags(r);
 for(const k of ['assetFile','numericFile'])need(typeof r[k]==='string'&&path.isAbsolute(r[k]),'absolute_path');
 for(const k of ['expectedAssetManifestSHA256','expectedNumericSHA256','expectedManifestSHA256'])hash(r[k],k);
 return freeze(r);
}
export function validateNumericObservations(value,expectedManifestSHA256){
 const v=record(value,['schema','mode','profile','scene','mapHash','inputManifestSHA256','originSemantics','parameterFittingAllowed','nativeCollisionVerified','calibrationVerified','runtimeReady','cases'],'numeric');
 need(v.schema==='skyterrain-numeric-observation-probe-input-v1'&&v.mode==='offline'&&v.profile===PROFILE&&v.scene===SCENE&&v.mapHash===MAP_HASH,'numeric_identity');
 falseFlags(v);need(v.parameterFittingAllowed===false&&v.originSemantics==='actor_origin_unadjusted','no_calibration');
 hash(expectedManifestSHA256,'expectedManifest');need(hash(v.inputManifestSHA256,'manifest')===expectedManifestSHA256,'numeric_manifest_binding');
 let total=0;const seen=new Set();
 v.cases=sequence(v.cases,1,64,'cases').map(item=>{
  const c=record(item,['id','windows','points'],'case');need(typeof c.id==='string'&&/^[a-z0-9_-]{1,64}$/.test(c.id)&&!seen.has(c.id),'case_id');seen.add(c.id);
  let count=0;const windowHashes=new Set();
  c.windows=sequence(c.windows,1,4,'windows').map(item=>{const w=record(item,['sourceSHA256','startIndex','count'],'window');hash(w.sourceSHA256,'window');need(!windowHashes.has(w.sourceSHA256),'duplicate_window');windowHashes.add(w.sourceSHA256);need(w.startIndex===count&&Number.isInteger(w.count)&&w.count>0&&w.count<=600,'window_partition');count+=w.count;return w;});
  total+=count;need(total<=4096,'total_sample_budget');
  const ps=sequence(c.points,count,count,'points');let time=-1;
  c.points=ps.map((item,index)=>{const p=record(item,['sourceIndex','windowIndex','t','position','age'],'sample');const w=c.windows.findIndex(w=>index>=w.startIndex&&index<w.startIndex+w.count);need(p.sourceIndex===index&&p.windowIndex===w,'sample_identity');need(typeof p.t==='number'&&Number.isFinite(p.t)&&p.t>time&&p.t<=86400&&(index!==0||p.t===0),'sample_time');time=p.t;need(typeof p.age==='number'&&Number.isFinite(p.age)&&p.age>=0&&p.age<=.5,'sample_age');p.position=point(p.position);return p;});return c;
 });return freeze(v);
}
function bytes(file,budget){const fd=fs.openSync(file,'r');try{const st=fs.fstatSync(fd);need(st.isFile()&&st.size>0&&st.size<=budget,'input_budget');const b=Buffer.alloc(st.size+1);let n=0;while(n<b.length){const k=fs.readSync(fd,b,n,b.length-n,null);if(!k)break;n+=k;}need(n===st.size,'input_changed');return b.subarray(0,n);}finally{fs.closeSync(fd);}}
function doc(file,budget,expected){const raw=bytes(file,budget),h=sha(raw);if(expected)need(h===expected,'input_hash_mismatch');return {value:JSON.parse(raw),sha256:h};}
function child(parent,name){need(typeof name==='string'&&name.length>0&&!/[\\/:\0]/.test(name)&&!['.','..'].includes(name),'child_path');const base=fs.realpathSync(parent),f=fs.realpathSync(path.join(base,name));need(path.dirname(f)===base,'child_escape');return f;}
function evidence(asset,file){
 const m=asset.manifest,ref=m.evidenceManifest;need(ref&&typeof ref==='object','evidence_reference');hash(ref.sha256,'evidence');const loaded=doc(child(path.dirname(file),ref.file),32*1024*1024,ref.sha256),e=loaded.value;
 need(e.schema_version==='sky-partial-static-world-v1'&&e.format==='skyterrain-static-v1'&&e.geometry_kind==='partial_static_world_candidate'&&e.coordinate_space==='sky_native'&&e.geometryKind===KIND&&e.mapName===m.mapName,'evidence_identity');
 falseFlags(e,['nativeCollisionVerified','calibrationVerified','runtimeReady','native_collision_verified','runtime_ready']);
 for(const [key,counter,snake,descriptor,dtype] of [['positions','vertexCount','vertex_count','positions','<f4'],['indices','triangleCount','triangle_count','triangles','<u4']]){
  const count=asset[key].length/3,f=e.files?.[descriptor];need(e[counter]===count&&e[snake]===count&&m[counter]===count,'evidence_counts');need(e[key]===m[key]&&e.sha256?.[key]===asset.assetDigests[key]&&f?.file===m[key]&&f.sha256===asset.assetDigests[key]&&f.bytes===asset[key].byteLength&&f.dtype===dtype&&same(f.shape,[count,3]),'evidence_arrays');
 }return loaded.sha256;
}
export function observationRays(position){const p=point(position);return {down:{origin:[...p],direction:[0,-1,0],maxDistance:256},up:{origin:[...p],direction:[0,1,0],maxDistance:256},fromAbove:{origin:[p[0],p[1]+2,p[2]],direction:[0,-1,0],maxDistance:258}};}
export function observationCollisionWindow(points){need(Array.isArray(points)&&points.length>0&&points.length<=600,'window_points');const ps=points.map(p=>point(p));return navigationWindow({minX:Math.min(...ps.map(p=>p[0]))-16,maxX:Math.max(...ps.map(p=>p[0]))+16,minZ:Math.min(...ps.map(p=>p[2]))-16,maxZ:Math.max(...ps.map(p=>p[2]))+16});}
export async function probeNumericGeometry(asset,numeric){
 numeric=validateNumericObservations(numeric,numeric?.inputManifestSHA256);
 await init();const runtime=assertPinnedCollisionRuntime();const cases=[];let sampleCount=0,queryCount=0,hitCount=0;
 for(const c of numeric.cases){const result={id:c.id,windows:[],points:[]};
  for(const [windowIndex,w] of c.windows.entries()){
   const points=c.points.slice(w.startIndex,w.startIndex+w.count),collisionWindow=observationCollisionWindow(points.map(p=>p.position));
   const mesh=preparePreservingGeometry({positions:asset.positions,indices:asset.indices},collisionWindow);let world;
   try{
    world=new TerrainWorld(mesh.positions,mesh.indices,{navigation:false});
    result.windows.push({windowIndex,sourceSHA256:w.sourceSHA256,startIndex:w.startIndex,count:w.count,collisionWindow,frameOrigin:mesh.frame.origin,geometryMetadata:mesh.metadata});
    for(const p of points){mesh.coverage.assertWorldCapsule(p.position,{radius:0,skin:0});const rays=observationRays(p.position);
     for(const ray of Object.values(rays)){
      const o=mesh.frame.toLocalPosition(ray.origin),d=ray.direction,hit=world.world.castRayAndGetNormal(new RAPIER.Ray({x:o[0],y:o[1],z:o[2]},{x:d[0],y:d[1],z:d[2]}),ray.maxDistance,true,undefined,undefined,undefined,undefined,col=>col===world.terrain);
      ray.hit=null;queryCount++;
      if(hit){const t=hit.timeOfImpact,n=[hit.normal.x,hit.normal.y,hit.normal.z];need(Number.isFinite(t)&&t>=0&&t<=ray.maxDistance&&n.every(Number.isFinite)&&Math.abs(Math.hypot(...n)-1)<.001,'invalid_ray_hit');const position=mesh.frame.toWorldPosition(o.map((x,i)=>x+d[i]*t));ray.hit={position,normal:n,distance:t,actorMinusHitY:p.position[1]-position[1]};hitCount++;}
     }
     result.points.push({...p,position:[...p.position],status:'probed',rays});sampleCount++;
    }
   }finally{world?.dispose();}
  }cases.push(result);
 }
 return {cases,sampleCount,queryCount,hitCount,noHitCount:queryCount-hitCount,queryRuntime:runtime};
}
function saveNew(file,value){const out=path.resolve(file);need(path.extname(out)==='.json'&&!fs.existsSync(out),'new_json_output');fs.mkdirSync(path.dirname(out),{recursive:true});const tmp=out+'.'+randomUUID()+'.pending';try{fs.writeFileSync(tmp,JSON.stringify(value,null,2)+'\n',{flag:'wx'});fs.linkSync(tmp,out);}finally{if(fs.existsSync(tmp))fs.unlinkSync(tmp);}}
export async function probeObservationGeometry({requestFile,outFile}){
 const report={schema:'observation-geometry-probe-report-v1',mode:'offline',profile:PROFILE,mapName:SCENE,geometryKind:KIND,status:'unavailable',reason:null,...FLAGS,parameters:'unchanged_defaults',originPolicy:PRESERVING_ORIGIN_POLICY,sampleCount:0,queryCount:0,cases:[],limitations:['Geometric actor-origin ray context only; a hit is not grounded or a calibrated foot point.','FromAbove offset 2m and fixed 256m range are diagnostic query bounds, not character dimensions.','Sparse samples and ray-only probes do not reproduce controls, capsule motion, dynamic collision or native flight.']};
 try{need(!fs.existsSync(outFile),'new_json_output');const rq=doc(requestFile,65536),r=validateProbeRequest(rq.value),n=doc(r.numericFile,2*1024*1024,r.expectedNumericSHA256),numeric=validateNumericObservations(n.value,r.expectedManifestSHA256);
  const asset=loadAsset(path.dirname(r.assetFile),path.basename(r.assetFile),{offline:true,expectedManifestSHA:r.expectedAssetManifestSHA256});need(asset.manifest.geometryKind===KIND&&asset.manifest.mapName===r.mapName&&asset.manifest.nativeCollisionVerified===false&&asset.manifest.calibrationVerified===false,'asset_identity');
  Object.assign(report,{requestSHA256:rq.sha256,assetDigests:asset.assetDigests,evidenceManifestSHA256:evidence(asset,r.assetFile),numericSHA256:n.sha256,observationManifestSHA256:r.expectedManifestSHA256});
  Object.assign(report,await probeNumericGeometry(asset,numeric),{status:'probed'});
 }catch(e){report.reason=String(e?.message??e).slice(0,512);}saveNew(outFile,report);return report;
}
if(process.argv[1]&&path.resolve(process.argv[1])===fileURLToPath(import.meta.url)){
 const args=process.argv.slice(2),get=k=>{const i=args.indexOf(k);need(i>=0&&args[i+1],'missing_'+k);return path.resolve(args[i+1]);};const r=await probeObservationGeometry({requestFile:get('--request'),outFile:get('--out')});console.log(JSON.stringify({status:r.status,sampleCount:r.sampleCount,queryCount:r.queryCount,hitCount:r.hitCount,reason:r.reason}));if(r.status!=='probed')process.exitCode=2;
}
