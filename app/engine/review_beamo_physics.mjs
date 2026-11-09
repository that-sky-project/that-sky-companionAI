// Offline-only candidate replay. No native client/server/network or calibration writes.
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {createHash} from 'node:crypto';
import {init,TerrainWorld,DEFAULTS} from './core.mjs';
import {loadAsset} from './assets.mjs';
import {validateSamples} from './calibrate_bound_samples.mjs';

const ROOT=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const PROFILE='netease-android-0.16.7-421589';
const SAMPLE_SHA='18be4c1717b4852452da6ac883225abc89da47dc27d0d797f19997b9f6ed2087';
const sha=b=>createHash('sha256').update(b).digest('hex');
const need=(condition,message)=>{if(!condition)throw Error(message);};
function boundedJSON(file,maximum=32*1024*1024){
  need(fs.statSync(file).size<=maximum,'input_budget');const raw=fs.readFileSync(file);
  need(raw.length<=maximum,'input_budget');return {doc:JSON.parse(raw),sha256:sha(raw)};
}

export function exactGround(positions,indices,x,z,top,bottom){
  let best=null;
  for(let i=0;i<indices.length;i+=3){
    const a=indices[i]*3,b=indices[i+1]*3,c=indices[i+2]*3;
    const ax=positions[a],az=positions[a+2],ux=positions[b]-ax,uz=positions[b+2]-az;
    const vx=positions[c]-ax,vz=positions[c+2]-az,den=ux*vz-uz*vx;
    if(Math.abs(den)<=1e-12)continue;
    const dx=x-ax,dz=z-az,u=(dx*vz-dz*vx)/den,v=(ux*dz-uz*dx)/den;
    if(u< -1e-8||v< -1e-8||u+v>1+1e-8)continue;
    const y=positions[a+1]+u*(positions[b+1]-positions[a+1])+v*(positions[c+1]-positions[a+1]);
    if(y>top+1e-8||y<bottom||best&&y<=best.y)continue;
    best={triangle:i/3,y};
  }
  return best;
}

export function runRoute(world,start,goal,{maximumSeconds=5}={}){
  let motor;const trace=[];let sampledContactObservations=0,lateralContactObservations=0,groundedFrames=0;
  try{
    motor=world.motor(start,[goal],0);let last=motor.sample(0),frames=0;
    for(let frame=1;frame<=maximumSeconds*60;frame++){
      last=motor.sample(frame/60);frames=frame;if(last.grounded)groundedFrames++;
      const count=motor.controller.numComputedCollisions();sampledContactObservations+=count;
      for(let i=0;i<count;i++){const contact=motor.controller.computedCollision(i);if(contact&&Math.abs(contact.normal1.y)<.7)lateralContactObservations++;}
      if(frame%3===0||last.arrived||last.blocked)trace.push({time:frame/60,...last,computedCollisions:count});
      if(last.arrived||last.blocked)break;
    }
    return {start,goal,status:last.arrived?'arrived':last.blocked?'blocked':'timeout',
      reason:last.reason??null,frames,elapsedSeconds:frames/60,groundedFrames,
      sampledContactObservations,lateralContactObservations,last,trace};
  }catch(error){return {start,goal,status:'rejected',reason:error.message,groundedFrames,
    sampledContactObservations,lateralContactObservations,trace};}
  finally{motor?.dispose();}
}

