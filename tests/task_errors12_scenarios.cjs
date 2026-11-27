'use strict';
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const ROOT=path.resolve(__dirname,'..');
const desktop=fs.readFileSync(path.join(ROOT,'app/web/desktop.js'),'utf8');
const wizard=fs.readFileSync(path.join(ROOT,'app/web/wizard.js'),'utf8');
const error='拉取成功，但安装包校验未通过：关键资源 assets/critical.bin [missing]；另有 11 项差异',code='apk_channel_missing';
const start=desktop.indexOf('// RESOURCE_WORKFLOW_MODEL_BEGIN'),end=desktop.indexOf('// RESOURCE_WORKFLOW_MODEL_END');
assert(start>=0&&end>start);const modelContext=vm.createContext({});
vm.runInContext(desktop.slice(start,end)+'\nglobalThis.model=resourceStatus;',modelContext);
const task={id:'synthetic',kind:'pull-apk',label:'提取安装包',running:false,ok:false,error,error_code:code,
  error_detail:{code,text:error},result:{ok:false,error,code,stage:'validate_pulled_apk'}};
let view=modelContext.model.taskView({task},{});
assert.equal(view.state,'error');assert.equal(view.message,error);assert(view.detail.includes(code));
view=modelContext.model.taskView({task:{...task,error_code:'',result:{code:'result_code'}}},{});
assert(view.detail.includes('result_code'));
function part(name,async=false){const re=new RegExp('^  '+(async?'async ':'')+'function '+name+'\\([^]*?^  }','m');const m=wizard.match(re);assert(m,name);return m[0];}
function env(){
  const nodes=new Map(),shown=[];let done;
  const $=id=>{if(!nodes.has(id))nodes.set(id,{textContent:'',value:'',hidden:true,disabled:false,dataset:{}});return nodes.get(id);};
  const state={resourceTaskRunning:false,cleanupBusy:false};
  const ctx=vm.createContext({$,state,api:async()=>({ok:true}),showResult:(node,text,ok)=>{node.textContent=text;shown.push({text,ok});},
    pollTask:cb=>{done=cb;},refresh:async()=>{},checkApk:async()=>{throw new Error('must not validate failed pull');}});
  vm.runInContext(part('detailLines')+'\n'+part('retryState')+'\n'+part('renderTask')+'\n'+part('pullApk',true)+'\nglobalThis.render=renderTask;globalThis.pull=pullApk;',ctx);
  return {ctx,$,shown,finish:async t=>done(t)};
}
(async()=>{
  let f=env();f.ctx.render(task);
  assert(f.$('wz-data-msg').textContent.includes(error));assert(f.$('wz-task-detail').textContent.includes(code));
  assert.equal(f.$('wz-task-details').hidden,false);
  f=env();await f.ctx.pull();await f.finish(task);
  const message=f.$('wz-apk-result').textContent;
  assert(message.startsWith(error));assert(message.includes(code));assert(!message.startsWith('拉取失败：'));
  assert.equal(f.shown.at(-1).ok,false);
  f=env();await f.ctx.pull();await f.finish({...task,error:'adb 拉取失败：synthetic',error_code:'adb_pull_failed',result:{code:'adb_pull_failed'}});
  assert(f.$('wz-apk-result').textContent.includes('adb 拉取失败：synthetic'));assert(f.$('wz-apk-result').textContent.includes('adb_pull_failed'));
  f=env();let tick,finished,cleared=0;
  f.ctx.setInterval=callback=>{tick=callback;return 1;};f.ctx.clearInterval=()=>{cleared++;};
  f.ctx.api=async()=>({ok:false,running:false,task});
  vm.runInContext(part('stopTaskPoll')+'\n'+part('pollTask')+'\nglobalThis.poll=pollTask;',f.ctx);
  f.ctx.poll(value=>{finished=value;});await tick();
  assert.equal(finished.error_code,code);assert.equal(cleared,1);
  assert(f.$('wz-task-detail').textContent.includes(code));assert(f.$('wz-data-msg').textContent.includes(error));
  f=env();let sent,refreshes=0;f.$('wz-apk-path').value='synthetic.apk';
  f.ctx.api=async(url,options)=>{sent={url,options};return {ok:false,message:error,code,members:[{name:'assets/critical.bin',reason:'missing'}],difference_count:12};};
  f.ctx.refresh=async()=>{refreshes++;};vm.runInContext(part('checkApk',true)+'\nglobalThis.check=checkApk;',f.ctx);
  await f.ctx.check(false);assert.equal(sent.url,'/api/wizard/apk/check');assert.equal(sent.options.body.path,'synthetic.apk');
  assert(f.$('wz-apk-result').textContent.includes(error));assert(f.$('wz-apk-result').textContent.includes(code));assert.equal(refreshes,0);
  const requestStart=desktop.indexOf('\nfunction mumuCheckText(');
  const requestEnd=desktop.indexOf('\nfunction currentTask(',requestStart);
  assert(requestStart>=0&&requestEnd>requestStart);
  const requestSource=desktop.slice(requestStart,requestEnd);
  const messages=[],failure=Object.assign(new Error(error),{data:{ok:false,message:error,code}});
  const requestContext=vm.createContext({parity:{},syncResourceButtons:()=>{},parityMsg:(id,text,bad)=>messages.push({id,text,bad}),
    parityApi:async()=>{throw failure;},friendlyError:e=>e.message,loadResources:async()=>{throw new Error('must not refresh after rejection');}});
  vm.runInContext(requestSource+'\nglobalThis.run=resourceRequest;',requestContext);
  await assert.rejects(()=>requestContext.run('/api/wizard/apk/check',{path:'synthetic.apk'}));
  assert(messages.at(-1).text.includes(error));assert(messages.at(-1).text.includes(code));assert.equal(messages.at(-1).bad,true);
  await assert.rejects(()=>requestContext.run('/api/wizard/mumu',{}));assert.equal(messages.at(-1).text,error);
  failure.data.android_label='安卓 15';
  failure.data.page_size=16384;
  await assert.rejects(()=>requestContext.run('/api/wizard/mumu',{}));
  assert.equal(messages.at(-1).text,'安卓 15 · '+error+' · 内存页大小：16 KiB');
  console.log(JSON.stringify({passed:9,failed:0,functions:'actual taskView/renderTask/pullApk/pollTask/checkApk/resourceRequest'}));
})().catch(e=>{console.error(e.stack);process.exitCode=1;});
