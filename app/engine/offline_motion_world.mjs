// Explicit offline-only collision adapter; core/defaults/live worker stay unchanged.
import RAPIER from '@dimforge/rapier3d-compat';
import {InitialContactQuery} from './initial_contact.mjs';
import {TerrainWorld,DEFAULTS} from './core.mjs';
import {LocalFrame,localizeGeometry} from './local_frame.mjs';
import {queryStartSupportProjection} from './start_support.mjs';
import {clipGeometryXZ,createCollisionCoverage} from './clipped_geometry.mjs';
import {assertPinnedCollisionRuntime} from './pinned_collision_runtime.mjs';
import {preparePreservingGeometry,PRESERVING_ORIGIN_POLICY} from './preserving_geometry.mjs';
import {prepareAutomaticAerial} from './auto_aerial_follow_v22.mjs';
import {prepareAutomaticHop} from './auto_hop_follow_v20.mjs';
import {createHopWorkerBridge,loadedHopWorldSHA256} from './hop_worker_bridge_v20.mjs';
export {PRESERVING_ORIGIN_POLICY};

const need=(value,reason)=>{if(!value)throw Error(reason);};
export const OFFLINE_COLLISION_MARGIN=16;
export const BOUNDED_STEP_POLICY='bounded_step_0_6_preflight_v14';
const checkedPolicy=p=>{need(p===undefined||p===BOUNDED_STEP_POLICY,'invalid_motor_policy');return p;};
// Rapier JS 0.21.0 / Rust 0.36.0, static-only terrain, standard up.
// Per-step translation norm + 20 iterations of worst-case autostep rise,
// forward test and normal nudge + capsule radius + grounded-query AABB.
// The .05 reserve is an offline numerical guard, NOT a native parameter.
export function offlineStepQueryReach(speed,dt=DEFAULTS.fixedDt,motorPolicy=undefined){
  checkedPolicy(motorPolicy);
  need(typeof speed==='number'&&Number.isFinite(speed)&&speed>0&&speed<=10,'invalid_coverage_speed');
  need(dt===DEFAULTS.fixedDt,'invalid_coverage_step');
  const c=motorPolicy?{...DEFAULTS,stepHeight:.6}:DEFAULTS,R=Math.hypot(speed*dt,Math.max(.02,c.maxFallSpeed*dt));
  return R+20*((c.stepHeight+c.skin)+(c.radius+c.skin)+.0001)+c.radius+(c.skin+.05)+.05;
}
// A deterministic collision coverage column, not a new navigation permission.
// This is chosen before replay and never retried with a different origin or size.
export function routeCollisionWindow(start,path,motorPolicy=undefined){
  checkedPolicy(motorPolicy);const margin=motorPolicy?24:OFFLINE_COLLISION_MARGIN;
  const frame=new LocalFrame({origin:[0,0,0]});
  const points=[frame.toLocalPosition(start),...frame.toLocalPath(path)];
  return Object.freeze(frame.toLocalWindow({
    minX:Math.min(...points.map(p=>p[0]))-margin,maxX:Math.max(...points.map(p=>p[0]))+margin,
    minZ:Math.min(...points.map(p=>p[2]))-margin,maxZ:Math.max(...points.map(p=>p[2]))+margin}));
}
function freeze(value){
  if(value&&typeof value==='object'&&!Object.isFrozen(value)){
    for(const child of Object.values(value))freeze(child);
    Object.freeze(value);
  }
  return value;
}
function options(value){
  need(value&&typeof value==='object'&&!Array.isArray(value),'offline_options_required');
  need([Object.prototype,null].includes(Object.getPrototypeOf(value)),'plain_options_required');
  const descriptors=Object.getOwnPropertyDescriptors(value),allowed=['offline','origin','navigationWindow','startSupport','collisionWindow','geometryPolicy','motorPolicy'];
  need(Reflect.ownKeys(descriptors).every(k=>allowed.includes(k)&&'value'in descriptors[k]),'unknown_or_accessor_option');
  need(value.offline===true,'offline_only');
  checkedPolicy(value.motorPolicy);
  need(value.startSupport===undefined||typeof value.startSupport==='boolean','start_support_boolean');
  need(value.geometryPolicy===undefined||value.geometryPolicy===PRESERVING_ORIGIN_POLICY,'invalid_geometry_policy');
  if(value.geometryPolicy!==undefined){need(!Object.hasOwn(value,'origin'),'manual_origin_with_derived_policy');need(value.collisionWindow!==undefined&&value.collisionWindow!==null,'explicit_collision_window_required');}
  return value;
}


