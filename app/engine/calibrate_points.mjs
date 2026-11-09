// Compare historical actor-origin grid samples with real geometry; no game I/O.
import fs from 'node:fs';
import path from 'node:path';
import {init,TerrainWorld} from './core.mjs';
import {loadAsset} from './assets.mjs';
const argv=process.argv.slice(2),arg=k=>{const i=argv.indexOf(k);return i<0?undefined:argv[i+1];};
const file=path.resolve(arg('--asset')),input=path.resolve(arg('--input')),out=path.resolve(arg('--out'));
const key=arg('--key');if(!key)throw Error('--key required');
if(fs.statSync(input).size>8*1024*1024)throw Error('input_budget');
const data=JSON.parse(fs.readFileSync(input,'utf8'))[key];
if(!data||!Array.isArray(data.points)||data.points.length>100000||data.cell!==1)throw Error('sample_schema');
const a=loadAsset(path.dirname(file),path.basename(file),{offline:true});
await init();const w=new TerrainWorld(a.positions,a.indices,{navigation:false});
const rows=[];
try{
  for(const [x,y,z,count] of data.points){
    if(![x,y,z,count].every(Number.isFinite)||count<=0)throw Error('sample_value');
    const below=w.ground([x,y+.5,z],4);
    if(!below){rows.push({origin:[x,y,z],count,status:'no_near_surface'});continue;}
    const patch=[];
    for(const dx of [-.49,0,.49])for(const dz of [-.49,0,.49]){
      const g=w.ground([x+dx,y+.5,z+dz],4);if(g)patch.push(g);
    }
    const range=patch.length?Math.max(...patch.map(p=>p.position[1]))-Math.min(...patch.map(p=>p.position[1])):null;
    const delta=y-below.position[1];
    const minNormalY=patch.length?Math.min(...patch.map(p=>p.normal[1])):null;
    const flat=patch.length===9&&range<=.10&&minNormalY>=Math.cos(10*Math.PI/180);
    rows.push({origin:[x,y,z],count,status:'near_surface',ground:below.position,normal:below.normal,
      originMinusGround:delta,footprintHits:patch.length,footprintHeightRange:range,minNormalY,
      flatFootprint:flat,conservativeCandidate:flat&&count>=3&&delta>=-.2&&delta<=1});
  }
  const quant=(x,p)=>{if(!x.length)return null;const a=[...x].sort((a,b)=>a-b),i=(a.length-1)*p,j=Math.floor(i);return a[j]+(a[Math.min(j+1,a.length-1)]-a[j])*(i-j);};
  const stats=xs=>({n:xs.length,p05:quant(xs,.05),p25:quant(xs,.25),median:quant(xs,.5),p75:quant(xs,.75),p95:quant(xs,.95)});
  const near=rows.filter(r=>r.status==='near_surface'),flat=near.filter(r=>r.flatFootprint),clean=near.filter(r=>r.conservativeCandidate);
  const hist={};for(const r of clean){const key=(Math.round(r.originMinusGround/.025)*.025).toFixed(3);hist[key]=(hist[key]||0)+1;}
  const summary={mapName:a.manifest.mapName,mapId:data.map_id,points:rows.length,nearSurface:near.length,
    noNearSurface:rows.length-near.length,flatFootprint:flat.length,conservativeCandidates:clean.length,
    allNear:stats(near.map(r=>r.originMinusGround)),flat:stats(flat.map(r=>r.originMinusGround)),
    conservative:stats(clean.map(r=>r.originMinusGround)),histogram:hist,
    rules:{rayStartAboveOrigin:.5,rayLength:4,footprintHalfWidth:.49,maxFootprintRelief:.1,minNormalY:Math.cos(10*Math.PI/180),minCount:3,deltaRange:[-.2,1]},
    warnings:['Aggregated actor origins, not labeled grounded or feet observations.','One-meter XZ quantization loses exact positions.','Counts are not independent measurements.','No automatic physics or live readiness changes.'],nativeCollisionVerified:false};
  fs.mkdirSync(out,{recursive:true});
  for(const [name,obj] of [['summary.json',summary],['rows.json',rows]]){const dest=path.join(out,name),tmp=dest+'.tmp';fs.writeFileSync(tmp,JSON.stringify(obj,null,2));fs.renameSync(tmp,dest);}
  console.log(JSON.stringify(summary));
}finally{w.dispose();}
