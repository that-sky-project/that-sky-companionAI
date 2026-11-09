// Explicit short-range static navigation. Complete original geometry rasterizes;
// only the candidate tile domain is windowed. No voxel/physics budget changes.
import {NavMeshQuery} from 'recast-navigation';
import {DEFAULTS,validateMesh,navigationWindow} from './core.mjs';
import {generateSparseNavMesh} from './sparse_nav.mjs';
export const ON_DEMAND_POLICY=Object.freeze({windowMeters:64,gridMeters:32,cacheEntries:3,maxRouteMeters:8});
const need=(ok,why)=>{if(!ok)throw Error('on_demand_nav:'+why);};
const canon=v=>Array.isArray(v)?'['+v.map(canon).join(',')+']':v&&typeof v==='object'?'{'+Object.keys(v).sort().map(k=>JSON.stringify(k)+':'+canon(v[k])).join(',')+'}':JSON.stringify(v);
const point=p=>Array.isArray(p)&&p.length===3&&p.every(x=>typeof x==='number'&&Number.isFinite(x)&&Math.abs(x)<1e6);
export function validateOnDemandDescriptor(d,asset,q){
 need(d?.format==='skyterrain-navigation-on-demand-v1'&&d.mode==='offline'&&d.status==='on_demand_static_candidate','descriptor_schema');
 for(const k of ['nativeCollisionVerified','calibrationVerified','runtimeReady'])need(d[k]===false,'offline_flags:'+k);
 need(d.mapName===asset.manifest.mapName&&d.geometryKind===asset.manifest.geometryKind&&['offline_beamo_augmented_candidates','initial_static_collision_candidates'].includes(d.geometryKind),'map_or_kind');
 need(asset.manifest.nativeCollisionVerified===false&&asset.manifest.calibrationVerified===false,'asset_flags');
 need(canon(d.config)===canon(DEFAULTS)&&(!Object.hasOwn(q,'config')||canon(q.config)===canon(DEFAULTS)),'unchanged_defaults_required');
 for(const [k,v]of Object.entries(asset.assetDigests))need(d.inputHashes?.[k]===v,'asset_pin:'+k);
 need(d.inputVertices===asset.positions.length/3&&d.inputTriangles===asset.indices.length/3,'input_counts');
 need(d.navigationCoverage==='on_demand_static_candidate'&&d.navigationWindow===null&&q.navigationWindow==null,'coverage');
 need(canon(d.policy)===canon(ON_DEMAND_POLICY),'fixed_policy');
 need(/^[0-9a-f]{64}$/.test(q.expectedNavigationSHA??'')&&q.expectedNavigationSHA===q.expectedNavigationReportSHA,'descriptor_identity_pin');
 return ON_DEMAND_POLICY;
}
export function chooseNavigationWindow(start,goal){
 need(point(start)&&point(goal),'finite_points');
 // Endpoint projection may contribute up to .3m at either end; completePath
 // separately retains the exact <=8m path-length gate.
 need(Math.hypot(start[0]-goal[0],start[2]-goal[2])<=8.6,'short_range_required');
 const axis=k=>Math.floor(((start[k]+goal[k])/2)/32)*32+16;
 const x=axis(0),z=axis(2);
 return navigationWindow({minX:x-32,maxX:x+32,minZ:z-32,maxZ:z+32});
}
const contains=(w,p,pad=0)=>p[0]>=w.minX+pad&&p[0]<=w.maxX-pad&&p[2]>=w.minZ+pad&&p[2]<=w.maxZ-pad;
export class OnDemandNavigation{
 #positions;#indices;#bounds;#cache=[];#tick=0;#hits=0;#builds=0;#closed=false;
 constructor(asset){this.#positions=asset.positions;this.#indices=asset.indices;this.#bounds=validateMesh(asset.positions,asset.indices);}
 audit(){return {kind:'on_demand_static_nav_lru',entries:this.#cache.length,capacity:3,hits:this.#hits,builds:this.#builds,windowMeters:64,gridMeters:32,fullMapBuilt:false};}
 acquire(start,goal,{cold=false}={}){
  need(!this.#closed,'disposed');need(point(start)&&point(goal),'finite_points');
  const hit=this.#cache.find(e=>contains(e.window,start,8)&&contains(e.window,goal,8));
  if(hit){hit.used=++this.#tick;this.#hits++;return {entry:hit,built:false};}
  if(!cold)return null;
  const window=chooseNavigationWindow(start,goal),c=DEFAULTS;
  const settings={tileSize:64,cs:c.cellSize,ch:c.cellHeight,walkableSlopeAngle:c.slopeDegrees,
   walkableHeight:Math.ceil(c.height/c.cellHeight),walkableClimb:Math.floor(c.stepHeight/c.cellHeight),
   walkableRadius:Math.ceil((c.radius+c.skin)/c.cellSize),minRegionArea:0,mergeRegionArea:0,
   detailSampleDist:6,detailSampleMaxError:1,windowMinX:window.minX,windowMaxX:window.maxX,
   windowMinZ:window.minZ,windowMaxZ:window.maxZ};
  // FULL source buffers are passed, including walls, ceilings and other layers.
  // generateSparseNavMesh applies the existing candidate/layer/raster budgets.
  const began=performance.now(),built=generateSparseNavMesh(this.#positions,this.#indices,this.#bounds,settings);
  need(built.success,built.error??'build_failed');let query;
  try{query=new NavMeshQuery(built.navMesh,{maxNodes:8192});}
  catch(e){built.navMesh.destroy();throw e;}
  const entry={nav:built.navMesh,query,window,stats:built.stats,buildMs:performance.now()-began,used:++this.#tick};
  if(this.#cache.length>=3){const oldest=this.#cache.reduce((a,b)=>a.used<b.used?a:b);oldest.query.destroy();oldest.nav.destroy();this.#cache.splice(this.#cache.indexOf(oldest),1);}
  this.#cache.push(entry);this.#builds++;return {entry,built:true};
 }
 dispose(){if(!this.#closed){for(const e of this.#cache){e.query.destroy();e.nav.destroy();}this.#cache=[];this.#closed=true;}}
}
