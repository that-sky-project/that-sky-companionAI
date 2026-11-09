// Offline coordinate adapter only. No collision policy, clipping, or foot correction.
export const LOCAL_FRAME_LIMITS=Object.freeze({actorCoordinate:1e6,geometryCoordinate:2**23,
  positionElements:6_000_000,indexElements:9_000_000,pathPoints:512,windowSpan:512});
const FALSE_FLAGS=Object.freeze({nativeCollisionVerified:false,calibrationVerified:false,
  runtimeReady:false,live_enabled:false});
const need=(ok,reason)=>{if(!ok)throw Error('local_frame:'+reason);};
const own=(object,key)=>Object.prototype.hasOwnProperty.call(object,key);
const arrayBufferLength=Object.getOwnPropertyDescriptor(ArrayBuffer.prototype,'byteLength').get;
const typedPrototype=Object.getPrototypeOf(Float32Array.prototype);
const typedLength=Object.getOwnPropertyDescriptor(typedPrototype,'length').get;
const typedBuffer=Object.getOwnPropertyDescriptor(typedPrototype,'buffer').get;
const typedOffset=Object.getOwnPropertyDescriptor(typedPrototype,'byteOffset').get;
const resizable=Object.getOwnPropertyDescriptor(ArrayBuffer.prototype,'resizable')?.get;

function record(value,keys,label){
  need(value!==null&&typeof value==='object'&&!Array.isArray(value),label+'_object');
  const proto=Object.getPrototypeOf(value);
  need(proto===Object.prototype||proto===null,label+'_prototype');
  const descriptors=Object.getOwnPropertyDescriptors(value),actual=Reflect.ownKeys(descriptors);
  need(actual.length===keys.length&&actual.every(k=>typeof k==='string'&&keys.includes(k)),label+'_fields');
  need(keys.every(k=>own(descriptors,k)&&own(descriptors[k],'value')),label+'_accessor');
  return Object.fromEntries(keys.map(k=>[k,descriptors[k].value]));
}
function sequence(value,size,label,maximum=size){
  need(Array.isArray(value)&&Object.getPrototypeOf(value)===Array.prototype,label+'_array');
  const descriptors=Object.getOwnPropertyDescriptors(value),n=descriptors.length?.value;
  need(Number.isInteger(n)&&n>=size&&n<=maximum,label+'_length');
  const keys=Reflect.ownKeys(descriptors);
  need(keys.length===n+1&&keys.every(k=>k==='length'||typeof k==='string'&&/^(0|[1-9][0-9]*)$/.test(k)&&Number(k)<n),label+'_fields');
  need(Array.from({length:n},(_,i)=>descriptors[i]).every(d=>d&&own(d,'value')),label+'_accessor_or_hole');
  return Array.from({length:n},(_,i)=>descriptors[i].value);
}
function finite(value,maximum,label){
  need(typeof value==='number'&&Number.isFinite(value)&&Math.abs(value)<maximum,label+'_finite_or_bounds');
  return value;
}
function vector(value,label){
  return sequence(value,3,label).map(v=>finite(v,LOCAL_FRAME_LIMITS.actorCoordinate,label));
}
function windowRecord(value){
  if(value===null)return null;
  const window=record(value,['minX','maxX','minZ','maxZ'],'window');
  for(const v of Object.values(window))finite(v,LOCAL_FRAME_LIMITS.actorCoordinate,'window');
  need(window.minX<window.maxX&&window.minZ<window.maxZ
    &&window.maxX-window.minX<=LOCAL_FRAME_LIMITS.windowSpan
    &&window.maxZ-window.minZ<=LOCAL_FRAME_LIMITS.windowSpan,'window_span');
  return window;
}
function typed(view,Type,maximum,label){
  need(view!==null&&typeof view==='object'&&Object.getPrototypeOf(view)===Type.prototype,label+'_type');
  let buffer,length,offset,bytes;
  try{
    buffer=typedBuffer.call(view);length=typedLength.call(view);offset=typedOffset.call(view);
    bytes=arrayBufferLength.call(buffer);
  }catch{throw Error('local_frame:'+label+'_ownership');}
  need(Object.getPrototypeOf(buffer)===ArrayBuffer.prototype&&!(resizable?.call(buffer)),label+'_ownership');
  need(Number.isInteger(length)&&length>0&&length%3===0&&length<=maximum
    &&offset>=0&&offset+length*4<=bytes,label+'_shape_or_budget');
  need(Object.getOwnPropertySymbols(view).length===0,label+'_fields');
  // Streaming own-key check avoids allocating millions of property descriptors.
  for(const key in view){
    need(own(view,key)&&/^(0|[1-9][0-9]*)$/.test(key)&&Number(key)<length,label+'_fields');
  }
  for(const key of ['length','buffer','byteOffset','byteLength'])need(!own(view,key),label+'_shadowed_intrinsic');
  return {buffer,length,offset};
}
function frozenBounds(lo,hi){return Object.freeze({lo:Object.freeze(lo),hi:Object.freeze(hi)});}

