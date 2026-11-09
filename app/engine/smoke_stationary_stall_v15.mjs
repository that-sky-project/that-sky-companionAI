import fs from 'node:fs';import path from 'node:path';import {fileURLToPath} from 'node:url';import {createHash} from 'node:crypto';
import {init} from './core.mjs';import {StagedWorkerState} from './staged_worker.mjs';
const argv=process.argv.slice(2),arg=(n,d)=>{const i=argv.indexOf(n);return i<0?d:argv[i+1];},engineDir=path.dirname(fileURLToPath(import.meta.url));
const assetRoot=path.resolve(arg('--assets','outputs')),catalogPath=arg('--catalog','research/integration/multimap-assets-v13/catalog-v13.json'),out=arg('--out','research/integration/follow-stall-v15/stationary-portable-v15.json');
const files=['core.mjs','staged_worker.mjs','offline_motion_world.mjs','follow_ground_v14.mjs'],hashes=()=>Object.fromEntries(files.map(p=>[p,createHash('sha256').update(fs.readFileSync(path.join(engineDir,p))).digest('hex')])),before=hashes();
const same=(a,b)=>JSON.stringify(a)===JSON.stringify(b),horizontal=(a,b)=>Math.hypot(a[0]-b[0],a[2]-b[2]),records=[];
await init();const catalog=JSON.parse(fs.readFileSync(catalogPath)),row=catalog.entries.find(x=>x.scene_name==='Prairie_ButterflyFields');if(!row)throw Error('Prairie_ButterflyFields_catalog_missing');
const start=[111.662,196.215,-436.443],peer=[114.092,195.88,-433.395],h=horizontal(start,peer),goal=[peer[0]+(start[0]-peer[0])/h,peer[1],peer[2]+(start[2]-peer[2])/h];
const load={op:'load',manifest:row.asset_relpath,expectedManifestSHA:row.asset_manifest_sha256,navigationReport:row.staged_navigation.report_relpath,expectedNavigationReportSHA:row.staged_navigation.report_sha256,expectedNavigationSHA:row.staged_navigation.navigation_sha256,navigationWindow:row.navigation_window??null};
for(const phaseMs of [100,120,130]){
 const state=new StagedWorkerState(assetRoot,{offline:true}),record={phaseMs,samples:[]};records.push(record);
 try{
  const loaded=state.request(load);record.loadStatus=loaded.status;if(loaded.status!=='ready')throw Error(loaded.reason);
  const q={start:[...start],goal:[...goal],speed:3,scope:'stationary_stall_v15_'+phaseMs,followIntent:{goal:[...goal],peer:[...peer],distance:1,segmentLength:6,maxEndpointDistance:1.3}};
  const begin=performance.now(),prepared=state.request({op:'preparePlan',...q});record.prepareMs=performance.now()-begin;record.prepare=prepared;
  if(prepared.status!=='ready')throw Error(prepared.reason);
  const plan=state.request({op:'plan',start:q.start,goal:q.goal,speed:3,scope:q.scope,now:0,preparedToken:prepared.preparedToken});record.plan=plan;
  record.exactReceipt=plan.status==='ready'&&same(prepared.path,plan.path)&&same(prepared.goal,plan.goal)&&same(prepared.followIntentAudit,plan.followIntentAudit)&&same(prepared.motorPolicy??null,plan.motorPolicy??null)&&same(prepared.motorPolicyAudit??null,plan.motorPolicyAudit??null)&&prepared.estimatedSeconds===plan.estimatedSeconds;
  if(!record.exactReceipt)throw Error('receipt_not_exact');
  const audit=prepared.followIntentAudit;record.endpointPromise=same(audit.peer,peer)&&horizontal(audit.selectedGoal,peer)<=1.3+1e-6&&(audit.truncated||same(audit.selectedGoal,plan.goal));if(!record.endpointPromise)throw Error('endpoint_promise');
  for(let n=1;n<=80;n++){const sample=state.request({op:'sample',token:plan.token,scope:q.scope,now:n*phaseMs/1000});record.samples.push(sample);if(sample.status!=='ready'||sample.blocked||sample.grounded!==true)throw Error(sample.reason??'sample_not_grounded');if(sample.arrived)break;}
  const last=record.samples.at(-1);record.arrived=last?.arrived===true;record.finalPeerHorizontalDistance=last?horizontal(last.position,peer):null;record.passed=record.arrived&&record.finalPeerHorizontalDistance<=1.3+1e-6;if(!record.passed)throw Error('did_not_arrive_near_stationary_peer');
 }catch(e){record.error=String(e.message);record.passed=false;}finally{state.dispose();}
}
const after=hashes(),report={schema:'stationary-stall-physical-smoke-v15',inputPrecision:'online origin coordinates rounded to 3 decimal places; feet subtract engineering 0.5m',targetNeverChanged:true,extraCommandsIssued:false,startFeet:start,peerFeet:peer,standingGoalFeet:goal,sourceHashes:before,sourceUnchanged:same(before,after),records,liveMovement:false};report.passed=report.sourceUnchanged&&records.length===3&&records.every(r=>r.passed&&r.exactReceipt&&r.endpointPromise&&r.samples.length&&r.samples.every(s=>s.status==='ready'&&s.grounded===true&&!s.blocked));fs.writeFileSync(out,JSON.stringify(report,null,2));console.log(JSON.stringify({out,passed:report.passed,sourceHashes:before,records:records.map(r=>({phaseMs:r.phaseMs,passed:r.passed,prepareMs:r.prepareMs,exactReceipt:r.exactReceipt,arrived:r.arrived,finalPeerHorizontalDistance:r.finalPeerHorizontalDistance,error:r.error}))}));process.exitCode=report.passed?0:1;
