import RAPIER from '@dimforge/rapier3d-compat';
import {init as initRecast, NavMeshQuery, getNavMeshPositionsAndIndices, exportNavMesh} from 'recast-navigation';
import {generateSoloNavMesh} from 'recast-navigation/generators';
import {generateSparseNavMesh} from './sparse_nav.mjs';

let ready;
export const init = () => ready ??= Promise.all([RAPIER.init(), initRecast()]);
export const DEFAULTS = Object.freeze({radius: .25, height: 1.5, skin: .015,
  stepHeight: .3, slopeDegrees: 42, snapGround: .25, gravity: 18,
  maxFallSpeed: 30, fixedDt: 1/60, maxElapsed: .25, speed: 3.5,
  goalTolerance: .12, maxProjection: .4, cellSize: .15, cellHeight: .075});
export function config(values = {}) {
  const c = {...DEFAULTS, ...values};
  for (const [k,v] of Object.entries(c)) {
    if (!(k in DEFAULTS) || typeof v !== 'number' || !Number.isFinite(v)) throw Error('invalid_config:'+k);
  }
  if(c.radius < .05 || c.radius > 2 || c.height < c.radius*2 || c.height > 6 ||
    c.skin < .001 || c.skin > .1 || c.stepHeight < 0 || c.stepHeight > 1 ||
    c.slopeDegrees < 1 || c.slopeDegrees > 65 || c.snapGround < 0 || c.snapGround > 1 ||
    c.gravity <= 0 || c.gravity > 100 || c.maxFallSpeed <= 0 || c.maxFallSpeed > 100 ||
    c.fixedDt < 1/240 || c.fixedDt > 1/30 || c.maxElapsed < c.fixedDt || c.maxElapsed > .5 ||
    c.speed <= 0 || c.speed > 10 || c.goalTolerance < .03 || c.goalTolerance > .4 ||
    c.maxProjection < .01 || c.maxProjection > 1 || c.cellSize < .05 || c.cellSize > .5 ||
    c.cellHeight < .025 || c.cellHeight > .25) throw Error('config_out_of_bounds');
  return Object.freeze(c);
}
export function vec(v) {
  if (!Array.isArray(v) || v.length !== 3 || !v.every(x=>typeof x==='number' && Number.isFinite(x) && Math.abs(x)<1e6)) throw Error('invalid_vector');
  return {x:v[0],y:v[1],z:v[2]};
}
const array = v => [v.x,v.y,v.z];
const distance = (a,b) => Math.hypot(a.x-b.x,a.y-b.y,a.z-b.z);
const horizontal = (a,b) => Math.hypot(a.x-b.x,a.z-b.z);
export function navigationWindow(value) {
  if(value==null)return null;
  const keys=['minX','maxX','minZ','maxZ'];
  if(typeof value!=='object'||Array.isArray(value)||Object.keys(value).length!==4||
    !keys.every(k=>typeof value[k]==='number'&&Number.isFinite(value[k])&&Math.abs(value[k])<1e6)||
    Object.keys(value).some(k=>!keys.includes(k))||value.minX>=value.maxX||value.minZ>=value.maxZ||
    value.maxX-value.minX>512||value.maxZ-value.minZ>512)throw Error('navigation_window_invalid');
  return Object.freeze(Object.fromEntries(keys.map(k=>[k,value[k]])));
}
const withinWindow=(w,p)=>!w||(p.x>=w.minX&&p.x<=w.maxX&&p.z>=w.minZ&&p.z<=w.maxZ);

export function validateMesh(p,t) {
  if(!(p instanceof Float32Array) || !(t instanceof Uint32Array) || !p.length || !t.length ||
    p.length%3 || t.length%3 || p.length>6_000_000 || t.length>9_000_000) throw Error('mesh_budget_or_shape');
  const lo=[Infinity,Infinity,Infinity],hi=[-Infinity,-Infinity,-Infinity];
  for(let i=0;i<p.length;i++) {
    // Geometry range is distinct from the stricter 1e6 actor-motion range.
    // Current Prairie_Cave includes finite far-field meshes up to 7,254,599.5.
    if(!Number.isFinite(p[i]) || Math.abs(p[i])>=2**23) throw Error('mesh_nonfinite_or_range');
    lo[i%3]=Math.min(lo[i%3],p[i]); hi[i%3]=Math.max(hi[i%3],p[i]);
  }
  for(const i of t) if(i>=p.length/3) throw Error('mesh_index');
  return {lo,hi};
}

