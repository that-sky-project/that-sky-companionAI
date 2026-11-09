"""Small runtime manifest referencing the full, hash-preserved research manifest."""
from pathlib import Path
import hashlib,json

def write_runtime_manifest(manifest_path):
    from .cli import save_json
    path=Path(manifest_path)
    if path.stat().st_size>32*1024*1024:raise ValueError('evidence_manifest_budget')
    raw=path.read_bytes();m=json.loads(raw)
    keys=('format','mapName','geometryKind','nativeCollisionVerified','positions','indices',
          'vertexCount','triangleCount','sha256')
    small={key:m[key] for key in keys}
    small['calibrationVerified']=m.get('calibrationVerified',False)
    small['evidenceManifest']={'file':path.name,'sha256':hashlib.sha256(raw).hexdigest()}
    small['nativeStaticRegistrationObserved']=m.get('native_static_registration_observed',False)
    target=path.with_name('runtime-manifest.json');save_json(target,small);return target
