// Offline coverage proof is tied to these exact published/installed ESM bytes.
import {createHash} from 'node:crypto';
import {readFileSync,statSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
const EXPECTED='02dc6a4e2fffc013bab08fbb44fa68f501d8303615e9afe4c7d89ad4eedda0d0';
let checked;
export function assertPinnedCollisionRuntime(){
  if(checked)return checked;
  const entry=fileURLToPath(import.meta.resolve('@dimforge/rapier3d-compat'));
  const size=statSync(entry).size;
  if(size<1||size>8_000_000)throw Error('collision_runtime_size_mismatch');
  const sha256=createHash('sha256').update(readFileSync(entry)).digest('hex');
  if(sha256!==EXPECTED)throw Error('collision_runtime_proof_version_mismatch');
  checked=Object.freeze({package:'@dimforge/rapier3d-compat',version:'0.21.0',rustVersion:'0.36.0',
    esmSHA256:sha256,gitHead:'b716d375efc0201003f0cd9ef7168eee0b62c177'});
  return checked;
}
