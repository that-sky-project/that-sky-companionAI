"""Bounded, resumable offline navigation for explicit Beamo world candidates."""
from __future__ import annotations
import argparse, collections, concurrent.futures, hashlib, json, os, re, shutil
import subprocess, sys, time
from pathlib import Path, PurePosixPath
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
for _p in Path(__file__).resolve().parents:
    if (_p / 'xg_exec.py').is_file():
        sys.path.insert(0, str(_p))
        break
import xg_exec  # noqa: E402
from skyterrain.baked_terrain import PROFILE
from skyterrain.catalog import Catalog, scene_hash
from skyterrain.cli import save_json

TIMEOUT = 180
MAX_JSON = 16 * 1024 * 1024
MAX_NAV = 128 * 1024 * 1024
FLAGS = dict(nativeCollisionVerified=False, calibrationVerified=False,
             runtimeReady=False, live_enabled=False, server_modified=False,
             native_collision_verified=False, calibration_verified=False,
             runtime_ready=False, collision_verified=False)
BUILDER = ROOT / 'engine/build_nav_asset.mjs'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def bounded(path, limit):
    with Path(path).open('rb') as f:
        size = os.fstat(f.fileno()).st_size
        if size > limit or not Path(path).is_file():
            raise ValueError('file_budget:' + str(path))
        data = f.read(limit + 1)
    if len(data) != size or len(data) > limit:
        raise ValueError('file_changed_or_budget:' + str(path))
    return data


def unique_pairs(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError('duplicate_json_key:' + key)
        obj[key] = value
    return obj


def read_json(path, limit=MAX_JSON):
    raw = bounded(path, limit)
    obj = json.loads(raw, object_pairs_hook=unique_pairs,
                     parse_constant=lambda value: (_ for _ in ()).throw(ValueError('nonfinite_json')))
    if not isinstance(obj, dict):
        raise ValueError('json_object_required')
    return obj, digest(raw)


def safe_path(root, relative, *, basename=False):
    if (type(relative) is not str or not relative or len(relative) > 512
            or '\\' in relative or ':' in relative):
        raise ValueError('unsafe_relative_path')
    p = PurePosixPath(relative)
    if p.is_absolute() or '..' in p.parts or p.as_posix() != relative or (basename and len(p.parts) != 1):
        raise ValueError('unsafe_relative_path')
    root = Path(root).resolve()
    candidate = root.joinpath(*p.parts)
    for part in [candidate, *candidate.parents]:
        if part == root:
            break
        if part.is_symlink() or (hasattr(part, 'is_junction') and part.is_junction()):
            raise ValueError('symlink_or_junction_path')
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root) or resolved == root:
        raise ValueError('path_escape')
    return resolved


def require_false(obj, required=()):
    for key in set(FLAGS) | {'resource_binding_verified'}:
        if key in obj and obj[key] is not False:
            raise ValueError('verification_flag_not_false:' + key)
    for key in required:
        if obj.get(key) is not False:
            raise ValueError('missing_false_verification_flag:' + key)


def hash_string(value):
    if type(value) is not str or not re.fullmatch('[0-9a-f]{64}', value):
        raise ValueError('invalid_sha256')
    return value


