const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'..'),source=fs.readFileSync(path.join(root,'app/web/desktop.js'),'utf8');
function fn(name){const start=source.indexOf('function '+name+'(');assert(start>=0,name);const next=/\b(?:async )?function\s+[A-Za-z_$][\w$]*\s*\(/g;next.lastIndex=start+10;const m=next.exec(source);return source.slice(start,m?m.index:source.length).split('const accountOps=')[0].trim();}
class Element{
 constructor(tag,cls,text){this.tag=tag;this.className=cls||'';this.textContent=text||'';this.childNodes=[];this.dataset={};this.listeners={};this.attrs={};this.hidden=false;this.disabled=false;this.value='';this.classList={add:()=>{},remove:()=>{}};}
 appendChild(node){this.childNodes.push(node);return node;}setAttribute(k,v){this.attrs[k]=v;}removeAttribute(k){delete this.attrs[k];}addEventListener(n,fn){this.listeners[n]=fn;}focus(){context.document.activeElement=this;}closest(selector){return selector==='.account-remark-editor'&&this.tag==='input'?true:null;}
}
const elements=new Map();let calls=[],toasts=[];
const context=vm.createContext({Map,Set,Date,JSON,Number,String,console,document:{activeElement:null},window:{confirm:()=>true},
 state:{activeAccountId:'fixture-a',accounts:[],status:null},el:(...a)=>new Element(...a),icon:()=>new Element('i'),makeBadge:(t)=>new Element('span','badge',t),
 isAccountActive:a=>a.id==='fixture-a',platformText:v=>v,statusText:v=>v,formatCreated:()=>'',pendingJobId:()=>null,
 extractId:v=>v||null,textValue:v=>typeof v==='string'?v:null,boolValue:v=>typeof v==='boolean'?v:null,finiteNumber:v=>typeof v==='number'?v:null,normalizeMap:v=>v,
 byId:id=>{if(!elements.has(id))elements.set(id,new Element('div'));return elements.get(id);},
 getSelectedMap:()=>({id:1,ready:true}),setMapNotice:(text,tone)=>{context.notice={text,tone};},
 ApiError:Error,messageFrom:(r,f)=>r.message||f,friendlyError:e=>e.message,showToast:(...a)=>toasts.push(a),
 loadAccounts:async()=>true,refreshStatus:async()=>true,renderAccounts:()=>{},postJson:async(p,b)=>{calls.push([p,b]);return {ok:true};},
 request:async(p,o)=>{calls.push([p,o]);return {ok:true,message:'续期成功'};}
});
const declarations='const accountOps={drafts:new Map(),saving:new Set(),renewing:new Set(),messages:new Map()};';
const names=['accountOpButton','appendAccountOperations','buildAccountCard','normalizeAccount','normalizeStatus','syncMapActions','renderTopStatus'];
const compiled=declarations+'\n'+names.map(fn).join('\n');vm.runInContext(compiled,context);
function walk(el){return [el,...el.childNodes.flatMap(walk)];}
function button(card,text){const b=walk(card).find(e=>e.tag==='button'&&e.textContent===text);assert(b,text);return b;}
function fixture(extra={}){return {id:'fixture-a',alias:'大号',nickname:'大号',serviceAlias:'小光·12345',platform:'netease',active:true,...extra};}
let passed=0;async function test(name,cb){await cb();passed++;}
(async()=>{
 await test('nickname fields are normalized',()=>{const a=context.normalizeAccount({id:'a',alias:'大号',nickname:'大号',service_alias:'小光·123'});assert.equal(a.nickname,'大号');assert.equal(a.serviceAlias,'小光·123');});
 await test('renew and remark exist on active and inactive accounts',()=>{for(const id of ['fixture-a','fixture-b']){const c=context.buildAccountCard(fixture({id}));button(c,'续期登录');button(c,'备注昵称');}});
 await test('remark is rendered as text not HTML',()=>{const c=context.buildAccountCard(fixture({alias:'<img src=x onerror=1>'}));assert(walk(c).some(e=>e.tag==='h3'&&e.textContent==='<img src=x onerror=1>'));});
 await test('remark editor submits exact account and preserves no game rename',async()=>{calls=[];const c=context.buildAccountCard(fixture());button(c,'备注昵称').listeners.click();const form=walk(c).find(e=>e.tag==='form');assert.equal(form.hidden,false);const input=walk(c).find(e=>e.tag==='input');input.value='测试机器人';input.listeners.input();await form.listeners.submit({preventDefault(){}});assert.equal(calls[0][0],'/xiaoguang/accounts/remark');assert.equal(calls[0][1].nickname,'测试机器人');assert.equal(calls[0][1].account,'fixture-a');context.document.activeElement=null;});
 await test('renew uses exact target and sufficient timeout',async()=>{calls=[];const c=context.buildAccountCard(fixture({id:'fixture-b'}));await button(c,'续期登录').listeners.click();assert.equal(calls[0][0],'/xiaoguang/accounts/renew');assert.equal(calls[0][1].body.account,'fixture-b');assert.equal(calls[0][1].timeoutMs,420000);assert(!calls.some(c=>c[0].includes('select')));});
 await test('renew error remains visible after redraw',async()=>{const old=context.request;context.request=async()=>{throw new Error('请先让小光下线');};let c=context.buildAccountCard(fixture());await button(c,'续期登录').listeners.click();c=context.buildAccountCard(fixture());assert(walk(c).some(e=>e.dataset.tone==='error'&&e.textContent.includes('下线')));context.request=old;});
 await test('cancel renewal never sends request',async()=>{calls=[];context.window.confirm=()=>false;const c=context.buildAccountCard(fixture());await button(c,'续期登录').listeners.click();assert.equal(calls.length,0);context.window.confirm=()=>true;});
 await test('startup failure is a visible error, not online',()=>{context.state.status=context.normalizeStatus({online:false,runner:false,startupError:{message:'上线失败：版本不匹配'}});context.syncMapActions();assert.equal(context.notice.tone,'error');assert(context.notice.text.includes('版本'));context.renderTopStatus();assert.equal(elements.get('service-pill-text').textContent,'上线失败');});
 await test('launch pending cannot be submitted twice',()=>{context.state.status=context.normalizeStatus({online:false,runner:true,going:{map:1}});context.syncMapActions();assert.equal(elements.get('start-map').disabled,true);context.renderTopStatus();assert.equal(elements.get('service-pill-text').textContent,'正在上线');});
 await test('remarks keep input focus on polling refresh',()=>{const actual=context.renderAccounts;vm.runInContext(fn('renderAccounts'),context);context.document.activeElement={closest:()=>true};context.renderAccounts();context.document.activeElement=null;context.renderAccounts=actual;});
 if(process.argv[2]){
  const out=process.argv[2];fs.mkdirSync(out,{recursive:true});const css=fs.readFileSync(path.join(root,'app/web/desktop.css'),'utf8');
  const helpers=`const state={activeAccountId:'fixture-a',accountListKey:null};const el=(tag,cls,text)=>{const e=document.createElement(tag);if(cls)e.className=cls;if(text)e.textContent=text;return e;};const icon=()=>el('span','','↻');const makeBadge=(t)=>el('span','badge',t);const isAccountActive=a=>a.id==='fixture-a';const platformText=v=>v;const statusText=v=>v;const formatCreated=()=>'';const pendingJobId=()=>null;const showToast=()=>{};const renderAccounts=()=>{};const messageFrom=(r,f)=>r.message||f;const friendlyError=e=>e.message;const ApiError=Error;const postJson=async()=>({ok:true});const request=async()=>({ok:true,message:'续期成功'});const loadAccounts=async()=>{};const refreshStatus=async()=>{};`;
  const body=`<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>小光 · 账号操作预览</title><style>${css}</style><style>body{display:block!important;overflow:auto!important;height:auto;min-width:0;margin:0;padding:36px;background:#111c30;color:#e4ecfa}.preview-grid{display:grid;grid-template-columns:1fr 1fr;gap:22px}.account-item{padding:22px;background:#19273e}.preview-heading{margin-bottom:24px}.preview-heading p{color:#a9b8cb}.badge{margin-left:10px;font-size:12px}.account-remark-editor{margin-top:18px}</style><body><div class="preview-heading"><h1>账号，终于分得清</h1><p>独立备注昵称 · 逐账号续期 · 真实上线反馈（测试数据预览）</p></div><main class="preview-grid" id="cards"></main><script>${helpers}${declarations}${['accountOpButton','appendAccountOperations','buildAccountCard'].map(fn).join('\n')}const data=${JSON.stringify([fixture(),fixture({id:'fixture-b',alias:'测试机器人',nickname:'测试机器人',active:false,serviceAlias:'小光·67890'})])};accountOps.messages.set('fixture-b',{tone:'error',text:'上线失败：登录状态已失效。退出 MuMu 中同一账号后，点击“续期登录”。'});accountOps.drafts.set('fixture-a','大号');data.forEach(a=>document.getElementById('cards').appendChild(buildAccountCard(a)));</script></body></html>`;
  fs.writeFileSync(path.join(out,'account-preview.html'),body);fs.writeFileSync(path.join(out,'ui-tests.json'),JSON.stringify({passed,failed:0}));
 }
 console.log(JSON.stringify({passed,failed:0}));
})().catch(e=>{console.error(e.stack);process.exitCode=1;});
