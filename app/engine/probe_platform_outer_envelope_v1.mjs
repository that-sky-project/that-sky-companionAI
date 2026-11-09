// Analysis-only supplement: outer .50m envelope and tested upper-plane center.
// No native radius inference, v1 mutation, new preregistration or movement.
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {createHash} from 'node:crypto';
import {loadAsset} from './assets.mjs';
import {preparePreservingGeometry} from './preserving_geometry.mjs';
import {projectedCoverage} from './probe_local_flat_patch_candidates_v1.mjs';
import {expandedHull,trianglePrismIntersection} from './probe_platform_support_diagnostic_v1.mjs';
const ROOT=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const sha=b=>createHash('sha256').update(b).digest('hex'),need=(v,m)=>{if(!v)throw Error(m);};
const cross=(a,b,c)=>(b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0]);
const dot=(a,b)=>a[0]*b[0]+a[1]*b[1],sub=(a,b)=>[a[0]-b[0],a[1]-b[1]];
export function inTriangle(p,t,eps=1e-10){const s=[cross(t[0],t[1],p),cross(t[1],t[2],p),cross(t[2],t[0],p)];return s.every(v=>v>=-eps)||s.every(v=>v<=eps);}
export function pointSegmentDistance(p,a,b){const v=sub(b,a),den=dot(v,v),t=den?Math.max(0,Math.min(1,dot(sub(p,a),v)/den)):0;return Math.hypot(p[0]-a[0]-t*v[0],p[1]-a[1]-t*v[1]);}
export function unionBoundary(triangles){
  const edges=triangles.flatMap(t=>t.map((a,i)=>[a,t[(i+1)%3]])),inside=p=>triangles.some(t=>inTriangle(p,t)),result=new Map();
  for(const [a,b]of edges){
    const r=sub(b,a),rr=dot(r,r);if(rr<1e-20)continue;const ts=[0,1];
    for(const [c,d]of edges){
      const s=sub(d,c),q=sub(c,a),den=r[0]*s[1]-r[1]*s[0];
      if(Math.abs(den)>1e-12){const t=(q[0]*s[1]-q[1]*s[0])/den,u=(q[0]*r[1]-q[1]*r[0])/den;if(t>1e-10&&t<1-1e-10&&u>=-1e-10&&u<=1+1e-10)ts.push(t);}
      else if(Math.abs(q[0]*r[1]-q[1]*r[0])<1e-10)for(const p of [c,d]){const t=dot(sub(p,a),r)/rr;if(t>1e-10&&t<1-1e-10)ts.push(t);}
    }
    ts.sort((a,b)=>a-b);const n=[-r[1]/Math.sqrt(rr),r[0]/Math.sqrt(rr)];
    for(let i=1;i<ts.length;i++){
      if(ts[i]-ts[i-1]<1e-9)continue;const p=a.map((x,k)=>x+r[k]*ts[i-1]),q=a.map((x,k)=>x+r[k]*ts[i]),m=p.map((x,k)=>(x+q[k])/2);
      if(inside(m.map((x,k)=>x+n[k]*1e-5))===inside(m.map((x,k)=>x-n[k]*1e-5)))continue;
      const key=[p,q].map(p=>p.map(x=>x.toFixed(8)).join(',')).sort().join('|');result.set(key,[p,q]);
    }
  }return [...result.values()];
}
function clipY(poly,y,above){const out=[],f=p=>above?p[1]-y:y-p[1];for(let i=0;i<poly.length;i++){const p=poly[i],q=poly[(i+1)%poly.length],a=f(p),b=f(q),ia=a>=0,ib=b>=0;if(ia)out.push(p);if(ia!==ib){const t=a/(a-b);out.push(p.map((x,k)=>x+(q[k]-x)*t));}}return out;}
function inPoly(p,poly){if(poly.length<3)return false;for(let i=1;i<poly.length-1;i++)if(inTriangle(p,[poly[0],poly[i],poly[i+1]]))return true;return false;}
function polygonDistance(p,poly){if(inPoly(p,poly))return 0;return Math.min(...poly.map((a,i)=>pointSegmentDistance(p,a,poly[(i+1)%poly.length])));}
const OFFSETS=[[0,0],[1,0],[0,1],[-1,0],[0,-1],[.75,.75],[-.75,.75],[-.75,-.75],[.75,-.75]];
export function runSupplement(outFile){
  need(!fs.existsSync(outFile),'new_output_required');
  const priorFile=path.join(ROOT,'outputs/platform-upper-support-diagnostic-v1/report.json'),priorRaw=fs.readFileSync(priorFile),prior=JSON.parse(priorRaw),priorSHA=sha(priorRaw);
  const ref=JSON.parse(fs.readFileSync(path.join(ROOT,'outputs/pilot-standing-geometry-v1/input.json'))),asset=loadAsset(path.dirname(ref.assetFile),path.basename(ref.assetFile),{offline:true,expectedManifestSHA:ref.expectedAssetManifestSHA256});
  need(JSON.stringify(asset.assetDigests)===JSON.stringify(prior.assetDigests),'asset_binding');
  const mesh=preparePreservingGeometry({positions:asset.positions,indices:asset.indices},prior.queryWindow);need(mesh.indices.length/3===prior.retainedTriangles,'retained_mesh_changed');
  const topY=prior.staticUpperPlaneY,flat=prior.flatUpperTrianglesForSupportAnalysisOnly.map(t=>t.vertices.map(p=>[p[0],p[2]])),boundary=unionBoundary(flat);
  const minY=topY+.05-.015-.005,maxY=topY+1.5+.05+.015+.005,triangles=[],obstacles=[];
  for(let t=0;t<mesh.indices.length;t+=3){
    const p=[0,1,2].map(k=>{const j=mesh.indices[t+k]*3;return mesh.frame.toWorldPosition([mesh.positions[j],mesh.positions[j+1],mesh.positions[j+2]]);});
    if(Math.max(...p.map(v=>v[1]))<minY||Math.min(...p.map(v=>v[1]))>maxY)continue;
    const row={triangle:t/3,sourceTriangle:mesh.sourceTriangles[t/3],p};triangles.push(row);
    const clipped=clipY(clipY(p,minY,true),maxY,false);if(clipped.length)obstacles.push(clipped.map(v=>[v[0],v[2]]));
  }
  need(boundary.length>0,'no_upper_boundary');
  const radius=p=>{
    if(!flat.some(t=>inTriangle(p,t)))return 0;
    const support=Math.min(...boundary.map(([a,b])=>pointSegmentDistance(p,a,b))),volume=Math.min(...obstacles.map(poly=>polygonDistance(p,poly)));
    return Math.min(support,volume);
  };
  const vs=flat.flat(),lo=[Math.min(...vs.map(p=>p[0])),Math.min(...vs.map(p=>p[1]))],hi=[Math.max(...vs.map(p=>p[0])),Math.max(...vs.map(p=>p[1]))];
  let best={center:null,geometricRadius:0},count=0;
  function consider(p){count++;const r=radius(p);if(r>best.geometricRadius)best={center:p,geometricRadius:r};}
  for(let x=lo[0];x<=hi[0]+1e-9;x+=.05)for(let z=lo[1];z<=hi[1]+1e-9;z+=.05)consider([x,z]);
  const coarse=[...best.center];for(let ix=-10;ix<=10;ix++)for(let iz=-10;iz<=10;iz++)consider([coarse[0]+ix*.005,coarse[1]+iz*.005]);
  const conservativeRadius=Math.floor((best.geometricRadius-.002)*1000)/1000;
  const testedCenterDiscCoverage=projectedCoverage(expandedHull([best.center],conservativeRadius),flat);
  const testedCenterDiscContacts=triangles.filter(t=>trianglePrismIntersection(t.p,expandedHull([best.center],conservativeRadius),minY,maxY));
  // Circumscribed polygon can exceed radius by<.3%; apply a separate2mm reserve and require bothchecks.
  const centerDiscClear=testedCenterDiscCoverage.uncoveredArea<=1e-6&&testedCenterDiscContacts.length===0;
  const cases=[];
  for(const [label,center]of [['observed',prior.centerObservedActorXYZ.filter((_,i)=>i!==1)],['largest_tested',best.center]]){
    for(const radiusValue of [.25,.50])for(const braking of [.10,.25]){
      const required=.15+radiusValue+.015+braking,hull=expandedHull(OFFSETS.map(([x,z])=>[center[0]+x,center[1]+z]),required),coverage=projectedCoverage(hull,flat),contacts=[];
      for(const t of triangles)if(trianglePrismIntersection(t.p,hull,minY,maxY)){contacts.push({triangle:t.triangle,sourceTriangle:t.sourceTriangle});if(contacts.length===20)break;}
      cases.push({centerLabel:label,centerWorldGroundXYZ:[center[0],topY,center[1]],horizontalOuterRadius:radiusValue,
        radiusMeaning:radiusValue===.5?'conservative_outer_hypothesis_not_native_radius':'unchanged_offline_default_not_native_radius',
        brakingMargin:braking,pointTolerance:.15,skin:.015,sameNinePointOffsets:true,requiredSupportHullXZ:hull,
        uncoveredArea:coverage.uncoveredArea,volumeObstruction:contacts.length>0,contactExamples:contacts,
        allStaticGeometryConditionsMet:coverage.uncoveredArea<=1e-6&&contacts.length===0,livePermit:false});
    }
  }
  const proposedBudgets=[.25,.50].flatMap(r=>[.10,.25].map(b=>({outerRadius:r,brakingMargin:b,pointTolerance:.15,skin:.015,
    maximumCenterExcursionDiscCandidate:Math.max(0,conservativeRadius-r-b-.15-.015),
    originalOneMeterNinePointPatternFits:false,measuredStoppingDistance:false})));
  const result={schema:'platform-outer-envelope-diagnostic-v1',sourceDiagnosticSHA256:priorSHA,
    originalV1SearchAndDiagnosticUnmodified:sha(fs.readFileSync(priorFile))===priorSHA,
    queryWindow:prior.queryWindow,assetDigests:asset.assetDigests,retainedAllCollisionTriangles:true,
    boundarySegmentsXZ:boundary,upperPlaneY:topY,upperFlatTriangleCount:flat.length,
    largestTestedCenter:{worldGroundXYZ:[best.center[0],topY,best.center[1]],testedCenters:count,
      coarseSpacing:.05,fineSpacing:.005,notGlobalOptimalityProof:true,rawMinimumBoundaryOrObstacleDistance:best.geometricRadius,
      conservativeEnvelopeRadius:conservativeRadius,independentlyCheckedCircumscribedDiscCoverage:testedCenterDiscCoverage,
      conservativeVolumeClear:centerDiscClear,volumeContactCount:testedCenterDiscContacts.length,
      actorAnchorY:null},unchangedNinePointEnvelopeCases:cases,possibleSmallerCenterDomainBudgets:proposedBudgets,
    collisionVerticalEnvelope:{minY,maxY,heightUnchanged:1.5,clearanceLiftUnchanged:.05},
    rawRaysReusedFrom:path.join(ROOT,'outputs/platform-upper-support-diagnostic-v1/rays.json'),newRays:0,newRapierShapeQueries:0,
    actorGapUsed:false,motorRadiusChanged:false,nativeRadiusVerified:false,preregistrationV2Created:false,
    nativeCollisionVerified:false,calibrationVerified:false,runtimeReady:false,livePermitted:false,deviceAccessed:false,serverAccessed:false,
    interpretation:['A larger.50m horizontal envelope is a conservative planning hypothesis only; the .497m observed verticalgap is not a radius measurement.',
      'Original ninepoint offsets and completedv1 results are not changed. Additional .10/.25m braking margins are geometry buffers, not measured stopping distances.',
      'A smaller proposed center excursion requires a separately frozenv2, new distinct train/holdout points and actual dry/native support/stop checks.',
      'Free-disc bound uses all upper-plane boundary segments and all preserved triangles crossing the same conservative vertical slab; it is not a native capsule reconstruction.',
      'ReportedY remains static top-plane height, never an avatar teleport coordinate.']};
  const tmp=outFile+'.pending';fs.writeFileSync(tmp,JSON.stringify(result,null,2)+'\n',{flag:'wx'});fs.renameSync(tmp,outFile);return result;
}
if(process.argv[1]&&path.resolve(process.argv[1])===fileURLToPath(import.meta.url)){
  const r=runSupplement(path.resolve(process.argv[2]));console.log(JSON.stringify({largestTestedCenter:r.largestTestedCenter,
    cases:r.unchangedNinePointEnvelopeCases.map(c=>({center:c.centerLabel,r:c.horizontalOuterRadius,b:c.brakingMargin,missing:c.uncoveredArea,obstruction:c.volumeObstruction})),
    smallerBudgets:r.possibleSmallerCenterDomainBudgets}));
}
