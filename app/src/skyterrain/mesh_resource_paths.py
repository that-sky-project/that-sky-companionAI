"""Static Mesh constructor path candidates, not a runtime resource-cache resolver.

CN 0.16.8/421629: same mesh path layout as 0.16.7 (constructor addresses documented for 0.16.7; 0.16.8 code addresses not re-derived)
0x98..0xa7. Path builder 0x1e85e1c appends the suffixes below for
literal, bare .fbx sources. Other source forms deliberately remain unresolved.
No asset reads, Lua execution, collision construction, or live integration.
"""
from collections.abc import Mapping
from dataclasses import dataclass
import re

PROFILE = 'netease-android-0.16.8-421629'
SUFFIX_FIELDS = (
    ('stripGeometry', '_StripGeo'), ('stripAnimation', '_StripAnim'),
    ('uncompressedAnim', '_UncompAnim'), ('additive', '_Add'),
    ('computeOcclusions', '_CompOcc'), ('computeEdges', '_CompEdges'),
    ('computeAdjacency', '_CompAdj'), ('compressPositions', '_ZipPos'),
    ('compressUvs', '_ZipUvs'), ('stripUv13', '_StripUv13'),
    ('stripNormals', '_StripNorm'), ('forceIndex32', '_ForceIdx32'),
    ('copyFrameDelay', '_CopyFrameDelay'),
)
BOOL_FIELDS = tuple(key for key, _ in SUFFIX_FIELDS) + ('registerCollision', 'loadAsync', 'loadLazy')
_ALLOWED = frozenset(BOOL_FIELDS) | {'source', 'name', 'class'}
_BARE_FBX = re.compile(r'([A-Za-z0-9_+\-]{1,128})\.fbx\Z')


@dataclass(frozen=True)
class MeshPathCandidate:
    path: str | None
    reasons: tuple[str, ...]
    defaulted_fields: tuple[str, ...] = ()
    runtime_binding_verified: bool = False
    collision_verified: bool = False


@dataclass(frozen=True)
class ConditionalMeshComparison:
    status: str
    retained_path_candidate: str | None
    ignored_differences: tuple[str, ...]
    conflicts: tuple[str, ...]
    reasons: tuple[str, ...]
    existing_object_assumed: bool = True
    runtime_binding_verified: bool = False
    collision_verified: bool = False
    lookup_mode: str = 'exact'


def _snapshot(properties, profile):
    if profile != PROFILE:
        raise ValueError('unsupported_mesh_resource_profile')
    if not isinstance(properties, Mapping):
        raise TypeError('mesh_properties_must_be_mapping')
    result = dict(properties)
    if any(type(k) is not str for k in result):
        raise ValueError('nonliteral_property_key')
    return result


def _issues(properties):
    reasons = [f'unknown_property:{k}' for k in sorted(set(properties) - _ALLOWED)]
    if properties.get('class', 'Mesh') != 'Mesh':
        reasons.append('not_Mesh_class')
    if 'name' in properties and (type(properties['name']) is not str or not properties['name']):
        reasons.append('nonliteral_or_empty_name')
    if 'source' in properties and (type(properties['source']) is not str or
                                     _BARE_FBX.fullmatch(properties['source']) is None):
        reasons.append('unsupported_source_requires_literal_bare_fbx')
    for key in BOOL_FIELDS:
        if key in properties and type(properties[key]) is not bool:
            reasons.append(f'nonliteral_boolean:{key}')
    return reasons


def project_mesh_path(properties, *, profile):
    """Project a newly constructed object's path under the pinned native defaults.

    The return value never asserts that this object won a name lookup or that the
    member exists. Unknown keys/source forms fail closed, rather than guessing.
    """
    p = _snapshot(properties, profile)
    reasons = _issues(p)
    source = p.get('source')
    match = _BARE_FBX.fullmatch(source) if type(source) is str else None
    if source is None and 'unsupported_source_requires_literal_bare_fbx' not in reasons:
        reasons.append('unsupported_source_requires_literal_bare_fbx')
    if reasons:
        return MeshPathCandidate(None, tuple(reasons))
    suffix = ''.join(text for key, text in SUFFIX_FIELDS if p.get(key, False))
    return MeshPathCandidate('assets/Data/Meshes/Bin/' + match[1] + suffix + '.mesh', (),
                             tuple(k for k in BOOL_FIELDS if k not in p))


def compare_conditional_existing(existing_constructor, request, *, profile, lookup_mode='exact'):
    """Compare two snapshots, assuming the first object already exists unchanged.

    This is NOT queue replay: existence/lifetime/getter semantics are assumptions.
    Duplicate declarations and mutable Lua aliases must be handled by the caller.
    Exact lookup requires equal names. Explicit source_alias lookup additionally
    requires that the request names the old bare source stem, as native Mesh's
    alias getter does; it never permits an arbitrary different-name substitute.
    """
    old = _snapshot(existing_constructor, profile)
    new = _snapshot(request, profile)
    if type(lookup_mode) is not str or lookup_mode not in ('exact', 'source_alias'):
        raise ValueError('unsupported_mesh_lookup_mode')
    old_path = project_mesh_path(old, profile=profile)
    reasons = list(old_path.reasons) + _issues(new)
    names_literal = type(old.get('name')) is str and bool(old.get('name')) and type(new.get('name')) is str and bool(new.get('name'))
    if not names_literal:
        reasons.append('same_literal_name_required')
    elif lookup_mode == 'exact' and new['name'] != old['name']:
        reasons.append('same_literal_name_required')
    elif lookup_mode == 'source_alias':
        if old_path.path is None or old['source'][:-4] != new['name'] or old['name'] == new['name']:
            reasons.append('explicit_native_source_alias_required')
    if reasons:
        return ConditionalMeshComparison('unresolved', None, (), (), tuple(reasons), lookup_mode=lookup_mode)
    ignored, conflicts = [], []
    for key, requested in new.items():
        if key in ('class', 'name'):
            continue
        prior = old.get(key, False if key in BOOL_FIELDS else None)
        if type(prior) is type(requested) and prior == requested:
            continue
        if key in ('registerCollision', 'loadLazy') or (key == 'computeOcclusions' and requested is False):
            ignored.append(key)
        else:
            conflicts.append(key)
    return ConditionalMeshComparison(
        'incompatible_if_existing' if conflicts else 'compatible_if_existing',
        None if conflicts else old_path.path, tuple(sorted(ignored)), tuple(sorted(conflicts)),
        ('assumes_existing_object_getters_equal_constructor_plus_native_defaults',), lookup_mode=lookup_mode)
