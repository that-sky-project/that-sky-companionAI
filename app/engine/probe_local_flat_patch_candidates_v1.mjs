// Pure geometry candidate selection. No avatar-gap input, motor, device or server.
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {createHash} from 'node:crypto';
import RAPIER from '@dimforge/rapier3d-compat';
import {loadAsset} from './assets.mjs';
import {preparePreservingGeometry} from './preserving_geometry.mjs';
import {assertPinnedCollisionRuntime} from './pinned_collision_runtime.mjs';

const ROOT=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const need=(v,why)=>{if(!v)throw Error(why);};
const sha=b=>createHash('sha256').update(b).digest('hex');
const SEED=[3.196500063,1.091200829,-4.516999245];
const SPEC=Object.freeze({searchRadius:10,centerGrid:.5,maximumCandidates:3,minimumCenterSeparation:3,
  slopeDegrees:3,pointTolerance:.15,offlineCapsuleRadius:.25,offlineCapsuleHeight:1.5,skin:.015,
  supportRegionHalfSize:1+.15+.25+.015,clearanceGrid:.25,clearanceLift:.05,
  wallAndLedgeMargin:1,overheadClearance:2,belowLayerSeparation:.5,
  sameFloorTolerance:.005,adjacentHeightReserve:.01,maximumUncoveredArea:1e-6,
  maximumUncoveredPieces:4096,maximumRays:300000,maximumShapeQueries:100000,
  maximumCandidateVerticalDifferenceFromSeedActorY:2,selectionUsesActorFloorGap:false});
const OFFSETS=[['T0',0,0],['T1',1,0],['T2',0,1],['T3',-1,0],['T4',0,-1],
  ['H0',.75,.75],['H1',-.75,.75],['H2',-.75,-.75],['H3',.75,-.75]];
const area=p=>Math.abs(p.reduce((a,v,i)=>{const q=p[(i+1)%p.length];return a+v[0]*q[1]-q[0]*v[1];},0)/2);
const signed=p=>p.reduce((a,v,i)=>{const q=p[(i+1)%p.length];return a+v[0]*q[1]-q[0]*v[1];},0)/2;
function clipHalf(poly,a,b,inside){
  if(!poly.length)return [];
  const out=[],side=p=>(b[0]-a[0])*(p[1]-a[1])-(b[1]-a[1])*(p[0]-a[0]);
  for(let i=0;i<poly.length;i++){
    const p=poly[i],q=poly[(i+1)%poly.length],fp=side(p),fq=side(q),pi=inside?fp>=0:fp<=0,qi=inside?fq>=0:fq<=0;
    if(pi)out.push(p);
    if(pi!==qi){const t=fp/(fp-fq);out.push([p[0]+(q[0]-p[0])*t,p[1]+(q[1]-p[1])*t]);}
  }return out;
}
export function subtractTriangle(poly,triangle){
  const tri=signed(triangle)>=0?triangle:[...triangle].reverse();let remaining=poly;const out=[];
  for(let i=0;i<3&&remaining.length;i++){
    const a=tri[i],b=tri[(i+1)%3],outside=clipHalf(remaining,a,b,false);
    if(outside.length>=3&&area(outside)>1e-12)out.push(outside);
    remaining=clipHalf(remaining,a,b,true);
  }return out;
}
export function projectedCoverage(rect,triangles){
  let rest=[rect];
  for(const tri of triangles){
    const next=[];for(const p of rest)next.push(...subtractTriangle(p,tri));rest=next;
    need(rest.length<=SPEC.maximumUncoveredPieces,'coverage_partition_budget');
    if(!rest.length)break;
  }return {uncoveredArea:rest.reduce((s,p)=>s+area(p),0),pieces:rest.length};
}
function grid(lo,hi,step){const n=Math.ceil((hi-lo)/step);return Array.from({length:n+1},(_,i)=>lo+(hi-lo)*i/n);}
function saveNew(file,obj){need(!fs.existsSync(file),'new_output_required');fs.mkdirSync(path.dirname(file),{recursive:true});const tmp=file+'.pending';fs.writeFileSync(tmp,JSON.stringify(obj,null,2)+'\n',{flag:'wx'});fs.renameSync(tmp,file);}

