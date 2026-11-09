// Explicit finite aerial fallback after the ordinary short-hop proof failed.
import {performance} from 'node:perf_hooks';
import {DEFAULTS} from './core.mjs';
import {aerialProfile} from './aerial_profiles_v22.mjs';
import {probeAerialLanding,prepareBoundedAerial} from './bounded_aerial_v22.mjs';
const H=(a,b)=>Math.hypot(a[0]-b[0],a[2]-b[2]);
const causes=new Set(['collision_blocked','small_step_blocked','incomplete_polygon_corridor','goal_support_slope','ground_contact_gap','ground_route_incomplete']);
export function prepareAutomaticAerial(world,{groundResult,start,target,speed,scope,collisionWindow,aerialProfiles=[]}={}){
 if(groundResult?.status!=='blocked'||!causes.has(groundResult.reason))return {status:'not_eligible',reason:'not_a_confirmed_local_geometry_block'};
 if(!Array.isArray(aerialProfiles)||aerialProfiles.length>3||new Set(aerialProfiles).size!==aerialProfiles.length||aerialProfiles.some(x=>!['drop_v22','flap_v22','jump_drop_v23'].includes(x)))throw Error('aerial_profiles_invalid');
 if(aerialProfiles.includes('jump_drop_v23')&&!aerialProfiles.includes('drop_v22'))throw Error('jump_drop_requires_natural_drop');
 const orderedProfiles=['drop_v22','jump_drop_v23','flap_v22'].filter(x=>aerialProfiles.includes(x));let naturalDropObstructed=false;
 const distance=H(start,target);if(distance<.2)return {status:'blocked',reason:'aerial_no_forward_target'};
 const direction=[(target[0]-start[0])/distance,0,(target[2]-start[2])/distance],failures=[],began=performance.now();let attempts=0;
 const supportAt=(x,z,p)=>{const r=DEFAULTS.radius+DEFAULTS.skin;if(x-r<collisionWindow.minX||x+r>collisionWindow.maxX||z-r<collisionWindow.minZ||z+r>collisionWindow.maxZ)return null;const hit=world.ground([x,start[1]+p.maxRise+.4,z],p.maxRise+p.maxDrop+.8);if(!hit||hit.normal[1]<Math.cos(DEFAULTS.slopeDegrees*Math.PI/180))return null;return [x,hit.position[1]+2*DEFAULTS.skin,z];};
 for(const profileId of orderedProfiles){const p=aerialProfile(profileId),drop=profileId==='drop_v22'||profileId==='jump_drop_v23';if(profileId==='jump_drop_v23'&&!naturalDropObstructed)continue;if(drop&&target[1]>=start[1]-.3)continue;if(!drop&&target[1]<start[1]-p.maxDrop)continue;
  const tiers=drop?[Math.min(distance,p.maxHorizontalDistance-.35),Math.min(5.5,distance),Math.min(4,distance)]:[Math.min(4,distance),Math.min(3.5,distance),Math.min(3,distance),Math.min(2.5,distance),Math.min(2,distance),Math.min(1.5,distance)];
  for(const d of [...new Set(tiers)]){if(attempts>=10||performance.now()-began>4000)break;const candidate=supportAt(start[0]+direction[0]*d,start[2]+direction[2]*d,p);if(!candidate)continue;const dy=candidate[1]-start[1];if(drop?(dy>=-.3||dy<-p.maxDrop):(dy<-p.maxDrop||dy>p.maxRise-.05))continue;attempts++;
   try{const request={start,landing:candidate,speed,scope,collisionWindow,profileId},probe=probeAerialLanding(world,request),landing=probe.landing,forward=(landing[0]-start[0])*direction[0]+(landing[2]-start[2])*direction[2];
    if(forward<.35||(drop?landing[1]>start[1]-.3:landing[1]<start[1]-p.maxDrop))throw Error('aerial_insufficient_supported_progress');
    const prepared=prepareBoundedAerial(world,{...request,landing});return {status:'ready',kind:profileId,prepared,audit:{trigger:groundResult.reason,profileId,attempts,failures,desiredCandidate:candidate,selectedLanding:landing,actualForwardProgress:forward,requiresGroundingAndLanding:true,engineeringProfile:true,nativePhysicsEquivalent:false}};
   }catch(e){if(profileId==='drop_v22'&&String(e.message)==='bounded_aerial:motor_collision_blocked')naturalDropObstructed=true;failures.push({profileId,distance:d,reason:String(e.message).slice(0,180)});}
  }
 }
 return {status:'blocked',reason:'no_prevalidated_aerial_route',audit:{attempts,failures,aerialProfiles,orderedProfiles,naturalDropObstructed}};
}