export class TerrainWorld {
  constructor(positions, indices, options={}) {
    this.config=config(options.config);
    this.navigationWindow=navigationWindow(options.navigationWindow);
    this.navigationCoverage=this.navigationWindow?'bounded_window':'full_static_candidate';
    this.bounds=validateMesh(positions,indices);
    this.geometryPrecisionWarnings=[...this.bounds.lo,...this.bounds.hi].some(x=>Math.abs(x)>=1e6)
      ?['far_field_float32_precision_unverified_actor_range_unchanged']:[];
    this.world=new RAPIER.World({x:0,y:-this.config.gravity,z:0});
    this.world.timestep=this.config.fixedDt;
    this.terrain=this.world.createCollider(RAPIER.ColliderDesc.trimesh(positions,indices,
      RAPIER.TriMeshFlags.FIX_INTERNAL_EDGES_TWO_SIDED | RAPIER.TriMeshFlags.DELETE_DEGENERATE_TRIANGLES));
    this.world.step();
    this.nav=null; this.query=null; this.navError=null; this.motors=new Set();
    if(options.navigation !== false) {
      const c=this.config, b=this.bounds;
      const cells=Math.ceil((b.hi[0]-b.lo[0])/c.cellSize)*Math.ceil((b.hi[2]-b.lo[2])/c.cellSize);
      const tiled=!!this.navigationWindow||options.tiled===true||cells>4_000_000||(b.hi[1]-b.lo[1])/c.cellHeight>=8190;
      this.navMode=tiled?'tiled':'solo';
      const settings={tileSize:64,cs:c.cellSize,ch:c.cellHeight,
        walkableSlopeAngle:c.slopeDegrees,walkableHeight:Math.ceil(c.height/c.cellHeight),
        walkableClimb:Math.floor(c.stepHeight/c.cellHeight),
        walkableRadius:Math.ceil((c.radius+c.skin)/c.cellSize),
        minRegionArea:0,mergeRegionArea:0,detailSampleDist:6,detailSampleMaxError:1,
        ...(this.navigationWindow?{windowMinX:this.navigationWindow.minX,windowMaxX:this.navigationWindow.maxX,
          windowMinZ:this.navigationWindow.minZ,windowMaxZ:this.navigationWindow.maxZ}:{})};
      let built=tiled?generateSparseNavMesh(positions,indices,b,settings):generateSoloNavMesh(positions,indices,settings);
      if(tiled&&!built.success&&['candidate_tile_budget','nav_occupied_layer_budget'].includes(built.error)) {
        const first={reason:built.error,...built.stats};
        // Preserve voxel resolution. Larger tiles address measured full-map layer counts,
        // still bounded by an isolated build timeout and an explicit 192M-cell ceiling.
        built=generateSparseNavMesh(positions,indices,b,{...settings,tileSize:128,maxRasterCells:192_000_000});
        built.stats.attempts=[first];
      }
      this.navBuildStats=built.stats??null;
      if(!built.success) {this.navError=built.error;return;}
      this.nav=built.navMesh;this.query=new NavMeshQuery(this.nav,{maxNodes:8192});
    }
  }
  plan(start,goal) {
    const s=vec(start),g=vec(goal),c=this.config;
    if(!withinWindow(this.navigationWindow,s)||!withinWindow(this.navigationWindow,g))return {status:'blocked',reason:'outside_navigation_window'};
    if(!this.query) return {status:'unavailable',reason:this.navError||'navigation_absent'};
    const halfExtents={x:c.maxProjection,y:c.maxProjection,z:c.maxProjection};
    const a=this.query.findNearestPoly(s,{halfExtents}), b=this.query.findNearestPoly(g,{halfExtents});
    if(!a.success||!b.success||!a.nearestRef||!b.nearestRef) return {status:'blocked',reason:'off_navmesh'};
    if(distance(a.nearestPoint,s)>c.maxProjection||distance(b.nearestPoint,g)>c.maxProjection)
      return {status:'blocked',reason:'projection_too_far'};
    const corridor=this.query.findPath(a.nearestRef,b.nearestRef,a.nearestPoint,b.nearestPoint,{maxPathPolys:2048});
    try {
      if(!corridor.success||!corridor.polys.size||corridor.polys.get(corridor.polys.size-1)!==b.nearestRef||
         (corridor.status & (16|32|64))) return {status:'blocked',reason:'partial_or_exhausted_path'};
      const straight=this.query.findStraightPath(a.nearestPoint,b.nearestPoint,corridor.polys,{maxStraightPathPoints:512,straightPathOptions:2});
      try {
        if(!straight.success||straight.straightPathCount<1 || (straight.status & 16)) return {status:'blocked',reason:'straight_path_failed'};
        const path=[];
        for(let i=0;i<straight.straightPathCount;i++) path.push([straight.straightPath.get(3*i),straight.straightPath.get(3*i+1),straight.straightPath.get(3*i+2)]);
        if(path.some(p=>!withinWindow(this.navigationWindow,vec(p))))return {status:'blocked',reason:'path_leaves_navigation_window'};
        if(distance(vec(path.at(-1)),b.nearestPoint)>.05) return {status:'blocked',reason:'path_truncated'};
        let length=0,prev=s;for(const p of path){length+=distance(prev,vec(p));prev=vec(p);}
        return {status:'ready',reason:'initial_static_geometry',path,goal:array(b.nearestPoint),length};
      } finally {straight.straightPath.destroy();straight.straightPathFlags.destroy();straight.straightPathRefs.destroy();}
    } finally {corridor.polys.destroy();}
  }
  ground(point,maxDistance=10) {
    const p=vec(point);
    if(!Number.isFinite(maxDistance)||maxDistance<=0||maxDistance>10000) throw Error('ray_budget');
    const hit=this.world.castRayAndGetNormal(new RAPIER.Ray(p,{x:0,y:-1,z:0}),maxDistance,true,undefined,undefined,undefined,undefined,c=>c===this.terrain);
    return hit?{position:[p.x,p.y-hit.timeOfImpact,p.z],normal:array(hit.normal),distance:hit.timeOfImpact}:null;
  }
  motor(start,path,now,speed=this.config.speed) {
    if(this.motors.size) throw Error('single_motor_only');
    const m=new CharacterMotor(this,start,path,now,speed);this.motors.add(m);return m;
  }
  navGeometry(){return this.nav?getNavMeshPositionsAndIndices(this.nav):[[],[]];}
  exportNav(){if(!this.nav)throw Error('navigation_absent');return exportNavMesh(this.nav);}
  dispose(){for(const m of [...this.motors])m.dispose();this.query?.destroy();this.nav?.destroy();this.world.free();}
}