export async function runCandidateSearch(outFile){
  need(!fs.existsSync(outFile),'new_output_required');
  const inputFile=path.join(ROOT,'outputs/pilot-standing-geometry-v1/input.json');
  const inputRaw=fs.readFileSync(inputFile),ref=JSON.parse(inputRaw);
  // Only asset references are read from the native input; no gap/query/height observations are used.
  const asset=loadAsset(path.dirname(ref.assetFile),path.basename(ref.assetFile),{offline:true,expectedManifestSHA:ref.expectedAssetManifestSHA256});
  need(asset.manifest.mapName==='CandleSpace'&&asset.manifest.geometryKind==='offline_beamo_augmented_candidates','asset_identity');
  await RAPIER.init();const runtime=assertPinnedCollisionRuntime();
  const window={minX:SEED[0]-14,maxX:SEED[0]+14,minZ:SEED[2]-14,maxZ:SEED[2]+14};
  const mesh=preparePreservingGeometry({positions:asset.positions,indices:asset.indices},window);
  const world=new RAPIER.World({x:0,y:0,z:0});
  // Keep positive-area thin faces: never set DELETE_* flags. FIX_* merges exact duplicate vertices only.
  const flags=RAPIER.TriMeshFlags.FIX_INTERNAL_EDGES_TWO_SIDED;
  need(!(flags&(RAPIER.TriMeshFlags.DELETE_DEGENERATE_TRIANGLES|RAPIER.TriMeshFlags.DELETE_BAD_TOPOLOGY_TRIANGLES|RAPIER.TriMeshFlags.DELETE_DUPLICATE_TRIANGLES)),'face_deletion_flag');
  const terrain=world.createCollider(RAPIER.ColliderDesc.trimesh(mesh.positions,mesh.indices,flags));world.step();
  need(terrain.shape.indices.length===mesh.indices.length,'rapier_triangle_count_changed');
  let rayCount=0,shapeCount=0;const rayCache=new Map(),failures={},tested=[];
  const top=mesh.metadata.physicalLocalBounds.hi[1]+mesh.frame.origin[1]+2;
  const reject=k=>{failures[k]=(failures[k]??0)+1;return {accepted:false,reason:k};};
  function ray(origin,dir,distance){
    const key=JSON.stringify([origin,dir,distance]);if(rayCache.has(key))return rayCache.get(key);
    need(++rayCount<=SPEC.maximumRays,'ray_budget');mesh.coverage.assertWorldCapsule(origin,{radius:0,skin:0});
    const p=mesh.frame.toLocalPosition(origin),v=a=>({x:a[0],y:a[1],z:a[2]});
    const h=world.castRayAndGetNormal(new RAPIER.Ray(v(p),v(dir)),distance,true,undefined,undefined,undefined,undefined,c=>c===terrain);
    let result=null;
    if(h){const n=[h.normal.x,h.normal.y,h.normal.z],norm=Math.hypot(...n),t=h.timeOfImpact;
      need(n.every(Number.isFinite)&&Math.abs(norm-1)<.001&&Number.isFinite(t)&&t>=0&&t<=distance,'invalid_hit');
      result={position:mesh.frame.toWorldPosition(p.map((x,i)=>x+dir[i]*t)),normal:n,
        slopeDegrees:Math.acos(Math.min(1,Math.abs(n[1])/norm))*180/Math.PI,distance:t,featureId:h.featureId??null};}
    rayCache.set(key,result);return result;
  }
  function floor(x,z,expectedY=null){
    const down=ray([x,expectedY===null?SEED[1]+2:expectedY+1,z],[0,-1,0],8),above=ray([x,top,z],[0,-1,0],16);
    if(!down||!above)return {ok:false,reason:'floor_miss'};
    if(Math.abs(down.position[1]-above.position[1])>SPEC.sameFloorTolerance)return {ok:false,reason:'multiple_above_layers'};
    return {ok:true,hit:down,fromAbove:above};
  }
  function clearCapsule(x,y,z){
    need(++shapeCount<=SPEC.maximumShapeQueries,'shape_query_budget');
    const p=mesh.frame.toLocalPosition([x,y+SPEC.offlineCapsuleHeight/2+SPEC.clearanceLift,z]);
    mesh.coverage.assertWorldCapsule([x,y,z],{radius:SPEC.offlineCapsuleRadius,skin:SPEC.skin});
    const capsule=new RAPIER.Capsule((SPEC.offlineCapsuleHeight-2*SPEC.offlineCapsuleRadius)/2,SPEC.offlineCapsuleRadius+SPEC.skin);
    return !world.intersectionWithShape({x:p[0],y:p[1],z:p[2]},{x:0,y:0,z:0,w:1},capsule,undefined,undefined,undefined,undefined,c=>c===terrain);
  }
  function flatCoverage(x,y,z){
    const h=SPEC.supportRegionHalfSize,rect=[[x-h,z-h],[x+h,z-h],[x+h,z+h],[x-h,z+h]],triangles=[];
    const allowedY=Math.tan(SPEC.slopeDegrees*Math.PI/180)*Math.SQRT2*h+.02;
    for(let t=0;t<mesh.indices.length;t+=3){
      const v=[0,1,2].map(k=>{const j=mesh.indices[t+k]*3;return mesh.frame.toWorldPosition([mesh.positions[j],mesh.positions[j+1],mesh.positions[j+2]]);});
      if(Math.max(...v.map(p=>p[0]))<x-h||Math.min(...v.map(p=>p[0]))>x+h||Math.max(...v.map(p=>p[2]))<z-h||Math.min(...v.map(p=>p[2]))>z+h)continue;
      if(Math.max(...v.map(p=>p[1]))<y-allowedY||Math.min(...v.map(p=>p[1]))>y+allowedY)continue;
      const a=v[1].map((q,k)=>q-v[0][k]),b=v[2].map((q,k)=>q-v[0][k]),n=[a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]];
      if(Math.abs(n[1])/Math.hypot(...n)>=Math.cos(SPEC.slopeDegrees*Math.PI/180))triangles.push(v.map(p=>[p[0],p[2]]));
    }
    const proof=projectedCoverage(rect,triangles);return {...proof,eligibleFlatTriangles:triangles.length,regionXZ:{minX:x-h,maxX:x+h,minZ:z-h,maxZ:z+h}};
  }
  function validate(center){
    const [x,y,z]=center,points=[];let maxSlope=0,minClearance=Infinity,maxNeighborDelta=0;
    for(const [id,dx,dz]of OFFSETS){
      const f=floor(x+dx,z+dz,y);if(!f.ok)return reject(f.reason);
      if(f.hit.slopeDegrees>SPEC.slopeDegrees)return reject('nine_point_slope');
      const p=f.hit.position;maxSlope=Math.max(maxSlope,f.hit.slopeDegrees);
      const up=ray([p[0],p[1]+.04,p[2]],[0,1,0],SPEC.overheadClearance);
      if(up)return reject('nine_point_overhead');
      const below=ray([p[0],p[1]-.02,p[2]],[0,-1,0],SPEC.belowLayerSeparation-.02);
      if(below)return reject('nine_point_close_lower_layer');
      points.push({id,split:id.startsWith('T')?'train':'holdout',worldGroundXYZ:p,normal:f.hit.normal,slopeDegrees:f.hit.slopeDegrees,
        downFromAboveAgreement:Math.abs(p[1]-f.fromAbove.position[1]),nativeActorY:null});
    }
    const coverage=flatCoverage(x,y,z);if(coverage.uncoveredArea>SPEC.maximumUncoveredArea)return reject('continuous_projected_flat_coverage_missing');
    const h=SPEC.supportRegionHalfSize,coords=grid(-h,h,SPEC.clearanceGrid),samples=new Map();
    for(let i=0;i<coords.length;i++)for(let j=0;j<coords.length;j++){
      const xx=x+coords[i],zz=z+coords[j],f=floor(xx,zz,y);if(!f.ok)return reject('dense_'+f.reason);
      if(f.hit.slopeDegrees>SPEC.slopeDegrees)return reject('dense_slope');
      const yy=f.hit.position[1];maxSlope=Math.max(maxSlope,f.hit.slopeDegrees);
      if(!clearCapsule(xx,yy,zz))return reject('capsule_overlap');
      if(ray([xx,yy+.04,zz],[0,1,0],SPEC.overheadClearance))return reject('dense_overhead');
      samples.set(`${i},${j}`,yy);
      for(const [ii,jj]of [[i-1,j],[i,j-1]])if(ii>=0&&jj>=0){
        const d=Math.abs(yy-samples.get(`${ii},${jj}`));maxNeighborDelta=Math.max(maxNeighborDelta,d);
        if(d>Math.tan(SPEC.slopeDegrees*Math.PI/180)*(2*h/(coords.length-1))+SPEC.adjacentHeightReserve)return reject('neighbor_height_jump');
      }
    }
    // The1m wall/ledge margin is sampled around all9 tolerance disks. It is not a native contact claim.
    for(const p of points)for(let k=0;k<16;k++){
      const angle=k*Math.PI/8,dx=Math.cos(angle),dz=Math.sin(angle),r=SPEC.wallAndLedgeMargin+SPEC.pointTolerance;
      const xx=p.worldGroundXYZ[0]+dx*r,zz=p.worldGroundXYZ[2]+dz*r,f=floor(xx,zz,y);
      if(!f.ok||Math.abs(f.hit.position[1]-p.worldGroundXYZ[1])>.2)return reject('sampled_ledge_margin');
      for(const height of [.25,.75,1.25]){
        const hit=ray([p.worldGroundXYZ[0],p.worldGroundXYZ[1]+height,p.worldGroundXYZ[2]],[dx,0,dz],r+SPEC.offlineCapsuleRadius+SPEC.skin);
        if(hit)return reject('sampled_wall_margin');
      }
    }
    return {accepted:true,worldGroundCenter:center,horizontalDistanceFromCurrent:Math.hypot(x-SEED[0],z-SEED[2]),
      points,maxSlopeDegrees:maxSlope,maxNeighborHeightDelta:maxNeighborDelta,flatProjectedCoverage:coverage,
      denseClearanceSampleCount:samples.size,minimumTestedOverheadClearance:SPEC.overheadClearance,
      wallLedgeMargin:'sampled_16directions_per_registered_point_only',drynessVerified:false,nativeActorY:null,
      robotCanReachCandidate:false,nativeCollisionVerified:false};
  }
  const coarse=[],candidates=[];
  try{
    for(let ix=-20;ix<=20;ix++)for(let iz=-20;iz<=20;iz++){
      if(Math.hypot(ix*SPEC.centerGrid,iz*SPEC.centerGrid)>SPEC.searchRadius)continue;
      const x=SEED[0]+ix*SPEC.centerGrid,z=SEED[2]+iz*SPEC.centerGrid,f=floor(x,z);
      if(!f.ok){failures['coarse_'+f.reason]=(failures['coarse_'+f.reason]??0)+1;continue;}
      if(f.hit.slopeDegrees>SPEC.slopeDegrees)continue;
      if(Math.abs(f.hit.position[1]-SEED[1])>SPEC.maximumCandidateVerticalDifferenceFromSeedActorY)continue;
      coarse.push({center:f.hit.position,slopeDegrees:f.hit.slopeDegrees,distance:Math.hypot(x-SEED[0],z-SEED[2])});
    }
    coarse.sort((a,b)=>a.distance-b.distance||a.center[0]-b.center[0]||a.center[2]-b.center[2]);
    for(const c of coarse){
      if(candidates.some(p=>Math.hypot(c.center[0]-p.worldGroundCenter[0],c.center[2]-p.worldGroundCenter[2])<SPEC.minimumCenterSeparation))continue;
      const result=validate(c.center);tested.push({center:c.center,accepted:result.accepted,reason:result.reason??null});
      if(result.accepted)candidates.push(result);
      if(candidates.length===SPEC.maximumCandidates)break;
    }
    const report={schema:'local-flat-patch-candidates-v1',status:'probed',spec:SPEC,searchSeedWorldXYZ:SEED,
      selectionUsesActorGap:false,assetReferenceInputSHA256:sha(inputRaw),assetDigests:asset.assetDigests,
      geometryMetadata:mesh.metadata,queryRuntime:runtime,rapierFlags:flags,rapierTrianglesRetained:terrain.shape.indices.length/3,
      rayCount,shapeQueryCount:shapeCount,coarseFlatCenterCount:coarse.length,validatedCenterCount:tested.length,
      failures,tested,candidates,nativeMapIdentitySource:'root guarded scene observation; this geometry search is static only',
      nativeCollisionVerified:false,calibrationVerified:false,runtimeReady:false,livePermitted:false,
      deviceAccessed:false,serverAccessed:false,motorExecuted:false,navigationRebuilt:false,offsetFitted:false,
      limitations:['Static candidate meshes can include visual/noncollision/water faces; dry ground requires independent native/visual observation.',
        'All givenY are static ground hitY, NOT avatar anchorY. Do not teleport or write these values as avatar coordinates.',
        'Projected union proves candidate flat face coverage to1e-6m² numeric tolerance; dense queries plus16-direction margin checks are not exhaustive native collision.',
        'No route from current position was planned or executed. Preserve native movement and collect actual finalXYZ.',
        'Only one pinned scene and10m radius searched; thresholds/window never widened after failures.']};
    saveNew(outFile,report);return report;
  }finally{world.free();}
}

if(process.argv[1]&&path.resolve(process.argv[1])===fileURLToPath(import.meta.url)){
  const out=path.resolve(process.argv[2]);const r=await runCandidateSearch(out);
  console.log(JSON.stringify({status:r.status,coarseFlatCenters:r.coarseFlatCenterCount,validated:r.validatedCenterCount,
    candidates:r.candidates.map(c=>({center:c.worldGroundCenter,distance:c.horizontalDistanceFromCurrent,maxSlope:c.maxSlopeDegrees})),
    failures:r.failures,rays:r.rayCount,shapeQueries:r.shapeQueryCount,out}));
}
