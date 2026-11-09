// Offline XZ-bounded geometry only. Unknown/outside coverage is never empty space.
import {LocalFrame,LOCAL_FRAME_LIMITS} from './local_frame.mjs';

export const CLIPPED_GEOMETRY_LIMITS=Object.freeze({positionElements:6_000_000,indexElements:9_000_000,
  // 500k independent triangles: 4.5M position elements, 1.5M indices, 26MB including provenance.
  // This only raises a bounded allocation budget; geometry/physics/window policies are unchanged.
  outputTriangles:500_000,windowSpan:512,pathPoints:512});
const FLAGS=Object.freeze({nativeCollisionVerified:false,calibrationVerified:false,runtimeReady:false,live_enabled:false});
const need=(ok,why)=>{if(!ok)throw Error('clipped_geometry:'+why);};
const own=(o,k)=>Object.prototype.hasOwnProperty.call(o,k);
const typedProto=Object.getPrototypeOf(Float32Array.prototype);
const typedLength=Object.getOwnPropertyDescriptor(typedProto,'length').get;
const typedBuffer=Object.getOwnPropertyDescriptor(typedProto,'buffer').get;
const typedOffset=Object.getOwnPropertyDescriptor(typedProto,'byteOffset').get;
const bufferLength=Object.getOwnPropertyDescriptor(ArrayBuffer.prototype,'byteLength').get;
const resizable=Object.getOwnPropertyDescriptor(ArrayBuffer.prototype,'resizable')?.get;
function record(value,required,optional,label){
  need(value!==null&&typeof value==='object'&&!Array.isArray(value),label+'_object');
  need([Object.prototype,null].includes(Object.getPrototypeOf(value)),label+'_prototype');
  const descriptors=Object.getOwnPropertyDescriptors(value),allowed=[...required,...optional],keys=Reflect.ownKeys(descriptors);
  need(required.every(k=>own(descriptors,k))&&keys.every(k=>typeof k==='string'&&allowed.includes(k)),label+'_fields');
  need(keys.every(k=>own(descriptors[k],'value')),label+'_accessor');
  return Object.fromEntries(keys.map(k=>[k,descriptors[k].value]));
}
function typed(value,Type,maximum,label){
  need(value&&typeof value==='object'&&Object.getPrototypeOf(value)===Type.prototype,label+'_type');
  let length,buffer,offset,n;
  try{length=typedLength.call(value);buffer=typedBuffer.call(value);offset=typedOffset.call(value);n=bufferLength.call(buffer);}
  catch{throw Error('clipped_geometry:'+label+'_ownership');}
  need(Object.getPrototypeOf(buffer)===ArrayBuffer.prototype&&!resizable?.call(buffer),label+'_ownership');
  need(length>0&&length%3===0&&length<=maximum&&offset>=0&&offset+4*length<=n,label+'_shape_or_budget');
  need(Object.getOwnPropertySymbols(value).length===0,label+'_fields');
  for(const key in value)need(own(value,key)&&/^(0|[1-9][0-9]*)$/.test(key)&&Number(key)<length,label+'_fields');
  for(const key of ['length','buffer','byteOffset','byteLength'])need(!own(value,key),label+'_shadowed_intrinsic');
  return {length,buffer};
}
const finite=(x,limit=LOCAL_FRAME_LIMITS.geometryCoordinate)=>typeof x==='number'&&Number.isFinite(x)&&Math.abs(x)<limit;
const sub=(a,b)=>a.map((n,i)=>n-b[i]);
const dot=(a,b)=>a.reduce((n,x,i)=>n+x*b[i],0);
const cross=(a,b)=>[a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]];
const normal=T=>cross(sub(T[1],T[0]),sub(T[2],T[0]));
const norm=a=>Math.hypot(...a);
function clipPlane(polygon,axis,bound,minimum){
  if(!polygon.length)return [];
  const inside=p=>minimum?p[axis]>=bound:p[axis]<=bound,result=[];
  let previous=polygon.at(-1),previousInside=inside(previous);
  for(const current of polygon){
    const currentInside=inside(current);
    if(currentInside!==previousInside){
      const denominator=current[axis]-previous[axis];need(denominator!==0,'parallel_clip_crossing');
      const t=(bound-previous[axis])/denominator;need(Number.isFinite(t)&&t>=0&&t<=1,'clip_parameter');
      const p=previous.map((n,i)=>n+t*(current[i]-n));p[axis]=bound;
      need(p.every(n=>finite(n)),'clip_nonfinite_or_range');result.push(p);
    }
    if(currentInside)result.push(current);
    previous=current;previousInside=currentInside;
  }
  const unique=[];
  for(const p of result)if(!unique.length||p.some((v,k)=>v!==unique.at(-1)[k]))unique.push(p);
  if(unique.length>1&&unique[0].every((v,k)=>v===unique.at(-1)[k]))unique.pop();
  need(unique.length<=7,'convex_clip_vertex_budget');return unique;
}
function budgetOptions(value){
  const o=record(value,[],['maxOutputTriangles'],'options'),limit=own(o,'maxOutputTriangles')?o.maxOutputTriangles:CLIPPED_GEOMETRY_LIMITS.outputTriangles;
  need(Number.isInteger(limit)&&limit>0&&limit<=CLIPPED_GEOMETRY_LIMITS.outputTriangles,'output_triangle_budget_option');
  return limit;
}
function createCoverage(frame,worldWindow,localWindow,maxXZQuantizationError){
  // Capsule height and vertical sweeps need no Y restriction: every height layer is retained.
  const padding=options=>{
    const o=record(options,['radius','skin'],['extraMargin'],'capsule');
    need(finite(o.radius,512)&&o.radius>=0&&finite(o.skin,512)&&o.skin>=0,'capsule_radius_or_skin');
    const extra=own(o,'extraMargin')?o.extraMargin:0;need(finite(extra,512)&&extra>=0,'capsule_extra_margin');
    const margin=o.radius+o.skin+extra+maxXZQuantizationError;
    need(Number.isFinite(margin)&&2*margin<=Math.min(worldWindow.maxX-worldWindow.minX,worldWindow.maxZ-worldWindow.minZ),'capsule_does_not_fit_coverage');
    return margin;
  };
  const check=(position,margin)=>{
    const p=frame.toLocalVector(position); // Validate/copy without translation or rounding.
    need(p[0]-margin>=worldWindow.minX&&p[0]+margin<=worldWindow.maxX
      &&p[2]-margin>=worldWindow.minZ&&p[2]+margin<=worldWindow.maxZ,'outside_collision_coverage');
  };
  return Object.freeze({worldWindow,localWindow,maxXZQuantizationError,
    verticalCoverage:'all_original_height_layers',outsidePolicy:'throw_unknown_coverage',
    assertWorldCapsule(position,options){check(position,padding(options));return true;},
    assertWorldSweep(start,end,options){const margin=padding(options);check(start,margin);check(end,margin);return true;},
    assertWorldPath(points,options){
      const margin=padding(options),local=frame.toLocalPath(points);
      // A rectangular XZ domain is convex: both ends imply the entire linear swept segment is covered.
      // Validate original coordinates again rather than round-tripping and changing them.
      for(let i=0;i<local.length;i++)check(points[i],margin);return true;
    },...FLAGS});
}

