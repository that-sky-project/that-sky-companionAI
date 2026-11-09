import fs from 'node:fs';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {validateMesh} from './core.mjs';
function within(root,p){const rel=path.relative(root,p);return rel!==''&&!rel.startsWith('..'+path.sep)&&rel!=='..'&&!path.isAbsolute(rel);}
function boundedRead(p,max){
  // One bounded descriptor read: a growing/replaced file cannot bypass stat budgets.
  const fd=fs.openSync(p,'r');
  try{
    const st=fs.fstatSync(fd);if(!st.isFile()||st.size>max)throw Error('asset_budget');
    const b=Buffer.alloc(st.size+1);let n=0;
    while(n<b.length){const count=fs.readSync(fd,b,n,b.length-n,null);if(!count)break;n+=count;}
    if(n!==st.size)throw Error('asset_changed_during_read');
    return b.subarray(0,n);
  }finally{fs.closeSync(fd);}
}
const digest=b=>createHash('sha256').update(b).digest('hex');
export function loadAsset(root,relative,{offline=false,expectedManifestSHA}={}){
  if(expectedManifestSHA!==undefined&&(typeof expectedManifestSHA!=='string'||!/^[0-9a-f]{64}$/.test(expectedManifestSHA)))throw Error('expected_manifest_sha_invalid');
  root=fs.realpathSync(root);if(typeof relative!=='string'||relative.length>256||path.isAbsolute(relative))throw Error('asset_path');
  const manifestPath=fs.realpathSync(path.resolve(root,relative));if(!within(root,manifestPath))throw Error('asset_escape');
  const manifestBytes=boundedRead(manifestPath,1024*1024),assetDigests={manifest:digest(manifestBytes)};
  if(expectedManifestSHA!==undefined&&assetDigests.manifest!==expectedManifestSHA)throw Error('asset_manifest_hash');
  // Parse, hash, and decode these same in-memory bytes, never a second path read.
  const m=JSON.parse(manifestBytes.toString('utf8'));
  if(m.format!=='skyterrain-static-v1')throw Error('asset_format');
  if(!offline && (m.geometryKind!=='verified_static_collision'||m.nativeCollisionVerified!==true||m.calibrationVerified!==true))throw Error('live_verification_required');
  // Beamo augmentation is an explicit offline candidate, never a live asset kind.
  const offlineBeamo=offline===true&&m.geometryKind==='offline_beamo_augmented_candidates';
  if(!['initial_static_collision_candidates','synthetic_fixture','verified_static_collision'].includes(m.geometryKind)&&!offlineBeamo)throw Error('asset_geometry_kind');
  const read=(key,Type,count,max)=>{
    if(typeof m[key]!=='string'||path.basename(m[key])!==m[key])throw Error('asset_child_path');
    const file=fs.realpathSync(path.join(path.dirname(manifestPath),m[key]));if(!within(root,file))throw Error('asset_escape');
    const b=boundedRead(file,max);
    if(!Number.isInteger(count)||count<1||b.length!==count*3*4)throw Error('asset_count');
    assetDigests[key]=digest(b);if(m.sha256?.[key]!==assetDigests[key])throw Error('asset_hash');
    return new Type(b.buffer.slice(b.byteOffset,b.byteOffset+b.length));
  };
  const positions=read('positions',Float32Array,m.vertexCount,24_000_000),indices=read('indices',Uint32Array,m.triangleCount,36_000_000);
  validateMesh(positions,indices);return {manifest:m,positions,indices,assetDigests:Object.freeze(assetDigests)};
}
