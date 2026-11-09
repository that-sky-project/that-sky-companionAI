"""Cold admission contract. A local scalar fit alone NEVER admits a live bot."""
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
import hashlib
import json
import math
import re
import time

PROFILE = 'bounded_empirical_patch_pilot_v1'
GLOBAL_FLAGS = ('nativeCollisionVerified', 'calibrationVerified', 'runtimeReady')
REQUIRED = ('heightReference', 'subjectBinding', 'botOriginMapping',
            'patchGeometry', 'assetManifest', 'navigationReport')
_SEAL = object()


class AdmissionError(ValueError):
    pass


def require(value, reason):
    if not value:
        raise AdmissionError(reason)


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def vec(value):
    require(type(value) in (list, tuple) and len(value) == 3
            and all(finite(v) for v in value), 'finite_xyz_required')
    return tuple(float(v) for v in value)


def digest(value):
    require(type(value) is str and re.fullmatch('[0-9a-f]{64}', value), 'sha256_required')
    return value


def immutable(value):
    if isinstance(value, dict):
        return MappingProxyType({k: immutable(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(immutable(v) for v in value)
    return value


@dataclass(frozen=True)
class Admission:
    mode: str
    certificate_sha: str
    context: object
    patch: object
    bot_offset: tuple
    target_offset: tuple
    max_speed: float
    max_distance: float
    expires_wall: float
    _seal: object
    max_requested_distance: float | None = None

    @property
    def live_permitted(self):
        return self._seal is _SEAL and self.mode == 'bounded_empirical_pilot'


def _shape(context, patch, offset, speed, distance, expires, now, *, experimental_scene=None):
    require(type(context) is dict and set(context) == {
        'sceneName', 'sceneHash', 'roomGeneration', 'sessionBindingDigest',
        'clockDomain', 'bot', 'target'}, 'exact_context_required')
    if experimental_scene is None:
        require(context['sceneName'] == 'CandleSpace' and context['sceneHash'] == 3526133726,
                'scene_not_supported')
    else:
        require(type(experimental_scene) is tuple and len(experimental_scene) == 2,
                'experimental_scene_binding')
        name, hashed = experimental_scene
        require(type(name) is str and re.fullmatch(r'[A-Za-z0-9_-]{1,80}', name)
                and type(hashed) is int and 0 <= hashed <= 0xffffffff,
                'experimental_scene_identity')
        expected = 2166136261
        for byte in name.encode('utf-8'):
            expected = ((expected ^ byte) * 16777619) & 0xffffffff
        require(hashed == expected and context['sceneName'] == name and context['sceneHash'] == hashed,
                'experimental_scene_mismatch')
    require(type(context['roomGeneration']) is int and context['roomGeneration'] > 0,
            'room_generation_required')
    digest(context['sessionBindingDigest'])
    require(type(context['clockDomain']) is str and 1 <= len(context['clockDomain']) <= 128,
            'server_clock_domain_required')
    for role in ('bot', 'target'):
        subject = context[role]
        require(type(subject) is dict and set(subject) == {'uuid', 'slot', 'incarnation'},
                'exact_subject_required')
        import uuid
        require(type(subject['uuid']) is str and str(uuid.UUID(subject['uuid'])) == subject['uuid'],
                'full_subject_uuid_required')
        require(((role == 'bot' and (subject['slot'] is None or (type(subject['slot']) is int and 1 <= subject['slot'] <= 255)) and subject['incarnation'] is None)
                 or (type(subject['slot']) is int and 0 <= subject['slot'] <= 0xffffffff
                and type(subject['incarnation']) is int and subject['incarnation'] >= 0)),
                'subject_incarnation_required')
    require(context['bot']['uuid'] != context['target']['uuid'], 'distinct_subjects_required')
    require(type(patch) is dict and set(patch) == {'polygonXZ', 'minFeetY', 'maxFeetY',
        'supportMarginMeters', 'assetDigests', 'navigationSHA256'}, 'exact_patch_required')
    polygon = patch['polygonXZ']
    require(type(polygon) in (list, tuple) and 3 <= len(polygon) <= 16
            and all(type(p) in (list, tuple) and len(p) == 2 and all(finite(v) for v in p)
                    for p in polygon), 'finite_convex_patch_required')
    orientation = None
    for i, a in enumerate(polygon):
        b = polygon[(i + 1) % len(polygon)]
        require(math.hypot(b[0]-a[0], b[1]-a[1]) > 1e-8, 'degenerate_patch')
        for j, c in enumerate(polygon):
            if j in (i, (i+1) % len(polygon)):
                continue
            cross = (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])
            require(abs(cross) > 1e-10, 'degenerate_patch')
            if orientation is None:
                orientation = cross > 0
            require((cross > 0) == orientation, 'nonconvex_patch')
    require(finite(patch['minFeetY']) and finite(patch['maxFeetY'])
            and patch['minFeetY'] <= patch['maxFeetY'], 'patch_height_band')
    require(finite(patch['supportMarginMeters']) and patch['supportMarginMeters'] >= .765,
            'unverified_support_margin')
    require(type(patch['assetDigests']) is dict and set(patch['assetDigests']) == {
        'manifest', 'positions', 'indices'}, 'asset_pins_required')
    for value in patch['assetDigests'].values():
        digest(value)
    digest(patch['navigationSHA256'])
    require(finite(speed) and 0 < speed <= .25, 'speed_outside_pilot_limit')
    require(finite(distance) and 0 < distance <= .8, 'route_outside_pilot_limit')
    require(finite(expires) and finite(now) and now < expires <= now + 86400,
            'certificate_expired_or_invalid')
    return vec(offset)


def _read(root, descriptor):
    require(type(descriptor) is dict and set(descriptor) == {'file', 'sha256'}, 'artifact_descriptor')
    expected = digest(descriptor['sha256'])
    require(type(descriptor['file']) is str and '\\' not in descriptor['file']
            and ':' not in descriptor['file'], 'artifact_relative_path')
    relative = Path(descriptor['file'])
    require(not relative.is_absolute() and '..' not in relative.parts, 'artifact_relative_path')
    path = (root / relative).resolve(strict=True)
    require(path.is_relative_to(root), 'artifact_escape')
    require(0 < path.stat().st_size <= 4 * 1024 * 1024, 'artifact_budget')
    raw = path.read_bytes()
    require(len(raw) <= 4 * 1024 * 1024 and hashlib.sha256(raw).hexdigest() == expected,
            'artifact_hash_mismatch')
    value = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(AdmissionError('nonfinite_json')))
    require(type(value) is dict, 'artifact_object_required')
    return value


def load_admission(root, certificate, expected_sha, *, expected_context, wall=time.time):
    """All I/O is cold; caller pins the certificate hash independently of its contents."""
    root = Path(root).resolve(strict=True)
    doc = _read(root, {'file': certificate, 'sha256': expected_sha})
    require(doc.get('schema') == 'bounded-experimental-pilot-admission-v1' and doc.get('profile') == PROFILE,
            'explicit_bounded_certificate_required')
    require(doc.get('enabled') is True and doc.get('evidenceKind') == 'recorded_real',
            'real_enabled_evidence_required')
    require(all(doc.get(key) is False for key in GLOBAL_FLAGS), 'global_flags_must_remain_false')
    require(doc.get('context') == expected_context, 'current_subject_context_mismatch')
    artifacts = doc.get('artifacts')
    require(type(artifacts) is dict and set(artifacts) == set(REQUIRED), 'complete_evidence_set_required')
    rows = {key: _read(root, descriptor) for key, descriptor in artifacts.items()}
    require(doc.get('experimental') is True and doc.get('nativeEquivalent') is False,
            'experimental_not_native_equivalence_required')
    reference = rows['heightReference']
    require(reference.get('schema') == 'local-empirical-height-reference-v1'
            and reference.get('evidenceKind') == 'recorded_real'
            and reference.get('formalCalibration') is False
            and reference.get('subject') == expected_context['target']
            and finite(reference.get('offsetMeters'))
            and bool(reference.get('sourceCaptureSHA256s')), 'local_target_reference_required')
    for source in reference['sourceCaptureSHA256s']:
        digest(source)
    binding = rows['subjectBinding']
    require(binding.get('schema') == 'production-pilot-subject-binding-v1'
            and binding.get('onlineSubjectBound') is True
            and binding.get('evidenceKind') == 'recorded_real'
            and binding.get('bindingKind') == 'production_bot_udp_and_native_test_target'
            and binding.get('context') == expected_context, 'production_bot_and_target_binding_required')
    digest(binding.get('sourceEvidenceSHA256'))
    pose = rows['botOriginMapping']
    require(pose.get('schema') == 'production-bot-origin-mapping-v1'
            and pose.get('evidenceKind') == 'recorded_real'
            and pose.get('realBotObserved') is True and pose.get('context') == expected_context
            and pose.get('notCopiedFromLocalTarget') is True
            and pose.get('offset') == doc.get('botOffset'), 'real_bot_origin_mapping_required')
    digest(pose.get('sourceEvidenceSHA256'))
    require(doc.get('parameterSources') == {
        'heightReference': 'local_empirical_provisional_not_bot_calibration',
        'botOriginOffset': 'production_bot_origin_mapping',
        'speedLimit': 'operator_imposed_not_native_speed',
        'motor': 'engineering_defaults_not_client_matched',
        'flight': 'disabled', 'water': 'disabled'}, 'explicit_parameter_sources_required')
    patch = doc.get('patch')
    require(type(patch) is dict, 'patch_required')
    require(rows['patchGeometry'].get('certificatePatch') == patch, 'patch_evidence_mismatch')
    require(artifacts['assetManifest']['sha256'] == patch['assetDigests']['manifest']
            and rows['assetManifest'].get('sha256', {}).get('positions') == patch['assetDigests']['positions']
            and rows['assetManifest'].get('sha256', {}).get('indices') == patch['assetDigests']['indices']
            and rows['navigationReport'].get('navigationHash') == patch['navigationSHA256'],
            'patch_asset_nav_binding')
    offset = _shape(expected_context, patch, doc.get('botOffset'), doc.get('maxSpeed'),
                    doc.get('maxDistance'), doc.get('expiresWall'), wall())
    return Admission('bounded_empirical_pilot', digest(expected_sha), immutable(expected_context),
        immutable(patch), offset, (0., float(reference['offsetMeters']), 0.),
        doc['maxSpeed'], doc['maxDistance'], doc['expiresWall'], _SEAL)


def simulation_admission(context, patch, *, offset=(0., .5, 0.), speed=.25, distance=.8,
                         now=0.):
    """Only for explicitly labelled dependency tests; NEVER a live permit."""
    offset = _shape(context, patch, offset, speed, distance, now + 3600, now)
    return Admission('simulation', '0'*64, immutable(context), immutable(patch), offset, offset,
                     speed, distance, now + 3600, _SEAL)


def check_seal(admission, simulation):
    require(isinstance(admission, Admission) and admission._seal is _SEAL, 'verified_admission_required')
    require(type(simulation) is bool and (admission.mode == 'simulation' if simulation
        else admission.mode in ('bounded_empirical_pilot', 'experimental_ground_trial')),
        'simulation_cannot_enable_live')


def experimental_admission(context, patch, *, offset, enabled=False, now=None,
                           speed=.25, distance=.8, requested_distance=None, scene_binding=None):
    """Explicit engineering trial. No native equivalence/calibration claim.

    Unknown self slot may stay None; real room engine + own_uuid is bound by the
    controller observation adapter. This is intentionally not a certificate.
    """
    require(enabled is True, 'experimental_opt_in_required')
    now = time.time() if now is None else now
    offset = _shape(context, patch, offset, .25, .8, now+3600, now,
                    experimental_scene=scene_binding)
    requested_distance = min(distance, 6.) if requested_distance is None else requested_distance
    require(finite(speed) and 0 < speed <= 3., 'experimental_walk_speed_range')
    require(finite(distance) and 0 < distance <= 8. and finite(requested_distance)
            and 0 < requested_distance <= min(6., distance), 'experimental_route_budget_range')
    payload = json.dumps({'context': context, 'patch': patch, 'offset': offset,
        'speed': speed, 'travelBudget': distance, 'segment': requested_distance}, sort_keys=True).encode()
    return Admission('experimental_ground_trial', hashlib.sha256(payload).hexdigest(),
        immutable(context), immutable(patch), offset, offset, speed, distance, now+3600, _SEAL, requested_distance)
