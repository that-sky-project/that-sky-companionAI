"""Pure mathematical test geometry; no APK mesh, player capture or account data."""
from pathlib import Path
import hashlib
import json

READY_COUNT = 40
TOTAL_COUNT = READY_COUNT + 1


def profile(*, thin=False):
    points = [-.6,.7,-.6,.6,.7,-.6,.6,.7,.6,-.6,.7,.6,
              -.6,.8,-.6,.6,.8,-.6,.6,.8,.6,-.6,.8,.6]
    triangles = [0,1,2,0,2,3,4,6,5,4,7,6,0,5,1,0,4,5,
                 1,6,2,1,5,6,2,7,3,2,6,7,3,4,0,3,7,4]
    if thin:
        points, triangles = [1.,0.,0.,1.+1e-8,0.,0.,1.,0.,1.], [0,1,2]
    return dict(source_kind='render_mesh_candidate', native_collision_verified=False,
                coordinate_space='sky_native', resource_path='assets/Data/Synthetic/box.mesh',
                positions=points, triangles=triangles)


def inline_catalog(name='TableXmas'):
    return dict(schema='skyterrain-props-v1',coordinate_space='sky_native',props={name:profile()})


def write_catalog(root, *, build=421629, count=READY_COUNT):
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    (root/'runtime').mkdir(exist_ok=True)
    names=['TableXmas','Float32Thin'] + ['SyntheticProp%03d'%i for i in range(max(0,count-2))]
    entries={}
    for name in names:
        shape=profile(thin=name=='Float32Thin')
        doc=dict(schema='skyterrain-props-v1',coordinate_space='sky_native',props={name:shape})
        raw=json.dumps(doc,separators=(',',':')).encode()
        rel='runtime/'+name+'.json';(root/rel).write_bytes(raw)
        entries[name]=dict(status='ready',runtime_eligible=True,source_kind=shape['source_kind'],
            shard=rel,byte_length=len(raw),sha256=hashlib.sha256(raw).hexdigest(),
            vertex_count=len(shape['positions'])//3,triangle_count=len(shape['triangles'])//3,reasons=[])
    entries['SyntheticMissing']=dict(status='no_geometry',runtime_eligible=False,reasons=['synthetic_no_geometry'])
    catalog=dict(schema='skyterrain-props-index-v1',coordinate_space='sky_native',props=entries)
    raw=json.dumps(catalog,separators=(',',':')).encode();(root/'catalog.json').write_bytes(raw)
    marker=dict(schema='xg-props-generation-v1',status='complete',build=build,
        catalog_sha256=hashlib.sha256(raw).hexdigest(),counts=dict(ready=len(names),total=len(entries)))
    (root/'generation.json').write_text(json.dumps(marker),encoding='utf-8')
    return root
