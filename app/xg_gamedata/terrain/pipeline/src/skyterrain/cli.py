"""Build local, auditable map assets from an explicitly selected APK. No game I/O."""
from __future__ import annotations
import argparse, hashlib, json, os, re, shutil, tempfile, zipfile
from pathlib import Path, PurePosixPath
from collections import Counter
import numpy as np
from .formats import read_mesh, read_meshes, NodeMeshoptDecoder, DEFAULT_LIMITS, FormatError
from .scene import Scene
from .assembly import assemble_initial_static

def atomic(path: Path, data: bytes):
    # Content-addressed names plus long scene names can exceed Win32 MAX_PATH.
    # Keep temp names short; extended native paths preserve atomic replace.
    def native(p):
        value=os.path.abspath(p)
        if os.name=='nt' and not value.startswith('\\\\?\\'):
            value=('\\\\?\\UNC\\'+value[2:]) if value.startswith('\\\\') else '\\\\?\\'+value
        return value
    os.makedirs(native(path.parent), exist_ok=True)
    fd,name=tempfile.mkstemp(prefix='.atomic-',suffix='.tmp',dir=native(path.parent))
    try:
        with os.fdopen(fd,'wb') as f: f.write(data);f.flush();os.fsync(f.fileno())
        os.replace(name,native(path))
    finally:
        if os.path.exists(name):os.unlink(name)

def save_json(path,obj):atomic(path,json.dumps(obj,ensure_ascii=False,indent=2,allow_nan=False).encode())