/** An explicit XYZ translation; positions move, directions/velocities never do. */
export class LocalFrame {
  #origin;
  constructor(options){
    need(new.target===LocalFrame,'frame_subclass');
    const values=record(options,['origin'],'options');
    this.#origin=Object.freeze(vector(values.origin,'origin'));
    Object.freeze(this);
  }
  get origin(){return this.#origin;}
  get verification(){return FALSE_FLAGS;}
  #position(point,sign){
    return vector(point,'position').map((value,i)=>
      finite(value+sign*this.#origin[i],LOCAL_FRAME_LIMITS.actorCoordinate,'translated_position'));
  }
  toLocalPosition(point){return this.#position(point,-1);}
  toWorldPosition(point){return this.#position(point,1);}
  #path(points,sign){
    return sequence(points,1,'path',LOCAL_FRAME_LIMITS.pathPoints).map(point=>this.#position(point,sign));
  }
  toLocalPath(points){return this.#path(points,-1);}
  toWorldPath(points){return this.#path(points,1);}
  #window(value,sign){
    const input=windowRecord(value);if(input===null)return null;
    return windowRecord({minX:input.minX+sign*this.#origin[0],maxX:input.maxX+sign*this.#origin[0],
      minZ:input.minZ+sign*this.#origin[2],maxZ:input.maxZ+sign*this.#origin[2]});
  }
  toLocalWindow(window){return this.#window(window,-1);}
  toWorldWindow(window){return this.#window(window,1);}
  toLocalVector(value){void this.#origin;return vector(value,'vector');}
  toWorldVector(value){void this.#origin;return vector(value,'vector');}
}

/** Copy the complete mesh to a frame. Indices/order/triangle count are unchanged. */
export function localizeGeometry(frame,geometry){
  need(frame!==null&&typeof frame==='object'&&Object.getPrototypeOf(frame)===LocalFrame.prototype,'frame_ownership');
  const origin=frame.origin; // Private-field access rejects fabricated prototype-only frames.
  const input=record(geometry,['positions','indices'],'geometry');
  const p=typed(input.positions,Float32Array,LOCAL_FRAME_LIMITS.positionElements,'positions');
  const t=typed(input.indices,Uint32Array,LOCAL_FRAME_LIMITS.indexElements,'indices');
  need(p.buffer!==t.buffer,'geometry_input_buffers_alias');
  const sourceLo=[Infinity,Infinity,Infinity],sourceHi=[-Infinity,-Infinity,-Infinity];
  for(let i=0;i<p.length;i++){
    const value=finite(input.positions[i],LOCAL_FRAME_LIMITS.geometryCoordinate,'geometry');
    sourceLo[i%3]=Math.min(sourceLo[i%3],value);sourceHi[i%3]=Math.max(sourceHi[i%3],value);
  }
  for(let i=0;i<t.length;i++)need(input.indices[i]<p.length/3,'geometry_index');
  const positions=new Float32Array(p.length),indices=new Uint32Array(t.length);
  const localLo=[Infinity,Infinity,Infinity],localHi=[-Infinity,-Infinity,-Infinity];
  let maxTranslationFloat32Quantization=0;
  for(let i=0;i<p.length;i++){
    // JS Number subtraction is float64; origin is never pre-rounded to float32.
    const exact=finite(input.positions[i]-origin[i%3],LOCAL_FRAME_LIMITS.geometryCoordinate,'translated_geometry');
    positions[i]=exact;
    finite(positions[i],LOCAL_FRAME_LIMITS.geometryCoordinate,'float32_geometry');
    maxTranslationFloat32Quantization=Math.max(maxTranslationFloat32Quantization,Math.abs(positions[i]-exact));
    localLo[i%3]=Math.min(localLo[i%3],positions[i]);localHi[i%3]=Math.max(localHi[i%3],positions[i]);
  }
  for(let i=0;i<t.length;i++)indices[i]=input.indices[i];
  const metadata=Object.freeze({schema:'offline-local-frame-geometry-v1',origin:Object.freeze([...origin]),
    vertexCount:p.length/3,triangleCount:t.length/3,indexCount:t.length,
    sourceBounds:frozenBounds(sourceLo,sourceHi),localBounds:frozenBounds(localLo,localHi),
    maxTranslationFloat32Quantization,allOriginalTrianglesRetained:true,
    indicesUnchanged:true,clippingPerformed:false,footOffsetApplied:false,
    outputBuffersIndependent:true,...FALSE_FLAGS});
  return Object.freeze({positions,indices,metadata});
}
