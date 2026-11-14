'use strict';
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const ROOT=path.resolve(__dirname,'../..');
const source=fs.readFileSync(path.join(ROOT,'app/web/desktop.js'),'utf8');
const begin=source.indexOf('function checkedTitle()'),end=source.indexOf('async function refreshLogs()',begin);
assert(begin>=0&&end>begin);
const warning='仅完成基本格式校验；本地词表未生成，未进行本地词表审核，游戏内仍可能屏蔽或拦截。';
function fixture(reject=false){
  const nodes=new Map(),calls=[],messages=[],confirmations=[];
  const byId=id=>{if(!nodes.has(id))nodes.set(id,{value:'',hidden:false,placeholder:''});return nodes.get(id);};
  byId('owner-title-input').value='姐姐';
  const ctx=vm.createContext({byId,parityApi:async(url,body)=>{
    calls.push({url,body});if(reject)throw new Error('审核规则不可用或不完整');
    return {ok:true,message:body.check?warning:'已改为“姐姐”。'+warning,title:'姐姐',restart_required:false};
  },parityMsg:(id,message)=>messages.push({id,message}),
    parityConfirm:async(title,message)=>{confirmations.push({title,message});return true;},
    setText:(id,text)=>{byId(id).textContent=text;},refreshStatus:async()=>{},Error});
  vm.runInContext(source.slice(begin,end)+'\nglobalThis.check=checkTitle;globalThis.save=saveTitle;',ctx);
  return {ctx,byId,calls,messages,confirmations};
}
(async()=>{
  let f=fixture();await f.ctx.check();
  assert.equal(f.calls.length,1);assert.equal(f.calls[0].body.check,true);
  assert.equal(f.messages[0].message,warning);assert(!f.messages[0].message.includes('审核通过'));
  f=fixture();await f.ctx.save();
  assert.equal(f.calls.length,2);assert.equal(f.confirmations.length,1);
  assert.equal(f.calls[1].body.title,'姐姐');assert.equal(f.calls[1].body.check,undefined);
  assert.equal(f.messages.length,2);for(const m of f.messages){assert(m.message.includes('未进行本地词表审核'));assert(!m.message.includes('审核通过'));}
  assert.equal(f.byId('owner-title-current').textContent,'当前称呼：姐姐');
  assert.equal(f.byId('owner-title-restart').hidden,true);
  f=fixture(true);await assert.rejects(()=>f.ctx.save(),/审核规则不可用/);
  assert.equal(f.calls.length,1);assert.equal(f.confirmations.length,0);assert.equal(f.messages.length,0);
  console.log(JSON.stringify({passed:3,failed:0,source:'actual desktop checkedTitle/checkTitle/saveTitle'}));
})().catch(error=>{console.error(error.stack);process.exitCode=1;});