export class CharacterMotor {
  constructor(terrain,start,path,now,speed) {
    this.terrain=terrain;this.c=terrain.config;this.world=terrain.world;
    this.p=vec(start); if(!Array.isArray(path)||!path.length||path.length>512)throw Error('path_budget');
    this.path=path.map(vec);this.goal=this.path.at(-1);this.index=0;
    if(!withinWindow(terrain.navigationWindow,this.p)||this.path.some(p=>!withinWindow(terrain.navigationWindow,p)))throw Error('outside_navigation_window');
    if(!Number.isFinite(now)||!Number.isFinite(speed)||speed<=0||speed>10)throw Error('invalid_time_or_speed');
    this.now=now;this.speed=speed;this.vy=0;this.grounded=false;this.heading=0;this.stuck=0;
    this.arrived=false;this.blocked=false;this.reason='moving';this.closed=false;this.debt=0;
    this.verticalGoalBest=null;this.verticalGoalStall=0;
    // Opt-in bounded hop state; ordinary ground motors never enter this branch.
    this.boundedHop=null;
    const c=this.c, center={x:this.p.x,y:this.p.y+c.height/2+c.skin,z:this.p.z};
    const shape=new RAPIER.Capsule((c.height-2*c.radius)/2,c.radius);
    if(this.world.intersectionWithShape(center,{x:0,y:0,z:0,w:1},shape,undefined,undefined,undefined,undefined,col=>col===terrain.terrain))throw Error('start_overlaps_collision');
    this.collider=this.world.createCollider(RAPIER.ColliderDesc.capsule((c.height-2*c.radius)/2,c.radius).setTranslation(center.x,center.y,center.z));
    this.controller=this.world.createCharacterController(c.skin);
    this.controller.setUp({x:0,y:1,z:0});this.controller.setSlideEnabled(true);
    this.controller.setMaxSlopeClimbAngle(c.slopeDegrees*Math.PI/180);
    this.controller.setMinSlopeSlideAngle(c.slopeDegrees*Math.PI/180);
    if(c.stepHeight>0)this.controller.enableAutostep(c.stepHeight,c.radius,false);
    if(c.snapGround>0)this.controller.enableSnapToGround(c.snapGround);
    this.world.step();this.cached=this.result([0,0,0]);
  }
  result(velocity){return Object.freeze({position:array(this.p),velocity,grounded:this.grounded,arrived:this.arrived,blocked:this.blocked,reason:this.reason,heading:this.heading,
    ...(this.boundedHop?{hop:Object.freeze({id:this.boundedHop.id,phase:this.boundedHop.phase,elapsed:this.boundedHop.elapsed,verticalSpeed:this.vy,leftGround:this.boundedHop.leftGround,cancelRequested:this.boundedHop.cancelRequested})}:{})});}
  startBoundedHop(options){
    if(!options||typeof options!=='object'||Array.isArray(options)||Object.keys(options).sort().join(',')!=='id,landing,maxAirSeconds,maxHorizontalDistance,maxRise,verticalSpeed')throw Error('hop_options');
    const {id,landing,maxAirSeconds,maxHorizontalDistance,maxRise,verticalSpeed}=options,target=vec(landing);
    if(typeof id!=='string'||!/^[a-zA-Z0-9_-]{1,80}$/.test(id)||![maxAirSeconds,maxHorizontalDistance,maxRise,verticalSpeed].every(Number.isFinite)||
      maxRise<=0||maxRise>1||maxAirSeconds<=0||maxAirSeconds>1.5||maxHorizontalDistance<=0||maxHorizontalDistance>2||verticalSpeed<=0||verticalSpeed**2/(2*this.c.gravity)>maxRise+1e-9)throw Error('hop_budget');
    if(this.closed||this.blocked||this.boundedHop||!this.grounded||!this.controller.computedGrounded())throw Error('hop_grounded_unused_motor_required');
    if(horizontal(this.p,target)>maxHorizontalDistance||target.y-this.p.y>maxRise||this.p.y-target.y>.3||!withinWindow(this.terrain.navigationWindow,target))throw Error('hop_target_budget');
    this.path=[target];this.index=0;this.goal=target;this.arrived=false;this.reason='moving';this.stuck=0;this.verticalGoalBest=null;this.verticalGoalStall=0;
    this.boundedHop={id,start:{...this.p},maxAirSeconds,maxHorizontalDistance,maxRise,elapsed:0,leftGround:false,cancelRequested:false,phase:'takeoff',active:true};
    this.vy=verticalSpeed;this.cached=this.result([0,0,0]);return this.cached;
  }
  requestHopLanding(){
    if(this.closed||this.blocked||!this.boundedHop?.active)throw Error('hop_not_active');
    this.boundedHop.cancelRequested=true;this.boundedHop.phase='settling';return this.cached=this.result([0,0,0]);
  }
  sample(now) {
    if(!Number.isFinite(now))throw Error('invalid_time');
    if(this.closed||now<=this.now||this.arrived||this.blocked||this.boundedHop?.complete)return this.cached;
    const elapsed=now-this.now;this.now=now;
    // Never catch up a stalled network by teleporting a large elapsed distance.
    if(elapsed>this.c.maxElapsed){this.blocked=true;this.reason='clock_gap';return this.cached=this.result([0,0,0]);}
    this.debt+=elapsed;const before={...this.p};let steps=0;
    while(this.debt+1e-10>=this.c.fixedDt && !this.arrived && !this.blocked&&!this.boundedHop?.complete){this.step(this.c.fixedDt);this.debt-=this.c.fixedDt;steps++;}
    const dt=steps*this.c.fixedDt;
    return this.cached=this.result(dt?[(this.p.x-before.x)/dt,(this.p.y-before.y)/dt,(this.p.z-before.z)/dt]:[0,0,0]);
  }
  step(dt) {
    while(this.index<this.path.length-1 && horizontal(this.p,this.path[this.index])<this.c.goalTolerance)this.index++;
    const target=this.path[this.index],d=horizontal(this.p,target);
    let dx=0,dz=0;
    if(d>.015){const step=Math.min(d,this.speed*dt);dx=(target.x-this.p.x)/d*step;dz=(target.z-this.p.z)/d*step;this.heading=Math.atan2(dx,dz);}
    const hop=this.boundedHop;
    if(hop?.active&&hop.cancelRequested){dx=0;dz=0;}
    this.vy=Math.max(-this.c.maxFallSpeed,this.vy-this.c.gravity*dt);
    const dy=hop?.active?this.vy*dt:(this.grounded?0:this.vy*dt);
    this.controller.computeColliderMovement(this.collider,{x:dx,y:dy,z:dz},undefined,undefined,c=>c===this.terrain.terrain);
    const m=this.controller.computedMovement(),next={x:this.p.x+m.x,y:this.p.y+m.y,z:this.p.z+m.z};
    if(!withinWindow(this.terrain.navigationWindow,next)){this.blocked=true;this.reason='outside_navigation_window';return;}
    this.grounded=this.controller.computedGrounded();
    if(this.grounded)this.vy=0;
    this.p=next;
    this.collider.setTranslation({x:this.p.x,y:this.p.y+this.c.height/2+this.c.skin,z:this.p.z});
    this.world.step();
    if(hop?.active){
      hop.elapsed+=dt;hop.leftGround ||= !this.grounded;
      if(hop.leftGround&&this.grounded){hop.active=false;hop.complete=true;hop.phase=hop.cancelRequested?'cancelled_landed':'landed';}
      else hop.phase=hop.cancelRequested?'settling':this.vy>0?'rising':'falling';
      if(hop.active&&(hop.elapsed>hop.maxAirSeconds+1e-9||this.p.y-hop.start.y>hop.maxRise+2*this.c.skin||horizontal(this.p,hop.start)>hop.maxHorizontalDistance+1e-6||(!hop.leftGround&&hop.elapsed>.1))){
        this.blocked=true;this.reason='bounded_hop_budget';
      }
    }
    const moved=Math.hypot(m.x,m.z),wanted=Math.hypot(dx,dz);
    this.stuck=wanted>.001&&moved<wanted*.05?this.stuck+dt:0;
    if(this.stuck>1){this.blocked=true;this.reason='collision_blocked';}
    if(this.p.y<this.terrain.bounds.lo[1]-5){this.blocked=true;this.reason='below_geometry';}
    const nearGoal=horizontal(this.p,this.goal)<=this.c.goalTolerance,verticalError=Math.abs(this.p.y-this.goal.y);
    // Local fail-closed timeout, not a native physics/calibration parameter.
    // Preserve arrival tolerance; descent/ascent that improves Y keeps progressing.
    if(!this.blocked&&this.index===this.path.length-1&&nearGoal&&verticalError>=.35){
      if(this.verticalGoalBest===null||verticalError<=this.verticalGoalBest-.05){this.verticalGoalBest=verticalError;this.verticalGoalStall=0;}
      else this.verticalGoalStall+=dt;
      if(this.verticalGoalStall>=2){this.blocked=true;this.reason='vertical_goal_mismatch';}
    }else{this.verticalGoalBest=null;this.verticalGoalStall=0;}
    if(!this.blocked&&this.grounded&&nearGoal&&verticalError<.35){this.arrived=true;this.reason='arrived';}
  }
  cancel(reason='cancelled'){this.arrived=false;this.blocked=true;this.reason=reason;this.closed=true;return this.cached=this.result([0,0,0]);}
  dispose(){this.cancel();if(this.collider){this.world.removeCharacterController(this.controller);this.world.removeCollider(this.collider,true);this.collider=null;}this.terrain.motors.delete(this);}
}
