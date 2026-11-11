"""Anonymous numeric projection only. Does not start a connection or install hooks."""
import math
import hashlib
import json
from bisect import bisect_left

# These are observed native labels, not claims about jumping semantics.
RAW_LABELS = ('0x21dd40c', '0x21ddcc8', '0x1715ed0', '0x1715f48',
              '0x1715f70', '0x1f80844', '0x21deffc', '0x21ddc78',
              '0x1715ea8', '0x1715ef8', '0x1715f20')
NUMERIC_FIELDS = ('gen', 'start', 'mode', 'code90', 'clock', 'h_gen', 'h_start',
                  'yaw1', 'yaw2', 'yaw3', 'fx', 'fy', 'fz', 'vx', 'vy', 'vz',
                  'seated', 'sit_enter', 'avatar_state')

# Exactly the pinned 0.16.8 POSE schema, not a prefix-based export of Actor data.
# Flatten each record in this order; tuple = (label, bit width, components).
POSE_LAYOUT = (
    ('0x1715e80', 12, 1), ('0x1715ea8', 3, 1), ('0x1715ed0', 3, 1),
    ('0x1715ef8', 3, 1), ('0x1715f20', 3, 1), ('0x1715f48', 3, 1),
    ('0x1715f70', 3, 1), ('0x1716078', 12, 3), ('0x1716174', 6, 3),
    ('0x1716260', 8, 3), ('0x1716360', 8, 3), ('0x1716464', 5, 3),
    ('0x1716568', 5, 3), ('0x171666c', 8, 3), ('0x1716770', 8, 3),
    ('0x1716a64', 10, 1), ('0x1716a94', 1, 1),
)
POSE_LAYOUT_SHA256 = hashlib.sha256(
    json.dumps(POSE_LAYOUT, separators=(',', ':')).encode('ascii')).hexdigest()
MAX_PROJECTED_ROW_BYTES = 2400  # 200 samples + time fields fit the existing 512 KiB report.


def _project_pose(observation, starts, boundaries, current, history):
    counts = getattr(observation, '_counts', None)
    if counts is None:
        return {'coverage': 'unavailable', 'reason': 'codec_pose_metadata_absent'}
    counts = dict(counts)
    ends = [p for label, p in boundaries if label == '0x21deffc']
    if len(ends) != 1 or not current < history < ends[0]:
        raise ValueError('pose_projection_segment_bounds')
    result = {'coverage': 'complete', 'layout_sha256': POSE_LAYOUT_SHA256,
              'current': [], 'history': []}
    for segment, count_name, lower, upper in (
            ('current', 'pose_records', current, history),
            ('history', 'history_pose_records', history, ends[0])):
        count = counts.get(count_name)
        if type(count) is not int or not 0 <= count <= 4:
            raise ValueError('pose_projection_count')
        result[segment + '_count'] = count
        positions = {label: sorted(p for name, p in boundaries
                                  if name == label and lower <= p < upper)
                     for label, _, _ in POSE_LAYOUT}
        if any(len(p) != count for p in positions.values()):
            raise ValueError('pose_projection_label_count')
        for record in range(count):
            values, expected = [], positions[POSE_LAYOUT[0][0]][record]
            for label, width, components in POSE_LAYOUT:
                position = positions[label][record]
                if position != expected:
                    raise ValueError('pose_projection_record_order')
                index = bisect_left(starts, position)
                for component in range(components):
                    i = index + component
                    if i >= len(starts) or starts[i] != position + component * width:
                        raise ValueError('pose_projection_component_shape')
                    token = observation._tokens[i]
                    if (token.kind != 'bits' or token.extent != width
                            or type(token.value) is not int
                            or not 0 <= token.value < (1 << width)):
                        raise ValueError('pose_projection_component_value')
                    values.append(token.value)
                expected += width * components
            if expected > upper:
                raise ValueError('pose_projection_record_end')
            result[segment].append(values)
    return result


def project_actor(body, anim_pose):
    """Parse once, export explicit motion whitelist; never raw bytes/identity/outfit."""
    if not isinstance(body, bytes) or not 0 < len(body) <= 0x2420:
        raise ValueError('actor_size')
    observation = anim_pose.inspect_actor_wire(body, verified_profile_sha256=anim_pose.SHA,
                                              check_zero_padding=False)
    loc = anim_pose._locate(observation)
    values = {name: anim_pose._val(name, observation._tokens[i]) for name, i in loc.items()}
    xyz = [float(values[n]) for n in ('ox', 'oy', 'oz')]
    if not all(math.isfinite(v) and abs(v) < 10000 for v in xyz):
        raise ValueError('actor_position')
    out = {'xyz': xyz, 'animation': {n: int(values[n]) for n in NUMERIC_FIELDS if n in values},
           'animation_key': f"{int(values['key']):016x}",
           'history_animation_key': f"{int(values['h_key']):016x}", 'raw_motion_codes': {}}
    starts = anim_pose._starts(observation)
    boundaries = list(observation._boundaries)
    current = next(p for label, p in boundaries if label == anim_pose.CURRENT)
    history = next(p for label, p in boundaries if label == anim_pose.ANIM_BLOCK)
    for label in RAW_LABELS:
        positions = [p for name, p in boundaries if name == label]
        if label.startswith('0x171'):
            positions = [p for p in positions if current <= p < history]
        if len(positions) != 1:
            continue
        index = next((i for i, bit in enumerate(starts) if bit >= positions[0]), None)
        if index is None:
            continue
        token = observation._tokens[index]
        width = token.extent * 8 if token.kind == 'raw' else token.extent
        if not 1 <= width <= 64:
            raise ValueError('motion_code_width')
        value = int.from_bytes(token.value, 'little') if token.kind == 'raw' else int(token.value)
        out['raw_motion_codes'][label] = {'code': value, 'width': width}
    out['pose_records'] = _project_pose(observation, starts, boundaries, current, history)
    # Oversize/malformed projections must never silently drop whole input frames.
    # Rare large shapes retain explicit counts/coverage, not truncated pose arrays.
    size = len(json.dumps(out, ensure_ascii=False, allow_nan=False,
                          separators=(',', ':')).encode('utf-8'))
    if size > MAX_PROJECTED_ROW_BYTES:
        pose = out['pose_records']
        out['pose_records'] = {k: v for k, v in pose.items() if k not in ('current', 'history')}
        out['pose_records'].update(coverage='omitted_output_budget',
                                   full_row_bytes=size, row_byte_limit=MAX_PROJECTED_ROW_BYTES)
        if len(json.dumps(out, separators=(',', ':')).encode()) > MAX_PROJECTED_ROW_BYTES:
            raise ValueError('projection_output_budget')
    return out


def target_body(data, slot):
    """Same length framing as current RoomTracker; ignore every other occupant."""
    if not isinstance(data, bytes) or len(data) > 4 * 1024 * 1024:
        raise ValueError('snapshot_size')
    offset = 0
    found = None
    while offset + 5 <= len(data):
        size = int.from_bytes(data[offset + 1:offset + 5], 'little')
        end = offset + 5 + size
        if not size or end > len(data):
            raise ValueError('snapshot_record')
        if data[offset] == slot:
            if found is not None:
                raise ValueError('duplicate_target')
            found = data[offset + 5:end]
        offset = end
    if offset != len(data):
        raise ValueError('snapshot_tail')
    return found
