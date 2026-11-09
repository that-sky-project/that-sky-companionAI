"""Build new offline Beamo/world candidates from explicit, hash-bound selections.

No resource lookup heuristics, Lua/native execution, navigation/catalog writes,
server operations, or promotion to verified/live assets are performed.
"""
from pathlib import Path, PurePosixPath
from collections import Counter
import argparse, hashlib, json, os, re, sys, zipfile
import numpy as np
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from skyterrain.assembly import Assembly, AssemblyLimits
from skyterrain.assets import write_runtime_manifest
from skyterrain.baked_terrain import PROFILE, ELF_SHA256, merge_candidate_assemblies
from skyterrain.beamo_assembly import ExplicitBeamoBinding, assemble_beamo_candidates
from skyterrain.beamo_links import read_lod0_table
from skyterrain.cli import atomic, save_json
from skyterrain.scene import Scene

MAX_JSON = 32 * 1024 * 1024
MAX_MEMBER = 128 * 1024 * 1024
MAX_BINDING_BYTES = 256 * 1024 * 1024
MAX_BINDING_ARRAY_BYTES = 128 * 1024 * 1024
MAX_BASE_ARRAY_BYTES = 80 * 1024 * 1024
LIMITS = AssemblyLimits()
MAP_NAME = re.compile(r'[A-Za-z0-9_-]{1,80}\Z')
SHA = re.compile(r'[0-9a-f]{64}\Z')


def sha(data): return hashlib.sha256(data).hexdigest()


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result: raise ValueError('duplicate_json_key:' + key)
        result[key] = value
    return result


