"""CLI: python -m skyterrain.formats inspect FILE --node NODE --decoder-module MODULE"""
from pathlib import Path
import argparse
import json
from . import read_mesh,read_meshes,NodeMeshoptDecoder,extract_members

def main(argv=None):
    parser=argparse.ArgumentParser(description='Inspect Sky native geometry (render-only semantics).')
    commands=parser.add_subparsers(dest='command',required=True)
    inspect=commands.add_parser('inspect');inspect.add_argument('file');inspect.add_argument('--node',default='node');inspect.add_argument('--decoder-module')
    extract=commands.add_parser('extract');extract.add_argument('apk');extract.add_argument('output');extract.add_argument('members',nargs='+')
    args=parser.parse_args(argv)
    if args.command=='extract':
        result=extract_members(args.apk,args.members,args.output)
    else:
        decoder=NodeMeshoptDecoder(args.node,args.decoder_module) if args.decoder_module else None
        value=read_meshes(args.file,decoder) if Path(args.file).suffix=='.meshes' else read_mesh(args.file)
        result={'source':value.source,'version':value.version,'positions':len(value.positions),'triangles':len(value.triangles),
                'geometry_kind':value.geometry_kind,'coordinate_space':value.coordinate_space,
                'chunks':len(value.chunks),'chunk_kinds':sorted(set(c.kind for c in value.chunks)),
                'bounds_min':value.metadata.get('bounds_min'),'bounds_max':value.metadata.get('bounds_max')}
    print(json.dumps(result,ensure_ascii=False))
    return 0

if __name__=='__main__':raise SystemExit(main())