// A center ray does not cover the capsule footprint. Recover one shallow bottom
// contact only; this proposal still requires the ORIGINAL full capsule test.
export function queryShallowGroundContactProjection(world, rawStart, prior) {
 const reject=reason=>({status:'blocked',reason});
 const finitePoint=p=>Array.isArray(p)&&p.length===3&&p.every(Number.isFinite);
 if(!finitePoint(rawStart)||!prior||!['adjusted','unchanged'].includes(prior.status)||
    !prior.support||!finitePoint(prior.support.position)||!finitePoint(prior.support.normal))return reject('contact_recovery_support_required');
 const c=world.config,ny=prior.support.normal[1],floor=Math.cos(DEFAULTS.slopeDegrees*Math.PI/180);
 if(c.radius!==DEFAULTS.radius||c.height!==DEFAULTS.height||c.skin!==DEFAULTS.skin||c.slopeDegrees!==DEFAULTS.slopeDegrees)return reject('contact_recovery_capsule_config');
 if(Math.abs(prior.support.position[1]-rawStart[1])>.02||ny<floor||Math.abs(Math.hypot(...prior.support.normal)-1)>.001)return reject('contact_recovery_support_band');
 const shape=new RAPIER.Capsule((c.height-2*c.radius)/2,c.radius),center={x:rawStart[0],y:rawStart[1]+c.height/2+c.skin,z:rawStart[2]};
 const hit=world.terrain.contactShape(shape,center,{x:0,y:0,z:0,w:1},0);
 if(!hit)return reject('contact_recovery_no_contact');
 const a=p=>[p.x,p.y,p.z],point1=a(hit.point1),point2=a(hit.point2),normal=a(hit.normal1);
 if(!Number.isFinite(hit.distance)||!finitePoint(point1)||!finitePoint(point2)||!finitePoint(normal)||
    hit.distance>=0||hit.distance<-.015||normal[1]<floor||Math.abs(Math.hypot(...normal)-1)>.001)return reject('contact_recovery_not_shallow_ground');
 // Limit both witnesses to the bottom spherical cap and a narrow support band;
 // wall sides, head/ceiling contacts, remote and unsupported hits stay blocked.
 if([point1,point2].some(p=>p[1]<rawStart[1]-.02||p[1]>rawStart[1]+.1||Math.hypot(p[0]-rawStart[0],p[2]-rawStart[2])>c.radius+.001))return reject('contact_recovery_not_bottom');
 const delta=Math.max(prior.deltaY??0,-hit.distance/normal[1]+.0001);
 if(!Number.isFinite(delta)||delta<=0||delta>.02)return reject('contact_recovery_lift_budget');
 const adjusted=[rawStart[0],rawStart[1]+delta,rawStart[2]];
 return {...prior,status:'adjusted',rawStart:[...rawStart],adjustedStart:adjusted,deltaY:delta,delta:[0,delta,0],
   reason:'shallow_bottom_contact_proposal',calculations:{...prior.calculations,requiredY:adjusted[1],formula:'rawY+max(existing_support_lift,-penetration_distance/up_normal_y+0.0001)'},
   contactRecovery:{kind:'single_measured_bottom_contact',maximumLift:.02,maximumPenetration:.015,
      distance:hit.distance,point1,point2,normal,normalPolicy:'unchanged_actual_contact',
      formula:'max(existing_support_lift,-penetration_distance/up_normal_y+0.0001)',attempts:1},
   audit:{...prior.audit,kind:'shallow_bottom_contact_not_avatar_calibration',maximumLift:.02,collisionClearanceVerified:false,footOffsetCalibrated:false,nativeCollisionVerified:false,calibrationInstalled:false,runtimeReady:false}};
}

