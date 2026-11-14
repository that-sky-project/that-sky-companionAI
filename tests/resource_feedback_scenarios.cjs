'use strict';
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const ROOT=path.resolve(__dirname,'..');
const source=fs.readFileSync(path.join(ROOT,'app/web/desktop.js'),'utf8');
const begin=source.indexOf('// RESOURCE_WORKFLOW_MODEL_BEGIN'),end=source.indexOf('// RESOURCE_WORKFLOW_MODEL_END');
assert(begin>=0&&end>begin);
const context=vm.createContext({});
vm.runInContext(source.slice(begin,end)+'\nglobalThis.model=resourceStatus;',context);
const model=context.model;
const results=[];
function test(name,fn){fn();results.push({name,ok:true});}
const pending=()=>({ok:true,account:'fixture-a',steps:[{id:'capture',done:false},{id:'data',done:false}]});
const ready=()=>({ok:true,account:'fixture-a',catalog:{account:'fixture-a',certified:true,maps:115,variants:1},steps:[{id:'capture',done:true,detail:{account:'fixture-a',result:{ok:true,account:'fixture-a',maps:115,variants:1}}},{id:'data',done:true}]});
const cap=()=>({state:'ready',worker_pid:123,scene:'CandleSpace',recorded:['CandleSpace'],message:'遇境这个号已经录过了，去下一张图',last_registered:{scene:'CandleSpace',account:'fixture-a'}});
const task=(extra={})=>({id:'fixture-task',kind:'catalog',label:'生成本账号地图目录',running:true,ok:null,stage:'catalog',done:75,total:115,msg:'自检 75/115',...extra});
const wrap=t=>({ok:t?.ok!==false,running:!!t?.running,task:t,meta:{account:'fixture-a'}});

test('idle envelope is not a fake task',()=>assert.equal(model.currentTask({ok:true,running:false,task:null}),null));
test('registered home is clearly successful while collector waits',()=>{const v=model.captureView(cap(),pending());assert.equal(v.state,'success');assert.match(v.title,/已自动登记/);assert.match(v.message,/不需要逐张跑图/);assert.doesNotMatch(v.message,/去下一张图/);assert.match(v.detail,/停止采集/);});
test('stopped successful recording retains success',()=>assert.equal(model.captureView({...cap(),state:'stopped',worker_pid:null},pending()).state,'success'));
test('registration alone does not certify map catalog',()=>assert.equal(model.catalogView(pending(),{ok:true,running:false,task:null}).state,'idle'));
test('different account recording is not declared successful',()=>assert.notEqual(model.captureView({...cap(),last_registered:{scene:'CandleSpace',account:'fixture-b'}},pending()).state,'success'));
test('catalog done flag alone is not trusted',()=>assert.equal(model.catalogView({...pending(),capture:{catalog:{done:true}}},null).state,'idle'));
test('collector errors remain visible after earlier success',()=>assert.equal(model.captureView({...cap(),state:'error',last_error:'fixture read error'},ready()).state,'error'));
test('fresh capture is still running, not previous completion',()=>assert.equal(model.captureView({...cap(),state:'recording'},ready()).state,'running'));
test('progress uses actual current-stage counters',()=>{const v=model.catalogView(pending(),wrap(task()));assert.equal(v.state,'running');assert.equal(v.progress.value,65);assert.match(v.detail,/75\/115/);assert.match(v.detail,/逐张校验/);});
test('counter at 100 is not final success',()=>{const v=model.catalogView(pending(),wrap(task({done:115,total:115})));assert.equal(v.state,'running');assert.equal(v.progress.value,99);assert.match(v.message,/暂不算构建成功/);});
test('unknown total uses indeterminate progress',()=>{const v=model.catalogView(pending(),wrap(task({done:0,total:null,stage:'starting',msg:'任务已开始'})));assert.equal(v.progress.value,null);assert.match(v.detail,/等待首条进度/);});
test('invalid counters do not yield NaN progress',()=>assert.equal(model.taskView(wrap(task({done:NaN,total:115})),pending()).progress.value,null));
test('negative counter clamps to zero',()=>assert.equal(model.taskView(wrap(task({done:-1,total:115})),pending()).progress.value,0));
test('success without finished field is shown as success',()=>{const t=task({running:false,ok:true,result:{ok:true,account:'fixture-a'},done:115});assert.equal(model.taskView(wrap(t),ready()).state,'success');assert.match(model.taskView(wrap(t),ready()).title,/构建成功/);});
test('successful exit still waits for backend integrity validation',()=>assert.equal(model.catalogView(pending(),wrap(task({running:false,ok:true,result:{account:'fixture-a'}}))).state,'waiting'));
test('verified catalog explicitly reports map and variant counts',()=>{const v=model.catalogView(ready(),wrap(task({running:false,ok:true})));assert.equal(v.state,'success');assert.equal(v.progress.value,100);assert.match(v.title,/校验通过/);assert.match(v.message,/115 张地图 \+ 1 个独立落点/);});
test('completed catalog persists after another task',()=>assert.equal(model.catalogView(ready(),wrap(task({kind:'data',running:true}))).state,'success'));
test('current rebuild does not reuse stale green state',()=>assert.equal(model.catalogView(ready(),wrap(task())).state,'running'));
test('failure overrides previous green state and offers retry',()=>{const v=model.catalogView(ready(),wrap(task({running:false,ok:false,error:'缺少地图文件'})));assert.equal(v.state,'error');assert.match(v.message,/缺少地图文件/);assert.match(v.detail,/重试/);assert.doesNotMatch(v.title,/构建成功/);});
test('wrong-account success detail is not accepted',()=>{const s=ready();s.steps[0].detail.account='fixture-b';s.catalog.account='fixture-b';assert.notEqual(model.catalogView(s,null).state,'success');});
test('unknown terminal outcome is not shown as completed',()=>assert.equal(model.taskView(wrap(task({running:false,ok:null})),pending()).state,'waiting'));
test('summary read failure clears successful certification',()=>assert.equal(model.catalogView(ready(),null,'读取失败').state,'error'));
test('data task needs its own integrity confirmation',()=>assert.equal(model.taskView(wrap(task({kind:'data',running:false,ok:true})),pending()).state,'waiting'));
test('no new static asset route or import is needed',()=>assert.doesNotMatch(source,/import .*resource_status/));
test('manual registration is in an explicitly advanced section',()=>assert.match(source,/高级：登记已有录制（仅登记失败时使用）/));