def load_inputs(world_summary, base_catalog):
    summary, summary_sha = read_json(world_summary)
    base, base_sha = read_json(base_catalog, 8 * 1024 * 1024)
    if summary.get('schema') != 'explicit-beamo-world-build-v1' or summary.get('profile') != PROFILE:
        raise ValueError('world_summary_profile_or_schema')
    if base.get('format') != 'skyterrain-catalog-v1' or base.get('profile') != PROFILE:
        raise ValueError('base_catalog_profile_or_schema')
    require_false(summary, ('nativeCollisionVerified', 'calibrationVerified', 'runtimeReady'))
    catalog = Catalog(base.get('entries'))
    base_rows = {row['scene_name']: row for row in catalog.entries}
    for name, row in base_rows.items():
        if row['scene_hash'] != scene_hash(name):
            raise ValueError('catalog_scene_hash_mismatch')
        require_false(row)
        for key in ('asset_relpath', 'navigation_relpath', 'navigation_report_relpath'):
            if row.get(key) is not None:
                safe_path(Path(base_catalog).resolve().parent, row[key])
    rows = summary.get('rows')
    if not isinstance(rows, list) or len(rows) > 4096 or summary.get('map_count') != len(rows):
        raise ValueError('world_inventory_budget_or_count')
    seen = set()
    for row in rows:
        name = row.get('mapName') if isinstance(row, dict) else None
        if type(name) is not str or not re.fullmatch('[A-Za-z0-9_-]{1,80}', name) or name in seen or name not in base_rows:
            raise ValueError('world_scene_identity')
        seen.add(name)
        require_false(row, ('nativeCollisionVerified', 'calibrationVerified', 'runtimeReady'))
        hash_string(row.get('scene_sha256'))
        if row.get('status') not in ('built_candidate', 'partial_candidate', 'no_beamo', 'failed'):
            raise ValueError('world_scene_status')
    return rows, base_rows, {'input_summary_sha256': summary_sha, 'base_catalog_sha256': base_sha}


def validate_input(world_root, row):
    name = row['mapName']
    build, build_sha = read_json(safe_path(world_root, name + '/build.json'))
    if build != row:
        raise ValueError('world_row_build_report_mismatch')
    asset = safe_path(world_root, name + '/static-world/runtime-manifest.json')
    manifest, manifest_sha = read_json(asset, 1024 * 1024)
    if (manifest.get('format') != 'skyterrain-static-v1' or manifest.get('mapName') != name
            or manifest.get('geometryKind') != 'offline_beamo_augmented_candidates'):
        raise ValueError('asset_scene_or_kind')
    require_false(manifest, ('nativeCollisionVerified', 'calibrationVerified'))
    link = manifest.get('evidenceManifest', {})
    evidence_file = safe_path(asset.parent, link.get('file'), basename=True)
    evidence, evidence_sha = read_json(evidence_file)
    if evidence_sha != hash_string(link.get('sha256')):
        raise ValueError('evidence_sha256')
    output_evidence = row.get('output_manifests', {}).get('static_world')
    if type(output_evidence) is not str or Path(output_evidence).resolve() != evidence_file:
        raise ValueError('world_evidence_path_mismatch')
    require_false(evidence, ('nativeCollisionVerified', 'calibrationVerified', 'runtimeReady'))
    if (evidence.get('mapName') != name or evidence.get('coordinate_space') != 'sky_native'
            or evidence.get('explicit_beamo_build', {}).get('scene_sha256') != row['scene_sha256']):
        raise ValueError('evidence_scene_identity')
    hashes = {'manifest': manifest_sha}
    for key, ek, count_key, merged_key, dtype, limit in (
            ('positions', 'positions', 'vertexCount', 'merged_vertices', '<f4', 24_000_000),
            ('indices', 'triangles', 'triangleCount', 'merged_triangles', '<u4', 36_000_000)):
        count = manifest.get(count_key)
        if type(count) is not int or count < 1 or count * 12 > limit or row.get(merged_key) != count:
            raise ValueError('geometry_count_or_budget')
        if evidence.get(count_key) != count:
            raise ValueError('evidence_geometry_count')
        file = safe_path(asset.parent, manifest.get(key), basename=True)
        raw = bounded(file, limit)
        hashes[key] = digest(raw)
        entry = evidence.get('files', {}).get(ek, {})
        if (len(raw) != count * 12 or manifest.get('sha256', {}).get(key) != hashes[key]
                or evidence.get('sha256', {}).get(key) != hashes[key]
                or entry.get('sha256') != hashes[key] or entry.get('file') != manifest[key]
                or entry.get('bytes') != len(raw) or entry.get('shape') != [count, 3]
                or entry.get('dtype') != dtype):
            raise ValueError('geometry_actual_bytes_binding')
        array = np.frombuffer(raw, dtype=dtype)
        if key == 'positions' and not np.isfinite(array).all():
            raise ValueError('nonfinite_geometry')
        if key == 'indices' and int(array.max()) >= manifest['vertexCount']:
            raise ValueError('geometry_index_range')
    return {'asset_manifest_path': str(asset), 'input_manifest_sha256': manifest_sha,
            'inputHashes': hashes, 'evidence_manifest_sha256': evidence_sha,
            'scene_sha256': row['scene_sha256'], 'geometry_build_report_sha256': build_sha}


