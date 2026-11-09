"""Pure conditional Mesh registry model; never a runtime cache-winner claim.

Inputs are explicitly ordered declaration snapshots ASSUMED already registered.
Exact names precede source-basename aliases. Current requests all observe the
same prior registry; pending constructors are never inserted. Identity occupancy
is retained even when unsupported properties prevent path/compatibility proof.
"""
from collections.abc import Mapping
from dataclasses import asdict,dataclass
import hashlib,json,math
from .mesh_resource_paths import PROFILE,project_mesh_path,compare_conditional_existing


@dataclass(frozen=True)
class BindingLimits:
    max_snapshots:int=20_000
    max_values:int=2_000_000
    max_text_bytes:int=32*1024*1024
    max_string:int=4096
    max_depth:int=12
    def __post_init__(self):
        maxima={'max_snapshots':100_000,'max_values':8_000_000,
                'max_text_bytes':128*1024*1024,'max_string':65536,'max_depth':32}
        for key,ceiling in maxima.items():
            value=getattr(self,key)
            if type(value) is not int or not 1<=value<=ceiling:
                raise ValueError('invalid_binding_limit:'+key)


class _Invalid(ValueError):pass


class _Copier:
    def __init__(self,limits):self.limits=limits;self.values=0;self.text_bytes=0
    def copy(self,value,depth=0,active=None):
        self.values+=1
        if self.values>self.limits.max_values or depth>self.limits.max_depth:
            raise _Invalid('snapshot_budget')
        if value is None or type(value) in (bool,int):return value
        if type(value) is float:
            if not math.isfinite(value):raise _Invalid('nonfinite_snapshot_value')
            return value
        if type(value) is str:
            if len(value)>self.limits.max_string:raise _Invalid('snapshot_string_budget')
            self.text_bytes+=len(value.encode('utf8'))
            if self.text_bytes>self.limits.max_text_bytes:raise _Invalid('snapshot_text_budget')
            return value
        if not isinstance(value,(Mapping,list,tuple)):raise _Invalid('nonliteral_snapshot_value')
        active=set() if active is None else active
        if id(value) in active:raise _Invalid('cyclic_snapshot')
        active.add(id(value))
        if isinstance(value,Mapping):
            out={}
            for key,item in value.items():
                if type(key) is not str:raise _Invalid('nonliteral_snapshot_key')
                out[self.copy(key,depth+1,active)]=self.copy(item,depth+1,active)
        else:out=[self.copy(item,depth+1,active) for item in value]
        active.remove(id(value));return out


