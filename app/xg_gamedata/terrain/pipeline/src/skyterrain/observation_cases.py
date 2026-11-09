"""Hash-bound, anonymous offline observations. Never installs physics parameters."""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re

COLUMNS = ['timestamp', 'map_hash', 'x', 'y', 'z', 'age']
FLAGS = ('nativeCollisionVerified', 'calibrationVerified', 'runtimeReady')
SOURCE_KEYS = frozenset(('schema','columns','manual_mode_label','samples','count','status','reason',
                        'requested_duration_seconds','maximum_wait_seconds','poll_interval_seconds',
                        'observed_frequency','poll_result_counts','identity_fields_exported','grounded_verified',
                        'manual_label_is_not_grounded_or_native_flight_flag','extended_status_fields_collected','origin_semantics'))

def need(value: bool, reason: str) -> None:
    if not value:
        raise ValueError(reason)

def finite(value) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False

def _file(root: Path, relative: str, expected_hash: str | None = None) -> tuple[Path, bytes]:
    need(isinstance(relative, str) and '\\' not in relative and ':' not in relative, 'invalid_relative_path')
    parts = PurePosixPath(relative)
    need(not parts.is_absolute() and parts.parts and all(p not in ('.', '..') for p in parts.parts), 'invalid_relative_path')
    target = root.joinpath(*parts.parts).resolve(strict=True)
    need(target.is_relative_to(root) and target != root, 'path_escape')
    need(target.is_file() and target.stat().st_size <= 262144, 'observation_file_budget')
    raw = target.read_bytes()
    need(len(raw) <= 262144, 'observation_file_budget')
    if expected_hash is not None:
        need(isinstance(expected_hash, str) and re.fullmatch('[0-9a-f]{64}', expected_hash) is not None, 'invalid_sha256')
        need(hashlib.sha256(raw).hexdigest() == expected_hash, 'observation_hash_mismatch')
    return target, raw

@dataclass(frozen=True)
class ObservationCase:
    case_id: str
    manual_label: str
    samples: tuple[tuple[float, ...], ...]
    window_starts: tuple[int, ...]
    window_gaps: tuple[float, ...]
    limitations: tuple[str, ...]

def load_observation_cases(case_root: str | Path, manifest: str) -> tuple[ObservationCase, ...]:
    root = Path(case_root).resolve(strict=True)
    _, raw = _file(root, manifest)
    doc = json.loads(raw)
    need(isinstance(doc, dict), 'manifest_object_required')
    need(doc.get('schema') == 'skyterrain-observation-regression-v1' and doc.get('mode') == 'offline', 'offline_observations_required')
    need(all(doc.get(flag) is False for flag in FLAGS), 'unverified_flags_required')
    need(doc.get('parameterFittingAllowed') is False, 'parameter_fitting_not_allowed')
    entries = doc.get('cases')
    need(isinstance(entries, list) and 0 < len(entries) <= 64, 'case_budget')
    seen, result = set(), []
    for entry in entries:
        need(isinstance(entry, dict), 'case_shape')
        key, label = entry.get('id'), entry.get('manualLabel')
        need(isinstance(key, str) and re.fullmatch('[a-z0-9_-]{1,64}', key) is not None and key not in seen, 'case_id')
        need(isinstance(label, str) and 0 < len(label) <= 160, 'manual_label')
        seen.add(key)
        need(entry.get('acceptedFor') == 'offline_trajectory_regression_only' and entry.get('nativeModeVerified') is False, 'case_not_offline_only')
        map_hash = entry.get('mapHash')
        need(type(map_hash) is int and 0 <= map_hash <= 0xffffffff, 'map_hash')
        limitations, windows = entry.get('limitations'), entry.get('windows')
        need(isinstance(limitations, list) and limitations and all(isinstance(s, str) and 0 < len(s) <= 300 for s in limitations), 'limitations_required')
        need(isinstance(windows, list) and 0 < len(windows) <= 4, 'window_budget')
        samples, starts, gaps = [], [], []
        for window in windows:
            need(isinstance(window, dict), 'window_shape')
            need(isinstance(window.get('sha256'), str) and re.fullmatch('[0-9a-f]{64}', window['sha256']) is not None, 'window_hash_required')
            _, body = _file(root, window.get('path'), window.get('sha256'))
            observed = json.loads(body)
            need(isinstance(observed, dict), 'sample_object_required')
            need(set(observed) <= SOURCE_KEYS, 'unexpected_sample_fields')
            need(observed.get('schema') == 'anonymous-segmented-numeric-position-v1', 'sample_schema')
            need(observed.get('manual_label_is_not_grounded_or_native_flight_flag') is True, 'manual_label_guard_required')
            need(observed.get('status') == 'sampled' and observed.get('columns') == COLUMNS, 'sample_schema')
            need(observed.get('identity_fields_exported') is False and observed.get('grounded_verified') is False, 'anonymous_unverified_samples_required')
            rows = observed.get('samples')
            need(isinstance(rows, list) and 0 < len(rows) <= 600 and type(observed.get('count')) is int and observed['count'] == len(rows), 'sample_budget_or_count')
            need(type(window.get('count')) is int and window['count'] == len(rows), 'window_count_mismatch')
            starts.append(len(samples))
            for index, row in enumerate(rows):
                need(isinstance(row, list) and len(row) == 6 and all(finite(v) for v in row), 'sample_numbers')
                need(type(row[1]) is int and row[1] == map_hash, 'sample_map_mismatch')
                need(row[0] > 0 and all(abs(v) < 1e6 for v in row[2:5]) and 0 <= row[5] <= .5, 'sample_bounds_or_age')
                if samples:
                    need(row[0] > samples[-1][0], 'nonincreasing_time')
                    if index == 0:
                        gaps.append(row[0] - samples[-1][0])
                samples.append(tuple(row))
        result.append(ObservationCase(key, label, tuple(samples), tuple(starts), tuple(gaps), tuple(limitations)))
    return tuple(result)

def observation_summary(cases: tuple[ObservationCase, ...]) -> dict:
    return {'mode': 'offline', 'caseCount': len(cases), 'sampleCount': sum(len(c.samples) for c in cases),
            **{flag: False for flag in FLAGS}, 'parameterFittingAllowed': False,
            'cases': [{'id': c.case_id, 'sampleCount': len(c.samples), 'windowCount': len(c.window_starts),
                       'observedSpanSeconds': c.samples[-1][0] - c.samples[0][0],
                       'windowGapsSeconds': list(c.window_gaps), 'manualLabel': c.manual_label,
                       'limitations': list(c.limitations)} for c in cases]}
