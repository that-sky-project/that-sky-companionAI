import fs from 'node:fs';import path from 'node:path';import {createHash} from 'node:crypto';
import {init} from './core.mjs';import {StagedWorkerState} from './staged_worker.mjs';
await init();const argv=process.argv.slice(2),arg=(k,d)=>{const i=argv.indexOf(k);return i<0?d:argv[i+1];};
const root=path.resolve(arg('--assets','outputs')),catalog=JSON.parse(fs.readFileSync(arg('--catalog','research/integration/multimap-assets-v13/catalog-v13.json'))),row=catalog.entries.find(x=>x.scene_name==='Prairie_ButterflyFields');
const bot=[117.815,196.463,-438.166],peer=[116.634,196.411,-430.549],records=[];
const load={op:'load',manifest:row.asset_relpath,expectedManifestSHA:row.asset_manifest_sha256,navigationReport:row.staged_navigation.report_relpath,expectedNavigationReportSHA:row.staged_navigation.report_sha256,expectedNavigationSHA:row.staged_navigation.navigation_sha256,navigationWindow:row.navigation_window??null};
for(const direction of ['toward_peer','reverse'])for(const phaseMs of [100,120,130]){
 const state=new StagedWorkerState(root,{offline:true}),loaded=state.request(load);let current=[...(direction==='toward_peer'?bot:peer)],target=direction==='toward_peer'?peer:bot;const result={direction,phaseMs,loaded:loaded.status,segments:[]};
 try{
  if(loaded.status!=='ready'){result.error=loaded.reason;records.push(result);continue;}
  for(let segment=0;segment<4;segment++){
   const distance=Math.hypot(...target.map((x,i)=>x-current[i]));if(distance<.15){result.arrived=true;break;}
   const clipped=distance>6?current.map((x,i)=>x+(target[i]-x)*5.999999/distance):[...target],scope='pond_v14_'+direction+'_'+phaseMs;
   const q={start:current,goal:clipped,speed:3,scope,followIntent:{goal:[...target],peer:[...target],distance:0,segmentLength:6,maxEndpointDistance:1.3}},prepared=state.request({op:'preparePlan',...q}),rec={start:current,rawGoal:clipped,prepare:prepared,samples:[]};result.segments.push(rec);
   if(prepared.status!=='ready'){result.error=prepared.reason;break;}
   const plan=state.request({op:'plan',start:current,goal:clipped,speed:3,scope,now:0,preparedToken:prepared.preparedToken});rec.plan=plan;
   if(plan.status!=='ready'){result.error=plan.reason;break;}
   if(JSON.stringify(plan.motorPolicy??null)!==JSON.stringify(prepared.motorPolicy??null)||JSON.stringify(plan.motorPolicyAudit??null)!==JSON.stringify(prepared.motorPolicyAudit??null)||JSON.stringify(plan.followIntentAudit)!==JSON.stringify(prepared.followIntentAudit))throw Error('receipt_not_exact');
   rec.policyReceiptExact=true;let last;
   for(let n=1;n<=120;n++){last=state.request({op:'sample',token:plan.token,scope,now:n*phaseMs/1000});rec.samples.push(last);if(last.status!=='ready'||last.arrived||last.blocked)break;}
   if(last?.status!=='ready'||!last.arrived||last.blocked||rec.samples.some(s=>!s.grounded)){result.error='live_replay_not_grounded_arrival';break;}
   current=last.position;
  }
  result.final=current;result.distanceToPeer=Math.hypot(current[0]-target[0],current[2]-target[2]);result.arrived=result.distanceToPeer<=.15&&!result.error;
 }finally{state.dispose();}records.push(result);
}
const report={schema:'pond-follow-policy-smoke-v14',passed:records.length===6&&records.every(r=>r.arrived===true&&!r.error&&r.segments.length>0&&r.segments.every(s=>s.policyReceiptExact===true&&s.samples.length>0&&s.samples.every(f=>f.status==='ready'&&f.grounded===true&&!f.blocked)&&s.samples.at(-1).arrived===true)),actualStaticAsset:true,coordinatesFromRoundedOnlineRecord:true,liveMovement:false,nativeCollisionVerified:false,records,sourceSHA256:Object.fromEntries(['staged_worker.mjs','offline_motion_world.mjs','follow_ground_v14.mjs','core.mjs'].map(n=>[n,createHash('sha256').update(fs.readFileSync(new URL(n,import.meta.url))).digest('hex')]))};
const out=arg('--out','research/integration/pond-step-v14/real-pool-smoke.json');fs.mkdirSync(path.dirname(out),{recursive:true});const tmp=out+'.tmp';fs.writeFileSync(tmp,JSON.stringify(report,null,2));fs.renameSync(tmp,out);console.log(JSON.stringify({out,passed:report.passed,results:records.map(r=>({direction:r.direction,phase:r.phaseMs,arrived:r.arrived,error:r.error,segments:r.segments.map(s=>({status:s.prepare.status,route:s.prepare.followIntentAudit?.routeKind,policy:s.prepare.motorPolicy,rejected:s.prepare.followIntentAudit?.rejected,frames:s.samples.length,last:s.samples.at(-1)?.position}))}))}));

if(!report.passed)process.exitCode=1;
