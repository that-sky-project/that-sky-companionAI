// Geometry validation of root-proposed v2. No live enablement or v1 replacement.
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {createHash} from 'node:crypto';
import RAPIER from '@dimforge/rapier3d-compat';
import {loadAsset} from './assets.mjs';
import {preparePreservingGeometry} from './preserving_geometry.mjs';
import {assertPinnedCollisionRuntime} from './pinned_collision_runtime.mjs';
import {projectedCoverage} from './probe_local_flat_patch_candidates_v1.mjs';
import {expandedHull,convexHull,trianglePrismIntersection} from './probe_platform_support_diagnostic_v1.mjs';
const ROOT=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const need=(v,m)=>{if(!v)throw Error(m);},sha=b=>createHash('sha256').update(b).digest('hex');
const SPEC={trainingAxisOffset:.4,holdoutDiagonalOffset:.3,positionTolerance:.10,
  conservativeOuterRadius:.50,unchangedMotorRadius:.25,skin:.015,brakingMargin:.15,
  height:1.5,clearanceLift:.05,slopeDegrees:3,samePlaneTolerance:.005,
  overheadClearance:2,maxRouteMeters:.8,initialSpeedMetersPerSecond:.25,uncoveredAreaTolerance:1e-6};
const OFFSETS=[['T0',0,0],['T1',.4,0],['T2',0,.4],['T3',-.4,0],['T4',0,-.4],
  ['H0',.3,.3],['H1',-.3,.3],['H2',-.3,-.3],['H3',.3,-.3]];
