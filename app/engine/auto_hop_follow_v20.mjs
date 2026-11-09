// Cold, opt-in fallback only. A network/ownership failure is never a jump request.
import {performance} from 'node:perf_hooks';
import {DEFAULTS} from './core.mjs';
import {prepareBoundedHop} from './bounded_hop_v20.mjs';

const finitePoint=p=>Array.isArray(p)&&p.length===3&&p.every(n=>typeof n==='number'&&Number.isFinite(n)&&Math.abs(n)<1e6);
const horizontal=(a,b)=>Math.hypot(a[0]-b[0],a[2]-b[2]);
const geometryCauses=new Set(['collision_blocked','small_step_blocked','incomplete_polygon_corridor','goal_support_slope','ground_contact_gap','ground_route_incomplete']);
const supportY=Math.cos(50*Math.PI/180); // Native anchor gate, NOT a global KCC slope change.

export function prepareAutomaticHop(world,{groundResult,start,target,speed,scope,collisionWindow}={}){
  if(groundResult?.status==='ready')return {status:'not_needed',reason:'ground_route_available'};
  if(groundResult?.status!=='blocked'||!geometryCauses.has(groundResult.reason))
    return {status:'not_eligible',reason:'not_a_confirmed_local_geometry_block'};
  if(!finitePoint(start)||!finitePoint(target)||!Number.isFinite(speed)||speed<=0||speed>3)
    throw Error('auto_hop:request');
  if(!world||world.motors?.size!==0)throw Error('auto_hop:idle_world_required');
  const distance=horizontal(start,target);
  if(distance<.35)return {status:'blocked',reason:'no_forward_landing_search'};
  const dx=(target[0]-start[0])/distance,dz=(target[2]-start[2])/distance;
  const candidates=[],seen=new Set();let probes=0;
  // The radial pattern is source-derived. Forward distances and search budgets
  // are explicit engineering choices and do not claim native jump triggers.
  for(const forward of [...new Set([1.9,2,1.6,1.25,.9,.75,.55,.35].map(x=>Math.min(x,distance)))]){
    for(let i=0;i<32;i++){
      const radius=.15*Math.sqrt(i/32),angle=i*2.399963140487671;
      const x=start[0]+dx*forward+radius*Math.cos(angle),z=start[2]+dz*forward+radius*Math.sin(angle);
      if(horizontal(start,[x,0,z])>2)continue;
      const pad=DEFAULTS.radius+DEFAULTS.skin;
      if(!collisionWindow||x-pad<collisionWindow.minX||x+pad>collisionWindow.maxX||z-pad<collisionWindow.minZ||z+pad>collisionWindow.maxZ)continue;
      probes++;
      const hit=world.ground([x,start[1]+1.05,z],1.1);
      if(!hit||!finitePoint(hit.position)||!finitePoint(hit.normal)||Math.abs(Math.hypot(...hit.normal)-1)>.001||hit.normal[1]<supportY)continue;
      // Ray support is a candidate only. Full capsule clearance and the entire
      // warmup/takeoff/landing/soft-cancel trajectory must still pass below.
      const landing=[x,hit.position[1]+2*DEFAULTS.skin,z],rise=landing[1]-start[1];
      if(rise<.08||rise>.95||horizontal(start,landing)>2)continue;
      const key=landing.map(n=>Math.round(n*1000)).join(',');if(seen.has(key))continue;seen.add(key);
      candidates.push({landing,normal:hit.normal,forward,radius,rise,
        score:horizontal(start,landing)+.2*radius});
    }
  }
  // Spend the bounded proof budget across distance tiers, not 8 nearby rays.
  // These priorities/minimum progress are engineering choices, not native rules.
  const tiers=[...new Set([1.9,2,1.6,1.25,.9,.75,.55,.35].map(x=>Math.min(x,distance)))];
  const rings=tiers.map(forward=>candidates.filter(c=>c.forward===forward)
    .sort((a,b)=>a.radius-b.radius||a.score-b.score));
  const schedule=[];
  for(let rank=0;rank<32;rank++) for(const ring of rings)
    if(ring[rank])schedule.push(ring[rank]);
  const minimumForwardProgress=Math.min(.35,Math.max(.2,distance-.2));
  const failures=[],began=performance.now();let attempts=0;
  for(const candidate of schedule){
    if(attempts>=8||performance.now()-began>1000)break;
    attempts++;
    try{
      const prepared=prepareBoundedHop(world,{start,landing:candidate.landing,speed,scope,collisionWindow});
      const terminal=prepared.receipt.mainTrace.at(-1);
      const forwardProgress=(terminal.position[0]-start[0])*dx+(terminal.position[2]-start[2])*dz;
      if(terminal.phase!=='landed'||!terminal.grounded||forwardProgress<minimumForwardProgress){
        failures.push(`auto_hop:insufficient_actual_forward_progress:${forwardProgress.toFixed(6)}`);
        continue;
      }
      return {status:'ready',kind:'automatic_small_hop',prepared,
        audit:{trigger:groundResult.reason,probes,candidates:candidates.length,attempts,
          selectedLanding:candidate.landing,selectedSupportNormal:candidate.normal,
          selectedForward:candidate.forward,actualForwardProgress:forwardProgress,minimumForwardProgress,
          attemptTiers:schedule.slice(0,attempts).map(c=>c.forward),
          requiresGroundingAndLanding:true,nativeTriggerVerified:false,liveEnabled:false}};
    }catch(error){failures.push(String(error.message).slice(0,160));}
  }
  return {status:'blocked',reason:'no_prevalidated_short_hop',audit:{probes,candidates:candidates.length,attempts,failures,attemptTiers:schedule.slice(0,attempts).map(c=>c.forward),minimumForwardProgress,nativeTriggerVerified:false,liveEnabled:false}};
}
