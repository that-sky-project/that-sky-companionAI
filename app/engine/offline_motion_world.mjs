// Explicit offline-only collision adapter; core/defaults/live worker stay unchanged.
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

export class OfflineMotionWorld{
  #motorPolicy;#frame;#world;#motor=null;#hop=null;#closed=false;#supportEnabled;#lastAudit=null;#coverage=null;
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
    if(result.support)result.support.position=this.#frame.toWorldPosition(result.support.position);
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
      const supportWorld=this.#motorPolicy?{config:DEFAULTS,navigationWindow:this.#world.navigationWindow,ground:this.#world.ground.bind(this.#world)}:this.#world;
      // Autostep policy does not alter the capsule/start-only .3m support proposal.
      const proposal=queryStartSupportProjection(supportWorld,localStart);
      this.#lastAudit=this.#audit(proposal);
      if(proposal.status!=='adjusted')throw error;
      // Original collision constructor must still accept the proposed start.
      try{inner=this.#world.motor(proposal.adjustedStart,localPath,now,speed);}
      catch(rejected){
        this.#lastAudit=freeze({...this.#lastAudit,applied:false,collisionConstructorAccepted:false});
        throw rejected;
      }
      this.#lastAudit=freeze({...this.#lastAudit,collisionConstructorAccepted:true});
    }
    const frame=this.#frame;let closed=false,coverageFailure=null;
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
      snapshot:()=>{need(!closed&&!coverageFailure,'inactive_motor');return convert(inner.cached);},
      retarget:(actualStart,nextPath,nextWindow,nextSpeed=speed)=>{
        need(!closed&&!coverageFailure&&!inner.closed&&!inner.arrived&&!inner.blocked&&inner.grounded&&inner.cached.grounded,'active_grounded_motor_required');
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
      sample:time=>{if(coverageFailure)throw coverageFailure;return convert(inner.sample(time));},
      cancel:reason=>{if(coverageFailure)throw coverageFailure;return convert(inner.cancel(reason));},
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
