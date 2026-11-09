"""Create hash-bound offline selections under an explicit prior-MeshDefs scenario.

Reads packaged declarations as data, never executes Lua. This conditional plan
does not observe a live resource registry and cannot enable production physics.
"""
from pathlib import Path
from collections import Counter
import argparse, copy, hashlib, json, re, sys, zipfile
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from skyterrain.cli import save_json
from skyterrain.resource_tables import read_resource_tables, PROFILE
from skyterrain.mesh_resource_bindings import resolve_conditional_mesh_bindings
from skyterrain.scene import Scene
from build_beamo_world import _zip_read, verify_profile


def digest(raw): return hashlib.sha256(raw).hexdigest()


def checked_references(report):
    refs=report.get('resource_references',[])
    if not isinstance(refs,list) or any(not isinstance(r,dict) or type(r.get('family')) is not str for r in refs):
        raise ValueError('invalid_resource_references')
    if any(r['family']=='Mesh' for r in refs):
        raise ValueError('unsupported_Mesh_template_reference')
    return dict(Counter(r['family'] for r in refs))


def expand_mesh_references(report, global_templates):
    """Only unchanged, literal global definitions; no local redefinition replay."""
    refs=report.get('resource_references',[])
    if not isinstance(refs,list) or any(not isinstance(r,dict) or type(r.get('family')) is not str for r in refs):
        raise ValueError('invalid_resource_references')
    current=[d for d in report['declarations'] if d['family']=='Mesh']
    if any(d['directive']!='resource' for d in current):
        raise ValueError('unsupported_scene_Mesh_directive')
    requests=copy.deepcopy(current)
    for ref in refs:
        if ref['family']!='Mesh':continue
        name=ref.get('name')
        if type(name) is not str or name not in global_templates:
            raise ValueError('unsupported_Mesh_template_reference')
        template=copy.deepcopy(global_templates[name])
        if template.get('has_symbolic_properties',False):
            raise ValueError('symbolic_Mesh_template_reference')
        template.update(directive='resource',pc=ref.get('pc'),
            expanded_global_template_name=name,global_template_pc=template.get('pc'))
        requests.append(template)
    pcs=[d.get('pc') for d in requests]
    if any(type(p) is not int or p<0 for p in pcs) or len(pcs)!=len(set(pcs)):
        raise ValueError('ambiguous_resource_request_order')
    requests.sort(key=lambda d:d['pc'])
    return dict(Counter(r['family'] for r in refs)),requests