def build_map(apk, map_name, output, *, node, meshopt, profile=None):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',map_name):raise ValueError('invalid_map_name')
    output=Path(os.path.abspath(os.fspath(output)));os.makedirs(output,exist_ok=True)
    decoder=NodeMeshoptDecoder(node,meshopt)
    sources={};cache={}
    with zipfile.ZipFile(apk) as z:
        names=z.namelist()
        if len(names)!=len(set(names)):raise FormatError('duplicate_archive_members')
        if profile is not None:
            from .baked_terrain import PROFILE,ELF_SHA256
            if profile!=PROFILE:raise ValueError('unsupported_native_profile')
            member='lib/arm64-v8a/libBootloader.so';info=z.getinfo(member)
            if info.file_size>DEFAULT_LIMITS.max_file_bytes:raise FormatError('native_profile_file_budget')
            digest=hashlib.sha256();count=0
            with z.open(info) as f:
                while block:=f.read(1024*1024):
                    count+=len(block)
                    if count>DEFAULT_LIMITS.max_file_bytes:raise FormatError('native_profile_file_budget')
                    digest.update(block)
            if count!=info.file_size or digest.hexdigest()!=ELF_SHA256:raise FormatError('native_profile_binary_mismatch')
            sources[member]={'bytes':count,'sha256':digest.hexdigest(),'crc32':f'{info.CRC:08x}','native_profile_verified':True}
        def load(member):
            info=z.getinfo(member)
            if info.is_dir() or info.file_size>DEFAULT_LIMITS.max_file_bytes:raise FormatError('archive_member_budget')
            with z.open(info) as f:data=f.read(DEFAULT_LIMITS.max_file_bytes+1)
            if len(data)!=info.file_size:raise FormatError('archive_member_length')
            sources[member]={'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest(),'crc32':f'{info.CRC:08x}'}
            return data
        suffix='/'+map_name+'/Objects.level.bin'
        matches=[n for n in names if n.endswith(suffix)]
        if len(matches)!=1:raise FormatError('scene_missing_or_ambiguous')
        parent=str(PurePosixPath(matches[0]).parent)
        scene=Scene.from_bytes(load(matches[0]),source=matches[0])
        bundle=read_meshes(load(parent+'/BstBaked.meshes'),decoder)
        # Compiled Lua is not executed. Resource-basename resolution stays explicitly provisional.
        resources={}
        for n in names:
            if n.endswith('.mesh'):resources.setdefault(PurePosixPath(n).stem,[]).append(n)
        def resolve(name):
            if name not in cache:
                hits=resources.get(name,[])
                if len(hits)!=1:raise FormatError('resource_missing_or_ambiguous')
                cache[name]=read_mesh(load(hits[0]))
            return cache[name]
        assembly=assemble_initial_static(scene,resolve)
    assembly.manifest['resource_resolution']={'method':'unique_exact_basename_in_selected_APK','compiled_lua_binding_verified':False}
    # Installer adaptation: the runtime only reads the baked static-world
    # candidate.  Render-only and initial-only copies are development evidence,
    # not runtime assets, so they are skipped when this flag is set.  Geometry
    # bytes and manifests are unaffected.
    required_only=os.environ.get('SKYTERRAIN_REQUIRED_ONLY')=='1'
    candidate=None
    from .assets import write_runtime_manifest
    if not required_only:
        candidate=assembly.write(output/'initial-collision',map_name=map_name)
        write_runtime_manifest(candidate)
    world_candidate=None
    if profile is not None:
        from .baked_terrain import make_baked_candidate,merge_candidate_assemblies
        baked=make_baked_candidate(bundle,profile=profile)
        world=merge_candidate_assemblies(baked,assembly)
        world_candidate=world.write(output/'static-world',map_name=map_name)
        write_runtime_manifest(world_candidate)
    render_manifest=None
    if not required_only:
        render=output/'render';render.mkdir(exist_ok=True)
        p=np.asarray(bundle.positions,dtype='<f4').tobytes();t=np.asarray(bundle.triangles,dtype='<u4').tobytes()
        pn='positions-'+hashlib.sha256(p).hexdigest()+'.f32';tn='triangles-'+hashlib.sha256(t).hexdigest()+'.u32'
        atomic(render/pn,p);atomic(render/tn,t)
        render_manifest={'format':'skyterrain-render-v1','mapName':map_name,'geometryKind':'render_only',
          'nativeCollisionVerified':False,'version':bundle.version,'positions':pn,'indices':tn,
          'vertexCount':len(bundle.positions),'triangleCount':len(bundle.triangles),
          'sha256':{'positions':hashlib.sha256(p).hexdigest(),'indices':hashlib.sha256(t).hexdigest()},
          'chunks':[{'kind':c.kind,'triangleStart':c.triangle_start,'triangleCount':c.triangle_count,'group':c.group} for c in bundle.chunks]}
        save_json(render/'manifest.json',render_manifest)
    summary={'mapName':map_name,'sceneObjects':len(scene.objects),'geometryNodes':len(scene.geometry_nodes()),
      'sceneClassCounts':dict(sorted(Counter(o.class_name for o in scene.objects).items())),
      'geometryClassifications':dict(sorted(Counter(o.classification for o in scene.geometry_nodes()).items())),
      'initialExclusionReasons':dict(sorted(Counter(reason for row in assembly.manifest['excluded'] for reason in row.get('reasons',[])).items())),
      'renderVertices':len(bundle.positions),'renderTriangles':len(bundle.triangles),'meshVersion':bundle.version,
      'initialCandidateInstances':len(assembly.manifest['included']),
      'excludedInstances':len(assembly.manifest['excluded']),
      'initialCandidateTriangles':len(assembly.triangles),'candidateManifest':str(candidate),
      'staticWorldManifest':str(world_candidate) if world_candidate else None,
      'liveReady':False,'runtimeReady':False,'collisionCoverage':'initial_static_candidates',
      'missing':['dynamic_scene_enable_state','client_character_calibration'],
      'sources':sources}
    save_json(output/'build.json',summary)
    return {k:v for k,v in summary.items() if k!='sources'}

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--apk',required=True,type=Path);p.add_argument('--map',required=True,dest='map_name')
    p.add_argument('--out',required=True,type=Path);p.add_argument('--node',default=shutil.which('node') or 'node')
    p.add_argument('--profile',help='Explicit native version profile; required for baked collision projection')
    p.add_argument('--meshopt',type=Path,default=Path(__file__).resolve().parents[2]/'engine/node_modules/meshoptimizer/meshopt_decoder.mjs')
    a=p.parse_args(argv);print(json.dumps(build_map(a.apk,a.map_name,a.out,node=a.node,meshopt=a.meshopt,profile=a.profile),ensure_ascii=False,indent=2))

if __name__=='__main__':main()