def _digest(properties):
    return hashlib.sha256(json.dumps(properties,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode()).hexdigest()


def _snapshot(value,copier,profile,require_source):
    if not isinstance(value,Mapping):raise _Invalid('snapshot_mapping_required')
    row=copier.copy(value)
    if row.get('family')!='Mesh' or type(row.get('family')) is not str:
        raise _Invalid('literal_Mesh_family_required')
    name=row.get('name')
    if type(name) is not str or not name or '\0' in name:
        raise _Invalid('literal_name_required')
    if row.get('directive','resource')!='resource':
        raise _Invalid('unsupported_declaration_directive')
    props=row.get('properties')
    if not isinstance(props,dict):raise _Invalid('properties_mapping_required')
    if ('name' in props and (type(props['name']) is not str or props['name']!=name)
            or 'class' in props and (type(props['class']) is not str or props['class']!='Mesh')):
        raise _Invalid('constructor_identity_mismatch')
    # Explicit wrapper identities mirror the known GatherCons assignment. No
    # unknown path properties are stripped or defaulted here.
    props.setdefault('name',name);props.setdefault('class','Mesh')
    if require_source or 'source' in props:
        if type(props.get('source')) is not str:
            raise _Invalid('literal_source_identity_required')
        identity_check=project_mesh_path({'source':props['source']},profile=profile)
        if identity_check.path is None:
            raise _Invalid('unsupported_source_identity_form')
    return {'name':name,'properties':props,'properties_sha256':_digest(props),
            'source_pc':row.get('pc') if type(row.get('pc')) is int else None}


def resolve_conditional_mesh_bindings(prior_snapshots,requests,*,profile,
                                      prior_order_assumed=False,provenance=None,
                                      limits=BindingLimits()):
    """Return an offline conditional plan, not loaded assets or registry mutation.

    Snapshot shape: {family:'Mesh', name:<literal>, properties:{...}}.
    Prior source identities must be literal bare .fbx. Unknown nonidentity
    properties remain stored; they block only lookups that actually hit them.
    Repeated prior exact names/unsupported identity shapes invalidate the plan.
    Request-local errors are retained individually. Resource-definition templates
    and remote constructors are not registration snapshots in this model.
    """
    if profile!=PROFILE:raise ValueError('unsupported_mesh_resource_profile')
    if not isinstance(limits,BindingLimits):raise TypeError('BindingLimits_required')
    report={'schema':'sky-conditional-mesh-bindings-v1','profile':profile,
            'status':'unresolved_prior','prior_order_assumed':prior_order_assumed is True,
            'assumptions':['prior_snapshots_are_explicitly_ordered_already_registered_Mesh_objects',
                           'existing_getters_equal_constructor_plus_pinned_native_defaults',
                           'no_external_registry_mutation_or_object_unloading',
                           'all_current_requests_lookup_only_the_fixed_prior_registry'],
            'provenance':{},'reasons':[],'registry':{'exact':[],'aliases':[]},
            'registrations':[],'decisions':[],'pending_names_with_multiple_requests':[],
            'runtime_binding_verified':False,'collision_verified':False,'runtime_ready':False,
            'lua_executed':False,'asset_files_read':False}
    if type(prior_order_assumed) is not bool or not prior_order_assumed:
        report['reasons']=['explicit_prior_order_assumption_required'];return report
    if not isinstance(prior_snapshots,(list,tuple)) or not isinstance(requests,(list,tuple)):
        report['reasons']=['ordered_snapshot_sequences_required'];return report
    if len(prior_snapshots)+len(requests)>limits.max_snapshots:
        report['reasons']=['snapshot_count_budget'];return report
    copier=_Copier(limits)
    try:
        if provenance is not None and not isinstance(provenance,Mapping):
            raise _Invalid('provenance_mapping_required')
        report['provenance']=copier.copy({} if provenance is None else provenance)
        prior=[_snapshot(row,copier,profile,True) for row in prior_snapshots]
        names=[row['name'] for row in prior]
        if len(set(names))!=len(names):raise _Invalid('duplicate_prior_exact_name')
    except (ValueError,TypeError,OverflowError,RecursionError) as error:
        report['reasons']=[str(error)];return report
    exact={};aliases={};by_name={row['name']:row for row in prior}
    for ordinal,row in enumerate(prior):
        name=row['name'];exact[name]=ordinal
        alias=row['properties']['source'][:-4]
        event={'ordinal':ordinal,'exact_name':name,'properties_sha256':row['properties_sha256'],
               'source_pc':row['source_pc'],'alias_key':alias,'alias_inserted':False}
        if alias==name:event['alias_action']='same_as_exact_name_no_alias'
        elif alias in exact:
            event.update(alias_action='suppressed_existing_exact',occupant=alias)
        elif alias in aliases:
            event.update(alias_action='suppressed_existing_alias',occupant=aliases[alias])
        else:
            aliases[alias]=name;event.update(alias_action='inserted',alias_inserted=True)
        report['registrations'].append(event)
    report['registry']={'exact':[{'name':name,'prior_ordinal':ordinal} for name,ordinal in exact.items()],
                        'aliases':[{'key':key,'target_name':name} for key,name in aliases.items()]}
    report['status']='conditional_plan'
    pending={}
    for ordinal,value in enumerate(requests):
        decision={'request_ordinal':ordinal,'request_name':None,'decision':'unresolved_request',
                  'lookup_mode':None,'matched_prior_name':None,'path_candidate':None,
                  'reasons':[],'pending_new_visible_to_this_batch':False,
                  'runtime_binding_verified':False,'collision_verified':False}
        try:request=_snapshot(value,copier,profile,False)
        except (ValueError,TypeError,OverflowError,RecursionError) as error:
            decision['reasons']=[str(error)];report['decisions'].append(decision);continue
        name=request['name'];decision['request_name']=name
        decision['request_properties_sha256']=request['properties_sha256']
        if name in exact:mode='exact';target=name
        elif name in aliases:mode='source_alias';target=aliases[name]
        else:mode='miss';target=None
        decision.update(lookup_mode=mode,matched_prior_name=target)
        if target is None:
            projected=project_mesh_path(request['properties'],profile=profile)
            if projected.path is None:decision['reasons']=list(projected.reasons)
            else:decision.update(decision='pending_new',path_candidate=projected.path)
        else:
            old=by_name[target];decision['matched_prior_ordinal']=exact[target]
            comparison=compare_conditional_existing(old['properties'],request['properties'],profile=profile,lookup_mode=mode)
            decision['comparison']=asdict(comparison)
            if comparison.status=='unresolved':
                decision.update(decision='unresolved_existing',reasons=list(comparison.reasons))
            elif comparison.status=='compatible_if_existing':
                decision.update(decision='retained_exact' if mode=='exact' else 'retained_alias',
                                path_candidate=comparison.retained_path_candidate)
            elif mode=='exact':
                decision.update(decision='exact_incompatible',reasons=['same_name_existing_comparison_failed'])
            else:
                projected=project_mesh_path(request['properties'],profile=profile)
                if projected.path is None:decision['reasons']=list(projected.reasons)
                else:decision.update(decision='pending_new_alias_incompatible',path_candidate=projected.path,
                                     reasons=['different_name_alias_incompatible_requires_new_request_object'])
        if decision['decision'].startswith('pending_new'):
            pending.setdefault(name,[]).append(ordinal)
        report['decisions'].append(decision)
    report['pending_names_with_multiple_requests']=[{'name':name,'request_ordinals':ordinals}
        for name,ordinals in pending.items() if len(ordinals)>1]
    report['classified_requests']=len(report['decisions'])
    return report
