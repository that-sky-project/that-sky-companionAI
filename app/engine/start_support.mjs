// Start-only projection proposal from a queried support plane; never avatar-foot calibration.
import {DEFAULTS,navigationWindow} from './core.mjs';

export const START_SUPPORT_LIMITS=Object.freeze({maximumLift:.3,queryAbove:.3,queryDistance:.6,epsilon:.0001,normalLengthTolerance:.001});
const point=v=>Array.isArray(v)&&v.length===3&&v.every(n=>typeof n==='number'&&Number.isFinite(n)&&Math.abs(n)<1e6);
const defaultConfig=c=>c&&typeof c==='object'&&!Array.isArray(c)&&Object.keys(c).length===Object.keys(DEFAULTS).length&&Object.keys(DEFAULTS).every(k=>c[k]===DEFAULTS[k]);
const copyHit=h=>h&&point(h.position)&&Array.isArray(h.normal)?{position:[...h.position],normal:[...h.normal],distance:h.distance}:null;
function base(rawStart,support){return {status:'blocked',rawStart:point(rawStart)?[...rawStart]:null,adjustedStart:null,deltaY:null,delta:null,reason:null,support:copyHit(support),
 audit:{kind:'start_support_projection_not_avatar_calibration',maximumLift:.3,epsilon:.0001,defaultParametersOnly:true,normalPolicy:'use queried normal unchanged; no abs, flip, or normalization',
 collisionCheckRequired:true,collisionClearanceVerified:false,nativeCollisionVerified:false,footOffsetCalibrated:false,calibrationInstalled:false,runtimeReady:false}};}
const blocked=(result,reason,details={})=>({...result,status:'blocked',reason,...details});

/** Pure function. supportHit is the actual world.ground result; callers must preserve its normal. */
export function calculateStartSupportProjection(rawStart,supportHit,config=DEFAULTS){
 const result=base(rawStart,supportHit),limits=START_SUPPORT_LIMITS;
 if(!point(rawStart))return blocked(result,'invalid_start');
 if(!defaultConfig(config))return blocked(result,'unsupported_nondefault_config');
 if(!supportHit)return blocked(result,'no_support_in_start_band');
 if(!point(supportHit.position)||!Array.isArray(supportHit.normal)||supportHit.normal.length!==3||!supportHit.normal.every(Number.isFinite))return blocked(result,'invalid_support_hit');
 if(supportHit.position[0]!==rawStart[0]||supportHit.position[2]!==rawStart[2])return blocked(result,'support_xz_mismatch');
 const groundY=supportHit.position[1],normalY=supportHit.normal[1],heightDifference=groundY-rawStart[1];
 if(heightDifference>limits.queryAbove||heightDifference<limits.queryAbove-limits.queryDistance)return blocked(result,'support_outside_start_band');
 const distance=supportHit.distance,expectedDistance=rawStart[1]+limits.queryAbove-groundY;
 if(typeof distance!=='number'||!Number.isFinite(distance)||distance<0||distance>limits.queryDistance||Math.abs(distance-expectedDistance)>1e-6)return blocked(result,'support_ray_distance_mismatch');
 if(normalY<=0||normalY>1)return blocked(result,'support_normal_not_upward');
 if(Math.abs(Math.hypot(...supportHit.normal)-1)>limits.normalLengthTolerance)return blocked(result,'support_normal_not_unit');
 if(normalY<Math.cos(config.slopeDegrees*Math.PI/180))return blocked(result,'support_slope_exceeds_existing_limit');
 const slopeClearance=Math.max(0,config.radius*(1/normalY-1)-config.skin);
 const requiredY=groundY+slopeClearance+limits.epsilon,newY=Math.max(rawStart[1],requiredY),deltaY=newY-rawStart[1];
 const calculations={groundY,normalY,slopeClearance,requiredY,formula:'newY=max(rawY,groundY+max(0,radius*(1/queriedNormalY-1)-skin)+0.0001)'};
 if(deltaY>limits.maximumLift)return blocked(result,'minimum_foot_lift_exceeds_0_3',{proposedDeltaY:deltaY,calculations});
 const adjustedStart=[rawStart[0],newY,rawStart[2]];
 if(!point(adjustedStart))return blocked(result,'adjusted_start_outside_actor_range',{proposedDeltaY:deltaY,calculations});
 return {...result,status:deltaY>0?'adjusted':'unchanged',adjustedStart,deltaY,delta:[0,deltaY,0],reason:deltaY>0?'projected_to_queried_support':'already_at_or_above_required_start',calculations};
}

/** Read-only query proposal. Does not create a motor, change config, plan, path, goal, or mesh. */
export function queryStartSupportProjection(world,rawStart){
 let result=base(rawStart,null);
 if(!point(rawStart))return blocked(result,'invalid_start');
 if(!world||typeof world.ground!=='function'||!defaultConfig(world.config))return blocked(result,'invalid_world_or_nondefault_config');
 let window;
 try{window=navigationWindow(world.navigationWindow);}catch{return blocked(result,'navigation_window_invalid');}
 if(window&&(rawStart[0]<window.minX||rawStart[0]>window.maxX||rawStart[2]<window.minZ||rawStart[2]>window.maxZ))return blocked(result,'outside_navigation_window');
 const origin=[rawStart[0],rawStart[1]+START_SUPPORT_LIMITS.queryAbove,rawStart[2]];
 if(!point(origin))return blocked(result,'query_origin_outside_actor_range');
 const configBefore=JSON.stringify(world.config),windowBefore=JSON.stringify(world.navigationWindow);
 let hit;
 try{hit=world.ground(origin,START_SUPPORT_LIMITS.queryDistance);}catch(error){return blocked(result,'support_query_failed',{queryError:String(error?.message??error).slice(0,160)});}
 if(JSON.stringify(world.config)!==configBefore||JSON.stringify(world.navigationWindow)!==windowBefore)return blocked(result,'world_constraints_changed_during_query');
 result=calculateStartSupportProjection(rawStart,hit,world.config);
 return {...result,query:{origin,distance:START_SUPPORT_LIMITS.queryDistance},audit:{...result.audit,windowChecked:true,navigationWindow:window?{...window}:null}};
}