export async function validateV2(outFile){
  need(!fs.existsSync(outFile),'new_output_required');
  const oldFiles=['outputs/local-flat-patch-candidates-v1/report.json','outputs/platform-upper-support-diagnostic-v1/report.json','outputs/platform-upper-support-diagnostic-v1/outer-envelope.json'];
  const pins=Object.fromEntries(oldFiles.map(p=>[p,sha(fs.readFileSync(path.join(ROOT,p)))]));
  const prior=JSON.parse(fs.readFileSync(path.join(ROOT,oldFiles[1]))),outer=JSON.parse(fs.readFileSync(path.join(ROOT,oldFiles[2])));
  const center=outer.largestTestedCenter.worldGroundXYZ;need(outer.largestTestedCenter.conservativeVolumeClear,'center_not_proven_geometry');
  const ref=JSON.parse(fs.readFileSync(path.join(ROOT,'outputs/pilot-standing-geometry-v1/input.json'))),asset=loadAsset(path.dirname(ref.assetFile),path.basename(ref.assetFile),{offline:true,expectedManifestSHA:ref.expectedAssetManifestSHA256});
  need(JSON.stringify(asset.assetDigests)===JSON.stringify(prior.assetDigests),'asset_binding');
  const mesh=preparePreservingGeometry({positions:asset.positions,indices:asset.indices},prior.queryWindow);
  need(mesh.indices.length/3===prior.retainedTriangles,'mesh_changed');
  await RAPIER.init();const runtime=assertPinnedCollisionRuntime(),world=new RAPIER.World({x:0,y:0,z:0});
  const flags=RAPIER.TriMeshFlags.FIX_INTERNAL_EDGES_TWO_SIDED;
  need(!(flags&(RAPIER.TriMeshFlags.DELETE_DEGENERATE_TRIANGLES|RAPIER.TriMeshFlags.DELETE_DUPLICATE_TRIANGLES|RAPIER.TriMeshFlags.DELETE_BAD_TOPOLOGY_TRIANGLES)),'delete_face_flag');
  const terrain=world.createCollider(RAPIER.ColliderDesc.trimesh(mesh.positions,mesh.indices,flags));world.step();
  need(terrain.shape.indices.length===mesh.indices.length,'rapier_face_count');
  const rays=[],v=a=>({x:a[0],y:a[1],z:a[2]});let shapes=0;
  function ray(o,d,max){const local=mesh.frame.toLocalPosition(o),h=world.castRayAndGetNormal(new RAPIER.Ray(v(local),v(d)),max,true,undefined,undefined,undefined,undefined,c=>c===terrain);
    const r={origin:o,direction:d,maxDistance:max,hit:null};if(h){const n=[h.normal.x,h.normal.y,h.normal.z];r.hit={position:mesh.frame.toWorldPosition(local.map((x,i)=>x+d[i]*h.timeOfImpact)),normal:n,distance:h.timeOfImpact,slopeDegrees:Math.acos(Math.min(1,Math.abs(n[1])/Math.hypot(...n)))*180/Math.PI};}rays.push(r);return r;}
  try{
    const points=OFFSETS.map(([id,dx,dz])=>{
      const x=center[0]+dx,z=center[2]+dz,down=ray([x,center[1]+.5,z],[0,-1,0],8),above=ray([x,center[1]+2,z],[0,-1,0],8),up=ray([x,center[1]+.04,z],[0,1,0],SPEC.overheadClearance),lower=ray([x,center[1]-.005,z],[0,-1,0],4);
      const shapeCases=[];for(const radius of [SPEC.unchangedMotorRadius,SPEC.conservativeOuterRadius]){
        let overlaps=0;for(let k=-1;k<16;k++){const a=k*Math.PI/8,p=mesh.frame.toLocalPosition([x+(k<0?0:.1*Math.cos(a)),center[1]+.75+.05,z+(k<0?0:.1*Math.sin(a))]);
          const shape=new RAPIER.Capsule((SPEC.height-2*radius)/2,radius+SPEC.skin);shapes++;
          if(world.intersectionWithShape(v(p),{x:0,y:0,z:0,w:1},shape,undefined,undefined,undefined,undefined,c=>c===terrain))overlaps++;}
        shapeCases.push({radius,tests:17,overlaps,nativeRadiusVerified:false});}
      const layerPass=!!down.hit&&!!above.hit&&[down.hit,above.hit].every(h=>Math.abs(h.position[1]-center[1])<=.005&&h.slopeDegrees<=3);
      return {id,split:id.startsWith('T')?'train':'holdout',targetWorldXZ:[x,z],staticGroundY:down.hit?.position[1]??null,nativeActorY:null,
        down,fromAbove:above,up,firstLowerLayer:lower,lowerLayerGap:lower.hit?center[1]-lower.hit.position[1]:null,
        upperLayerAndSlopePass:layerPass,overheadPass:!up.hit,shapeCases};
    });
    const flat=prior.flatUpperTrianglesForSupportAnalysisOnly.map(t=>t.vertices.map(p=>[p[0],p[2]])),allTriangles=[];
    for(let t=0;t<mesh.indices.length;t+=3){const p=[0,1,2].map(k=>{const j=mesh.indices[t+k]*3;return mesh.frame.toWorldPosition([mesh.positions[j],mesh.positions[j+1],mesh.positions[j+2]]);});allTriangles.push({sourceTriangle:mesh.sourceTriangles[t/3],p,minY:Math.min(...p.map(q=>q[1])),maxY:Math.max(...p.map(q=>q[1]))});}
    const envelopes=[];for(const radius of [SPEC.unchangedMotorRadius,SPEC.conservativeOuterRadius]){
      const pad=SPEC.positionTolerance+radius+SPEC.skin+SPEC.brakingMargin,hull=expandedHull(points.map(p=>p.targetWorldXZ),pad),coverage=projectedCoverage(hull,flat),minY=center[1]+.05-.015-.005,maxY=center[1]+1.5+.05+.015+.005,contacts=[];
      for(const t of allTriangles)if(t.maxY>=minY&&t.minY<=maxY&&trianglePrismIntersection(t.p,hull,minY,maxY))contacts.push(t.sourceTriangle);
      envelopes.push({radius,totalSupportPadding:pad,requiredUpperSupportHullXZ:hull,uncoveredArea:coverage.uncoveredArea,
        supportPass:coverage.uncoveredArea<=SPEC.uncoveredAreaTolerance,conservativeVolumeEnvelope:{minY,maxY},obstructionCount:contacts.length,obstructionSourceTriangles:contacts,
        volumePass:contacts.length===0,nativeRadiusVerified:false});
    }
    const legs=points.slice(1).map((p,i)=>({from:points[i].id,to:p.id,distance:Math.hypot(p.targetWorldXZ[0]-points[i].targetWorldXZ[0],p.targetWorldXZ[1]-points[i].targetWorldXZ[1])}));
    const geometryPass=points.every(p=>p.upperLayerAndSlopePass&&p.overheadPass&&p.shapeCases.every(c=>c.overlaps===0))&&envelopes.every(e=>e.supportPass&&e.volumePass)&&legs.every(l=>l.distance<=SPEC.maxRouteMeters);
    const result={schema:'platform-v2-geometric-candidate-v1',proposalFrom:'root explicit0.4/0.3pattern,.10tolerance,.50outerradius,.015skin,.15braking',spec:SPEC,
      geometryPass,worldGroundCenter:center,actorAnchorY:null,allowedCenterHullXZ:convexHull(points.map(p=>p.targetWorldXZ)),queryWindow:prior.queryWindow,
      points,envelopes,registeredSequence:points.map(p=>p.id),routeLegs:legs,maxSequenceLeg:Math.max(...legs.map(l=>l.distance)),
      minimumDistinctTargetSeparation:Math.min(...points.flatMap((p,i)=>points.slice(i+1).map(q=>Math.hypot(p.targetWorldXZ[0]-q.targetWorldXZ[0],p.targetWorldXZ[1]-q.targetWorldXZ[1])))),
      sourcePins:pins,oldResultsUnmodified:oldFiles.every(p=>sha(fs.readFileSync(path.join(ROOT,p)))===pins[p]),assetDigests:asset.assetDigests,
      queryRuntime:runtime,retainedTriangleCount:terrain.shape.indices.length/3,faceDeletionFlags:false,rayCount:rays.length,shapeQueryCount:shapes,
      nativeRadiusVerified:false,positionToleranceVerified:false,brakingDistanceVerified:false,calibrationSamplesCollected:0,
      nativeCollisionVerified:false,calibrationVerified:false,runtimeReady:false,livePermitted:false,
      geometryCandidateOnly:true,v2FrozenByThisScript:false,deviceAccessed:false,serverAccessed:false,motorModified:false,motorExecuted:false,
      limitations:['This is a new geometry candidate, not reinterpretation of failedv1 data.',
        'Root must separately freezev2 protocol and measure actual joystick/stop behavior before using the .10position and .15brake budgets.',
        'Originalv1 .5m minimum target separation is not met by this deliberately smaller proposal; v2must explicitly declare the new min spacing and retain train/holdout independence.',
        'AllY values are static ground-plane heights. Never install them as avatar coordinates; avatar Y remains under the native client.',
        'r=.50 is a conservative horizontal envelope hypothesis, not native anatomy inferred from actor-topgap.',
        'Finite ray/shape checks plus preserved-mesh projected union/prism exclusion establish only this static candidate geometry. Dryness/native support/session identity are separate.']};
    const tmp=outFile+'.pending';fs.writeFileSync(tmp,JSON.stringify(result,null,2)+'\n',{flag:'wx'});fs.renameSync(tmp,outFile);return result;
  }finally{world.free();}
}
if(process.argv[1]&&path.resolve(process.argv[1])===fileURLToPath(import.meta.url)){
  const r=await validateV2(path.resolve(process.argv[2]));console.log(JSON.stringify({geometryPass:r.geometryPass,center:r.worldGroundCenter,
    targets:r.points.map(p=>({id:p.id,xz:p.targetWorldXZ,layer:p.upperLayerAndSlopePass,clear:p.overheadPass})),
    envelopes:r.envelopes.map(e=>({r:e.radius,pad:e.totalSupportPadding,uncovered:e.uncoveredArea,obstacles:e.obstructionCount})),
    maxLeg:r.maxSequenceLeg,minSeparation:r.minimumDistinctTargetSeparation,rays:r.rayCount,shapes:r.shapeQueryCount,oldUnchanged:r.oldResultsUnmodified}));
}
