// Build navigation and replay real, collision-adjusted trajectories without game I/O.
import fs from 'node:fs';
import path from 'node:path';
import {performance} from 'node:perf_hooks';
import {loadAsset} from './assets.mjs';
import {init,TerrainWorld} from './core.mjs';
const argv=process.argv.slice(2),arg=k=>{const i=argv.indexOf(k);return i<0?undefined:argv[i+1];};
if(!arg('--asset')||!arg('--out'))throw Error('--asset and --out required');
const file=path.resolve(arg('--asset')),out=path.resolve(arg('--out'));fs.mkdirSync(out,{recursive:true});
const asset=loadAsset(path.dirname(file),path.basename(file),{offline:true});
await init();const t=performance.now();const world=new TerrainWorld(asset.positions,asset.indices);
const builtMs=performance.now()-t;
const ground=[];
for(const z of [-18,-12,-6,0,6,12,18])for(const x of [-18,-12,-6,0,6,12,18]){
  const hit=world.ground([x,30,z],100);
  if(hit&&hit.normal[1]>.75)ground.push(hit.position);
}
const routes=[];
try{
  for(let i=0;i<ground.length && routes.length<12;i++){
    const start=ground[i];let goal=null,plan=null;
    for(let j=ground.length-1;j>=0;j--){
      if(Math.hypot(start[0]-ground[j][0],start[2]-ground[j][2])<8)continue;
      const p=world.plan(start,ground[j]);if(p.status==='ready'){goal=ground[j];plan=p;break;}
    }
    if(!plan)continue;
    let motor;
    try{motor=world.motor(start,plan.path,0);}catch(e){routes.push({start,goal,status:'blocked',reason:e.message});continue;}
    const trace=[];let last=motor.sample(0);const deadline=Math.min(60,plan.length/world.config.speed*3+5);
    for(let frame=1;frame<=deadline*60;frame++){
      last=motor.sample(frame/60);if(frame%6===0||last.arrived||last.blocked)trace.push({time:frame/60,...last});
      if(last.arrived||last.blocked)break;
    }
    routes.push({start,goal,plannedGoal:plan.goal,path:plan.path,length:plan.length,
      status:last.arrived?'arrived':last.blocked?'blocked':'timeout',reason:last.reason,trace});motor.dispose();
  }
  const [positions,indices]=world.navGeometry();
  const atomic=(name,data)=>{const p=path.join(out,name),tmp=p+'.tmp';fs.writeFileSync(tmp,data);fs.renameSync(tmp,p);};
  if(world.nav)atomic('navigation.bin',world.exportNav());
  atomic('navigation-preview.json',JSON.stringify({positions,indices}));
  const report={mode:'offline',mapName:asset.manifest.mapName,geometryKind:asset.manifest.geometryKind,
    nativeCollisionVerified:asset.manifest.nativeCollisionVerified,config:world.config,builtMs,
    navigationReady:!!world.nav,navigationMode:world.navMode,navError:world.navError,
    inputVertices:asset.positions.length/3,inputTriangles:asset.indices.length/3,
    navVertices:positions.length/3,navTriangles:indices.length/3,groundProbes:ground.length,
    trials:routes.length,arrived:routes.filter(r=>r.status==='arrived').length,
    blocked:routes.filter(r=>r.status==='blocked').length,timeout:routes.filter(r=>r.status==='timeout').length,
    meaning:'Offline static geometry test; not native movement equivalence or live client verification.',routes};
  atomic('replay.json',JSON.stringify(report));
  const {routes:_,...summary}=report;atomic('summary.json',JSON.stringify(summary,null,2));console.log(JSON.stringify(summary));
}finally{world.dispose();}
