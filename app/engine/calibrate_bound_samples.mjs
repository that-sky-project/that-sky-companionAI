// Anonymous round3 actor-origin samples versus candidate geometry. Never installs calibration.
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {createHash} from 'node:crypto';
import {init,TerrainWorld} from './core.mjs';
import {loadAsset} from './assets.mjs';

const sha=b=>createHash('sha256').update(b).digest('hex');
export function validateSamples(data){
  if(data.schema!=='anonymous-numeric-position-v1'||JSON.stringify(data.columns)!==JSON.stringify(['timestamp','map_hash','x','y','z','age']))throw Error('sample_schema');
  if(data.status!=='sampled'||data.identity_fields_exported!==false||!Array.isArray(data.samples)||data.samples.length<1||data.samples.length>300||data.count!==data.samples.length)throw Error('sample_status');
  let previous=-Infinity,map;
  for(const r of data.samples){
    if(!Array.isArray(r)||r.length!==6||!r.every(Number.isFinite)||r[0]<=previous||!Number.isInteger(r[1])||r[1]<0||r[1]>0xffffffff||r[5]<0||r[5]>1||r.slice(2,5).some(x=>Math.abs(x)>=1e6))throw Error('sample_value');
    if(map!==undefined&&map!==r[1])throw Error('sample_map_changed');
    previous=r[0];map=r[1];
  }
  return map;
}

