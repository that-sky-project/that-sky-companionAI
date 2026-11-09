// Offline representation: retain short original faces; condition only large faces.
// Query/permission coverage remains separate from physical geometry overhang.
import {LocalFrame,LOCAL_FRAME_LIMITS} from './local_frame.mjs';
import {chooseDyadicOrigin,createExactSourceTranslationChecker} from './dyadic_origin.mjs';
import {CLIPPED_GEOMETRY_LIMITS,geometryRecord,validateGeometryBuffer,clipPolygonPlane,createCollisionCoverage} from './clipped_geometry.mjs';
export const PRESERVING_ORIGIN_POLICY='zero-facing-32-retain-short-v1';
export const WHOLE_TRIANGLE_MAXIMUM_EDGE=32;
const FLAGS=Object.freeze({nativeCollisionVerified:false,calibrationVerified:false,runtimeReady:false,live_enabled:false});
const need=(ok,why)=>{if(!ok)throw Error('preserving_geometry:'+why);};
const sub=(a,b)=>a.map((x,k)=>x-b[k]);
const dot=(a,b)=>a.reduce((s,x,k)=>s+x*b[k],0);
const cross=(a,b)=>[a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]];
const normal=t=>cross(sub(t[1],t[0]),sub(t[2],t[0]));
const norm=v=>Math.hypot(...v);
const finite=x=>typeof x==='number'&&Number.isFinite(x)&&Math.abs(x)<LOCAL_FRAME_LIMITS.geometryCoordinate;
const blankBounds=()=>({lo:[Infinity,Infinity,Infinity],hi:[-Infinity,-Infinity,-Infinity]});
const addBounds=(b,points)=>{for(const p of points)for(let k=0;k<3;k++){b.lo[k]=Math.min(b.lo[k],p[k]);b.hi[k]=Math.max(b.hi[k],p[k]);}};
const freezeBounds=b=>Object.freeze({lo:Object.freeze([...b.lo]),hi:Object.freeze([...b.hi])});
// Used only when the Number normal says zero. Exact dyadic arithmetic prevents
// an extremely thin, nonzero source/fan from being silently discarded.
const bits=new DataView(new ArrayBuffer(8));
function dyadic(x){bits.setFloat64(0,x,false);const b=bits.getBigUint64(0,false),e=Number((b>>52n)&2047n),m=b&((1n<<52n)-1n);return [(b>>63n?-1n:1n)*(e?(1n<<52n)+m:m),e?e-1075:-1074];}
function minus(a,b){const e=Math.min(a[1],b[1]);return [(a[0]<<BigInt(a[1]-e))-(b[0]<<BigInt(b[1]-e)),e];}
const times=(a,b)=>[a[0]*b[0],a[1]+b[1]];
function exactZero(t){const q=t.map(p=>p.map(dyadic)),a=q[1].map((x,k)=>minus(x,q[0][k])),b=q[2].map((x,k)=>minus(x,q[0][k]));return [[1,2],[2,0],[0,1]].every(([j,k])=>minus(times(a[j],b[k]),times(a[k],b[j]))[0]===0n);}
function isNonzero(t,n,reason){if(norm(n)>0)return true;need(exactZero(t),reason);return false;}
export function preparePreservingGeometry(geometry,worldWindow,options={}){
  const input=geometryRecord(geometry,['positions','indices'],[],'geometry');
  const pv=validateGeometryBuffer(input.positions,Float32Array,CLIPPED_GEOMETRY_LIMITS.positionElements,'positions');
  const iv=validateGeometryBuffer(input.indices,Uint32Array,CLIPPED_GEOMETRY_LIMITS.indexElements,'indices');
  need(pv.buffer!==iv.buffer,'input_buffers_alias');
  const positions=input.positions,indices=input.indices;
  for(const x of positions)need(finite(x),'source_position_nonfinite_or_range');
  for(const x of indices)need(x<positions.length/3,'source_index_bounds');
  const window=Object.freeze(new LocalFrame({origin:[0,0,0]}).toLocalWindow(worldWindow));
  need(window!==null,'explicit_window_required');
  const opt=geometryRecord(options,[],['maxOutputTriangles'],'options');
  const maximum=Object.hasOwn(opt,'maxOutputTriangles')?opt.maxOutputTriangles:CLIPPED_GEOMETRY_LIMITS.outputTriangles;
  need(Number.isInteger(maximum)&&maximum>0&&maximum<=CLIPPED_GEOMETRY_LIMITS.outputTriangles,'output_triangle_budget_option');
  function scan(visit){
    const retained=blankBounds(),domain=blankBounds();
    let touched=0,wholeCount=0,longCount=0,sourceZero=0,fanZero=0,outputCount=0,overhang=0;
    for(let t=0;t<indices.length/3;t++){
      const ids=[indices[t*3]*3,indices[t*3+1]*3,indices[t*3+2]*3];
      if(Math.max(...ids.map(n=>positions[n]))<window.minX||Math.min(...ids.map(n=>positions[n]))>window.maxX||Math.max(...ids.map(n=>positions[n+2]))<window.minZ||Math.min(...ids.map(n=>positions[n+2]))>window.maxZ)continue;
      touched++;const source=ids.map(n=>[positions[n],positions[n+1],positions[n+2]]),N=normal(source);
      need(Number.isFinite(norm(N)),'source_normal_nonfinite');
      if(!isNonzero(source,N,'source_normal_number_zero_but_exact_nonzero')){sourceZero++;continue;}
      let clipped=source;
      for(const [axis,bound,minimum] of [[0,window.minX,true],[0,window.maxX,false],[2,window.minZ,true],[2,window.maxZ,false]])clipped=clipPolygonPlane(clipped,axis,bound,minimum);
      if(!clipped.length)continue;
      let positiveDomain=false;
      for(let j=1;j<clipped.length-1;j++){
        const tri=[clipped[0],clipped[j],clipped[j+1]],n=normal(tri);
        // Domain bounds need only exact positive area, not a representable fan.
        if(norm(n)>0||!exactZero(tri))positiveDomain=true;
      }
      if(positiveDomain)addBounds(domain,clipped);
      const longest=Math.max(...source.map((point,k)=>norm(sub(point,source[(k+1)%3]))));
      const whole=longest<=WHOLE_TRIANGLE_MAXIMUM_EDGE;
      if(!whole&&!positiveDomain)continue;
      const polygon=whole?source:clipped;
      let emitted=false;
      for(let j=1;j<polygon.length-1;j++){
        const points=[polygon[0],polygon[j],polygon[j+1]],N64=normal(points);
        if(!isNonzero(points,N64,'fan_normal_number_zero_but_exact_nonzero')){fanZero++;continue;}
        need(dot(N64,N)>0,'float64_winding_changed');
        need(outputCount<maximum,'output_triangle_budget');outputCount++;emitted=true;
        addBounds(retained,points);
        for(const point of points)overhang=Math.max(overhang,window.minX-point[0],point[0]-window.maxX,window.minZ-point[2],point[2]-window.maxZ);
        visit?.({points,source,N,sourceTriangle:t,whole});
      }
      if(emitted){if(whole)wholeCount++;else longCount++;}
    }
    need(outputCount>0&&[...retained.lo,...retained.hi,...domain.lo,...domain.hi].every(finite),'no_geometry_in_coverage');
    need(overhang<=WHOLE_TRIANGLE_MAXIMUM_EDGE,'overhang_source_edge_bound');
    return {retained,domain,touched,wholeCount,longCount,sourceZero,fanZero,outputCount,overhang};
  }
  const first=scan(),origin=chooseDyadicOrigin(first.retained),frame=new LocalFrame({origin});
  const assertSourceTranslation=createExactSourceTranslationChecker(origin);
  const outPositions=new Float32Array(first.outputCount*9),outIndices=new Uint32Array(first.outputCount*3),sources=new Uint32Array(first.outputCount);
  let offset=0,maxRound=0,maxXZ=0,maxPlane=0,exactSourcePoints=0;
  const second=scan(({points,source,N,sourceTriangle,whole})=>{
    const unit=N.map(x=>x/norm(N)),sourceLocal=sub(source[0],origin);
    const local=points.map(point=>{
      // These are ORIGINAL emitted source bits, never pre-rounded generated points.
      if(whole||source.some(p=>p.every((x,k)=>x===point[k]))){assertSourceTranslation(point[0],point[1],point[2]);exactSourcePoints++;}
      return point.map((value,k)=>{const exact=value-origin[k],rounded=Math.fround(exact);need(finite(rounded),'local_position_range');const error=Math.abs(rounded-exact);maxRound=Math.max(maxRound,error);if(k!==1)maxXZ=Math.max(maxXZ,error);return rounded;});
    });
    const N32=normal(local);need(norm(N32)>0,'float32_triangle_collapsed');need(dot(N32,N)>0,'float32_winding_changed');
    for(const point of local){maxPlane=Math.max(maxPlane,Math.abs(dot(sub(point,sourceLocal),unit)));outPositions.set(point,offset);offset+=3;}
    sources[offset/9-1]=sourceTriangle;
  });
  need(offset===outPositions.length&&JSON.stringify(first)===JSON.stringify(second),'two_pass_geometry_changed');
  for(let i=0;i<outIndices.length;i++)outIndices[i]=i;
  const physical=blankBounds();for(let i=0;i<outPositions.length;i+=3)addBounds(physical,[[outPositions[i],outPositions[i+1],outPositions[i+2]]]);
  const retainedWorldBounds=freezeBounds(first.retained),domainWorldBounds=freezeBounds(first.domain);
  const domainLocalBounds=freezeBounds({lo:first.domain.lo.map((v,k)=>Math.fround(v-origin[k])),hi:first.domain.hi.map((v,k)=>Math.fround(v-origin[k]))});
  const physicalLocalBounds=freezeBounds(physical),localWindow=Object.freeze(frame.toLocalWindow(window));
  const coverage=createCollisionCoverage(frame,window,localWindow,maxXZ);
  const metadata=Object.freeze({schema:'offline-preserving-geometry-v1',...FLAGS,originPolicy:PRESERVING_ORIGIN_POLICY,origin:frame.origin,
    retainedWorldBounds,domainWorldBounds,domainLocalBounds,physicalLocalBounds,coverageWindow:window,
    inputTriangleCount:indices.length/3,inputVertexCount:positions.length/3,outputTriangleCount:sources.length,outputVertexCount:outPositions.length/3,outputTriangleBudget:maximum,
    wholeShortTriangles:first.wholeCount,clippedLongTriangles:first.longCount,sourceZeroAreaTrianglesDropped:first.sourceZero,fanZeroAreaTrianglesDropped:first.fanZero,
    aabbTouchedTriangleCount:first.touched,maximumOverhang:first.overhang,fixedWholeTriangleMaximumEdge:WHOLE_TRIANGLE_MAXIMUM_EDGE,
    sourceFloat32TranslationExact:true,sourceFloat32TranslationExactScope:'emitted_original_source_vertices_only',exactSourcePointChecks:exactSourcePoints,clippedFloat64VerticesExact:false,
    maxXZQuantizationError:maxXZ,maxFloat32QuantizationError:maxRound,maxFloat32PlaneError:maxPlane,
    sourceTriangleMapping:'output_triangle_to_original_index_triangle',allHeightLayersRetained:true,yClippingPerformed:false,
    clippingPerformed:first.longCount>0,wholeSourceTriangleOverhangAllowed:true,capSurfacesCreated:false,alternateFacesOrBoxesCreated:false,
    footOffsetApplied:false,outputBuffersIndependent:true,navigationPermissionExpanded:false,requiresCoverageGuards:true,outsideCoverage:'unknown_and_rejected',
    domainBoundsRule:'all_positive_area_source_intersections_with_original_window',motionBoundsRequired:true});
  return Object.freeze({frame,positions:outPositions,indices:outIndices,sourceTriangles:sources,coverage,metadata,domainLocalBounds,physicalLocalBounds});
}