export async function reviewBeamoPhysics({assetFile,samplesFile,reviewFile,outFile}){
  const previous=boundedJSON(reviewFile);need(previous.doc.status==='passed'&&previous.doc.profile===PROFILE,'independent_review_profile');
  const pin=previous.doc.maps?.CandleSpace?.merged;need(pin?.runtime_sha256,'missing_review_asset_pin');
  const sample=boundedJSON(samplesFile,1024*1024),data=sample.doc;
  need(sample.sha256===SAMPLE_SHA,'sample_pin_mismatch');const mapHash=validateSamples(data);
  need(data.samples.length===60&&data.grounded_verified===false,'sample_count_or_grounding_changed');
  const asset=loadAsset(path.dirname(assetFile),path.basename(assetFile),{offline:true,expectedManifestSHA:pin.runtime_sha256});
  need(asset.manifest.mapName==='CandleSpace'&&asset.manifest.geometryKind==='offline_beamo_augmented_candidates','wrong_candidate_map');
  need(asset.manifest.nativeCollisionVerified===false&&asset.manifest.calibrationVerified===false,'offline_candidate_flags');
  const manifestLink=asset.manifest.evidenceManifest;
  need(manifestLink&&typeof manifestLink.file==='string'&&path.basename(manifestLink.file)===manifestLink.file,'evidence_path');
  const evidence=boundedJSON(path.join(path.dirname(assetFile),manifestLink.file));
  need(evidence.sha256===manifestLink.sha256&&evidence.sha256===pin.manifest_sha256,'evidence_pin_mismatch');
  need(evidence.doc.runtimeReady===false&&evidence.doc.runtime_ready===false&&evidence.doc.native_collision_verified===false,'evidence_not_offline');
  const output=path.resolve(outFile),research=path.join(ROOT,'research','integration');
  need(output.startsWith(research+path.sep)&&![assetFile,samplesFile,reviewFile].map(p=>path.resolve(p)).includes(output),'report_scope');
  await init();const world=new TerrainWorld(asset.positions,asset.indices,{navigation:false});
  try{
    need(!world.nav,'unexpected_navigation');
    const records=data.samples.map(([timestamp,hash,x,y,z,age])=>{
      const top=y+.5,bottom=top-4,ground=world.ground([x,top,z],4);
      const exact=exactGround(asset.positions,asset.indices,x,z,top,bottom);
      return {timestamp,mapHash:hash,origin:[x,y,z],age,rapierGround:ground?.position??null,
        independentTriangle:exact,mismatch:Boolean(ground)!==Boolean(exact)||Boolean(ground&&Math.abs(ground.position[1]-exact.y)>.002)};
    });
    const unique=[...new Map(data.samples.map(r=>[JSON.stringify(r.slice(2,5)),r.slice(2,5)])).values()];
    need(unique.length===4,'expected_four_unique_positions');
    const routes=[];
    for(const [anchorIndex,[x,y,z]] of unique.entries()){
      // Generated surface-foot origins, not the recorded actor-origin Y and not a calibrated offset.
      const start=world.ground([x,y+.5,z],4),end=world.ground([x-.75,y+.5,z],4);
      if(!start||!end){routes.push({anchorIndex,status:'rejected',reason:'route_endpoint_no_ground'});continue;}
      const probes=[];
      for(let i=0;i<=15;i++){
        const px=x-.75*i/15,hit=world.ground([px,y+.5,z],4);
        probes.push(hit?.position??null);
      }
      if(probes.some(p=>!p||Math.abs(p[1]-start.position[1])>.1)){
        routes.push({anchorIndex,status:'rejected',reason:'short_surface_route_not_flat',probes});continue;
      }
      for(const direction of ['out','back']){
        const [a,b]=direction==='out'?[start.position,end.position]:[end.position,start.position];
        routes.push({anchorIndex,direction,generatedLength:.75,...runRoute(world,a,b)});
      }
    }
    const result={schema:'offline-beamo-physics-review-v1',mode:'offline',profile:PROFILE,
      parameterMode:'unchanged_default_capsule_and_motor',config:world.config,declaredDefaults:DEFAULTS,
      navigation:false,navigationReady:!!world.nav,nativeCollisionVerified:false,runtimeReady:false,
      calibrationVerified:false,footOffsetCalibrated:false,calibrationInstalled:false,
      nativeStaticRegistrationObserved:asset.manifest.nativeStaticRegistrationObserved??false,
      nativeStaticRegistrationObservedMeaning:'Inherited any=true from old base static-registration analysis only; does not verify all merged geometry, Beamo selection, its runtime gate, or native avatar physics.',
      sampleSHA256:sample.sha256,independentReviewSHA256:previous.sha256,assetDigests:asset.assetDigests,
      evidenceManifestSHA256:evidence.sha256,count:records.length,uniquePositions:unique.length,mapHash,
      groundHits:records.filter(r=>r.rapierGround).length,independentHits:records.filter(r=>r.independentTriangle).length,
      rapierExactMismatches:records.filter(r=>r.mismatch).length,
      routeCount:routes.length,arrived:routes.filter(r=>r.status==='arrived').length,
      blocked:routes.filter(r=>r.status==='blocked').length,rejected:routes.filter(r=>r.status==='rejected').length,
      timedOut:routes.filter(r=>r.status==='timeout').length,
      groundedRoutes:routes.filter(r=>r.groundedFrames>0).length,
      sampledContactObservations:routes.reduce((n,r)=>n+(r.sampledContactObservations??0),0),
      lateralContactObservations:routes.reduce((n,r)=>n+(r.lateralContactObservations??0),0),
      targetMapMembershipVerified:false,recordedActorGroundedVerified:false,
      limitations:['Repeated samples are four nearby XZs, not a map-wide or multi-map proof.',
        'Route start/goal Y comes from candidate surfaces; no actor-to-foot offset is inferred or installed.',
        'Contact observations include repeated supporting-floor contacts and are not unique wall impacts.',
        'Four short routes and their reverse legs do not calibrate capsule, speed, gravity, steps, slope or collision registration.',
        'Uses offline Rapier/default parameters, not native-client-equivalent physics.'],records,routes};
    const raw=JSON.stringify(result,null,2);fs.mkdirSync(path.dirname(output),{recursive:true});
    const temporary=output+'.tmp';fs.writeFileSync(temporary,raw);fs.renameSync(temporary,output);
    const {records:_,routes:__,...summary}=result;return summary;
  }finally{world.dispose();}
}

if(process.argv[1]&&path.resolve(process.argv[1])===fileURLToPath(import.meta.url)){
  const args=process.argv.slice(2),get=(key,fallback)=>{const i=args.indexOf(key);return path.resolve(i<0?fallback:args[i+1]);};
  const summary=await reviewBeamoPhysics({assetFile:get('--asset',path.join(ROOT,'outputs/beamo-world-v1/CandleSpace/static-world/runtime-manifest.json')),
    samplesFile:get('--samples',path.join(ROOT,'research/calibration/bound-numeric-samples.json')),
    reviewFile:get('--review',path.join(ROOT,'research/integration/beamo-world-independent-review.json')),
    outFile:get('--out',path.join(ROOT,'research/integration/beamo-physics-review.json'))});
  console.log(JSON.stringify(summary));
}