export function verticalHits(positions,indices,x,z){
  const hits=[];
  for(let t=0;t<indices.length;t+=3){
    const ia=indices[t]*3,ib=indices[t+1]*3,ic=indices[t+2]*3;
    const ax=positions[ia],ay=positions[ia+1],az=positions[ia+2];
    const ux=positions[ib]-ax,uy=positions[ib+1]-ay,uz=positions[ib+2]-az;
    const vx=positions[ic]-ax,vy=positions[ic+1]-ay,vz=positions[ic+2]-az;
    const den=ux*vz-uz*vx;if(Math.abs(den)<1e-10)continue;
    const dx=x-ax,dz=z-az,b=(dx*vz-dz*vx)/den,c=(ux*dz-uz*dx)/den;
    if(b < -1e-7 || c < -1e-7 || b+c>1+1e-7)continue;
    const nx=uy*vz-uz*vy,ny=uz*vx-ux*vz,nz=ux*vy-uy*vx;
    hits.push({triangle:t/3,y:ay+b*uy+c*vy,slopeDegrees:Math.acos(Math.min(1,Math.abs(ny)/Math.hypot(nx,ny,nz)))*180/Math.PI});
  }
  return hits.sort((a,b)=>b.y-a.y);
}
function stat(values){
  const xs=values.filter(Number.isFinite).sort((a,b)=>a-b),n=xs.length;
  if(!n)return {count:0};
  return {count:n,min:xs[0],median:(xs[Math.floor((n-1)/2)]+xs[Math.ceil((n-1)/2)])/2,max:xs[n-1]};
}
function atomic(file,value){
  fs.mkdirSync(path.dirname(file),{recursive:true});const temp=file+'.'+process.pid+'.tmp';
  try{fs.writeFileSync(temp,JSON.stringify(value,null,2));fs.renameSync(temp,file);}finally{if(fs.existsSync(temp))fs.unlinkSync(temp);}
}
export async function compare(input,catalogFile,out){
  if(fs.statSync(input).size>1024*1024)throw Error('sample_budget');
  const raw=fs.readFileSync(input),data=JSON.parse(raw),map=validateSamples(data);
  const catalog=JSON.parse(fs.readFileSync(catalogFile,'utf8'));
  const entries=catalog.entries.filter(r=>r.scene_hash===map);
  if(entries.length!==1||entries[0].collision_status!=='ready')throw Error('map_not_ready');
  const row=entries[0],asset=loadAsset(path.resolve(catalog.asset_root),row.asset_relpath,{offline:true,expectedManifestSHA:row.asset_manifest_sha256});
  if(asset.manifest.mapName!==row.scene_name)throw Error('map_name_mismatch');
  await init();const world=new TerrainWorld(asset.positions,asset.indices,{navigation:false});
  const records=[];
  try{
    for(const [timestamp,mapHash,x,y,z,age] of data.samples){
      const hits=verticalHits(asset.positions,asset.indices,x,z),near=hits.filter(h=>h.y<=y+.5+1e-7&&h.y>=y-3.5);
      const ground=world.ground([x,y+.5,z],4),exact=near[0];
      const mismatch=Boolean(ground)!==Boolean(exact)||(ground&&Math.abs(ground.position[1]-exact.y)>.002);
      const nearbyLayers=[];for(const hit of near)if(!nearbyLayers.some(v=>Math.abs(v-hit.y)<.01))nearbyLayers.push(hit.y);
      const patch=[];for(const dx of [-.15,0,.15])for(const dz of [-.15,0,.15]){const g=world.ground([x+dx,y+.5,z+dz],4);if(g)patch.push(g.position[1]);}
      let quantizationNeighborHits=0;
      for(const dx of [-.0005,0,.0005])for(const dz of [-.0005,0,.0005])if(dx||dz){if(world.ground([x+dx,y+.5,z+dz],4))quantizationNeighborHits++;}
      records.push({timestamp,mapHash,origin:[x,y,z],age,ground:ground?.position??null,normal:ground?.normal??null,originMinusGround:ground?y-ground.position[1]:null,exactTriangleY:exact?.y??null,exactSlopeDegrees:exact?.slopeDegrees??null,nearbyLayers,rapierExactMismatch:mismatch,diagnosticPatchHits:patch.length,diagnosticPatchRelief:patch.length===9?Math.max(...patch)-Math.min(...patch):null,quantizationNeighborHits});
    }
    const ranges=[0,1,2].map(axis=>stat(records.map(r=>r.origin[axis])));
    const reliefs=records.map(r=>r.diagnosticPatchRelief).filter(v=>v!==null);
    const report={kind:'bound_actor_origin_vs_initial_static_geometry',mapName:row.scene_name,mapHash:map,sampleSHA256:sha(raw),assetDigests:asset.assetDigests,count:records.length,uniquePositions:new Set(records.map(r=>JSON.stringify(r.origin))).size,durationSeconds:data.samples.at(-1)[0]-data.samples[0][0],coordinateRanges:ranges,groundHits:records.filter(r=>r.ground).length,rapierExactMismatches:records.filter(r=>r.rapierExactMismatch).length,originMinusGround:stat(records.filter(r=>r.ground).map(r=>r.originMinusGround)),exactSlopeDegrees:stat(records.filter(r=>r.ground).map(r=>r.exactSlopeDegrees)),maximumPatchRelief:reliefs.length?Math.max(...reliefs):null,quantizationNeighborRayHits:records.reduce((n,r)=>n+r.quantizationNeighborHits,0),positionQuantum:.001,coordinateHalfQuantum:.0005,conditionalMapComparison:true,targetMapMembershipVerified:false,groundedVerified:false,footOffsetCalibrated:false,calibrationInstalled:false,warnings:['Received actor origin rounded to 3 decimal places, not feet; no grounded flag recorded.','The sample map comes from the bot confirmed room; target level-roster membership is not independently exported.','Single standing location and repeated coordinates do not identify capsule size, gravity, foot offset or other maps.','Initial static geometry is not native collision verification.','The 0.15 coordinate-unit diagnostic patch is not a measured avatar footprint.','Eight rounding-neighbor rays are discrete diagnostics, not a proof covering the entire uncertainty region.'],records};
    atomic(out,report);const {records:_,...summary}=report;return summary;
  }finally{world.dispose();}
}
if(process.argv[1]&&path.resolve(process.argv[1])===fileURLToPath(import.meta.url)){
  const args=process.argv.slice(2),get=k=>{const i=args.indexOf(k);if(i<0||!args[i+1])throw Error('missing '+k);return path.resolve(args[i+1]);};
  console.log(JSON.stringify(await compare(get('--input'),get('--catalog'),get('--out'))));
}
