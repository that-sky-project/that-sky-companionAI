// Geometry representation only: never a movement/goal tolerance or a native-physics claim.
import {LOCAL_FRAME_LIMITS} from './local_frame.mjs';
export const DYADIC_ORIGIN_GRID=32;
const need=(ok,why)=>{if(!ok)throw Error('dyadic_origin:'+why);};
function vector(value,label){
  need(Array.isArray(value)&&Object.getPrototypeOf(value)===Array.prototype,label+'_array');
  const d=Object.getOwnPropertyDescriptors(value);
  need(value.length===3&&Reflect.ownKeys(d).length===4&&['0','1','2'].every(k=>d[k]&&'value'in d[k]),label+'_shape');
  const result=[d[0].value,d[1].value,d[2].value];
  need(result.every(v=>typeof v==='number'&&Number.isFinite(v)&&Math.abs(v)<LOCAL_FRAME_LIMITS.geometryCoordinate),label+'_range');
  return result;
}
export function chooseDyadicOrigin(bounds){
  need(bounds&&typeof bounds==='object'&&!Array.isArray(bounds)&&[Object.prototype,null].includes(Object.getPrototypeOf(bounds)),'bounds_object');
  const d=Object.getOwnPropertyDescriptors(bounds);
  need(Reflect.ownKeys(d).length===2&&['lo','hi'].every(k=>d[k]&&'value'in d[k]),'bounds_fields');
  const lo=vector(d.lo.value,'lo'),hi=vector(d.hi.value,'hi');
  need(lo.every((x,k)=>x<=hi[k]),'bounds_order');
  return Object.freeze(lo.map((low,k)=>{
    const high=hi[k];
    const near=low>0?low:high<0?high:0;
    let anchor=Math.trunc(near/DYADIC_ORIGIN_GRID)*DYADIC_ORIGIN_GRID;
    // Actor/frame coordinates stay within the existing stricter range. Zero
    // always retains original source Float32 values when geometry is far away.
    if(Math.abs(anchor)>=LOCAL_FRAME_LIMITS.actorCoordinate)anchor=0;
    return anchor===0?0:anchor;
  }));
}
export function assertExactSourceTranslation(point,origin){
  const p=vector(point,'point'),a=vector(origin,'origin');
  for(let k=0;k<3;k++){
    need(Math.fround(p[k])===p[k],'source_not_float32');
    need(a[k]%DYADIC_ORIGIN_GRID===0,'anchor_not_grid');
    need(a[k]===0||(Math.sign(a[k])===Math.sign(p[k])&&Math.abs(a[k])<=Math.abs(p[k])),'anchor_not_toward_zero');
    // Under the validated invariant, the Float64 subtraction is exact too.
    need(Math.fround(p[k]-a[k])===p[k]-a[k],'source_translation_not_exact');
  }
  return true;
}

// Internal scalar seam for points constructed by the geometry scanner. Public
// array validation above is unchanged. The origin is validated and copied once
// per preparation, never retained by reference or shared across asset windows.
export function createExactSourceTranslationChecker(origin){
  const a=vector(origin,'origin');
  for(let k=0;k<3;k++)need(a[k]%DYADIC_ORIGIN_GRID===0,'anchor_not_grid');
  const ax=a[0],ay=a[1],az=a[2];
  const component=(p,anchor)=>{
    need(typeof p==='number'&&Number.isFinite(p)&&Math.abs(p)<LOCAL_FRAME_LIMITS.geometryCoordinate,'point_range');
    need(Math.fround(p)===p,'source_not_float32');
    need(anchor===0||(Math.sign(anchor)===Math.sign(p)&&Math.abs(anchor)<=Math.abs(p)),'anchor_not_toward_zero');
    need(Math.fround(p-anchor)===p-anchor,'source_translation_not_exact');
  };
  return Object.freeze(function checkExactSourceTranslation(px,py,pz){
    component(px,ax);component(py,ay);component(pz,az);return true;
  });
}