def prepare(apk, output, maps=None):
    out=Path(output).resolve()
    if out.exists(): raise ValueError('output_already_exists')
    with zipfile.ZipFile(apk) as z:
        names=z.namelist()
        if len(names)>500000 or len(names)!=len(set(names)):
            raise ValueError('archive_member_count_or_duplicates')
        members=set(names); native=verify_profile(z)
        available={m.group(1):name for name in names if (m:=re.fullmatch(
            r'assets/Data/Levels/([A-Za-z0-9_-]{1,80})/Resources\.lua\.luac',name))}
        chosen=sorted(available) if maps is None else list(maps)
        if not chosen or len(chosen)!=len(set(chosen)) or any(n not in available for n in chosen):
            raise ValueError('invalid_requested_maps')
        global_member='assets/Data/Resources/MeshDefs.lua.luac'
        global_raw=_zip_read(z,global_member,8*1024*1024)
        global_report=read_resource_tables(global_raw,profile=PROFILE)
        global_refs=checked_references(global_report)
        global_mesh=[d for d in global_report['declarations'] if d['family']=='Mesh']
        prior=[d for d in global_mesh if d['directive']=='resource']
        omitted_templates=[{'name':d['name'],'directive':d['directive']} for d in global_mesh
            if d['directive']!='resource']
        if any(d['directive']!='resourcedef' for d in omitted_templates):
            raise ValueError('unsupported_global_Mesh_directive')
        templates={d['name']:d for d in global_mesh if d['directive']=='resourcedef'}
        if len(templates)!=len(omitted_templates):raise ValueError('duplicate_global_template')
        provenance={'prior_source':global_member,'prior_source_sha256':digest(global_raw),
            'scenario':'MeshDefs resources registered in declaration order and unchanged; global templates unchanged; ignoreResourceRefs=false; resourceFilter accepts Mesh; no other prior Mesh objects or cross-class alias-key interference',
            'native_evidence':'source-basename alias; exact-first lookup; fixed prior registry during queue batch',
            'live_registry_observed':False}
        registry=resolve_conditional_mesh_bindings(prior,[],profile=PROFILE,
            prior_order_assumed=True,provenance=provenance)
        if registry['status']!='conditional_plan':
            raise ValueError('unresolved_global_registry:'+','.join(registry['reasons']))
        out.mkdir(parents=True,exist_ok=False)
        save_json(out/'prior-registry.json',registry)
        config={'profile':PROFILE,'scenes':{},'runtime_binding_verified':False,
            'nativeCollisionVerified':False,'runtimeReady':False,'scenario':provenance}
        rows=[]; payloads={}; decisions_total=Counter()
        for name in chosen:
            raw=_zip_read(z,available[name],8*1024*1024)
            declarations=read_resource_tables(raw,profile=PROFILE)
            reference_families,requests=expand_mesh_references(declarations,templates)
            scene_member=f'assets/Data/Levels/{name}/Objects.level.bin'
            scene_raw=_zip_read(z,scene_member,64*1024*1024)
            scene=Scene.from_bytes(scene_raw,source=scene_member)
            counts=Counter(n.fields.get('meshName') for n in scene.objects
                if n.class_name=='Beamo' and type(n.fields.get('meshName')) is str)
            plan=resolve_conditional_mesh_bindings(prior,requests,profile=PROFILE,
                prior_order_assumed=True,provenance=provenance)
            if plan['status']!='conditional_plan': raise ValueError('unresolved_scene_prior:'+name)
            grouped={}
            for decision in plan['decisions']:
                grouped.setdefault(decision['request_name'],[]).append(decision)
            bindings={}; chosen_rows=[]
            for resource,instances in sorted(counts.items()):
                candidates=grouped.get(resource,[])
                row={'resource_name':resource,'instances':instances,'status':'unresolved',
                     'runtime_binding_verified':False,'collision_verified':False}
                if len(candidates)!=1:
                    row['reason']='missing_or_duplicate_current_declaration'
                else:
                    decision=candidates[0]; row['decision']=decision
                    path=decision['path_candidate']
                    if path is None: row['reason']='conditional_path_unresolved'
                    elif path not in members: row.update(reason='conditional_path_absent',candidate=path)
                    else:
                        if path not in payloads:
                            payload=_zip_read(z,path)
                            payloads[path]={'source_sha256':digest(payload),'bytes':len(payload)}
                        proof=(f"conditional prior MeshDefs unchanged; {decision['decision']}; "
                            f"lookup={decision['lookup_mode']}; prior={decision['matched_prior_name']}; "
                            f"priorScriptSha={digest(global_raw)}; levelScriptSha={digest(raw)}; "
                            'native winner/lifetime/gate NOT observed')
                        bindings[resource]={'selected_source':path,
                            'source_sha256':payloads[path]['source_sha256'],'provenance':proof}
                        row.update(status='conditional_selected',selected_source=path,**payloads[path])
                chosen_rows.append(row);decisions_total[row['status']]+=1
            config['scenes'][name]=bindings
            statuses=dict(Counter(r['status'] for r in chosen_rows))
            row={'mapName':name,'beamo_instances':sum(counts.values()),'resource_names':len(counts),
                 'selected_names':len(bindings),'selected_instances':sum(counts[k] for k in bindings),
                 'statuses':statuses,'scene_sha256':digest(scene_raw),
                 'declarations_source_sha256':digest(raw),'runtime_binding_verified':False}
            row['reference_families']=reference_families
            row['expanded_Mesh_template_references']=reference_families.get('Mesh',0)
            rows.append(row)
            save_json(out/'scenes'/f'{name}.json',dict(row,resources=chosen_rows,
                request_decisions=dict(Counter(d['decision'] for d in plan['decisions'])),
                pending_duplicate_names=plan['pending_names_with_multiple_requests']))
        save_json(out/'bindings.json',config)
        result={'schema':'conditional-beamo-binding-preparation-v1','profile':PROFILE,
            'native_profile':native,'map_count':len(rows),'rows':rows,
            'unregistered_global_templates':omitted_templates,
            'global_non_Mesh_reference_families':global_refs,
            'resource_statuses':dict(decisions_total),'unique_selected_payloads':len(payloads),
            'beamo_instances':sum(r['beamo_instances'] for r in rows),
            'selected_instances':sum(r['selected_instances'] for r in rows),
            'assumptions':registry['assumptions']+[provenance['scenario']],
            'runtime_binding_verified':False,'nativeCollisionVerified':False,
            'runtimeReady':False,'live_enabled':False,'server_modified':False,
            'module_sha256':digest((ROOT/'src/skyterrain/mesh_resource_bindings.py').read_bytes()),
            'tool_sha256':digest(Path(__file__).read_bytes())}
        save_json(out/'summary.json',result)
        return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--apk',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
    p.add_argument('--maps',nargs='+');a=p.parse_args()
    result=prepare(a.apk,a.out,a.maps)
    print(json.dumps({k:v for k,v in result.items() if k not in ('rows','assumptions')},ensure_ascii=False))
    return 0 if result['selected_instances']==result['beamo_instances'] else 1


if __name__=='__main__':sys.exit(main())