def _json(raw):
    return json.loads(raw, object_pairs_hook=_unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite_json')))


def _read_file(path, budget):
    path = Path(path)
    if path.stat().st_size > budget: raise ValueError('file_size_budget')
    with path.open('rb') as stream: raw = stream.read(budget + 1)
    if len(raw) > budget: raise ValueError('file_size_budget')
    return raw


def _integer(value, label, ceiling):
    if type(value) is not int or not 0 <= value <= ceiling: raise ValueError('invalid_' + label)
    return value


def _digest(value):
    if type(value) is not str or SHA.fullmatch(value) is None: raise ValueError('invalid_sha256')
    return value


def _text(value):
    if type(value) is not str or not value.strip() or len(value) > 4096 or '\0' in value:
        raise ValueError('invalid_binding_text')
    return value


def _mesh_member(value):
    value = _text(value); p = PurePosixPath(value)
    if ('\\' in value or ':' in value or p.is_absolute() or '..' in p.parts
        or p.as_posix() != value or not value.startswith('assets/Data/Meshes/Bin/')
        or not value.endswith('.mesh')):
        raise ValueError('invalid_mesh_member_path')
    return value


def load_binding_config(path):
    raw = _read_file(path, MAX_JSON); doc = _json(raw)
    if type(doc) is not dict or doc.get('profile') != PROFILE or type(doc.get('scenes')) is not dict:
        raise ValueError('invalid_binding_config')
    if len(doc['scenes']) > 256: raise ValueError('scene_count_budget')
    for name, resources in doc['scenes'].items():
        if MAP_NAME.fullmatch(name) is None or type(resources) is not dict or len(resources) > 100000:
            raise ValueError('invalid_scene_bindings')
        for resource, item in resources.items():
            _text(resource)
            if type(item) is not dict: raise ValueError('invalid_binding_record')
            _mesh_member(item.get('selected_source')); _digest(item.get('source_sha256')); _text(item.get('provenance'))
    return doc, sha(raw)


def _zip_read(z, member, budget=MAX_MEMBER):
    info = z.getinfo(member)
    if info.is_dir() or info.file_size > budget: raise ValueError('archive_member_budget')
    with z.open(info) as stream: raw = stream.read(budget + 1)
    if len(raw) > budget or len(raw) != info.file_size: raise ValueError('archive_member_length')
    return raw


def verify_profile(z):
    info = z.getinfo('lib/arm64-v8a/libBootloader.so')
    if info.is_dir() or info.file_size > MAX_MEMBER: raise ValueError('native_profile_size')
    digest = hashlib.sha256(); count = 0
    with z.open(info) as stream:
        while block := stream.read(1024 * 1024):
            count += len(block)
            if count > MAX_MEMBER: raise ValueError('native_profile_size')
            digest.update(block)
    if count != info.file_size or digest.hexdigest() != ELF_SHA256: raise ValueError('native_profile_sha_mismatch')
    return {'member': info.filename, 'bytes': count, 'sha256': digest.hexdigest()}


def _safe_file(root, name):
    if type(name) is not str or name in ('', '.', '..') or Path(name).name != name or '/' in name or '\\' in name or ':' in name:
        raise ValueError('manifest_file_path_escape')
    target = root/name
    if target.is_symlink() or target.resolve().parent != root.resolve(): raise ValueError('manifest_file_path_escape')
    return target


def load_base_candidate(base_root, map_name):
    root = Path(base_root).resolve()
    directory = root/map_name/'static-world'
    if not directory.resolve().is_relative_to(root): raise ValueError('base_directory_escape')
    small_raw = _read_file(_safe_file(directory, 'runtime-manifest.json'), MAX_JSON)
    small = _json(small_raw); evidence = small['evidenceManifest']
    raw = _read_file(_safe_file(directory, evidence['file']), MAX_JSON)
    if sha(raw) != _digest(evidence['sha256']): raise ValueError('base_manifest_sha_mismatch')
    m = _json(raw)
    for item in (small, m):
        if item.get('format') != 'skyterrain-static-v1' or item.get('mapName') != map_name:
            raise ValueError('base_format_or_map_mismatch')
        if item.get('nativeCollisionVerified') is not False:
            raise ValueError('explicit_candidate_base_required')
        for flag in ('nativeCollisionVerified','calibrationVerified','runtimeReady','runtime_ready','native_collision_verified','collision_verified'):
            if flag in item and item[flag] is not False: raise ValueError('non_candidate_base_flags')
    if (m.get('coordinate_space') != 'sky_native' or m.get('runtime_ready') is not False
        or m.get('native_collision_verified') is not False):
        raise ValueError('non_candidate_base_coordinates_or_state')
    arrays = {}; read_hashes = {}; total_bytes = 0
    for key, link, count_name, dtype, ceiling in (
        ('positions','positions','vertexCount','<f4',LIMITS.max_vertices),
        ('triangles','indices','triangleCount','<u4',LIMITS.max_triangles)):
        n = _integer(m[count_name],count_name,ceiling)
        record = m['files'][key]; digest = _digest(m['sha256'][link])
        if (small[count_name] != n or type(small[count_name]) is not int or small['sha256'][link] != digest
            or small[link] != m[link] or record['file'] != m[link]
            or record['sha256'] != digest or record['dtype'] != dtype
            or record['shape'] != [n,3] or not all(type(v) is int for v in record['shape'])
            or record['bytes'] != n*12 or type(record['bytes']) is not int):
            raise ValueError('base_array_descriptor_mismatch')
        total_bytes += n*12
        if total_bytes > MAX_BASE_ARRAY_BYTES: raise ValueError('base_array_budget')
        data = _read_file(_safe_file(directory,m[link]),n*12)
        if len(data) != n*12 or sha(data) != digest: raise ValueError('base_array_sha_or_length_mismatch')
        arrays[key] = np.frombuffer(data,dtype=dtype).reshape((n,3))
        read_hashes[key] = digest
    if not np.isfinite(arrays['positions']).all(): raise ValueError('base_nonfinite_positions')
    if arrays['triangles'].size and int(arrays['triangles'].max()) >= len(arrays['positions']):
        raise ValueError('base_triangle_index')
    return Assembly(arrays['positions'],arrays['triangles'],m), {
        'runtime_manifest_sha256':sha(small_raw),'evidence_manifest_sha256':sha(raw),
        'actual_array_sha256':read_hashes,'bytes_read_for_arrays':total_bytes}


def write_candidate(assembly, directory, map_name):
    """Equivalent candidate manifest shape, using long-path-safe atomic writes."""
    directory = Path(directory); files = {}
    for key,dtype,ext in (('positions','<f4','.f32'),('triangles','<u4','.u32')):
        array = np.asarray(getattr(assembly,key),dtype=dtype); raw = array.tobytes()
        digest = sha(raw); name = key+'-'+digest+ext
        atomic(directory/name,raw)
        files[key] = {'file':name,'sha256':digest,'bytes':len(raw),'dtype':dtype,'shape':list(array.shape)}
    m = dict(assembly.manifest)
    m.update(files=files,format='skyterrain-static-v1',mapName=map_name,
        geometryKind='offline_beamo_augmented_candidates',nativeCollisionVerified=False,
        calibrationVerified=False,runtimeReady=False,runtime_ready=False,
        native_collision_verified=False,collision_verified=False,resource_binding_verified=False,
        positions=files['positions']['file'],indices=files['triangles']['file'],
        vertexCount=len(assembly.positions),triangleCount=len(assembly.triangles),
        sha256={'positions':files['positions']['sha256'],'indices':files['triangles']['sha256']})
    raw = json.dumps(m,ensure_ascii=False,indent=2,allow_nan=False).encode()
    if len(raw)>MAX_JSON: raise ValueError('output_manifest_budget')
    path = directory/'manifest.json';atomic(path,raw);write_runtime_manifest(path)
    return str(path)


def _build_one(z, name, records, base_root, out):
    scene_member = f'assets/Data/Levels/{name}/Objects.level.bin'
    scene_raw = _zip_read(z,scene_member,64*1024*1024)
    scene = Scene.from_bytes(scene_raw,source=scene_member)
    nodes = [n for n in scene.objects if n.class_name=='Beamo']
    report = {'mapName':name,'scene_sha256':sha(scene_raw),'beamo_objects':len(nodes),
              'runtimeReady':False,'nativeCollisionVerified':False,'calibrationVerified':False}
    if not nodes: return dict(report,status='no_beamo',geometry_written=False)
    lod_member = f'assets/Data/Levels/{name}/BstBaked.meshes'
    lod_errors = []
    try:
        lod_raw = _zip_read(z,lod_member); lod = read_lod0_table(lod_raw)
        report['baked_sha256'] = sha(lod_raw)
    except (KeyError,ValueError) as exc:
        lod = None; lod_errors.append({'error':type(exc).__name__,'detail':str(exc)[:500]})
    needed = {n.fields.get('meshName') for n in nodes if type(n.fields.get('meshName')) is str}
    bindings = {}; errors = []; consumed = array_bytes = 0
    for resource,item in records.items():
        if resource not in needed: continue
        try:
            info = z.getinfo(item['selected_source'])
            if consumed+info.file_size>MAX_BINDING_BYTES: raise ValueError('scene_binding_byte_budget')
            raw = _zip_read(z,item['selected_source']); consumed += len(raw)
            binding = ExplicitBeamoBinding.from_bytes(raw,selected_source=item['selected_source'],
                expected_sha256=item['source_sha256'],provenance=item['provenance'])
            size = binding.positions.nbytes+binding.triangles.nbytes
            if array_bytes+size>MAX_BINDING_ARRAY_BYTES: raise ValueError('scene_binding_array_budget')
            array_bytes += size; bindings[resource]=binding
        except (ValueError,KeyError,TypeError) as exc:
            errors.append({'resource_name':resource,'error':type(exc).__name__,'detail':str(exc)[:500]})
    base, base_evidence = load_base_candidate(base_root,name)
    beamo = assemble_beamo_candidates(scene,lod,bindings,limits=LIMITS)
    merged = merge_candidate_assemblies(base,beamo,limits=LIMITS)
    merged.manifest['explicit_beamo_build'] = {'binding_selection_verified':False,
        'base_actual_byte_binding':base_evidence,'scene_sha256':report['scene_sha256'],
        'source_selection':'explicit_input_only_no_alias_guessing'}
    paths = {'static_world':write_candidate(merged,out/'static-world',name)}
    # Installer adaptation: only the merged runtime candidate is consumed.
    # The intermediate Beamo-only candidate stays available in development runs.
    if os.environ.get('SKYTERRAIN_REQUIRED_ONLY') != '1':
        paths['beamo'] = write_candidate(beamo,out/'beamo',name)
    exact = sum(r['lod_identity']['exact_identity'] for r in beamo.manifest['included'])
    side_diagnostics = len(beamo.manifest['included'])-exact
    partial = bool(errors or lod_errors or beamo.manifest['excluded'] or side_diagnostics)
    report.update(status='partial_candidate' if partial else 'built_candidate',geometry_written=True,
        binding_errors=errors,lod_errors=lod_errors,missing_binding_names=sorted(needed-set(bindings)),
        unused_binding_names=sorted(set(records)-needed),explicit_bindings_loaded=len(bindings),
        binding_bytes_read=consumed,binding_array_bytes=array_bytes,
        beamo_included=len(beamo.manifest['included']),beamo_excluded=len(beamo.manifest['excluded']),
        lod_identity_exact=exact,lod_identity_diagnostic_instances=side_diagnostics,
        beamo_vertices=len(beamo.positions),beamo_triangles=len(beamo.triangles),
        base_vertices=len(base.positions),base_triangles=len(base.triangles),
        merged_vertices=len(merged.positions),merged_triangles=len(merged.triangles),
        base_actual_byte_binding=base_evidence,output_manifests=paths)
    return report


def build_world(apk, bindings_path, base_root, output, maps=None):
    doc, bindings_sha = load_binding_config(bindings_path)
    chosen = list(doc['scenes']) if maps is None else list(maps)
    if not chosen: raise ValueError('no_maps_requested')
    if len(chosen)!=len(set(chosen)) or any(type(n) is not str or MAP_NAME.fullmatch(n) is None for n in chosen):
        raise ValueError('invalid_requested_maps')
    if any(n not in doc['scenes'] for n in chosen): raise ValueError('map_not_in_binding_config')
    out=Path(output).resolve();base=Path(base_root).resolve()
    if out==base or out.is_relative_to(base) or base.is_relative_to(out): raise ValueError('output_base_overlap')
    if out.exists():raise ValueError('output_already_exists')
    with zipfile.ZipFile(apk) as z:
        names=z.namelist()
        if len(names)>500000 or len(names)!=len(set(names)): raise ValueError('archive_member_count_or_duplicates')
        profile=verify_profile(z)
        out.mkdir(parents=True,exist_ok=False)
        rows=[]
        for name in chosen:
            directory=out/name;directory.mkdir(exist_ok=False)
            if not directory.resolve().is_relative_to(out): raise ValueError('output_directory_escape')
            save_json(directory/'build.json',{'mapName':name,'status':'building','runtimeReady':False})
            try:
                row=_build_one(z,name,doc['scenes'][name],base,directory)
            except Exception as exc:
                row={'mapName':name,'status':'failed','error':type(exc).__name__,'detail':str(exc)[:1000],
                     'runtimeReady':False,'nativeCollisionVerified':False,'calibrationVerified':False}
            save_json(directory/'build.json',row);rows.append(row)
        result={'schema':'explicit-beamo-world-build-v1','profile':PROFILE,'native_profile_input':profile,
            'bindings_sha256':bindings_sha,'map_count':len(rows),'statuses':dict(Counter(r['status'] for r in rows)),
            'rows':rows,'runtimeReady':False,'nativeCollisionVerified':False,'calibrationVerified':False,
            'original_catalog_modified':False,'navigation_built':False,'server_modified':False,
            'module_sha256':sha((ROOT/'src/skyterrain/beamo_assembly.py').read_bytes()),
            'tool_sha256':sha(Path(__file__).read_bytes())}
        save_json(out/'summary.json',result)
    return result


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--apk',type=Path,required=True);p.add_argument('--bindings',type=Path,required=True)
    p.add_argument('--base-root',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
    p.add_argument('--maps',nargs='+')
    a=p.parse_args(argv)
    result=build_world(a.apk,a.bindings,a.base_root,a.out,a.maps)
    print(json.dumps({'map_count':result['map_count'],'statuses':result['statuses'],
        'summary':str(a.out/'summary.json'),'runtimeReady':False},ensure_ascii=False))
    return 0 if all(r['status'] in ('built_candidate','no_beamo') for r in result['rows']) else 1


if __name__=='__main__':sys.exit(main())
