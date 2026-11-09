// Application-side bounded orchestration of MIT recast-navigation public APIs.
// Builds occupied tiles only, checks every result, and never accepts truncation.
import {Raw, NavMesh, NavMeshParams, VerticesArray, TrianglesArray,
  RecastChunkyTriMesh, RecastBuildContext, ChunkIdsArray, Detour, statusFailed,
  freeHeightfield, freeCompactHeightfield, freeContourSet, freePolyMesh,
  freePolyMeshDetail} from 'recast-navigation';
import {buildTiledNavMeshRcConfig,
  tiledNavMeshGeneratorConfigDefaults} from 'recast-navigation/generators';
import {generateTileNavMeshData} from './vendor/tile_generator.mjs';
import {candidateTiles} from './nav_domains.mjs';

export function occupiedTiles(p, indices, bounds, c) {
  const size = c.tileSize * c.cs, pad = (c.walkableRadius + 3) * c.cs;
  const width = Math.max(1, Math.ceil((bounds.hi[0]-bounds.lo[0])/size));
  const depth = Math.max(1, Math.ceil((bounds.hi[2]-bounds.lo[2])/size));
  const tiles = new Map(); let visits = 0;
  for (let i=0;i<indices.length;i+=3) {
    const ids=[indices[i]*3,indices[i+1]*3,indices[i+2]*3];
    const lo=[0,1,2].map(k=>Math.min(...ids.map(j=>p[j+k])));
    const hi=[0,1,2].map(k=>Math.max(...ids.map(j=>p[j+k])));
    const x0=Math.max(0,Math.floor((lo[0]-pad-bounds.lo[0])/size));
    const z0=Math.max(0,Math.floor((lo[2]-pad-bounds.lo[2])/size));
    const x1=Math.min(width-1,Math.floor((hi[0]+pad-bounds.lo[0])/size));
    const z1=Math.min(depth-1,Math.floor((hi[2]+pad-bounds.lo[2])/size));
    visits+=(x1-x0+1)*(z1-z0+1);
    if(visits>8_000_000)throw Error('nav_triangle_tile_budget');
    for(let z=z0;z<=z1;z++)for(let x=x0;x<=x1;x++) {
      const key=x+':'+z, old=tiles.get(key);
      if(old){old.minY=Math.min(old.minY,lo[1]);old.maxY=Math.max(old.maxY,hi[1]);}
      else tiles.set(key,{x,y:z,minY:lo[1],maxY:hi[1]});
      if(tiles.size*c.tileSize*c.tileSize>64_000_000||tiles.size>16384)throw Error('nav_occupied_tile_budget');
    }
  }
  return [...tiles.values()].sort((a,b)=>a.y-b.y||a.x-b.x);
}
function freeTile(i) {
  if(i?.heightfield)freeHeightfield(i.heightfield);
  if(i?.compactHeightfield)freeCompactHeightfield(i.compactHeightfield);
  if(i?.contourSet)freeContourSet(i.contourSet);
  if(i?.polyMesh)freePolyMesh(i.polyMesh);
  if(i?.polyMeshDetail)freePolyMeshDetail(i.polyMeshDetail);
}
export function classifyTile(result) {
  if(result.success)return result.data?'data':'empty';
  // The pinned generator calls Detour even when a valid tile has zero polygons.
  if(result.error==='Failed to create Detour navmesh data' && result.intermediates?.polyMesh?.npolys()===0)return 'empty';
  throw Error('nav_tile_failed:'+String(result.error));
}
export function completeChunkCapacity(chunky,lo,hi,ids,max=16384) {
  for(let capacity=513;;capacity=Math.min(max,capacity*2)) {
    ids.resize(capacity);
    const count=chunky.getChunksOverlappingRect(lo,hi,ids,capacity);
    if(count<capacity)return capacity;
    if(capacity===max)throw Error('nav_tile_chunk_budget');
  }
}
export function generateSparseNavMesh(p, indices, bounds, values) {
  const c={...tiledNavMeshGeneratorConfigDefaults,...values};
  const cellBudget=c.maxRasterCells??64_000_000;
  if(!Number.isSafeInteger(cellBudget)||cellBudget<1||cellBudget>192_000_000)throw Error('nav_raster_budget_config');
  const stats={tileSize:c.tileSize,cellBudget,occupiedTiles:0,builtTiles:0,emptyTiles:0,failedTiles:0};
  let nav,vertices,triangles,chunky,ctx,chunkIds,rc,params,success=false;
  try {
    const domains=candidateTiles(p,indices,bounds,{tileSize:c.tileSize,cs:c.cs,ch:c.ch,
      walkableRadius:c.walkableRadius,walkableHeight:c.walkableHeight,walkableClimb:c.walkableClimb,
      walkableSlopeAngle:c.walkableSlopeAngle,maxTiles:Math.min(16384,Math.floor(cellBudget/(c.tileSize*c.tileSize))),
      ...Object.fromEntries(['windowMinX','windowMaxX','windowMinZ','windowMaxZ'].filter(k=>k in values).map(k=>[k,values[k]])),
      ...(Math.max(Math.abs(bounds.lo[0]),Math.abs(bounds.lo[2]))>100000?{gridOriginX:0,gridOriginZ:0}:{})});
    const tiles=domains.tiles.flatMap(t=>t.layers.map(l=>({...t,...l})));
    stats.occupiedTiles=domains.tiles.length;stats.layers=tiles.length;stats.domainStats=domains.stats;stats.gridOrigin=domains.gridOrigin;
    if(domains.scope)stats.windowScope=domains.scope;
    if(tiles.length*c.tileSize*c.tileSize>cellBudget||tiles.length>16384)throw Error('nav_occupied_layer_budget');
    if(!tiles.length)throw Error('navigation_empty');
    const build=buildTiledNavMeshRcConfig({recastConfig:c,navMeshBounds:[bounds.lo,bounds.hi]});
    rc=build.config;
    const tileBits=Math.ceil(Math.log2(Math.max(1,tiles.length)));
    const maxPolys=2**(22-tileBits);
    const [ox,oy,oz]=domains.gridOrigin;
    params=NavMeshParams.create({orig:{x:ox,y:oy,z:oz},tileWidth:build.tcs,tileHeight:build.tcs,
      maxTiles:2**tileBits,maxPolys});
    nav=new NavMesh();if(!nav.initTiled(params))throw Error('nav_tiled_init');
    vertices=new VerticesArray();vertices.copy(p);
    triangles=new TrianglesArray();triangles.copy(indices);
    chunky=new RecastChunkyTriMesh();
    if(!chunky.init(vertices,triangles,indices.length/3,c.chunkyTriMeshTrisPerChunk))throw Error('nav_chunk_index');
    ctx=new RecastBuildContext();chunkIds=new ChunkIdsArray();chunkIds.resize(513);
    const pad=rc.borderSize*rc.cs,chunkCapacities=new Map();
    for(const t of tiles) {
      // Clipped walkable surfaces determine vertical domains; full geometry still rasterizes.
      const minY=t.bmin[1],maxY=t.bmax[1];
      // Recast rcSpan has 13 height bits; reject rather than silently clipping.
      if((maxY-minY)/c.ch>=8191)throw Error('nav_tile_vertical_span_budget');
      const tile={x:t.x,y:t.y,bmin:t.bmin,bmax:t.bmax};
      const key=t.x+':'+t.y;
      if(!chunkCapacities.has(key))chunkCapacities.set(key,completeChunkCapacity(chunky,
        [tile.bmin[0]-pad,tile.bmin[2]-pad],[tile.bmax[0]+pad,tile.bmax[2]+pad],chunkIds));
      const maxChunkIds=chunkCapacities.get(key);
      stats.maxChunkCapacity=Math.max(stats.maxChunkCapacity??0,maxChunkIds);
      ctx.resetLog();
      const result=generateTileNavMeshData(vertices,triangles,rc,chunky,tile,
        {buildBvTree:c.buildBvTree,tileLayer:t.layer,maxChunkIds},true,ctx);
      try {
        const kind=classifyTile(result);
        if(kind==='empty'){stats.emptyTiles++;continue;}
        if(result.intermediates.polyMesh.npolys()>maxPolys){result.data.destroy();throw Error('nav_tile_poly_ref_budget');}
        const added=nav.addTile(result.data,Detour.DT_TILE_FREE_DATA,0);
        if(statusFailed(added.status)){result.data.destroy();throw Error('nav_tile_add_failed:'+added.status);}
        stats.builtTiles++;
      } finally {freeTile(result.intermediates);}
    }
    if(!stats.builtTiles)throw Error('navigation_empty');
    success=true;return {success:true,navMesh:nav,stats};
  } catch(error) {
    stats.failedTiles++;
    return {success:false,error:String(error?.message||error),stats};
  } finally {
    if(!success)nav?.destroy();
    vertices?.destroy();triangles?.destroy();chunky?.destroy();ctx?.destroy();chunkIds?.destroy();
    if(rc)Raw.destroy(rc);if(params)Raw.destroy(params.raw);
  }
}