/**
 * Four-plane Sutherland-Hodgman in world float64, fan triangulation, then one
 * float32 local write. Keeps all Y layers and winding. Does not create caps.
 * Returned buffers are owned; sourceTriangles[t] is the original index-triangle.
 * An explicit motion guard is mandatory: this is NOT an unrestricted map.
 */
export function clipGeometryXZ(frame,geometry,worldWindow,options={}){
  need(frame&&typeof frame==='object'&&Object.getPrototypeOf(frame)===LocalFrame.prototype,'frame_ownership');
  const origin=frame.origin;
  need(worldWindow!==null,'explicit_window_required');
  const localWindow=Object.freeze(frame.toLocalWindow(worldWindow));
  const inputWindow=record(worldWindow,['minX','maxX','minZ','maxZ'],[],'window');
  const window=Object.freeze({...inputWindow});
  const maximum=budgetOptions(options),input=record(geometry,['positions','indices'],[],'geometry');
  const p=typed(input.positions,Float32Array,CLIPPED_GEOMETRY_LIMITS.positionElements,'positions');
  const i=typed(input.indices,Uint32Array,CLIPPED_GEOMETRY_LIMITS.indexElements,'indices');
  need(p.buffer!==i.buffer,'input_buffers_alias');
  const positions=input.positions,indices=input.indices;
  for(let n=0;n<p.length;n++)need(finite(positions[n]),'source_position_nonfinite_or_range');
  for(let n=0;n<i.length;n++)need(indices[n]<p.length/3,'source_index_bounds');
  const output=[],sources=[];
  let retained=0,touched=0,sourceDegenerate=0,fanDegenerate=0,max64=0,max32=0,maxRound=0,maxXZ=0;
  for(let t=0;t<i.length/3;t++){
    const ia=indices[t*3]*3,ib=indices[t*3+1]*3,ic=indices[t*3+2]*3;
    if(Math.max(positions[ia],positions[ib],positions[ic])<window.minX||Math.min(positions[ia],positions[ib],positions[ic])>window.maxX
      ||Math.max(positions[ia+2],positions[ib+2],positions[ic+2])<window.minZ||Math.min(positions[ia+2],positions[ib+2],positions[ic+2])>window.maxZ)continue;
    touched++;
    const T=[ia,ib,ic].map(n=>[positions[n],positions[n+1],positions[n+2]]),N=normal(T),length=norm(N);
    need(Number.isFinite(length),'source_normal_nonfinite');
    if(length===0){sourceDegenerate++;continue;} // Zero-area triangles are not surfaces; explicitly accounted.
    const unit=N.map(n=>n/length);let polygon=T;
    for(const [axis,bound,minimum] of [[0,window.minX,true],[0,window.maxX,false],[2,window.minZ,true],[2,window.maxZ,false]])
      polygon=clipPlane(polygon,axis,bound,minimum);
    if(polygon.length<3)continue;
    let kept=false;
    for(let n=1;n<polygon.length-1;n++){
      const points=[polygon[0],polygon[n],polygon[n+1]],N64=normal(points);
      if(norm(N64)===0){fanDegenerate++;continue;}
      need(dot(N64,N)>0,'float64_winding_changed');
      need(sources.length<maximum,'output_triangle_budget');
      const local=[];
      for(const point of points){
        need(point[0]>=window.minX&&point[0]<=window.maxX&&point[2]>=window.minZ&&point[2]<=window.maxZ,'clip_vertex_outside_window');
        max64=Math.max(max64,Math.abs(dot(sub(point,T[0]),unit)));
        const converted=point.map((value,k)=>{
          const exact=value-origin[k];need(finite(exact),'local_position_range');const rounded=Math.fround(exact);
          need(finite(rounded),'float32_position_range');const error=Math.abs(rounded-exact);
          maxRound=Math.max(maxRound,error);if(k!==1)maxXZ=Math.max(maxXZ,error);return rounded;
        });
        const sourceLocal=T[0].map((v,k)=>v-origin[k]);
        max32=Math.max(max32,Math.abs(dot(sub(converted,sourceLocal),unit)));local.push(converted);
      }
      const N32=normal(local);
      need(norm(N32)>0,'float32_triangle_collapsed');need(dot(N32,N)>0,'float32_winding_changed');
      for(const point of local)output.push(...point);
      sources.push(t);kept=true;
    }
    if(kept)retained++;
  }
  need(sources.length>0,'no_geometry_in_coverage');
  const outPositions=Float32Array.from(output),outIndices=Uint32Array.from({length:output.length/3},(_,n)=>n);
  const sourceTriangles=Uint32Array.from(sources);
  const coverage=createCoverage(frame,window,localWindow,maxXZ);
  const metadata=Object.freeze({schema:'offline-clipped-geometry-xz-v1',origin:Object.freeze([...origin]),
    coverageWindow:window,localCoverageWindow:localWindow,
    inputTriangleCount:i.length/3,inputVertexCount:p.length/3,outputTriangleCount:sources.length,outputVertexCount:output.length/3,
    retainedSourceTriangleCount:retained,aabbTouchedTriangleCount:touched,
    sourceZeroAreaTrianglesDropped:sourceDegenerate,fanZeroAreaTrianglesDropped:fanDegenerate,
    outputTriangleBudget:maximum,maxXZQuantizationError:maxXZ,maxFloat32QuantizationError:maxRound,
    maxFloat64PlaneError:max64,maxFloat32PlaneError:max32,allHeightLayersRetained:true,yClippingPerformed:false,
    sourceTriangleMapping:'output_triangle_to_original_index_triangle',windingPreserved:true,
    clippingPerformed:true,capSurfacesCreated:false,footOffsetApplied:false,outputBuffersIndependent:true,
    geometryDomainRestricted:true,requiresCoverageGuards:true,navigationPermissionExpanded:false,
    outsideCoverage:'unknown_and_rejected',...FLAGS});
  return Object.freeze({positions:outPositions,indices:outIndices,sourceTriangles,metadata,coverage});
}

// Shared strict primitives; original clipGeometryXZ behavior is unchanged.
export {record as geometryRecord,typed as validateGeometryBuffer,clipPlane as clipPolygonPlane,createCoverage as createCollisionCoverage};
