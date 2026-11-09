// Independent upper-platform diagnostic; does not change the failed v1 search.
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {createHash} from 'node:crypto';
import RAPIER from '@dimforge/rapier3d-compat';
import {loadAsset} from './assets.mjs';
import {preparePreservingGeometry} from './preserving_geometry.mjs';
import {assertPinnedCollisionRuntime} from './pinned_collision_runtime.mjs';
import {projectedCoverage} from './probe_local_flat_patch_candidates_v1.mjs';

const ROOT=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const need=(v,m)=>{if(!v)throw Error(m);},sha=b=>createHash('sha256').update(b).digest('hex');
const CENTER=[1.28225708,1.153104544,-.5034486055];
const OFFSETS=[['T0',0,0],['T1',1,0],['T2',0,1],['T3',-1,0],['T4',0,-1],['H0',.75,.75],['H1',-.75,.75],['H2',-.75,-.75],['H3',.75,-.75]];
const SPEC={slopeDegrees:3,samePlaneTolerance:.005,pointTolerance:.15,radius:.25,skin:.015,height:1.5,
  clearanceLift:.05,overheadClearance:2,radialDirections:32,radialStep:.05,radialMaxDistance:3,
  polygonCircleSides:48,brakingMargins:[0,.05,.10,.15,.25,.50],queryWindowHalfSize:6,
  maximumRays:6000,maximumVolumeContactsRecorded:20,supportUncoveredAreaTolerance:1e-6};
