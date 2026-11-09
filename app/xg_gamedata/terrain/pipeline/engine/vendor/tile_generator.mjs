// Derived from @recast-navigation/generators 0.44.0, MIT; see LICENSE.recast-generators.
// Alterations: release rcConfig; set layer identity; bounded complete chunk-query capacity.
import { vec3, recastConfigDefaults, Raw, NavMesh, RecastBuildContext, VerticesArray, TrianglesArray, createRcConfig, calcGridSize, allocHeightfield, createHeightfield, TriangleAreasArray, markWalkableTriangles, rasterizeTriangles, filterLowHangingWalkableObstacles, filterLedgeSpans, filterWalkableLowHeightSpans, allocCompactHeightfield, buildCompactHeightfield, freeHeightfield, erodeWalkableArea, buildDistanceField, buildRegions, allocContourSet, buildContours, Recast, allocPolyMesh, buildPolyMesh, allocPolyMeshDetail, buildPolyMeshDetail, freeCompactHeightfield, freeContourSet, NavMeshCreateParams, createNavMeshData, freePolyMesh, freePolyMeshDetail, TileCacheMeshProcess, TileCache, DetourTileCacheParams, NavMeshParams, RecastChunkyTriMesh, statusFailed, cloneRcConfig, ChunkIdsArray, allocHeightfieldLayerSet, buildHeightfieldLayers, TileCacheData, Detour, getHeightfieldLayerHeights, getHeightfieldLayerAreas, getHeightfieldLayerCons, buildTileCacheLayer, freeHeightfieldLayerSet, statusToReadableString } from 'recast-navigation';
import {tiledNavMeshGeneratorConfigDefaults} from 'recast-navigation/generators';
export const generateTileNavMeshData = (positions, indices, rcConfig, chunkyTriMesh, tile, options = {}, keepIntermediates = false, buildContextParam) => {
  const tileIntermediate = {
    x: tile.x,
    y: tile.y
  };
  const ownsBuildContext = !buildContextParam;
  const buildContext = buildContextParam ?? new RecastBuildContext();
  const cleanup = () => {
    // Local patch: release the per-tile rcConfig clone, including retained-intermediate builds.
    Raw.destroy(tileConfig);
    if (ownsBuildContext) {
      buildContext.destroy();
    }
    if (keepIntermediates) return;
    if (tileIntermediate.compactHeightfield) {
      freeCompactHeightfield(tileIntermediate.compactHeightfield);
      tileIntermediate.compactHeightfield = undefined;
    }
    if (tileIntermediate.heightfield) {
      freeHeightfield(tileIntermediate.heightfield);
      tileIntermediate.heightfield = undefined;
    }
    if (tileIntermediate.contourSet) {
      freeContourSet(tileIntermediate.contourSet);
      tileIntermediate.contourSet = undefined;
    }
    if (tileIntermediate.polyMesh) {
      freePolyMesh(tileIntermediate.polyMesh);
      tileIntermediate.polyMesh = undefined;
    }
    if (tileIntermediate.polyMeshDetail) {
      freePolyMeshDetail(tileIntermediate.polyMeshDetail);
      tileIntermediate.polyMeshDetail = undefined;
    }
  };
  const failTileMesh = error => {
    buildContext.log(Recast.RC_LOG_ERROR, error);
    cleanup();
    return {
      success: false,
      error,
      intermediates: tileIntermediate
    };
  };
  const tileConfig = cloneRcConfig(rcConfig);
  // Expand the heightfield bounding box by border size to find the extents of geometry we need to build this tile.
  //
  // This is done in order to make sure that the navmesh tiles connect correctly at the borders,
  // and the obstacles close to the border work correctly with the dilation process.
  // No polygons (or contours) will be created on the border area.
  //
  // IMPORTANT!
  //
  //   :''''''''':
  //   : +-----+ :
  //   : |     | :
  //   : |     |<--- tile to build
  //   : |     | :
  //   : +-----+ :<-- geometry needed
  //   :.........:
  //
  // You should use this bounding box to query your input geometry.
  //
  // For example if you build a navmesh for terrain, and want the navmesh tiles to match the terrain tile size
  // you will need to pass in data from neighbour terrain tiles too! In a simple case, just pass in all the 8 neighbours,
  // or use the bounding box below to only pass in a sliver of each of the 8 neighbours.
  const expandedTileBoundsMin = [...tile.bmin];
  const expandedTileBoundsMax = [...tile.bmax];
  expandedTileBoundsMin[0] -= tileConfig.borderSize * tileConfig.cs;
  expandedTileBoundsMin[2] -= tileConfig.borderSize * tileConfig.cs;
  expandedTileBoundsMax[0] += tileConfig.borderSize * tileConfig.cs;
  expandedTileBoundsMax[2] += tileConfig.borderSize * tileConfig.cs;
  tileConfig.set_bmin(0, expandedTileBoundsMin[0]);
  tileConfig.set_bmin(1, expandedTileBoundsMin[1]);
  tileConfig.set_bmin(2, expandedTileBoundsMin[2]);
  tileConfig.set_bmax(0, expandedTileBoundsMax[0]);
  tileConfig.set_bmax(1, expandedTileBoundsMax[1]);
  tileConfig.set_bmax(2, expandedTileBoundsMax[2]);
  // Reset build timer
  buildContext.resetTimers();
  // Start the build process
  buildContext.startTimer(Recast.RC_TIMER_TOTAL);
  buildContext.log(Recast.RC_LOG_PROGRESS, `Building tile at x: ${tile.x}, y: ${tile.y}`);
  buildContext.log(Recast.RC_LOG_PROGRESS, ` - ${tileConfig.width} x ${tileConfig.height} cells`);
  buildContext.log(Recast.RC_LOG_PROGRESS, ` - ${positions.size / 3 / 1000}K verts, ${indices.size / 3 / 1000}K tris`);
  // Allocate voxel heightfield where we rasterize our input data to.
  const heightfield = allocHeightfield();
  tileIntermediate.heightfield = heightfield;
  if (!createHeightfield(buildContext, heightfield, tileConfig.width, tileConfig.height, expandedTileBoundsMin, expandedTileBoundsMax, tileConfig.cs, tileConfig.ch)) {
    return failTileMesh('Could not create heightfield');
  }
  const tbmin = [expandedTileBoundsMin[0], expandedTileBoundsMin[2]];
  const tbmax = [expandedTileBoundsMax[0], expandedTileBoundsMax[2]];
  // TODO: Make grow when returning too many items.
  const maxChunkIds = options.maxChunkIds ?? 512;
  if (!Number.isSafeInteger(maxChunkIds) || maxChunkIds < 1 || maxChunkIds > 16384) {
    return failTileMesh('Invalid bounded chunk capacity');
  }
  const chunkIdsArray = new ChunkIdsArray();
  chunkIdsArray.resize(maxChunkIds);
  const nChunksOverlapping = chunkyTriMesh.getChunksOverlappingRect(tbmin, tbmax, chunkIdsArray, maxChunkIds);
  if (nChunksOverlapping === 0) {
    chunkIdsArray.destroy();
    cleanup();
    return {
      success: true,
      intermediates: tileIntermediate
    };
  }
  for (let i = 0; i < nChunksOverlapping; ++i) {
    const nodeId = chunkIdsArray.get(i);
    const node = chunkyTriMesh.nodes(nodeId);
    const nNodeTris = node.n;
    const nodeTrianglesArray = chunkyTriMesh.getNodeTris(nodeId);
    const triangleAreasArray = new TriangleAreasArray();
    triangleAreasArray.resize(nNodeTris);
    // Find triangles which are walkable based on their slope and rasterize them.
    // If your input data is multiple meshes, you can transform them here, calculate
    // the are type for each of the meshes and rasterize them.
    markWalkableTriangles(buildContext, tileConfig.walkableSlopeAngle, positions, indices.size, nodeTrianglesArray, nNodeTris, triangleAreasArray);
    const success = rasterizeTriangles(buildContext, positions, indices.size, nodeTrianglesArray, triangleAreasArray, nNodeTris, heightfield, tileConfig.walkableClimb);
    nodeTrianglesArray.destroy();
    triangleAreasArray.destroy();
    if (!success) {
      chunkIdsArray.destroy();
      return failTileMesh('Could not rasterize triangles');
    }
  }
  chunkIdsArray.destroy();
  // Once all geometry is rasterized, we do initial pass of filtering to
  // remove unwanted overhangs caused by the conservative rasterization
  // as well as filter spans where the character cannot possibly stand.
  filterLowHangingWalkableObstacles(buildContext, tileConfig.walkableClimb, heightfield);
  filterLedgeSpans(buildContext, tileConfig.walkableHeight, tileConfig.walkableClimb, heightfield);
  filterWalkableLowHeightSpans(buildContext, tileConfig.walkableHeight, heightfield);
  // Compact the heightfield so that it is faster to handle from now on.
  // This will result more cache coherent data as well as the neighbours
  // between walkable cells will be calculated.
  const compactHeightfield = allocCompactHeightfield();
  tileIntermediate.compactHeightfield = compactHeightfield;
  if (!buildCompactHeightfield(buildContext, tileConfig.walkableHeight, tileConfig.walkableClimb, heightfield, compactHeightfield)) {
    return failTileMesh('Could not build compact heightfield');
  }
  if (!keepIntermediates) {
    freeHeightfield(tileIntermediate.heightfield);
    tileIntermediate.heightfield = undefined;
  }
  // Erode the walkable area by agent radius
  if (!erodeWalkableArea(buildContext, tileConfig.walkableRadius, compactHeightfield)) {
    return failTileMesh('Could not erode walkable area');
  }
  // (Optional) Mark areas
  // markConvexPolyArea(...)
  // Prepare for region partitioning, by calculating Distance field along the walkable surface.
  if (!buildDistanceField(buildContext, compactHeightfield)) {
    return failTileMesh('Failed to build distance field');
  }
  // Partition the walkable surface into simple regions without holes.
  if (!buildRegions(buildContext, compactHeightfield, tileConfig.borderSize, tileConfig.minRegionArea, tileConfig.mergeRegionArea)) {
    return failTileMesh('Failed to build regions');
  }
  //
  // Trace and simplify region contours.
  //
  const contourSet = allocContourSet();
  tileIntermediate.contourSet = contourSet;
  if (!buildContours(buildContext, compactHeightfield, tileConfig.maxSimplificationError, tileConfig.maxEdgeLen, contourSet, Recast.RC_CONTOUR_TESS_WALL_EDGES)) {
    return failTileMesh('Failed to create contours');
  }
  //
  // Build polygons mesh from contours.
  //
  const polyMesh = allocPolyMesh();
  tileIntermediate.polyMesh = polyMesh;
  if (!buildPolyMesh(buildContext, contourSet, tileConfig.maxVertsPerPoly, polyMesh)) {
    return failTileMesh('Failed to triangulate contours');
  }
  //
  // Create detail mesh which allows to access approximate height on each polygon.
  //
  const polyMeshDetail = allocPolyMeshDetail();
  tileIntermediate.polyMeshDetail = polyMeshDetail;
  if (!buildPolyMeshDetail(buildContext, polyMesh, compactHeightfield, tileConfig.detailSampleDist, tileConfig.detailSampleMaxError, polyMeshDetail)) {
    return failTileMesh('Failed to build detail mesh');
  }
  if (!keepIntermediates) {
    freeCompactHeightfield(compactHeightfield);
    tileIntermediate.compactHeightfield = undefined;
    freeContourSet(contourSet);
    tileIntermediate.contourSet = undefined;
  }
  // Update poly flags from areas.
  for (let i = 0; i < polyMesh.npolys(); i++) {
    if (polyMesh.areas(i) === Recast.RC_WALKABLE_AREA) {
      polyMesh.setAreas(i, 0);
    }
    if (polyMesh.areas(i) === 0) {
      polyMesh.setFlags(i, 1);
    }
  }
  const navMeshCreateParams = new NavMeshCreateParams();
  navMeshCreateParams.setPolyMeshCreateParams(polyMesh);
  navMeshCreateParams.setPolyMeshDetailCreateParams(polyMeshDetail);
  navMeshCreateParams.setWalkableHeight(tileConfig.walkableHeight * tileConfig.ch);
  navMeshCreateParams.setWalkableRadius(tileConfig.walkableRadius * tileConfig.cs);
  navMeshCreateParams.setWalkableClimb(tileConfig.walkableClimb * tileConfig.ch);
  navMeshCreateParams.setCellSize(tileConfig.cs);
  navMeshCreateParams.setCellHeight(tileConfig.ch);
  navMeshCreateParams.setBuildBvTree(options.buildBvTree ?? tiledNavMeshGeneratorConfigDefaults.buildBvTree);
  if (options.offMeshConnections) {
    navMeshCreateParams.setOffMeshConnections(options.offMeshConnections);
  }
  navMeshCreateParams.setTileX(tile.x);
  navMeshCreateParams.setTileY(tile.y);
  navMeshCreateParams.setTileLayer(options.tileLayer ?? 0);
  const createNavMeshDataResult = createNavMeshData(navMeshCreateParams);
  navMeshCreateParams.destroy();
  if (!createNavMeshDataResult.success) {
    return failTileMesh('Failed to create Detour navmesh data');
  }
  buildContext.log(Recast.RC_LOG_PROGRESS, `>> Polymesh: ${polyMesh.nverts()} vertices  ${polyMesh.npolys()} polygons`);
  cleanup();
  return {
    success: true,
    data: createNavMeshDataResult.navMeshData,
    intermediates: tileIntermediate
  };
};
