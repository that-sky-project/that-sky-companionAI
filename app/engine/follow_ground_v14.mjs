// Cold-only follow proposals. A failed nav query alone never authorizes motion.
import {Detour,statusDetail} from 'recast-navigation';
import {DEFAULTS} from './core.mjs';
import {calculateStartSupportProjection,START_SUPPORT_LIMITS} from './start_support.mjs';
import {BOUNDED_STEP_POLICY,routeCollisionWindow} from './offline_motion_world.mjs';
export {BOUNDED_STEP_POLICY};
const need=(ok,why)=>{if(!ok)throw Error('follow_ground:'+why);};
const point=p=>Array.isArray(p)&&p.length===3&&p.every(n=>typeof n==='number'&&Number.isFinite(n)&&Math.abs(n)<1e6);
const dist=(a,b)=>Math.hypot(...a.map((v,i)=>v-b[i]));
const horizontal=(a,b)=>Math.hypot(a[0]-b[0],a[2]-b[2]);
const arc=p=>p.slice(1).reduce((s,v,i)=>s+dist(v,p[i]),0);
export function validateFollowIntent(i){
 need(i&&typeof i==='object'&&!Array.isArray(i)&&Object.keys(i).sort().join(',')==='distance,goal,maxEndpointDistance,peer,segmentLength','intent_fields');
 need(point(i.goal)&&point(i.peer),'intent_points');
 for(const k of ['distance','segmentLength','maxEndpointDistance'])need(typeof i[k]==='number'&&Number.isFinite(i[k]),'intent_numbers');
 need(i.distance>=0&&i.distance<=1.3&&i.segmentLength>0&&i.segmentLength<=6&&i.maxEndpointDistance>=i.distance&&i.maxEndpointDistance<=1.3,'intent_limits');
 need(horizontal(i.goal,i.peer)<=i.maxEndpointDistance+1e-5&&Math.abs(i.goal[1]-i.peer[1])<=.001,'intent_peer_anchor');return i;
}
export function supportedFollowGoal(groundAt,p){
 const hit=groundAt([p[0],p[1]+.75,p[2]],1.5);
 need(hit&&point(hit.position)&&point(hit.normal),'goal_support_missing');
 need(Math.abs(hit.position[1]-p[1])<=.75+1e-6,'goal_support_height_band');
 need(Math.abs(Math.hypot(...hit.normal)-1)<=.001&&hit.normal[1]>=Math.cos(DEFAULTS.slopeDegrees*Math.PI/180),'goal_support_slope');
 return hit;
}
// A capsule may be supported beside its center while the center ray hits a
// steep rock side. This is a bounded geometric corroboration of the REAL KCC
// grounded result, never a substitute for it or permission to cross a gap.
export function queryPreflightSupport(world,position){
 const probes=[],r=DEFAULTS.radius-DEFAULTS.skin;
 const offsets=[[0,0,0]];
 for(const radius of [r*.5,r])for(let k=0;k<8;k++)offsets.push([radius*Math.cos(k*Math.PI/4),radius*Math.sin(k*Math.PI/4),radius]);
 for(const [dx,dz,radius]of offsets){
  const query=[position[0]+dx,position[1]+.05,position[2]+dz],hit=world.ground(query,.7),heightDifference=hit?position[1]-hit.position[1]:null;
  const valid=!!(hit&&point(hit.position)&&point(hit.normal)&&hit.normal[1]>=Math.cos(DEFAULTS.slopeDegrees*Math.PI/180)&&heightDifference>=-.05&&heightDifference<=.63);
  const probe={source:radius===0?'center':'footprint',radiusMeters:radius,query,hit,heightDifference};probes.push(probe);
  if(valid)return {supported:true,...probe,probeCount:probes.length};
 }
 return {supported:false,probeCount:probes.length,probes};
}
function preflightRoute(world,start,path,speed,stepHeight){
 need(world.config.stepHeight===stepHeight&&(stepHeight===.3?world.metadata.motorPolicy===undefined:world.metadata.motorPolicy===BOUNDED_STEP_POLICY&&world.config.snapGround===.6),'explicit_step_world_required');
 need(point(start)&&Array.isArray(path)&&path.length>=2&&path.length<=512&&path.every(point)&&arc(path)<=6+1e-6,'preflight_path_budget');
 const commandedSpeed=stepHeight===.6?speed*.99:speed;
 const proposal=calculateStartSupportProjection(start,world.ground([start[0],start[1]+START_SUPPORT_LIMITS.queryAbove,start[2]],START_SUPPORT_LIMITS.queryDistance),DEFAULTS),motorStart=proposal.status==='adjusted'?proposal.adjustedStart:[...start];
 const phaseResults=[];
 for(const dtMs of [100,120,130]){
  let motor;const dt=dtMs/1000,limit=Math.ceil(Math.min(8,arc(path)/commandedSpeed+3)/dt);let now=0,last=start,travel=0,horizontalTravel=0,maxStepRise=0,maxGroundGap=0,frames=0,final,initialGrounded=false,initialSettlingFrames=0;
  try{
   motor=world.motor(motorStart,path,0,commandedSpeed,routeCollisionWindow(start,path,stepHeight===.6?BOUNDED_STEP_POLICY:undefined));
   for(let i=0;i<limit;i++){
    now+=dt;const s=motor.sample(now);frames++;need(s.blocked!==true,'preflight_blocked:'+s.reason);
    // Default ground startup may settle vertically before any Actor movement
    // is published. Step-.6 receipt semantics remain unchanged and all-grounded.
    const initialDefault=stepHeight===.3&&!initialGrounded,drop=start[1]-s.position[1];
    if(initialDefault){
     if(s.grounded!==true){
      need(now<=.25+1e-9&&drop<=.35,'preflight_initial_settle_bounds');
      need(drop>=0&&s.velocity[1]<=0&&s.velocity[1]>=-DEFAULTS.gravity*(now+DEFAULTS.fixedDt)-1e-6,'preflight_initial_settle_velocity');
      need(dist(s.position,start)<=8+1e-9&&horizontal(s.position,start)<=speed*now+1e-6&&Math.hypot(s.velocity[0],s.velocity[2])<=speed+1e-6,'preflight_initial_settle_motion_budget');
      initialSettlingFrames++;maxGroundGap+=dt;continue;
     }
    }
    // After initial contact, ANY air frame still invalidates the whole route.
    if(s.grounded!==true)maxGroundGap+=dt;need(s.grounded===true,'preflight_airborne');
    initialGrounded=true;
    const deltaY=s.position[1]-last[1],moved=horizontal(s.position,last);travel+=dist(s.position,last);horizontalTravel+=moved;maxStepRise=Math.max(maxStepRise,Math.max(0,deltaY));
    // Exact experimental Actor envelope: default .3 is NOT allowed to borrow
    // the .6 receipt. Include accumulated spatial and horizontal budgets.
    const verticalLimit=Math.max(stepHeight+2*DEFAULTS.skin,moved*Math.tan(DEFAULTS.slopeDegrees*Math.PI/180)+2*DEFAULTS.skin);
    const budgets=[['vertical',Math.abs(deltaY),verticalLimit],['spatial_total',travel,8+1e-9],['horizontal_total',horizontalTravel,speed*now+1e-6],['horizontal_frame',moved,speed*((initialDefault?now:dt)+DEFAULTS.fixedDt)+1e-6]];
    for(const [budget,actual,limit]of budgets)if(actual>limit){const error=Error('follow_ground:preflight_motion_budget_'+budget);error.diagnostic={budget,stepHeight,dtMs,frame:frames,now,actual,limit,deltaY,moved,travel,horizontalTravel,previous:[...last],proposed:[...s.position]};throw error;}
    const support=queryPreflightSupport(world,s.position);
    if(!support.supported){const error=Error('follow_ground:preflight_support');error.diagnostic={dtMs,frame:frames,position:[...s.position],grounded:s.grounded,support};throw error;}
    last=s.position;final=s;if(s.arrived)break;
   }
   need(final?.arrived===true,'preflight_not_arrived');
   phaseResults.push({dtMs,arrived:true,blocked:false,groundedSamples:initialSettlingFrames===0,frames,travelMeters:travel,maxStepRise,maxGroundGap,...(stepHeight===.3?{initialSettlingFrames,postInitialContactGroundedSamples:true}: {})});
  }finally{motor?.dispose();}
 }
 return {schema:stepHeight===.6?'bounded-step-preflight-v14':'default-ground-preflight-v14',passed:true,requestedSpeedMps:speed,commandedSpeedMps:commandedSpeed,stepHeight,groundSnapMeters:stepHeight===.6?.6:DEFAULTS.snapGround,collisionMarginMeters:stepHeight===.6?24:16,phasesMs:[100,120,130],phaseResults,maxRouteMeters:8,nativeCollisionVerified:false};
}
export function preflightStepRoute(world,start,path,speed){return preflightRoute(world,start,path,speed,.6);}
function truncate(points,maximum){
 const out=[[...points[0]]];let left=maximum;
 for(let i=1;i<points.length;i++){const d=dist(points[i-1],points[i]);if(d<=left){out.push([...points[i]]);left-=d;}else{out.push(points[i-1].map((v,k)=>v+(points[i][k]-v)*left/d));return out;}}
 return out;
}
function completeLong(query,start,goal){
 const xyz=p=>({x:p[0],y:p[1],z:p[2]}),tuple=p=>[p.x,p.y,p.z],extent={halfExtents:{x:.2,y:.3,z:.2}},bad=s=>[Detour.DT_PARTIAL_RESULT,Detour.DT_OUT_OF_NODES,Detour.DT_BUFFER_TOO_SMALL].some(f=>statusDetail(s,f));
 const a=query.findNearestPoly(xyz(start),extent),b=query.findNearestPoly(xyz(goal),extent);
 if(!a.success||!b.success||!a.nearestRef||!b.nearestRef)return null;
 if(dist(start,tuple(a.nearestPoint))>.3||dist(goal,tuple(b.nearestPoint))>.3)return null;
 let c,t;try{
  c=query.findPath(a.nearestRef,b.nearestRef,a.nearestPoint,b.nearestPoint,{maxPathPolys:512});if(!c.success||bad(c.status)||c.polys.size<1||c.polys.get(c.polys.size-1)!==b.nearestRef)return null;
  t=query.findStraightPath(a.nearestPoint,b.nearestPoint,c.polys,{maxStraightPathPoints:128,straightPathOptions:Detour.DT_STRAIGHTPATH_ALL_CROSSINGS});if(!t.success||bad(t.status)||t.straightPathCount<2)return null;
  const points=[];for(let i=0;i<t.straightPathCount;i++){if(t.straightPathFlags.get(i)&Detour.DT_STRAIGHTPATH_OFFMESH_CONNECTION)return null;points.push([t.straightPath.get(i*3),t.straightPath.get(i*3+1),t.straightPath.get(i*3+2)]);}
  if(!(t.straightPathFlags.get(t.straightPathCount-1)&Detour.DT_STRAIGHTPATH_END)||points.some(p=>!point(p))||arc(points)>64||points.some((p,i)=>i&&Math.abs(p[1]-points[i-1][1])>.6))return null;return points;
 }finally{c?.polys.destroy();t?.straightPath.destroy();t?.straightPathFlags.destroy();t?.straightPathRefs.destroy();}
}
function navChoice(full,end,intent,start,groundAt,worldFor,inside,rejected,speed){
 // A nearest-nav endpoint may shift XZ inside the existing .3m projection gate.
 // Audit the ACTUAL supported nav endpoint, not the earlier candidate position.
 const selectedSupport=supportedFollowGoal(groundAt,full.at(-1)),selectedGoal=selectedSupport.position;
 need(horizontal(selectedGoal,intent.peer)<=intent.maxEndpointDistance+1e-6,'selected_nav_goal_outside_peer_radius');
 full=full.map(p=>[...p]);full[full.length-1]=selectedGoal;
 const fullLength=arc(full),points=truncate(full,Math.max(.1,intent.segmentLength-.3)),truncated=points.length!==full.length||dist(points.at(-1),full.at(-1))>1e-6;
 const support=truncated?supportedFollowGoal(groundAt,points.at(-1)):selectedSupport;points[points.length-1]=support.position;const length=arc(points);need(length>=.1&&length<=intent.segmentLength+1e-6&&points.every(inside),'nav_prefix_bounds');
 let collisionWindow=routeCollisionWindow(start,points),world=worldFor(collisionWindow),motorPolicy,motorPolicyAudit;
 try{preflightRoute(world,start,points,speed,.3);}catch(e){rejected.push(e.message);collisionWindow=routeCollisionWindow(start,points,BOUNDED_STEP_POLICY);world=worldFor(collisionWindow,BOUNDED_STEP_POLICY);motorPolicyAudit=preflightStepRoute(world,start,points,speed);motorPolicy=BOUNDED_STEP_POLICY;}
 return {world,collisionWindow,motorPolicy,motorPolicyAudit,motorSpeed:motorPolicyAudit?.commandedSpeedMps,plan:{status:'complete',start:points[0],goal:points.at(-1),path:points,length,pathLength:length},followIntentAudit:{peer:[...intent.peer],selectedGoal,prefixLength:length,fullPathLength:Math.max(fullLength,length),fullPathLengthKind:'complete_nav_corridor',fullRouteVerified:true,truncated,routeKind:'connected_nav_prefix',rejected},goalSupport:support};
}
export function prepareFollowRoute({query,start,intent,groundAt,worldFor,inside,speed}){
 validateFollowIntent(intent);const rejected=[],localRejected=[];const coldStarted=performance.now();let selected;
 try{selected=supportedFollowGoal(groundAt,intent.goal).position;}catch(e){rejected.push(e.message);}
 // A one-meter horizontal standoff can project into air beside a stair. Reuse
 // only actually supported nearby endpoints; all original corridor and full
 // capsule preflight requirements below still apply.
 if(!selected&&rejected.at(-1)==='follow_ground:goal_support_missing'){
  const alternatives=[intent.peer];
  for(const radius of [.5,intent.distance,intent.maxEndpointDistance])for(let i=0;i<12;i++)alternatives.push([intent.peer[0]+radius*Math.cos(i*Math.PI/6),intent.peer[1],intent.peer[2]+radius*Math.sin(i*Math.PI/6)]);
  const supported=[];
  for(const candidate of alternatives){try{const hit=supportedFollowGoal(groundAt,candidate);if(horizontal(hit.position,intent.peer)<=intent.maxEndpointDistance+1e-6&&inside(hit.position))supported.push(hit.position);}catch{}}
  supported.sort((a,b)=>horizontal(a,intent.goal)-horizontal(b,intent.goal));
  if(supported.length)selected=supported[0];
 }
 if(selected){
  // Keep the existing ordinary navigation/motor/hot-retarget behavior whenever
  // the actual requested standing point has a complete corridor.
  const directNav=completeLong(query,start,selected);
  if(directNav&&directNav.every(inside)){try{return navChoice(directNav,selected,intent,start,groundAt,worldFor,inside,rejected,speed);}catch(e){rejected.push(e.message);}}
 const destinations=[selected];
  // Only after the exact standing point failed, try a bounded 10cm neighborhood.
  // These are new supported endpoint proposals, never relaxed projection/radius.
  for(let n=0;n<12;n++){const p=[intent.goal[0]+.1*Math.cos(n*Math.PI/6),intent.goal[1],intent.goal[2]+.1*Math.sin(n*Math.PI/6)];if(horizontal(p,intent.peer)>intent.maxEndpointDistance+1e-6)continue;try{destinations.push(supportedFollowGoal(groundAt,p).position);}catch{}}
  for(let candidate=0;candidate<destinations.length;candidate++){let selected=destinations[candidate];
  if(candidate>0&&performance.now()-coldStarted>650)break;
 try{
  const full=dist(start,selected),truncated=full>intent.segmentLength-.1;let end=[...selected];
  if(truncated){let ratio=(intent.segmentLength-.1)/full;for(let i=0;i<4;i++){const reference=start.map((v,k)=>v+(selected[k]-v)*ratio);end=supportedFollowGoal(groundAt,reference).position;if(dist(start,end)<=intent.segmentLength-1e-6)break;ratio*=Math.max(.1,(intent.segmentLength-.1)/dist(start,end));}}
  const points=[[...start],[...end]],length=arc(points);need(length>=.1&&length<=intent.segmentLength+1e-6&&points.every(inside),'direct_prefix_bounds');
  let collisionWindow=routeCollisionWindow(start,points),world=worldFor(collisionWindow),motorPolicy,motorPolicyAudit;
  try{preflightRoute(world,start,points,speed,.3);}catch(e){rejected.push(e.message);collisionWindow=routeCollisionWindow(start,points,BOUNDED_STEP_POLICY);world=worldFor(collisionWindow,BOUNDED_STEP_POLICY);motorPolicyAudit=preflightStepRoute(world,start,points,speed);motorPolicy=BOUNDED_STEP_POLICY;}
  return {world,collisionWindow,motorPolicy,motorPolicyAudit,motorSpeed:motorPolicyAudit?.commandedSpeedMps,plan:{status:'complete',start:[...start],goal:end,path:points,length,pathLength:length},followIntentAudit:{peer:[...intent.peer],selectedGoal:selected,prefixLength:length,fullPathLength:Math.max(full,length),fullPathLengthKind:'supported_direct_reference',fullRouteVerified:!truncated,truncated,routeKind:'cold_collision_preflight',rejected},goalSupport:supportedFollowGoal(groundAt,end)};
 }catch(e){rejected.push(e.message);}}}
 // A far peer's height is not the height of an intermediate point on a
 // non-planar slope. When the long proposal fails, find a short real support
 // along the same direction and prove only that prefix, retaining the far goal.
 if(selected&&dist(start,selected)>intent.segmentLength-.1){
  const h=horizontal(start,selected),full=dist(start,selected),localStarted=performance.now();let localCandidates=0,localAttempts=0;
  for(const [forward,lateral] of [2,1,.75,.5,3,1.5].flatMap(d=>[0,.3,-.3,.6,-.6].map(side=>[d,side]))){
   if(localAttempts>=8||(localCandidates>0&&performance.now()-localStarted>650))break;localCandidates++;
   if(forward>=h-.1)continue;
   try{
    const x=start[0]+(selected[0]-start[0])*forward/h-(selected[2]-start[2])*lateral/h,z=start[2]+(selected[2]-start[2])*forward/h+(selected[0]-start[0])*lateral/h;
    const band=Math.hypot(forward,lateral)*Math.tan(DEFAULTS.slopeDegrees*Math.PI/180)+.6;
    const hit=groundAt([x,start[1]+band+.05,z],2*band+.1);
    need(hit&&point(hit.position)&&point(hit.normal),'local_prefix_support_missing');
    const support=supportedFollowGoal(groundAt,hit.position),end=support.position;
    const points=[[...start],[...end]],length=arc(points);
    need(length>=.25&&length<=intent.segmentLength-1e-6&&points.every(inside)&&horizontal(end,selected)<h-.25,'local_prefix_bounds');localAttempts++;
    let collisionWindow=routeCollisionWindow(start,points),world=worldFor(collisionWindow),motorPolicy,motorPolicyAudit;
    try{preflightRoute(world,start,points,speed,.3);}catch(e){localRejected.push(e.message);collisionWindow=routeCollisionWindow(start,points,BOUNDED_STEP_POLICY);world=worldFor(collisionWindow,BOUNDED_STEP_POLICY);motorPolicyAudit=preflightStepRoute(world,start,points,speed);motorPolicy=BOUNDED_STEP_POLICY;}
    return {world,collisionWindow,motorPolicy,motorPolicyAudit,motorSpeed:motorPolicyAudit?.commandedSpeedMps,plan:{status:'complete',start:[...start],goal:end,path:points,length,pathLength:length},followIntentAudit:{peer:[...intent.peer],selectedGoal:[...selected],prefixLength:length,fullPathLength:Math.max(full,length),fullPathLengthKind:'supported_direct_reference',fullRouteVerified:false,truncated:true,routeKind:'cold_collision_preflight',localSupportPrefix:true,localPrefixCandidates:localCandidates,localPrefixAttempts:localAttempts,rejected:[...rejected,...localRejected]},goalSupport:support};
   }catch(e){localRejected.push(e.message);}
  }
 }
 // Cold collision proof failed: retain actual nav connectivity, never use a line
 // merely because navigation failed. Select a supported nearby standing point.
 const candidates=[intent.goal,intent.peer];for(const radius of [.5,intent.distance,intent.maxEndpointDistance])for(let i=0;i<12;i++)candidates.push([intent.peer[0]+radius*Math.cos(i*Math.PI/6),intent.peer[1],intent.peer[2]+radius*Math.sin(i*Math.PI/6)]);
 const connected=[];
 for(const p of candidates){try{const hit=supportedFollowGoal(groundAt,p),end=hit.position;if(horizontal(end,intent.peer)>intent.maxEndpointDistance+1e-6)continue;const full=completeLong(query,start,end);if(!full||!full.every(inside))continue;const score=horizontal(end,intent.goal)+.01*arc(full);connected.push({full,end,score});}catch{}}
 connected.sort((a,b)=>a.score-b.score);
 for(const candidate of connected.slice(0,6)){if(performance.now()-coldStarted>4000)break;try{return navChoice(candidate.full,candidate.end,intent,start,groundAt,worldFor,inside,rejected,speed);}catch(e){rejected.push(e.message);}}
 // Optional local probes must not erase the original route failure: only that
 // failure may authorize the independently checked aerial fallback.
 need(false,'no_prevalidated_or_connected_follow_route:'+rejected.slice(-3).concat(localRejected.slice(-3)).join('.'));
}