export class OfflineMotionWorld{
  #initialContacts;#motorPolicy;#frame;#world;#motor=null;#hop=null;#closed=false;#supportEnabled;#lastAudit=null;#coverage=null;
  constructor(positions,indices,provided){
    const o=options(provided);this.#motorPolicy=o.motorPolicy;
    const runtime=o.collisionWindow===undefined?null:assertPinnedCollisionRuntime();
    let mesh;
    if(o.geometryPolicy===PRESERVING_ORIGIN_POLICY){
      mesh=preparePreservingGeometry({positions,indices},o.collisionWindow);this.#frame=mesh.frame;
    }else{
      this.#frame=new LocalFrame({origin:o.origin});
      mesh=o.collisionWindow===undefined?localizeGeometry(this.#frame,{positions,indices}):
        clipGeometryXZ(this.#frame,{positions,indices},o.collisionWindow);
    }
    this.#coverage=mesh.coverage??null;
    const window=this.#frame.toLocalWindow(o.navigationWindow??null);
    this.#world=new TerrainWorld(mesh.positions,mesh.indices,{navigation:false,navigationWindow:window,...(o.motorPolicy?{config:{stepHeight:.6,snapGround:.6}}:{})});
    this.#initialContacts=new InitialContactQuery(this.#world,mesh.positions,mesh.indices);
    const physicalBounds=freeze(structuredClone(this.#world.bounds));
    // Only the movement failure boundary changes ownership: physical collider/BVH
    // retains whole faces, while below_geometry still uses the ORIGINAL query domain.
    if(o.geometryPolicy===PRESERVING_ORIGIN_POLICY)this.#world.bounds=mesh.domainLocalBounds;
    Object.freeze(this.#world.config);
    if(this.#world.navigationWindow)Object.freeze(this.#world.navigationWindow);
    this.#supportEnabled=o.startSupport!==false;
    Object.defineProperty(this,'metadata',{value:freeze({...mesh.metadata,mode:'offline',
      navigationRebuilt:false,parameters:o.motorPolicy?'engineering_step_and_snap_0_6_other_defaults_unchanged':'unchanged_defaults',...(o.motorPolicy?{motorPolicy:o.motorPolicy,stepHeight:.6,groundSnapMeters:.6,routeCollisionMargin:24}:{}),startSupportEnabled:this.#supportEnabled,
      physicalBounds,motionBounds:freeze(structuredClone(this.#world.bounds)),
      motionBoundsRule:o.geometryPolicy===PRESERVING_ORIGIN_POLICY?'original_query_domain':'original_mesh',
      belowGeometryWorldY:(this.#world.bounds.lo[1]-5)+this.#frame.origin[1],
      collisionCoverageGuard:this.#coverage?'each_original_fixed_step':'full_mesh',
      internalQueryCoverageVerified:false,sourceDerivedQueryBound:!!this.#coverage,
      queryBoundRuntime:runtime,defaultRouteCollisionMargin:OFFLINE_COLLISION_MARGIN,
      queryBoundConditions:['static_terrain_only','standard_up',o.motorPolicy?'explicit_step_and_snap_0_6_other_defaults_unchanged':'unchanged_defaults','original_fixed_step','unit_normals_and_valid_TOI'],
      queryBoundNumericalReserve:.05}),enumerable:true});
  }
  #open(){need(!this.#closed,'offline_world_disposed');}
  get config(){return this.#world.config;}
  get frameOrigin(){return Object.freeze([...this.#frame.origin]);}
  get navigationWindow(){return this.#frame.toWorldWindow(this.#world.navigationWindow);}
  get startSupportAudit(){return this.#lastAudit;}
  #audit(proposal){
    const result=structuredClone(proposal);
    for(const key of ['rawStart','adjustedStart'])if(result[key])result[key]=this.#frame.toWorldPosition(result[key]);
    for(const hit of result.contacts??[]){hit.point=this.#frame.toWorldPosition(hit.point);hit.vertices=hit.vertices.map(p=>this.#frame.toWorldPosition(p));}
    if(result.support)result.support.position=this.#frame.toWorldPosition(result.support.position);
    if(result.contactRecovery){for(const key of ['point1','point2'])result.contactRecovery[key]=this.#frame.toWorldPosition(result.contactRecovery[key]);}
    if(result.query)result.query.origin=this.#frame.toWorldPosition(result.query.origin);
    if(result.audit?.navigationWindow)result.audit.navigationWindow=this.#frame.toWorldWindow(result.audit.navigationWindow);
    if(result.calculations){
      result.calculations.groundY+=this.#frame.origin[1];
      result.calculations.requiredY+=this.#frame.origin[1];
    }
    return freeze({...result,coordinateFrame:'world',applied:proposal.status==='adjusted'});
  }
  ground(point,maxDistance=10){
    this.#open();this.#coverage?.assertWorldCapsule(point,{radius:0,skin:0});
    const hit=this.#world.ground(this.#frame.toLocalPosition(point),maxDistance);
    return hit?{position:this.#frame.toWorldPosition(hit.position),normal:this.#frame.toWorldVector(hit.normal),distance:hit.distance}:null;
  }
  capsuleProbe(point,resolve=true){
    this.#open();this.#coverage?.assertWorldCapsule(point,{radius:DEFAULTS.radius+.5,skin:DEFAULTS.skin});
    const result=this.#initialContacts.probe(this.#frame.toLocalPosition(point),{resolve});
    for(const k of ['rawStart','adjustedStart'])if(result[k])result[k]=this.#frame.toWorldPosition(result[k]);
    for(const h of result.contacts??[]){h.point=this.#frame.toWorldPosition(h.point);h.vertices=h.vertices.map(v=>this.#frame.toWorldPosition(v));}
    return {...result,coordinateFrame:'world_feet'};
  }
  motor(start,path,now,speed=DEFAULTS.speed,requestWindow=null){
    this.#open();need(this.#motor===null&&this.#hop===null,'single_motor_only');
    this.#lastAudit=null;
    // A reusable static world may cover more than this route. Preserve the
    // original per-route movement/query boundary instead of widening it.
    let coverage=this.#coverage;
    if(requestWindow!==null){
      need(coverage,'route_window_requires_covered_world');
      const w=this.#frame.toWorldWindow(this.#frame.toLocalWindow(requestWindow)),outer=coverage.worldWindow;
      need(w.minX>=outer.minX&&w.maxX<=outer.maxX&&w.minZ>=outer.minZ&&w.maxZ<=outer.maxZ,'route_window_outside_cached_geometry');
      coverage=createCollisionCoverage(this.#frame,w,this.#frame.toLocalWindow(w),coverage.maxXZQuantizationError);
    }
    const localStart=this.#frame.toLocalPosition(start),localPath=this.#frame.toLocalPath(path);
    let inner;
    const auditBase={applied:false,rawStart:[...start],coordinateFrame:'world',footOffsetCalibrated:false,nativeCollisionVerified:false};
    try{
      coverage?.assertWorldPath([start,...path],{radius:DEFAULTS.radius,skin:DEFAULTS.skin});
      inner=this.#world.motor(localStart,localPath,now,speed);
      this.#lastAudit=freeze({...auditBase,status:'unchanged',adjustedStart:[...start],deltaY:0,
        reason:'original_start_accepted',collisionConstructorAccepted:true});}
    catch(error){
      this.#lastAudit=freeze({...auditBase,status:'blocked',adjustedStart:null,deltaY:null,
        reason:error.message,collisionConstructorAccepted:false});
      if(error.message!=='start_overlaps_collision'||!this.#supportEnabled)throw error;
      try{
      const supportWorld=this.#motorPolicy?{config:DEFAULTS,navigationWindow:this.#world.navigationWindow,ground:this.#world.ground.bind(this.#world)}:this.#world;
      // Autostep policy does not alter the capsule/start-only .3m support proposal.
      const proposal=queryStartSupportProjection(supportWorld,localStart);
      this.#lastAudit=this.#audit(proposal);
      if(!['adjusted','unchanged'].includes(proposal.status))throw error;
      // Original collision constructor must still accept the proposed start.
      try{if(proposal.status==='unchanged')throw error;inner=this.#world.motor(proposal.adjustedStart,localPath,now,speed);}
      catch(rejected){
        this.#lastAudit=freeze({...this.#lastAudit,applied:false,collisionConstructorAccepted:false});
        if(rejected.message!=='start_overlaps_collision')throw rejected;
        const recovery=queryShallowGroundContactProjection(this.#world,localStart,proposal);
        if(recovery.status!=='adjusted')throw rejected;
        this.#lastAudit=this.#audit(recovery);
        try{inner=this.#world.motor(recovery.adjustedStart,localPath,now,speed);}
        catch(stillBlocked){this.#lastAudit=freeze({...this.#lastAudit,applied:false,collisionConstructorAccepted:false});throw stillBlocked;}
      }
      }catch(originalSupportError){
        if(originalSupportError.message!=='start_overlaps_collision')throw originalSupportError;
        const separation=this.#initialContacts.probe(localStart);
        if(separation.status!=='adjusted')throw originalSupportError;
        coverage?.assertWorldPath([start,this.#frame.toWorldPosition(separation.adjustedStart)],{radius:DEFAULTS.radius,skin:DEFAULTS.skin});
        inner=this.#world.motor(separation.adjustedStart,localPath,now,speed);
        this.#lastAudit=freeze({...this.#audit(separation),initialContactExit:true});
      }
      this.#lastAudit=freeze({...this.#lastAudit,collisionConstructorAccepted:true});
    }
    const frame=this.#frame;let closed=false,coverageFailure=null;
    const exit=this.#lastAudit?.initialContactExit?this.#lastAudit:null;
    const exitDuration=exit?Math.max(DEFAULTS.fixedDt,exit.deltaLength/speed):0;
    let exitDone=!exit,exitLastTime=now,exitLastResult=exit?freeze({position:[...exit.rawStart],velocity:[0,0,0],grounded:true,arrived:false,blocked:false,reason:'initial_contact_exit',heading:0}):null,exitCancelled=false;
    const sampleWithExit=time=>{
      need(Number.isFinite(time),'invalid_time');
      if(exitCancelled)return exitLastResult;
      if(!exitDone&&time<=exitLastTime)return exitLastResult;
      if(!exitDone&&time-exitLastTime>DEFAULTS.maxElapsed){inner.cancel('clock_gap');exitCancelled=true;return exitLastResult=freeze({...exitLastResult,blocked:true,velocity:[0,0,0],reason:'clock_gap'});}
      if(!exitDone&&time<now+exitDuration){
        const t=Math.max(0,(time-now)/exitDuration),p=exit.rawStart.map((v,i)=>v+exit.delta[i]*t);
        coverage?.assertWorldSweep(exitLastResult.position,p,{radius:DEFAULTS.radius,skin:DEFAULTS.skin});
        exitLastTime=time;return exitLastResult=freeze({position:p,velocity:exit.delta.map(v=>v/exitDuration),grounded:true,arrived:false,blocked:false,reason:'initial_contact_exit',heading:Math.atan2(exit.delta[0],exit.delta[2])});
      }
      if(!exitDone)coverage?.assertWorldSweep(exitLastResult.position,exit.adjustedStart,{radius:DEFAULTS.radius,skin:DEFAULTS.skin});
      exitDone=true;return convert(inner.sample(time-exitDuration));
    };
    if(coverage){
      const originalStep=inner.step.bind(inner);
      const capsule={radius:DEFAULTS.radius,skin:DEFAULTS.skin};
      const position=()=>frame.toWorldPosition([inner.p.x,inner.p.y,inner.p.z]);
      // Instrument each ORIGINAL fixed step, including catch-up steps inside sample.
      // Default physics and time/debt semantics are unchanged. The source-derived
      // bound remains conditional on the pinned dependency and static/default setup.
      inner.step=dt=>{
        try{
          need(dt===DEFAULTS.fixedDt,'invalid_coverage_step');
          const before=position();
          const extraMargin=offlineStepQueryReach(speed,dt,this.#motorPolicy)-capsule.radius-capsule.skin;
          coverage.assertWorldCapsule(before,{...capsule,extraMargin});
          originalStep(dt);
          coverage.assertWorldSweep(before,position(),capsule);
        }catch(error){coverageFailure=error;inner.cancel('unknown_collision_coverage');throw error;}
      };
    }
    const convert=result=>freeze({...result,position:frame.toWorldPosition(result.position),velocity:frame.toWorldVector(result.velocity)});
    // Expose only the normal/count subset used by offline replay, never local contact positions.
    const controller=Object.freeze({
      numComputedCollisions:()=>inner.controller.numComputedCollisions(),
      computedCollision:index=>{const hit=inner.controller.computedCollision(index);
        return hit?{normal1:{x:hit.normal1.x,y:hit.normal1.y,z:hit.normal1.z}}:null;}
    });
    const motor=Object.freeze({controller,startSupportAudit:this.#lastAudit,
      snapshot:()=>{need(!closed&&!coverageFailure,'inactive_motor');return exitDone?convert(inner.cached):exitLastResult;},
      retarget:(actualStart,nextPath,nextWindow,nextSpeed=speed)=>{
        need(exitDone&&!closed&&!coverageFailure&&!inner.closed&&!inner.arrived&&!inner.blocked&&inner.grounded&&inner.cached.grounded,'active_grounded_motor_required');
        need(nextSpeed===speed&&nextSpeed===inner.speed,'retarget_speed_change_requires_cold_plan');
        const current=frame.toWorldPosition(inner.cached.position),checkedStart=frame.toWorldPosition(frame.toLocalPosition(actualStart));
        need(Math.hypot(...current.map((v,i)=>v-checkedStart[i]))<=1e-5,'retarget_start_mismatch');
        const points=frame.toLocalPath(nextPath);need(points.length>0&&points.length<=512,'retarget_path_budget');
        const nav=this.#world.navigationWindow;
        need(!nav||points.every(p=>p[0]>=nav.minX&&p[0]<=nav.maxX&&p[2]>=nav.minZ&&p[2]<=nav.maxZ),'outside_navigation_window');
        const w=frame.toWorldWindow(frame.toLocalWindow(nextWindow)),outer=this.#coverage?.worldWindow;
        need(outer&&w.minX>=outer.minX&&w.maxX<=outer.maxX&&w.minZ>=outer.minZ&&w.maxZ<=outer.maxZ,'route_window_outside_cached_geometry');
        const nextCoverage=createCollisionCoverage(frame,w,frame.toLocalWindow(w),this.#coverage.maxXZQuantizationError);
        nextCoverage.assertWorldPath([current,...nextPath],{radius:DEFAULTS.radius,skin:DEFAULTS.skin});
        const nextLocalPath=points.map(p=>({x:p[0],y:p[1],z:p[2]})),snapshot=convert(inner.cached);
        // Commit only after EVERY check. No sample, collider/controller rebuild,
        // clock/debt/gravity/support reset, or position write occurs here.
        inner.path=nextLocalPath;inner.index=0;inner.goal=nextLocalPath.at(-1);coverage=nextCoverage;
        return snapshot;
      },
      sample:time=>{if(coverageFailure)throw coverageFailure;return sampleWithExit(time);},
      cancel:reason=>{if(coverageFailure)throw coverageFailure;const result=convert(inner.cancel(reason));if(!exitDone){exitCancelled=true;return exitLastResult=freeze({...result,position:[...exitLastResult.position]});}return result;},
      dispose:()=>{if(!closed){inner.dispose();closed=true;this.#motor=null;}}
    });
    this.#motor=motor;return motor;
  }
  prepareHopSession({groundResult,start,target,speed,scope,collisionWindow,assetManifestSHA256,artifactDirectory,clock,currentScope,ttlSeconds,aerialProfiles=[]}){
    this.#open();need(this.#motor===null&&this.#hop===null&&this.#world.motors.size===0,'single_motor_only');need(this.#coverage&&!this.#motorPolicy,'hop_default_covered_world_required');
    const frame=this.#frame,outer=this.#coverage.worldWindow,w=frame.toWorldWindow(frame.toLocalWindow(collisionWindow));
    need(w.minX>=outer.minX&&w.maxX<=outer.maxX&&w.minZ>=outer.minZ&&w.maxZ<=outer.maxZ,'hop_window_outside_geometry');
    // Entire <=2-unit hop and every bounded KCC internal query remain in known source geometry.
    this.#coverage.assertWorldCapsule(start,{radius:DEFAULTS.radius,skin:DEFAULTS.skin,extraMargin:offlineStepQueryReach(speed)+2});
    const localStart=frame.toLocalPosition(start),localTarget=frame.toLocalPosition(target),queryWindow=frame.toLocalWindow(w),known=this.#world.navigationWindow??{minX:this.#world.bounds.lo[0],maxX:this.#world.bounds.hi[0],minZ:this.#world.bounds.lo[2],maxZ:this.#world.bounds.hi[2]};
    // Query margin is not a demand to let the avatar move across that entire area.
    // Keep the short-hop movement box inside original known/nav bounds.
    const localWindow={minX:Math.max(queryWindow.minX,known.minX,localStart[0]-2.5),maxX:Math.min(queryWindow.maxX,known.maxX,localStart[0]+2.5),minZ:Math.max(queryWindow.minZ,known.minZ,localStart[2]-2.5),maxZ:Math.min(queryWindow.maxZ,known.maxZ,localStart[2]+2.5)};
    let automatic=prepareAutomaticHop(this.#world,{groundResult,start:localStart,target:localTarget,speed,scope,collisionWindow:localWindow});
    let selectedWindow=localWindow;if(automatic.status!=='ready'&&aerialProfiles.length){const original=automatic.audit,extent=aerialProfiles.includes('drop_v22')?8.5:4.5;selectedWindow={minX:Math.max(queryWindow.minX,known.minX,localStart[0]-extent),maxX:Math.min(queryWindow.maxX,known.maxX,localStart[0]+extent),minZ:Math.max(queryWindow.minZ,known.minZ,localStart[2]-extent),maxZ:Math.min(queryWindow.maxZ,known.maxZ,localStart[2]+extent)};automatic=prepareAutomaticAerial(this.#world,{groundResult,start:localStart,target:localTarget,speed,scope,collisionWindow:selectedWindow,aerialProfiles});automatic.audit={...automatic.audit,shortHopAudit:original};}if(automatic.status!=='ready')return {status:'blocked',reason:automatic.reason,audit:automatic.audit};
    const landing=automatic.prepared.receipt.landing;
    const bridge=createHopWorkerBridge({world:this.#world,scope,assetBinding:{assetManifestSHA256,expectedWorldSHA256:loadedHopWorldSHA256(this.#world)},coordinateFrame:{kind:'translation_only',origin:[...frame.origin]},artifactDirectory,clock,currentScope,aerialProfileId:automatic.prepared.receipt.profileId??null});
    const prepared=bridge.prepare({scope,ttlSeconds,request:{start:localStart,landing,speed,collisionWindow:selectedWindow}});
    let closed=false;const close=reason=>{if(closed)return;try{bridge.hardCancel({scope,token:prepared.token,reason});}catch{}finally{closed=true;this.#hop=null;}};
    const session=Object.freeze({prepared:{...prepared,automaticHopAudit:{...automatic.audit,selectedLandingWorld:frame.toWorldPosition(landing)},worldRequestedStart:[...start],worldSelectedLanding:frame.toWorldPosition(landing)},
      invoke:(op,payload)=>{need(!closed,'hop_session_closed');try{const reply=bridge[op](payload);if(reply.state==='landed'||reply.state==='closed'){closed=true;this.#hop=null;}return {...reply,...(Array.isArray(reply.position)?{worldFeetPosition:frame.toWorldPosition(reply.position)}:{})};}catch(e){close(e.message);throw e;}},close});
    this.#hop=session;return {status:'ready',session};
  }
  dispose(){if(this.#closed)return;this.#hop?.close('world_disposed');this.#motor?.dispose();this.#world.dispose();this.#closed=true;}
}
