'use strict';
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const ROOT=path.resolve(__dirname,'..'),tests=[];
const desktop=fs.readFileSync(path.join(ROOT,'app/web/desktop.js'),'utf8');
const wizard=fs.readFileSync(path.join(ROOT,'app/web/wizard.js'),'utf8');
const sharedFixture=fs.readFileSync(path.join(__dirname,'reset_cleanup_scenarios.cjs'),'utf8');
const fixture=vm.createContext({require,__dirname,console,process});
vm.runInContext(sharedFixture.slice(0,sharedFixture.indexOf('(async()=>{'))+'\nglobalThis.fixtureEnv=environment;globalThis.fixtureModel=resourceModel;',fixture);
const model=fixture.fixtureModel();
async function test(name,fn){await fn();tests.push(name);}
function extract(source,start,end){const a=source.indexOf(start),b=source.indexOf(end,a+start.length);assert(a>=0&&b>a);return source.slice(a,b);}
function desktopStart(status={online:false,runner:false,chat:false}){
  const calls=[],messages=[],parity={task:{running:false},capture:{state:'idle'}};
  const f=fixture.fixtureEnv();
  const ctx=vm.createContext({parity,window:{XGDataCleanup:f.ctx.XGDataCleanup},
    currentTask:r=>r,captureRunning:r=>r?.state==='recording',syncResourceButtons:()=>{},
    request:async()=>status,resourceRequest:async(url,body)=>{calls.push({url,body});return {ok:true};},
    parityMsg:(id,msg)=>messages.push(msg),friendlyError:e=>e.message,Error});
  vm.runInContext(extract(desktop,'async function resourceGeneration(', 'async function resourceRequest(')+'\nglobalThis.start=resourceGeneration;',ctx);
  return {ctx,calls,messages,parity};
}
function wizardStart(status={online:false,runner:false,chat:false}){
  const nodes=new Map(),calls=[],f=fixture.fixtureEnv(),state={summary:{task:null}};
  const $=id=>{if(!nodes.has(id))nodes.set(id,{disabled:false,checked:true,textContent:'',attributes:{},setAttribute(k,v){this.attributes[k]=v;}});return nodes.get(id);};
  const ctx=vm.createContext({state,$,window:{XGDataCleanup:f.ctx.XGDataCleanup},Error,
    api:async(url,opts)=>{calls.push({url,opts});return url.endsWith('/status')?status:{ok:true};},pollTask:()=>{}});
  vm.runInContext(extract(wizard,'  async function startResource(', '  // ---------------------------------------------------------------- 第 5 步')+'\nglobalThis.start=startResource;',ctx);
  return {ctx,calls,state,$};
}
(async()=>{
  await test('old user without props cannot inherit green state from core data',()=>{
    const summary={steps:[{id:'data',done:true}],gameData:{complete:true}};
    assert.equal(model.propsView(summary,null).state,'waiting');
    assert.match(model.propsView(summary,null).message,/旧用户/);
  });
  await test('valid current props certificate displays real generated count',()=>{
    const v=model.propsView({props:{ready:true,state:'ready',count:3,message:'3 种已生成'}},null);
    assert.equal(v.state,'success');assert.match(v.message,/3/);
  });
  await test('props task success alone is not integrity proof',()=>{
    const task={kind:'props',ok:true,running:false};
    assert.notEqual(model.taskView(task,{props:{ready:false,state:'missing'}}).state,'success');
  });
  await test('props failure does not erase successful names and terrain',()=>{
    const task={kind:'data',ok:true,running:false,result:{warning:'道具形状未生成'}};
    const summary={steps:[{id:'data',done:true}],props:{ready:false,state:'missing'}};
    assert.equal(model.taskView(task,summary).state,'success');
    assert.match(model.taskView(task,summary).message,/道具形状未生成/);
    assert.equal(model.propsView(summary,task).state,'waiting');
  });
  await test('props reset gate does not invalidate existing catalog certificate',()=>{
    const summary={account:'fixture',catalog:{account:'fixture',certified:true,maps:2},
      props:{ready:true,state:'ready'},resourceReset:{active:true,steps:['props']}};
    assert.equal(model.catalogView(summary,null).state,'success');
    assert.equal(model.propsView(summary,null).state,'waiting');
  });
  await test('desktop independent props button posts only props endpoint',async()=>{
    const e=desktopStart();await e.ctx.start('/api/wizard/props/start',{});
    assert.equal(e.calls.length,1);assert.equal(e.calls[0].url,'/api/wizard/props/start');
    assert(!e.parity.generationBusy);
  });
  await test('desktop blocks online and active capture before POST',async()=>{
    const e=desktopStart({online:true,runner:true,chat:false});await assert.rejects(e.ctx.start('/api/wizard/props/start',{}));assert.equal(e.calls.length,0);
    const b=desktopStart();b.parity.capture.state='recording';await assert.rejects(b.ctx.start('/api/wizard/props/start',{}));assert.equal(b.calls.length,0);
  });
  await test('desktop pending flag prevents duplicate start',async()=>{
    const e=desktopStart();let release;e.ctx.request=()=>new Promise(r=>{release=r;});
    const first=e.ctx.start('/api/wizard/props/start',{});await e.ctx.start('/api/wizard/props/start',{});
    release({online:false,runner:false,chat:false});await first;assert.equal(e.calls.length,1);
  });
  await test('wizard starts props without terrain args and remains task locked',async()=>{
    const e=wizardStart();await e.ctx.start('props');
    assert.equal(e.calls.length,2);assert.equal(e.calls[1].url,'/api/wizard/props/start');
    assert.deepEqual(JSON.parse(JSON.stringify(e.calls[1].opts.body)),{});
    assert(e.state.resourceTaskRunning);assert(e.$('wz-props-start').disabled);
    await e.ctx.start('props');assert.equal(e.calls.length,2);
  });
  await test('wizard online or unknown status never posts generator request',async()=>{
    for(const status of [{online:true,runner:false,chat:false},{}]){
      const e=wizardStart(status);await e.ctx.start('props');assert.equal(e.calls.length,1);
      assert.equal(e.$('wz-props-state').attributes.role,'alert');
    }
  });
  await test('cleanup adds optional unchecked props independent from terrain',async()=>{
    const e=fixture.fixtureEnv();e.ui.open('cleanup');const f=e.ui.nodes.fields;
    assert.equal(f.props.checked,false);assert.equal(f.terrain.checked,true);
    Object.values(f).forEach(x=>x.checked=false);f.props.checked=true;
    e.setResponse({...e.getResponse(),selection:{terrain:false,catalog:false,names:false,captures:false,props:true},
      items:[{category:'props',path:'game/fixture/props',bytes:4096,files:1,action:'quarantine'}]});
    await e.ui.preview();assert.equal(e.calls[0].body.selection.props,true);assert.equal(e.calls[0].body.selection.terrain,false);
    e.ui.nodes.confirm.checked=true;await e.ui.commit();
    assert.match(e.ui.nodes.feedback.textContent,/单独补生成道具形状/);
    assert.match(e.ui.nodes.feedback.textContent,/不需要重做地形/);
    const actions=e.ui.nodes.guide.children[1].children.filter(x=>!x.hidden);
    assert.equal(actions.length,1);actions[0].click();assert.deepEqual(Array.from(e.navigation),['props']);
  });
  await test('both pages bind props button without changing catalog build action',()=>{
    assert(desktop.includes("parityButton('resource-props-start',()=>resourceGeneration('/api/wizard/props/start',{}))"));
    assert(wizard.includes('$("wz-props-start").addEventListener("click", startProps)'));
    assert(desktop.includes('/api/wizard/catalog/start'));assert(wizard.includes('/api/wizard/catalog/start'));
  });
  console.log(JSON.stringify({passed:tests.length,failed:0,tests},null,2));
})().catch(error=>{console.error(error.stack);process.exitCode=1;});