function environment(responses){
  const nodes=new Map();
  const byId=id=>{if(!nodes.has(id))nodes.set(id,{textContent:'',disabled:false,hidden:false,checked:false,value:0,dataset:{},attributes:{},classList:{toggle(){}},setAttribute(k,v){this.attributes[k]=v;},removeAttribute(k){delete this.attributes[k];if(k==='value')this.value=null;}});return nodes.get(id);};
  const ctx=vm.createContext({console,Number,Array,Promise,Error,Map,document:{hidden:false},state:{page:'resources'},window:{setTimeout(){return 1;},clearTimeout(){}},
    byId,setText(id,value){byId(id).textContent=value||'';},friendlyError:e=>e.message||String(e),ApiError:class extends Error{},messageFrom:(r,f)=>r?.message||f,
    request:async (url,options)=>{assert(!options?.method||options.method==='GET','tests must never mutate runtime');const value=responses[url];if(value instanceof Error)throw value;return value??{ok:true};}});
  const prefix=source.indexOf("'use strict';const PAGE_TITLES");assert(prefix>end);
  vm.runInContext(source.slice(0,prefix),ctx);
  return {ctx,nodes,byId};
}
function responses(summary,taskReply,capture){return {'/api/wizard':summary,'/api/wizard/task':taskReply,'/api/capture/status':capture,'/api/autostart':{enabled:false},'/api/consent':{ok:true}};}
async function uiTest(name,summary,taskReply,capture,check){const env=environment(responses(summary,taskReply,capture));await vm.runInContext('loadResources()',env.ctx);check(env);results.push({name,ok:true});return env;}

