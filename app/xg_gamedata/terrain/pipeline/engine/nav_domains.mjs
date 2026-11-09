// Navigation rasterization domains only. No collision triangles are edited or discarded.
// Layers are bounded rasterization domains, never inferred connectivity/portals.
const DEFAULTS = Object.freeze({tileSize:64, cs:.15, ch:.075, walkableRadius:2,
  walkableHeight:20, walkableClimb:4, walkableSlopeAngle:42,
  maxTiles:16384, maxIntervals:2_000_000, maxStripRows:2_000_000,
  maxTileVisits:4_000_000, maxLayersPerTile:64, maxSpanVoxels:8190,
  provenanceLimit:8});
const finite=x=>typeof x==='number'&&Number.isFinite(x);
const WINDOW_KEYS=Object.freeze(['windowMinX','windowMaxX','windowMinZ','windowMaxZ']);
function checkedConfig(values) {
  for(const k of Object.keys(values))if(k.startsWith('window')&&!WINDOW_KEYS.includes(k))throw Error('candidate_unknown_window_field:'+k);
  const windowCount=WINDOW_KEYS.filter(k=>Object.hasOwn(values,k)).length;
  if(windowCount!==0&&windowCount!==4)throw Error('candidate_incomplete_window');
  const c={...DEFAULTS,...values};
  for(const [k,v] of Object.entries(c))if(!finite(v))throw Error('candidate_invalid_config:'+k);
  for(const k of ['tileSize','walkableRadius','walkableHeight','walkableClimb','maxTiles','maxIntervals',
    'maxStripRows','maxTileVisits','maxLayersPerTile','maxSpanVoxels','provenanceLimit'])
    if(!Number.isSafeInteger(c[k])||c[k]<0)throw Error('candidate_invalid_integer:'+k);
  if(c.tileSize<1||c.cs<=0||c.ch<=0||c.walkableHeight<1||c.maxTiles<1||
    c.maxIntervals<1||c.maxStripRows<1||c.maxTileVisits<1||c.maxLayersPerTile<1||
    c.maxSpanVoxels<1||c.walkableSlopeAngle<=0||c.walkableSlopeAngle>=90)
    throw Error('candidate_config_range');
  // rcConfig.borderSize defaults to walkableRadius + 3 in the pinned generator.
  c.borderSize=values.borderSize??(c.walkableRadius+3);
  if(!Number.isSafeInteger(c.borderSize)||c.borderSize<0)throw Error('candidate_border_size');
  if(windowCount&&!(c.windowMinX<c.windowMaxX&&c.windowMinZ<c.windowMaxZ))throw Error('candidate_window_range');
  return c;
}
export function upwardWalkable(a,b,c,slopeDegrees) {
  const u=b.map((v,k)=>v-a[k]), w=c.map((v,k)=>v-a[k]);
  const n=[u[1]*w[2]-u[2]*w[1],u[2]*w[0]-u[0]*w[2],u[0]*w[1]-u[1]*w[0]];
  const length=Math.hypot(...n);
  // Recast markWalkableTriangles uses upward normal and strict > cos(slope).
  return length>0&&n[1]/length>Math.cos(slopeDegrees*Math.PI/180);
}
function clipPlane(poly,axis,edge,greater) {
  if(!poly.length)return [];
  const result=[];let a=poly.at(-1),da=(a[axis]-edge)*(greater?1:-1),insideA=da>=0;
  for(const b of poly) {
    const db=(b[axis]-edge)*(greater?1:-1),insideB=db>=0;
    if(insideA!==insideB) {
      const t=da/(da-db), q=a.map((v,k)=>v+t*(b[k]-v));
      q[axis]=edge;result.push(q);
    }
    if(insideB)result.push(b);
    a=b;da=db;insideA=insideB;
  }
  return result;
}
export function clipTriangleXZ(triangle,x0,z0,x1,z1) {
  let poly=triangle;
  for(const [axis,edge,greater] of [[0,x0,true],[0,x1,false],[2,z0,true],[2,z1,false]])
    poly=clipPlane(poly,axis,edge,greater);
  return poly;
}
function projectedArea(poly) {
  if(poly.length<3)return 0;
  const o=poly[0];let twice=0;
  for(let i=1;i+1<poly.length;i++)twice+=(poly[i][0]-o[0])*(poly[i+1][2]-o[2])-(poly[i][2]-o[2])*(poly[i+1][0]-o[0]);
  return Math.abs(twice)*.5;
}
export function aggregateHeightLayers(intervals,c,originY) {
  // Union padded vertical domains. This says nothing about floor connectivity.
  const lowerMargin=(c.walkableClimb+1)*c.ch;
  const upperMargin=(c.walkableHeight+c.walkableClimb+1)*c.ch;
  const domains=intervals.map(q=>({...q,lo:q.minY-lowerMargin,hi:q.maxY+upperMargin}))
    .sort((a,b)=>a.lo-b.lo||a.hi-b.hi||a.triangle-b.triangle);
  const groups=[];const eps=c.ch*1e-7;
  for(const q of domains) {
    let g=groups.at(-1);
    if(!g||q.lo>g.hi+eps) {
      if(groups.length>=c.maxLayersPerTile)throw Error('candidate_layer_budget');
      g={lo:q.lo,hi:q.hi,minY:q.minY,maxY:q.maxY,triangleContributions:0,sourceTriangleSample:[]};groups.push(g);
    }
    g.hi=Math.max(g.hi,q.hi);g.minY=Math.min(g.minY,q.minY);g.maxY=Math.max(g.maxY,q.maxY);
    g.triangleContributions++;
    if(g.sourceTriangleSample.length<c.provenanceLimit)g.sourceTriangleSample.push(q.triangle);
  }
  return groups.map((g,layer)=>{
    const voxelMin=Math.floor((g.lo-originY)/c.ch), voxelMax=Math.ceil((g.hi-originY)/c.ch);
    if(!Number.isSafeInteger(voxelMin)||!Number.isSafeInteger(voxelMax)||voxelMax-voxelMin>c.maxSpanVoxels)
      throw Error('candidate_layer_vertical_span_budget');
    return {layer,minY:g.minY,maxY:g.maxY,voxelMin,voxelMax,
      boundsMinY:originY+voxelMin*c.ch,boundsMaxY:originY+voxelMax*c.ch,
      lowerMargin,upperMargin,triangleContributions:g.triangleContributions,
      sourceTriangleSample:g.sourceTriangleSample,connectivity:'not_inferred'};
  });
}
export function candidateTiles(positions,indices,bounds,values={}) {
  const c=checkedConfig(values);
  if(!(Array.isArray(positions)||ArrayBuffer.isView(positions))||!(Array.isArray(indices)||ArrayBuffer.isView(indices))||
     !Number.isSafeInteger(positions.length)||!Number.isSafeInteger(indices.length)||
     positions.length%3||indices.length%3||!positions.length||!indices.length)throw Error('candidate_mesh_shape');
  if(!bounds||!Array.isArray(bounds.lo)||!Array.isArray(bounds.hi)||bounds.lo.length!==3||bounds.hi.length!==3||
     ![...bounds.lo,...bounds.hi].every(finite)||bounds.lo.some((v,k)=>v>bounds.hi[k]))throw Error('candidate_bounds');
  for(let j=0;j<positions.length;j++) {
    const v=positions[j],axis=j%3;
    if(!finite(v)||v<bounds.lo[axis]-1e-5||v>bounds.hi[axis]+1e-5)throw Error('candidate_vertex_or_bounds');
  }
  for(const id of indices)if(!Number.isSafeInteger(id)||id<0||id>=positions.length/3)throw Error('candidate_index');
  const tcs=c.tileSize*c.cs,pad=c.borderSize*c.cs;
  const originX=c.gridOriginX??bounds.lo[0],originZ=c.gridOriginZ??bounds.lo[2];
  let minTileX=Math.floor((bounds.lo[0]-originX)/tcs),minTileZ=Math.floor((bounds.lo[2]-originZ)/tcs);
  let maxTileX=Math.max(minTileX,Math.ceil((bounds.hi[0]-originX)/tcs)-1);
  let maxTileZ=Math.max(minTileZ,Math.ceil((bounds.hi[2]-originZ)/tcs)-1);
  if(![minTileX,minTileZ,maxTileX,maxTileZ].every(Number.isSafeInteger)||!finite(tcs)||tcs<=0)throw Error('candidate_grid_range');
  let windowScope;
  if(Object.hasOwn(c,'windowMinX')) {
    const requested={minX:c.windowMinX,maxX:c.windowMaxX,minZ:c.windowMinZ,maxZ:c.windowMaxZ};
    if(requested.maxX<=bounds.lo[0]||requested.minX>=bounds.hi[0]||requested.maxZ<=bounds.lo[2]||requested.minZ>=bounds.hi[2])
      throw Error('candidate_window_outside_geometry');
    const range=[Math.floor((requested.minX-originX)/tcs),Math.ceil((requested.maxX-originX)/tcs)-1,
      Math.floor((requested.minZ-originZ)/tcs),Math.ceil((requested.maxZ-originZ)/tcs)-1];
    if(!range.every(Number.isSafeInteger))throw Error('candidate_window_range');
    minTileX=Math.max(minTileX,range[0]);maxTileX=Math.min(maxTileX,range[1]);
    minTileZ=Math.max(minTileZ,range[2]);maxTileZ=Math.min(maxTileZ,range[3]);
    if(minTileX>maxTileX||minTileZ>maxTileZ)throw Error('candidate_window_empty_grid');
    const actual={minX:originX+minTileX*tcs,maxX:originX+(maxTileX+1)*tcs,
      minZ:originZ+minTileZ*tcs,maxZ:originZ+(maxTileZ+1)*tcs};
    // Selecting a tile does not authorize routes through its out-of-window fringe.
    // Support clipping still uses the complete padded tile so edge rasterization
    // sees external obstacles and headroom; the original collision mesh is intact.
    windowScope={requestedWindowBounds:requested,actualWindowBounds:actual,
      scope:{kind:'bounded_window',queryBounds:{...requested},outsideNavigation:'unknown',
        allPathPointsMustRemainInQueryBounds:true,fullCollisionGeometryRetained:true}};
  }
  // Reject only zero-area XZ intersections, not small positive triangles.
  const tiles=new Map();
  const stats={sourceTriangles:indices.length/3,walkableTriangles:0,nonWalkableTriangles:0,
    stripRows:0,tileVisits:0,clippedContributions:0,occupiedTiles:0,layers:0};
  for(let i=0;i<indices.length;i+=3) {
    const tri=[indices[i],indices[i+1],indices[i+2]].map(id=>[positions[id*3],positions[id*3+1],positions[id*3+2]]);
    if(!upwardWalkable(...tri,c.walkableSlopeAngle)){stats.nonWalkableTriangles++;continue;}
    stats.walkableTriangles++;
    const minZ=Math.min(...tri.map(p=>p[2])),maxZ=Math.max(...tri.map(p=>p[2]));
    const z0=Math.max(minTileZ,Math.floor((minZ-pad-originZ)/tcs)),z1=Math.min(maxTileZ,Math.floor((maxZ+pad-originZ)/tcs));
    if(z0>z1)continue;
    if(stats.stripRows+z1-z0+1>c.maxStripRows)throw Error('candidate_strip_budget');
    for(let z=z0;z<=z1;z++) {
      stats.stripRows++;
      const lowZ=originZ+z*tcs-pad,highZ=originZ+(z+1)*tcs+pad;
      let strip=clipPlane(tri,2,lowZ,true);strip=clipPlane(strip,2,highZ,false);
      if(projectedArea(strip)<=0)continue;
      const minX=Math.min(...strip.map(p=>p[0])),maxX=Math.max(...strip.map(p=>p[0]));
      const x0=Math.max(minTileX,Math.floor((minX-pad-originX)/tcs)),x1=Math.min(maxTileX,Math.floor((maxX+pad-originX)/tcs));
      if(x0>x1)continue;
      if(stats.tileVisits+x1-x0+1>c.maxTileVisits)throw Error('candidate_tile_visit_budget');
      for(let x=x0;x<=x1;x++) {
        stats.tileVisits++;
        let clipped=clipPlane(strip,0,originX+x*tcs-pad,true);
        clipped=clipPlane(clipped,0,originX+(x+1)*tcs+pad,false);
        if(projectedArea(clipped)<=0)continue;
        if(++stats.clippedContributions>c.maxIntervals)throw Error('candidate_interval_budget');
        const key=x+':'+z;let tile=tiles.get(key);
        if(!tile) {
          if(tiles.size>=c.maxTiles)throw Error('candidate_tile_budget');
          tile={x,y:z,intervals:[]};tiles.set(key,tile);
        }
        tile.intervals.push({minY:Math.min(...clipped.map(p=>p[1])),maxY:Math.max(...clipped.map(p=>p[1])),triangle:i/3});
      }
    }
  }
  const output=[...tiles.values()].sort((a,b)=>a.y-b.y||a.x-b.x).map(t=>{
    const layers=aggregateHeightLayers(t.intervals,c,bounds.lo[1]);stats.layers+=layers.length;
    return {x:t.x,y:t.y,layers:layers.map(l=>({...l,
      bmin:[originX+t.x*tcs,l.boundsMinY,originZ+t.y*tcs],
      bmax:[originX+(t.x+1)*tcs,l.boundsMaxY,originZ+(t.y+1)*tcs]}))};
  });
  stats.occupiedTiles=output.length;
  return {tiles:output,stats,config:c,tileWorldSize:tcs,borderWorldSize:pad,gridOrigin:[originX,bounds.lo[1],originZ],
    ...windowScope,
    collisionGeometryUnchanged:true,connectivity:'not_inferred',runtimeReady:false,
    instruction:'Rasterize original full collision geometry into each candidate domain; do not turn candidate layers into graph links.'};
}
