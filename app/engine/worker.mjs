// Local stdin/stdout only. No network listener, credentials, or game packets.
import {init,TerrainWorld} from './core.mjs';
import {loadAsset} from './assets.mjs';
import path from 'node:path';
import {randomUUID} from 'node:crypto';
const args=process.argv.slice(2),i=args.indexOf('--assets');
if(i<0||!args[i+1])throw Error('--assets directory required');
const root=path.resolve(args[i+1]),offline=args.includes('--offline');
await init();
let world=null,asset=null,motor=null,token=null,scope=null;
const stop=()=>{motor?.dispose();motor=null;token=null;};
function checkScope(s){if(typeof s!=='string'||!s.length||s.length>256)throw Error('invalid_scope');return s;}
function handle(q){
  switch(q.op){
    case 'load':{
      // Changing maps invalidates old movement even if the new asset fails to load.
      stop();world?.dispose();world=null;asset=null;scope=null;
      if(!offline&&q.navigationWindow!=null)throw Error('local_navigation_requires_offline');
      const loaded=loadAsset(root,q.manifest,{offline,expectedManifestSHA:q.expectedManifestSHA});
      const next=new TerrainWorld(loaded.positions,loaded.indices,{config:q.config,navigationWindow:q.navigationWindow});
      world=next;asset=loaded.manifest;
      return {status:'ready',assetDigests:loaded.assetDigests,mapName:asset.mapName,geometryKind:asset.geometryKind,nativeCollisionVerified:asset.nativeCollisionVerified,navigationReady:!!world.nav,navError:world.navError,navigationCoverage:world.navigationCoverage,navigationWindow:world.navigationWindow};
    }
    case 'plan':{
      if(!world)return {status:'unavailable',reason:'asset_not_loaded'};
      const s=checkScope(q.scope);stop();scope=s;
      const plan=world.plan(q.start,q.goal);if(plan.status!=='ready')return plan;
      try{motor=world.motor(q.start,plan.path,q.now,q.speed);}catch(e){return {status:'blocked',reason:e.message};}
      token=randomUUID();return {...plan,token,scope,estimatedSeconds:plan.length/(q.speed??world.config.speed)};
    }
    case 'sample':{
      if(!motor||q.token!==token||q.scope!==scope)return {status:'unavailable',reason:'stale_motion'};
      return {status:'ready',...motor.sample(q.now)};
    }
    case 'cancel':{
      if(q.token!==undefined&&q.token!==token)return {status:'unavailable',reason:'stale_motion'};
      stop();return {status:'ready'};
    }
    case 'invalidate':stop();scope=null;return {status:'ready'};
    case 'ground':return {status:world?'ready':'unavailable',ground:world?.ground(q.point,q.maxDistance)??null};
    case 'ping':return {status:'ready',protocol:1,offline};
    default:throw Error('unknown_operation');
  }
}
let pending='';
process.stdin.setEncoding('utf8');
process.stdin.on('data',chunk=>{
  pending+=chunk;
  if(Buffer.byteLength(pending)>65536){process.stderr.write('request_budget\n');process.exit(2);return;}
  let n;while((n=pending.indexOf('\n'))>=0){
    const line=pending.slice(0,n);pending=pending.slice(n+1);let q=null,r;
    try{q=JSON.parse(line);if(!q||Array.isArray(q)||typeof q!=='object')throw Error('request_object');r=handle(q);}
    catch(e){r={status:'unavailable',reason:String(e.message).slice(0,240)};}
    const out=JSON.stringify({id:q?.id??null,...r});
    if(Buffer.byteLength(out)>65536)process.stdout.write(JSON.stringify({id:q?.id??null,status:'unavailable',reason:'reply_budget'})+'\n');
    else process.stdout.write(out+'\n');
  }
});
process.stdin.on('end',()=>{stop();world?.dispose();});