def validate_navigation(report_path, info, name):
    report, report_sha = read_json(report_path, 1024 * 1024)
    if (report.get('format') != 'skyterrain-navigation-build-v1' or report.get('status') != 'built_offline'
            or report.get('mode') != 'offline' or report.get('mapName') != name
            or report.get('geometryKind') != 'offline_beamo_augmented_candidates'):
        raise ValueError('navigation_report_identity')
    require_false(report, ('nativeCollisionVerified', 'calibrationVerified', 'runtimeReady'))
    if report.get('inputHashes') != info['inputHashes']:
        raise ValueError('navigation_input_hashes')
    for key in ('navVertices', 'navTriangles'):
        if type(report.get(key)) is not int or report[key] < 1:
            raise ValueError('navigation_counts')
    nav_hash = hash_string(report.get('navigationHash'))
    if report.get('navigationFile') != 'navigation-' + nav_hash + '.bin':
        raise ValueError('navigation_content_address')
    nav_file = safe_path(Path(report_path).parent, report['navigationFile'], basename=True)
    raw = bounded(nav_file, MAX_NAV)
    if not raw or digest(raw) != nav_hash or report.get('navigationBytes') != len(raw):
        raise ValueError('navigation_actual_bytes_binding')
    return report, report_sha, nav_file


def builder_identity(node):
    resolved = shutil.which(str(node))
    if resolved is None:
        raise ValueError('node_executable_missing')
    executable = Path(resolved).resolve()
    h = hashlib.sha256()
    with executable.open('rb') as f:
        while block := f.read(1024 * 1024):
            h.update(block)
    files = sorted((ROOT / 'engine').rglob('*.mjs'))
    if not files or len(files) > 1024:
        raise ValueError('builder_files_budget')
    identity = {str(p.relative_to(ROOT)): digest(bounded(p, 32 * 1024 * 1024)) for p in files}
    for name in ('package.json', 'package-lock.json'):
        file = ROOT / 'engine' / name
        if file.is_file():
            identity['engine/' + name] = digest(bounded(file, 8 * 1024 * 1024))
    identity['tools/build_beamo_navigation.py'] = digest(bounded(Path(__file__), 1024 * 1024))
    return str(executable), {'builder_sha256': digest(json.dumps(identity, sort_keys=True).encode()),
                             'node_sha256': h.hexdigest()}


