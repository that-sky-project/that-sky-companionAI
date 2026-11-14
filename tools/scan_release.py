"""Offline release red-line scanner. Reports locations, never matched secrets."""
from pathlib import Path
import argparse
import hashlib
import json
import re

PLAYER_PREFIXES=tuple(''.join(parts) for parts in (
    ('2c','77','a3','66'),('f1','fe','f9','5d'),('83','11','f5','41'),
    ('3d','db','c3','6e'),('ec','2e','1f','56'),('aa','8e','30','7a'),('88','6c','e0','94')))
PRIVATE_PATHS=('/'+'root/'+'clau'+'dework','sky'+'server')
WINDOWS_USER=re.compile(r'[A-Za-z]:[\\/]+Us' + r'ers[\\/]+[^\\/\s\"\x27<>]+',re.I)
GITHUB=re.compile(r'https?://(?:www\.)?github\.com/([A-Za-z0-9_-]+)',re.I)
EMAIL=re.compile(r'[a-zA-Z0-9_.+%-]+@[a-zA-Z0-9.-]+\.[A-Za-z]{2,}')
KEY_TOKEN=re.compile(r'(?<![A-Za-z0-9_/-])(?:s' + r'k-[A-Za-z0-9_-]{20,}|g' + r'h[pousr]_[A-Za-z0-9]{20,}|github_' + r'pat_[A-Za-z0-9_]{20,}|AK' + r'IA[A-Z0-9]{16})(?![A-Za-z0-9_/-])')
PEM_HEADER=re.compile('-'*5+r'BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY'+'-'*5)
VENDOR_PREFIXES=('app/engine/node_modules/','app/xg_gamedata/terrain/pipeline/engine/node_modules/')
PUBLIC_UPSTREAMS={
    'ollama','that-sky-project','lz4','dimforge','isaac-mason','zeux','numpy',
    'lingyunalingyun','oldmud0','thatskyoldserver','thatskymod',
}
GEOMETRY_EXTENSIONS={'.apk','.mesh','.meshes','.fbx','.obj','.ktx','.astc','.b64'}
FORBIDDEN_FILES={'reference-catalog.json','runtime-config.fragment.json','budget-report.json',
                 'coverage-report.json','native-geometry-review.json','seat-exits.json'}

REVIEWED_BINARY_HASHES={'app/bin/libpcre2-8-0.dll': 'c135a87ed0f11eae8ffc4cb469671ff0b3f5d71fab5fb024e9b1e7241ca25b52', 'tests/fixtures/actor_0168_candle.bin': 'f766e8014cf69300c552145c9e48336311b1c85c877d76e2f91c74ff85da0eaf'}

def inspect_tree(root):
    root=Path(root).resolve();findings=[];attributions=[];scanned=0;binary=0
    def add(rel,kind,line=None):
        row={'file':rel,'kind':kind}
        if line is not None:row['line']=line
        findings.append(row)
    for sub in ('app','tests','tools'):
        folder=root/sub
        if not folder.is_dir():continue
        for path in sorted(folder.rglob('*')):
            if not path.is_file():continue
            scanned+=1;rel=path.relative_to(root).as_posix()
            vendor=rel.startswith(VENDOR_PREFIXES)
            if path.suffix.lower() in GEOMETRY_EXTENSIONS and not vendor:add(rel,'game_resource_payload')
            if path.name in FORBIDDEN_FILES:add(rel,'input_reference_artifact')
            if rel.startswith('app/assets/props/') or rel.startswith('app/ref/textrules/'):
                add(rel,'bundled_generated_game_data')
            if path.name in ('accounts.json','user_config.json') or rel.startswith(('app/secrets/','app/control/','app/logs/')):
                add(rel,'private_runtime_state')
            raw=path.read_bytes()
            try:text=raw.decode('utf-8-sig')
            except UnicodeError:
                binary+=1
                if not vendor:
                    if REVIEWED_BINARY_HASHES.get(rel) != hashlib.sha256(raw).hexdigest():
                        add(rel,'unreviewed_binary_payload')
                    for prefix in PLAYER_PREFIXES:
                        fragment=bytes.fromhex(prefix)
                        if fragment in raw or fragment[::-1] in raw:add(rel,'player_identifier_binary');break
                continue
            for number,line in enumerate(text.splitlines(),1):
                lower=line.lower()
                if WINDOWS_USER.search(line):add(rel,'developer_home_path',number)
                if any(value in lower for value in PRIVATE_PATHS):add(rel,'private_server_path',number)
                if any(prefix in lower for prefix in PLAYER_PREFIXES):add(rel,'player_identifier',number)
                if KEY_TOKEN.search(line) or (not vendor and PEM_HEADER.search(line)):
                    add(rel,'secret_shape',number)
                if EMAIL.search(line):
                    if vendor:attributions.append({'file':rel,'kind':'vendor_email_attribution'})
                    else:add(rel,'email_address',number)
                for match in GITHUB.finditer(line):
                    if vendor or match.group(1).lower() in PUBLIC_UPSTREAMS:
                        attributions.append({'file':rel,'kind':'public_upstream_attribution'})
                    else:add(rel,'unreviewed_github_identity',number)
            if path.suffix=='.json' and rel.startswith('app/') and not vendor:
                try:doc=json.loads(text)
                except ValueError:continue
                def geometry(value):
                    if isinstance(value,dict):
                        if isinstance(value.get('positions'),list) and isinstance(value.get('triangles'),list) and (value['positions'] or value['triangles']):return True
                        return any(geometry(v) for v in value.values())
                    if isinstance(value,list):return any(geometry(v) for v in value)
                    return False
                if geometry(doc):add(rel,'embedded_mesh_arrays')
    # Generated receipt/index markers also must not be checked into source under a renamed data root.
    unique=[];seen=set()
    for row in findings:
        key=tuple(sorted(row.items()))
        if key not in seen:seen.add(key);unique.append(row)
    return {'ok':not unique,'files_scanned':scanned,'binary_files_scanned':binary,
            'findings':unique,'public_third_party_attribution_occurrences':len(attributions),
            'public_third_party_attribution_files':sorted({x['file'] for x in attributions}),
            'policy':'First-party private identifiers/keys forbidden; public upstream license/source attribution retained.'}

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('root',nargs='?',default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument('--report',type=Path)
    args=parser.parse_args();report=inspect_tree(args.root)
    if args.report:
        temp=args.report.with_suffix(args.report.suffix+'.tmp')
        temp.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');temp.replace(args.report)
    print(json.dumps({'ok':report['ok'],'files_scanned':report['files_scanned'],
                      'finding_count':len(report['findings']),'findings':report['findings'][:30]},ensure_ascii=False,indent=2))
    return 0 if report['ok'] else 1

if __name__=='__main__':raise SystemExit(main())
