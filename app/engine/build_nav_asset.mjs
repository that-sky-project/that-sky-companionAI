// Isolated, bounded per-scene offline navigation build. Never promotes live flags.
import fs from 'node:fs';
import path from 'node:path';
import {createHash, randomUUID} from 'node:crypto';
import {fileURLToPath} from 'node:url';
import {performance} from 'node:perf_hooks';
import {loadAsset} from './assets.mjs';
import {init, TerrainWorld} from './core.mjs';

const hash = data => createHash('sha256').update(data).digest('hex');
function atomic(file, data) {
  const tmp = file + '.' + randomUUID() + '.tmp';
  try { fs.writeFileSync(tmp, data); fs.renameSync(tmp, file); }
  finally { if (fs.existsSync(tmp)) fs.unlinkSync(tmp); }
}
export async function buildNavigation(assetFile, outDir, options = {}) {
  const file = path.resolve(assetFile), out = path.resolve(outDir);
  fs.mkdirSync(out, {recursive: true});
  const report = {format: 'skyterrain-navigation-build-v1', mode: 'offline',
    status: 'unavailable', reason: null, nativeCollisionVerified: false,
    calibrationVerified: false, runtimeReady: false,
    meaning: 'Offline static navigation only; not native collision parity or live client validation.'};
  let world;
  const started = performance.now();
  try {
    const asset = loadAsset(path.dirname(file), path.basename(file), {offline: true});
    report.mapName = asset.manifest.mapName;
    report.geometryKind = asset.manifest.geometryKind;
    report.inputHashes = {manifest: hash(fs.readFileSync(file)), ...asset.manifest.sha256};
    report.inputVertices = asset.positions.length / 3;
    report.inputTriangles = asset.indices.length / 3;
    await init();
    world = new TerrainWorld(asset.positions, asset.indices, options);
    report.config = world.config;
    report.bounds = world.bounds;
    report.geometryPrecisionWarnings = world.geometryPrecisionWarnings;
    report.navigationCoverage = world.navigationCoverage;
    report.navigationWindow = world.navigationWindow;
    report.navigationMode = world.navMode ?? null;
    report.tileStats = world.navBuildStats ?? null;
    if (!world.nav) {
      report.reason = world.navError || 'navigation_absent';
    } else {
      const [positions, indices] = world.navGeometry();
      report.navVertices = positions.length / 3;
      report.navTriangles = indices.length / 3;
      if (!positions.length || !indices.length) report.reason = 'navigation_empty';
      else {
        const data = world.exportNav();
        const digest = hash(data);
        // Content-addressing ensures a failed rebuild cannot expose stale bytes.
        report.navigationFile = 'navigation-' + digest + '.bin';
        report.navigationHash = digest;
        report.navigationBytes = data.byteLength;
        atomic(path.join(out, report.navigationFile), data);
        report.status = 'built_offline';
      }
    }
  } catch (error) {
    report.reason = String(error?.message || error).slice(0, 512);
  } finally {
    world?.dispose();
  }
  report.buildMs = performance.now() - started;
  atomic(path.join(out, 'nav-build.json'), JSON.stringify(report, null, 2));
  return report;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const args = process.argv.slice(2), get = key => {
    const i = args.indexOf(key);
    return i < 0 ? undefined : args[i + 1];
  };
  if (!get('--asset') || !get('--out')) throw Error('--asset and --out required');
  const windowArgs=['--min-x','--max-x','--min-z','--max-z'];
  const anyWindow=windowArgs.some(k=>get(k)!==undefined);
  const options=anyWindow?{navigationWindow:Object.fromEntries(['minX','maxX','minZ','maxZ'].map((k,i)=>[k,Number(get(windowArgs[i]))]))}:{};
  const result = await buildNavigation(get('--asset'), get('--out'),options);
  console.log(JSON.stringify(result));
  if (result.status !== 'built_offline') process.exitCode = 2;
}