def run_worker(node, asset, attempt):
    started = time.monotonic()
    with (attempt / 'stdout.log').open('xb') as stdout, (attempt / 'stderr.log').open('xb') as stderr:
        try:
            proc = xg_exec.run([node, str(BUILDER), '--asset', asset, '--out', str(attempt)],
                                  cwd=ROOT, stdout=stdout, stderr=stderr, timeout=TIMEOUT,
                                  env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'), check=False)
            result = {'exit_code': proc.returncode, 'timed_out': False}
        except subprocess.TimeoutExpired:
            result = {'exit_code': 124, 'timed_out': True}
    result['seconds'] = time.monotonic() - started
    return result


def build_one(world_root, out, row, base_row, previous, node):
    name = row['mapName']
    result = {'mapName': name, 'geometry_status': row['status'], 'status': 'failed',
              'reason': None, **FLAGS}
    attempt = None
    try:
        if row['status'] == 'no_beamo':
            if row.get('beamo_objects') != 0 or row.get('geometry_written') is not False:
                raise ValueError('inconsistent_no_beamo')
            build, _ = read_json(safe_path(world_root, name + '/build.json'))
            if build != row:
                raise ValueError('world_row_build_report_mismatch')
            result.update(status='reuse_base', navigation_action='not_rebuilt',
                          base_asset_relpath=base_row.get('asset_relpath'), base_nav_status=base_row['nav_status'],
                          reason='no_beamo_no_new_navigation_created')
            return result
        if row['status'] == 'failed' or row.get('geometry_written') is not True:
            raise ValueError('world_geometry_unavailable')
        info = validate_input(world_root, row)
        result.update(info)
        if previous and previous.get('status') == 'built_offline':
            require_false(previous)
            for key in ('nav_report_relpath', 'nav_report_sha256', 'navigation_relpath', 'navigation_sha256', 'attempt'):
                if key in previous:
                    result[key] = previous[key]
            for key, value in info.items():
                if previous.get(key) != value:
                    raise ValueError('resume_success_input_changed_use_new_output')
            proof = safe_path(out, previous.get('nav_report_relpath'))
            report, report_sha, nav_file = validate_navigation(proof, info, name)
            if report_sha != previous.get('nav_report_sha256'):
                raise ValueError('resume_success_report_changed')
            if previous.get('navigation_sha256') != report['navigationHash']:
                raise ValueError('resume_success_navigation_changed')
            return dict(previous, resumed_verified=True)
        attempts = safe_path(out, name + '/attempts')
        attempts.mkdir(parents=True, exist_ok=True)
        children = list(attempts.iterdir())
        if len(children) >= 9999 or any(not re.fullmatch('[0-9]{4}', p.name) or not p.is_dir() or p.is_symlink() for p in children):
            raise ValueError('invalid_attempt_directory')
        # Recover a verified completion whose batch checkpoint was interrupted.
        recovered = []
        for child in children:
            completed = safe_path(out, child.relative_to(out).as_posix() + '/result.json')
            if not completed.exists():
                continue
            prior, _ = read_json(completed, 1024 * 1024)
            if prior.get('status') != 'built_offline':
                continue
            require_false(prior)
            if prior.get('mapName') != name or any(prior.get(k) != v for k, v in info.items()):
                raise ValueError('completed_attempt_input_changed_use_new_output')
            proof = safe_path(out, prior.get('nav_report_relpath'))
            report, report_sha, _ = validate_navigation(proof, info, name)
            if report_sha != prior.get('nav_report_sha256') or report['navigationHash'] != prior.get('navigation_sha256'):
                raise ValueError('completed_attempt_artifact_changed')
            recovered.append(prior)
        if len(recovered) > 1:
            raise ValueError('ambiguous_completed_attempts')
        if recovered:
            return dict(recovered[0], resumed_verified=True, recovered_checkpoint=True)
        ordinal = max((int(p.name) for p in children), default=0) + 1
        attempt = safe_path(out, name + f'/attempts/{ordinal:04d}')
        attempt.mkdir()
        result.update(attempt=ordinal, stdout_relpath=(attempt / 'stdout.log').relative_to(out).as_posix(),
                      stderr_relpath=(attempt / 'stderr.log').relative_to(out).as_posix())
        run = run_worker(node, info['asset_manifest_path'], attempt)
        result['run'] = run
        proof = safe_path(out, (attempt / 'nav-build.json').relative_to(out).as_posix())
        if proof.exists():
            raw = bounded(proof, 1024 * 1024)
            result.update(nav_report_relpath=proof.relative_to(out).as_posix(), nav_report_sha256=digest(raw))
        if run['timed_out']:
            result.update(status='timeout', reason='navigation_timeout_180s')
        elif run['exit_code'] != 0:
            result.update(status='unavailable', reason='navigation_worker_exit_' + str(run['exit_code']))
            if proof.exists():
                failure, _ = read_json(proof, 1024 * 1024)
                require_false(failure)
                result['worker_reason'] = failure.get('reason')
        else:
            # Re-read actual input bytes after the child exits, closing replacement races.
            if validate_input(world_root, row) != info:
                raise ValueError('input_changed_during_navigation')
            report, proof_sha, nav_file = validate_navigation(proof, info, name)
            result.update(status='built_offline', nav_report_sha256=proof_sha,
                          navigation_relpath=nav_file.relative_to(out).as_posix(),
                          navigation_sha256=report['navigationHash'], nav_vertices=report['navVertices'],
                          nav_triangles=report['navTriangles'], navigation_coverage=report.get('navigationCoverage'))
    except (OSError, ValueError, TypeError, KeyError) as error:
        result.update(status='failed', reason=str(error)[:512])
    if attempt is not None:
        save_json(safe_path(attempt, 'result.json'), result)
    return result


def build_batch(world_summary, base_catalog, output, *, node, maps=None, workers=2, resume=False):
    if type(workers) is not int or workers not in (1, 2):
        raise ValueError('workers_must_be_1_or_2')
    world_summary, base_catalog = Path(world_summary).resolve(), Path(base_catalog).resolve()
    world_root, base_root, out = world_summary.parent, base_catalog.parent, Path(output).resolve()
    if any(out.is_relative_to(p) or p.is_relative_to(out) for p in (world_root, base_root)):
        raise ValueError('output_overlaps_inputs')
    if out.exists() and not resume:
        raise ValueError('output_already_exists_requires_resume')
    if resume and not out.is_dir():
        raise ValueError('resume_output_missing')
    rows, base_rows, identities = load_inputs(world_summary, base_catalog)
    selected = [r['mapName'] for r in rows] if maps is None else list(maps)
    if not selected or len(set(selected)) != len(selected) or set(selected) - {r['mapName'] for r in rows}:
        raise ValueError('invalid_map_selection')
    node, implementation = builder_identity(node)
    identity = dict(identities, **implementation, profile=PROFILE,
                    world_summary_path=str(world_summary), base_catalog_path=str(base_catalog))
    old = None
    if resume:
        old, _ = read_json(safe_path(out, 'summary.json'))
        if old.get('schema') != 'beamo-navigation-batch-v1' or any(old.get(k) != v for k, v in identity.items()):
            raise ValueError('resume_source_or_builder_changed_use_new_output')
        if not isinstance(old.get('rows'), list) or {r.get('mapName') for r in old['rows']} != {r['mapName'] for r in rows}:
            raise ValueError('resume_inventory_changed')
        if len(old['rows']) != len(rows):
            raise ValueError('resume_duplicate_scene')
        require_false(old)
    else:
        out.mkdir(parents=True, exist_ok=False)
    lock_path = safe_path(out, '.build.lock')
    try:
        lock = lock_path.open('x', encoding='ascii')
    except FileExistsError as error:
        raise ValueError('batch_lock_present_verify_previous_process_before_recovery') from error
    try:
        lock.write(str(os.getpid())); lock.flush()
        saved = {r['mapName']: r for r in old['rows']} if old else {
            r['mapName']: dict(mapName=r['mapName'], status='not_selected', reason=None, **FLAGS) for r in rows}
        result = dict(schema='beamo-navigation-batch-v1', **identity, **FLAGS,
                      timeout_seconds=TIMEOUT, workers=workers, map_count=len(rows),
                      selected_maps=selected, original_catalog_modified=False)
        def checkpoint():
            result['rows'] = [saved[r['mapName']] for r in rows]
            result['statuses'] = dict(collections.Counter(r['status'] for r in result['rows']))
            save_json(safe_path(out, 'summary.json'), result)
        checkpoint()
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(build_one, world_root, out, r, base_rows[r['mapName']], saved[r['mapName']], node): r['mapName']
                       for r in rows if r['mapName'] in selected}
            for future in concurrent.futures.as_completed(futures):
                name = futures[future]
                try:
                    saved[name] = future.result()
                except Exception as error:
                    saved[name] = dict(mapName=name, status='failed', reason='worker_exception:' + str(error)[:400], **FLAGS)
                checkpoint()
                print(json.dumps({'mapName': name, 'status': saved[name]['status'], 'reason': saved[name].get('reason')}, ensure_ascii=False), flush=True)
        return result
    finally:
        lock.close()
        lock_path.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--world-summary', required=True, type=Path)
    parser.add_argument('--base-catalog', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--node', required=True)
    parser.add_argument('--maps', nargs='+')
    parser.add_argument('--workers', type=int, default=2)
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args(argv)
    result = build_batch(args.world_summary, args.base_catalog, args.out, node=args.node,
                         maps=args.maps, workers=args.workers, resume=args.resume)
    return 0 if all(r['status'] in ('built_offline', 'reuse_base', 'not_selected') for r in result['rows']) else 1


if __name__ == '__main__':
    sys.exit(main())