const cross=(a,b,c)=>(b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0]);
export function convexHull(points){
  const ps=[...new Map(points.map(p=>[JSON.stringify(p),p])).values()].sort((a,b)=>a[0]-b[0]||a[1]-b[1]);
  if(ps.length<3)return ps;
  const half=a=>{const h=[];for(const p of a){while(h.length>=2&&cross(h[h.length-2],h[h.length-1],p)<=0)h.pop();h.push(p);}return h;};
  return half(ps).slice(0,-1).concat(half([...ps].reverse()).slice(0,-1));
}
export function expandedHull(centers,radius){
  const r=radius/Math.cos(Math.PI/SPEC.polygonCircleSides),points=[];
  for(const c of centers)for(let i=0;i<SPEC.polygonCircleSides;i++){
    const a=(i+.5)*2*Math.PI/SPEC.polygonCircleSides;points.push([c[0]+r*Math.cos(a),c[1]+r*Math.sin(a)]);}
  return convexHull(points);
}
function clip3(poly,f){
  const out=[];for(let i=0;i<poly.length;i++){
    const p=poly[i],q=poly[(i+1)%poly.length],a=f(p),b=f(q),ia=a>=0,ib=b>=0;
    if(ia)out.push(p);if(ia!==ib){const t=a/(a-b);out.push(p.map((x,k)=>x+(q[k]-x)*t));}
  }return out;
}
export function trianglePrismIntersection(triangle,hull,minY,maxY){
  let p=clip3(triangle,v=>v[1]-minY);if(!p.length)return false;
  p=clip3(p,v=>maxY-v[1]);if(!p.length)return false;
  for(let i=0;i<hull.length&&p.length;i++){const a=hull[i],b=hull[(i+1)%hull.length];p=clip3(p,v=>cross(a,b,[v[0],v[2]]));}
  return p.length>0;
}
function save(file,data){need(!fs.existsSync(file),'new_output_required');fs.mkdirSync(path.dirname(file),{recursive:true});const tmp=file+'.pending';fs.writeFileSync(tmp,JSON.stringify(data,null,2)+'\n',{flag:'wx'});fs.renameSync(tmp,file);}
export async function diagnose(outDir){
  const reportFile=path.join(outDir,'report.json'),raysFile=path.join(outDir,'rays.json');need(!fs.existsSync(reportFile)&&!fs.existsSync(raysFile),'new_outputs_required');
  const oldFile=path.join(ROOT,'outputs/local-flat-patch-candidates-v1/report.json'),oldRaw=fs.readFileSync(oldFile),oldSHA=sha(oldRaw);
  const ref=JSON.parse(fs.readFileSync(path.join(ROOT,'outputs/pilot-standing-geometry-v1/input.json')));
  const asset=loadAsset(path.dirname(ref.assetFile),path.basename(ref.assetFile),{offline:true,expectedManifestSHA:ref.expectedAssetManifestSHA256});
  need(asset.manifest.mapName==='CandleSpace','map');await RAPIER.init();const runtime=assertPinnedCollisionRuntime();
  const h=SPEC.queryWindowHalfSize,window={minX:CENTER[0]-h,maxX:CENTER[0]+h,minZ:CENTER[2]-h,maxZ:CENTER[2]+h};
  const mesh=preparePreservingGeometry({positions:asset.positions,indices:asset.indices},window),world=new RAPIER.World({x:0,y:0,z:0});
  const flags=RAPIER.TriMeshFlags.FIX_INTERNAL_EDGES_TWO_SIDED;
  need(!(flags&(RAPIER.TriMeshFlags.DELETE_DEGENERATE_TRIANGLES|RAPIER.TriMeshFlags.DELETE_DUPLICATE_TRIANGLES|RAPIER.TriMeshFlags.DELETE_BAD_TOPOLOGY_TRIANGLES)),'delete_face_flag');
  const terrain=world.createCollider(RAPIER.ColliderDesc.trimesh(mesh.positions,mesh.indices,flags));world.step();need(terrain.shape.indices.length===mesh.indices.length,'face_count_changed');
  const rays=[],cache=new Map();let shapes=0;
  const vec=a=>({x:a[0],y:a[1],z:a[2]});
  function ray(origin,direction,maxDistance){
    const key=JSON.stringify([origin,direction,maxDistance]);if(cache.has(key))return cache.get(key);
    need(rays.length<SPEC.maximumRays,'ray_budget');mesh.coverage.assertWorldCapsule(origin,{radius:0,skin:0});
    const o=mesh.frame.toLocalPosition(origin),hit=world.castRayAndGetNormal(new RAPIER.Ray(vec(o),vec(direction)),maxDistance,true,undefined,undefined,undefined,undefined,c=>c===terrain);
    const r={id:rays.length,origin,direction,maxDistance,hit:null};
    if(hit){const n=[hit.normal.x,hit.normal.y,hit.normal.z],t=hit.timeOfImpact;
      need(Number.isFinite(t)&&t>=0&&t<=maxDistance&&Math.abs(Math.hypot(...n)-1)<.001,'invalid_hit');
      r.hit={position:mesh.frame.toWorldPosition(o.map((v,i)=>v+direction[i]*t)),normal:n,distance:t,
        slopeDegrees:Math.acos(Math.min(1,Math.abs(n[1])/Math.hypot(...n)))*180/Math.PI,featureId:hit.featureId??null};}
    rays.push(r);cache.set(key,r);return r;
  }
  function layers(x,z,upperY){
    const entries=[];let y=upperY+2;
    for(let i=0;i<12&&y>upperY-4;i++){
      const r=ray([x,y,z],[0,-1,0],y-(upperY-4));if(!r.hit)break;
      const yy=r.hit.position[1];entries.push({rayId:r.id,y:yy,normal:r.hit.normal,slopeDegrees:r.hit.slopeDegrees,
        gapBelowFirst:entries.length?entries[0].y-yy:0});y=yy-.005;
    }return entries;
  }
  try{
    const centerDown=ray(CENTER,[0,-1,0],8);need(centerDown.hit,'center_floor_missing');const topY=centerDown.hit.position[1];
    const triangles=[],flat=[],flat3=[];
    for(let t=0;t<mesh.indices.length;t+=3){
      const p=[0,1,2].map(k=>{const j=mesh.indices[t+k]*3;return mesh.frame.toWorldPosition([mesh.positions[j],mesh.positions[j+1],mesh.positions[j+2]]);});
      const a=p[1].map((v,k)=>v-p[0][k]),b=p[2].map((v,k)=>v-p[0][k]),n=[a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]];
      const row={triangle:t/3,sourceTriangle:mesh.sourceTriangles[t/3],p,minY:Math.min(...p.map(v=>v[1])),maxY:Math.max(...p.map(v=>v[1]))};triangles.push(row);
      if(Math.abs(n[1])/Math.hypot(...n)>=Math.cos(SPEC.slopeDegrees*Math.PI/180)&&p.every(v=>Math.abs(v[1]-topY)<=SPEC.samePlaneTolerance)){
        flat.push(p.map(v=>[v[0],v[2]]));flat3.push(row);}
    }
    const points=OFFSETS.map(([id,dx,dz])=>{
      const x=CENTER[0]+dx,z=CENTER[2]+dz,down=ray([x,topY+.5,z],[0,-1,0],8),above=ray([x,topY+2,z],[0,-1,0],8),up=ray([x,topY+.04,z],[0,1,0],SPEC.overheadClearance);
      const footprint=expandedHull([[x,z]],SPEC.pointTolerance+SPEC.radius+SPEC.skin),coverage=projectedCoverage(footprint,flat);
      let overlaps=0;for(let k=-1;k<16;k++){
        const a=k*Math.PI/8,xx=x+(k<0?0:SPEC.pointTolerance*Math.cos(a)),zz=z+(k<0?0:SPEC.pointTolerance*Math.sin(a));
        const p=mesh.frame.toLocalPosition([xx,topY+SPEC.height/2+SPEC.clearanceLift,zz]),shape=new RAPIER.Capsule((SPEC.height-2*SPEC.radius)/2,SPEC.radius+SPEC.skin);shapes++;
        if(world.intersectionWithShape(vec(p),{x:0,y:0,z:0,w:1},shape,undefined,undefined,undefined,undefined,c=>c===terrain))overlaps++;
      }
      return {id,split:id.startsWith('T')?'train':'holdout',targetWorldXZ:[x,z],staticGroundXYZ:down.hit?.position??null,
        downRayId:down.id,fromAboveRayId:above.id,upRayId:up.id,
        sameUpperLayer:!!down.hit&&!!above.hit&&Math.abs(down.hit.position[1]-topY)<=SPEC.samePlaneTolerance&&Math.abs(down.hit.position[1]-above.hit.position[1])<=SPEC.samePlaneTolerance,
        downSlope:down.hit?.slopeDegrees??null,overheadClear:!up.hit,layers:layers(x,z,topY),
        toleranceAndCapsuleFootprint:coverage,shapeTests:17,shapeOverlaps:overlaps};
    });
    const radial=[];
    for(let k=0;k<SPEC.radialDirections;k++){
      const a=k*2*Math.PI/SPEC.radialDirections;let last=null,firstOutside=null;
      for(let step=0;step<=Math.round(SPEC.radialMaxDistance/SPEC.radialStep);step++){
        const r=step*SPEC.radialStep,x=CENTER[0]+r*Math.cos(a),z=CENTER[2]+r*Math.sin(a),q=ray([x,topY+.5,z],[0,-1,0],8);
        const onTop=!!q.hit&&Math.abs(q.hit.position[1]-topY)<=SPEC.samePlaneTolerance&&q.hit.slopeDegrees<=SPEC.slopeDegrees;
        const entry={radius:r,worldXZ:[x,z],rayId:q.id,hitY:q.hit?.position[1]??null,slopeDegrees:q.hit?.slopeDegrees??null};
        if(!onTop){firstOutside=entry;break;}last=entry;
      }radial.push({angleDegrees:a*180/Math.PI,lastUpperPlane:last,firstNonUpperPlane:firstOutside});
    }
    const centers=points.map(p=>p.targetWorldXZ),centerHull=convexHull(centers),braking=[];
    for(const b of SPEC.brakingMargins){
      const supportHull=expandedHull(centers,SPEC.pointTolerance+SPEC.radius+SPEC.skin+b),coverage=projectedCoverage(supportHull,flat);
      const minY=topY+SPEC.clearanceLift-SPEC.skin-SPEC.samePlaneTolerance,maxY=topY+SPEC.height+SPEC.clearanceLift+SPEC.skin+SPEC.samePlaneTolerance,contacts=[];
      for(const t of triangles){
        if(t.maxY<minY||t.minY>maxY)continue;
        if(trianglePrismIntersection(t.p,supportHull,minY,maxY)){contacts.push({triangle:t.triangle,sourceTriangle:t.sourceTriangle});if(contacts.length>=SPEC.maximumVolumeContactsRecorded)break;}
      }
      braking.push({additionalBrakingMargin:b,requiredSupportHullXZ:supportHull,coverage,
        continuousProjectedUpperSupport:coverage.uncoveredArea<=SPEC.supportUncoveredAreaTolerance,
        conservativeCapsuleEnvelope:{minY,maxY},envelopeIntersectsAnyPreservedTriangle:contacts.length>0,contactExamples:contacts,
        motionStoppingDistanceVerified:false,livePermit:false});
    }
    const report={schema:'platform-upper-support-diagnostic-v1',spec:SPEC,centerObservedActorXYZ:CENTER,staticUpperPlaneY:topY,
      selectionUsedActorFloorGap:false,queryWindow:window,queryWindowMayContainOuterSlopes:true,
      allowedCenterHullXZ:centerHull,allowedCenterHullIsOnlyProposed:false,
      domainStatus:'diagnostic proposal only; no v2 registered or approved here',
      fullCollisionGeometryMetadata:mesh.metadata,assetDigests:asset.assetDigests,queryRuntime:runtime,rapierFlags:flags,
      retainedTriangles:terrain.shape.indices.length/3,flatUpperTrianglesForSupportAnalysisOnly:flat3.map(t=>({triangle:t.triangle,sourceTriangle:t.sourceTriangle,vertices:t.p})),
      centerDownRayId:centerDown.id,points,radialBoundaryBrackets:radial,brakingMarginGeometry:braking,
      rayCount:rays.length,shapeQueryCount:shapes,v1SearchReportSHA256:oldSHA,v1SourceAndResultUnmodified:sha(fs.readFileSync(oldFile))===oldSHA,
      priorRejectionCause:'Nearest v1 center passed9point slope/layer checks, failed entire2.83m square projected coverage; not rejected just for lower soil.',
      nativeCollisionVerified:false,calibrationVerified:false,runtimeReady:false,livePermitted:false,deviceAccessed:false,serverAccessed:false,
      motorExecuted:false,avatarYOffsetInstalled:false,preregistrationV2Created:false,
      limitations:['Upper-plane triangle selection is an analysis set only. The full collider retains all lower layers, sidewalls and positive-area thin faces.',
        'Lower soil separated below the top is not by itself ambiguous upper support; report all raw layers without changingv1 failures.',
        '48-sided circumscribed circle envelopes conservatively contain radius+skin+tolerance. Capsule/envelope dimensions are offline defaults, not native anatomy.',
        'Prism triangle-intersection absence is sufficient static volume clearance for this envelope only. All slopes and heights remain asv1.',
        'Radial boundaries are0.05m brackets in32directions, not an exact native walkable boundary.',
        'Braking margins are diagnostic geometry cases, not measured stopping distances or approval to move. Root must freeze anyv2 before new calibration data.',
        'Static groundY must not be written as avatar anchorY; collect actual native positions after ordinary movement.',
        'Water/dryness, animation support, native contact and session/UDP association remain separately unverified.']};
    save(raysFile,{schema:'platform-diagnostic-raw-rays-v1',queryRuntime:runtime,rays});save(reportFile,report);
    return report;
  }finally{world.free();}
}
if(process.argv[1]&&path.resolve(process.argv[1])===fileURLToPath(import.meta.url)){
  const r=await diagnose(path.resolve(process.argv[2]));
  console.log(JSON.stringify({topY:r.staticUpperPlaneY,allNineUpper:r.points.every(p=>p.sameUpperLayer),
    allNineFootprintsCovered:r.points.every(p=>p.toleranceAndCapsuleFootprint.uncoveredArea<=1e-6),
    shapeOverlaps:r.points.reduce((s,p)=>s+p.shapeOverlaps,0),
    marginCases:r.brakingMarginGeometry.map(p=>({b:p.additionalBrakingMargin,uncovered:p.coverage.uncoveredArea,volumeObstruction:p.envelopeIntersectsAnyPreservedTriangle})),
    radialMinInside:Math.min(...r.radialBoundaryBrackets.map(p=>p.lastUpperPlane?.radius??0)),
    rays:r.rayCount,shapes:r.shapeQueryCount,v1Unchanged:r.v1SourceAndResultUnmodified}));
}