(async()=>{
 await uiTest('DOM shows registered home and numeric catalog progress',pending(),wrap(task()),cap(),env=>{assert.equal(env.byId('resource-capture-panel').dataset.state,'success');assert.equal(env.byId('resource-catalog-progress').value,65);assert.match(env.byId('resource-catalog-detail').textContent,/75\/115/);assert.equal(env.byId('resource-catalog-start').disabled,true);});
 await uiTest('DOM success includes 100 percent and next action',ready(),wrap(task({running:false,ok:true})),cap(),env=>{assert.equal(env.byId('resource-catalog-progress').value,100);assert.equal(env.byId('resource-catalog-panel').dataset.state,'success');assert.match(env.byId('resource-catalog-state').textContent,/构建成功/);assert.match(env.byId('resource-task-state').textContent,/构建成功/);assert.match(env.byId('resource-catalog-detail').textContent,/完成安装设置/);});
 await uiTest('DOM failed job is not transport failure',pending(),wrap(task({running:false,ok:false,error:'fixture catalog failure'})),cap(),env=>{assert.match(env.byId('resource-catalog-message').textContent,/fixture catalog failure/);assert.equal(env.byId('resource-catalog-start').textContent,'重试构建地图目录');assert.equal(env.byId('resource-catalog-progress').hidden,true);assert.doesNotMatch(env.byId('resource-task-state').textContent,/状态读取失败/);});
 await uiTest('DOM unknown total removes value attribute',pending(),wrap(task({done:0,total:null})),cap(),env=>{assert.equal(env.byId('resource-catalog-progress').value,null);assert.equal(env.byId('resource-catalog-progress').hidden,false);});
 const snap=responses(ready(),wrap(task({running:false,ok:true})),cap());const env=environment(snap);await vm.runInContext('loadResources()',env.ctx);snap['/api/wizard']=new Error('fixture offline');await vm.runInContext('loadResources()',env.ctx);assert.equal(env.byId('resource-catalog-panel').dataset.state,'error');assert.equal(env.byId('resource-catalog-progress').hidden,true);results.push({name:'DOM refresh failure cannot retain stale green completion',ok:true});
 if(process.argv[2]){
   const out=path.resolve(process.argv[2]);fs.mkdirSync(out,{recursive:true});
   const css=fs.readFileSync(path.join(ROOT,'app/web/desktop.css'),'utf8');
   const renderer=source.slice(source.indexOf('function resourceStatusMarkup('),source.indexOf('function mountParity('));
   const preview=environment(responses(pending(),wrap(task()),cap()));
   const markup=prefix=>vm.runInContext(`resourceStatusMarkup(${JSON.stringify(prefix)})`,preview.ctx);
   const cases=[['遇境：成功后不用再跑图','preview-capture',model.captureView(cap(),pending())],['地图目录：正在逐张校验','preview-running',model.catalogView(pending(),wrap(task()))],['地图目录：校验通过才算成功','preview-success',model.catalogView(ready(),wrap(task({running:false,ok:true})))],['地图目录：失败原因与重试','preview-error',model.catalogView(pending(),wrap(task({running:false,ok:false,error:'示例：目录文件校验未通过'})))]];
   const html='<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>小光 · 采集与目录进度</title><style>'+css+'</style><style>body{display:block!important;overflow:auto!important;padding:30px;margin:0;min-width:0;height:auto;background:#101a30;color:#e7edfa}.preview-head{margin:0 0 18px}.preview-grid{display:grid;grid-template-columns:1fr 1fr;gap:18px}.preview-card{padding:18px;background:rgba(255,255,255,.035);border:1px solid #31415e;border-radius:20px}.preview-card h2{font-size:17px;margin:0 0 10px}.resource-status-panel{margin:0}.preview-head p{color:#aebedb;font-size:13px}progress{min-width:0}</style><body><header class="preview-head"><h1 style="font-size:25px">采集一次遇境，其余地图自动生成</h1><p>采集成功与目录构建分开显示 · 真实进度 · 完成校验后才显示成功</p></header><main class="preview-grid">'+cases.map(([title,id])=>'<section class="preview-card"><h2>'+title+'</h2>'+markup(id)+'</section>').join('')+'</main><script>const byId=id=>document.getElementById(id);const setText=(id,v)=>byId(id).textContent=v||"";'+renderer+'\n'+cases.map(([,id,view])=>'renderResourceStatus('+JSON.stringify(id)+','+JSON.stringify(view)+');').join('\n')+'</script></body></html>';
   fs.writeFileSync(path.join(out,'preview.html'),html);fs.writeFileSync(path.join(out,'ui-test-results.json'),JSON.stringify({passed:results.length,failed:0,results},null,2));
 }
 console.log(JSON.stringify({passed:results.length,failed:0,tests:results.map(r=>r.name)},null,2));
})().catch(error=>{console.error(error.stack);process.exitCode=1;});
