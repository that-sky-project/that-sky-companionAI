"""CN social interaction catalogue; pure data and lookup, no native/network imports.

Codes/names are copied from existing CN OFFER_IDS/KINDS, never international
SOCIAL_OFFER_TYPES ordinals. No relationship levels are inferred. Existing
capture annotations describe acceptance patterns, NOT active-invite success.
Gifting, paid unlocks and social-candle transactions are not action entries.
"""
import re
from types import MappingProxyType

_IDS = {2: 'hand_hold',
 3: 'hug',
 4: 'high_five',
 5: 'fist_bump',
 26: 'hug',
 35: 'high_five',
 36: 'hug',
 37: 'fist_bump',
 40: 'carry',
 41: 'carry',
 42: 'high_five',
 43: 'high_five',
 44: 'hairtousle',
 45: 'hairtousle',
 46: 'playfight',
 47: 'playfight',
 49: 'bearhug',
 50: 'bearhug',
 56: 'duetdance',
 57: 'duetdance',
 58: 'handshake',
 59: 'handshake',
 61: 'sidehug',
 62: 'sidehug',
 63: 'princesscarry',
 64: 'princesscarry',
 65: 'duetbow',
 66: 'duetbow',
 67: 'hug',
 68: 'duetbownfc',
 69: 'duetbownfc',
 70: 'whisper',
 71: 'whisper',
 72: 'revolvingdance',
 73: 'revolvingdance',
 74: 'secrethandshake',
 75: 'secrethandshake',
 76: 'whisper',
 77: 'whisper',
 78: 'revolvingdance',
 79: 'revolvingdance',
 80: 'secrethandshake',
 81: 'secrethandshake',
 84: 'playfight',
 85: 'playfight',
 86: 'hairtousle',
 87: 'hairtousle',
 90: 'bearhug',
 91: 'bearhug',
 92: 'handshake',
 93: 'handshake',
 94: 'duetdance',
 95: 'duetdance',
 96: 'sidehug',
 97: 'sidehug',
 100: 'duetbow',
 101: 'duetbow',
 102: 'hug',
 103: 'fist_bump',
 104: 'fist_bump',
 252: 'fortune_pony_carry'}
_NAMES = {'bearhug': '熊抱',
 'carry': '背背',
 'duetbow': '双人鞠躬',
 'duetbownfc': '双人鞠躬',
 'duetdance': '双人舞',
 'fist_bump': '碰拳',
 'fortune_pony_carry': '骑小马',
 'hairtousle': '摸头',
 'hand_hold': '牵手',
 'handshake': '握手',
 'high_five': '击掌',
 'hug': '拥抱',
 'playfight': '打闹',
 'princesscarry': '公主抱',
 'revolvingdance': '转圈舞',
 'secrethandshake': '秘密握手',
 'sidehug': '侧抱',
 'whisper': '说悄悄话'}
_PAIRS = {'bearhug': ('BearHugGive', 'BearHugTake'),
 'carry': ('', 'JumpOnShoulders_1'),
 'duetbow': ('DuetBowGive', 'DuetBowTake'),
 'duetbownfc': ('DuetBowNFCGive', 'DuetBowNFCTake'),
 'duetdance': ('DuetDanceGive', 'DuetDanceTake'),
 'fist_bump': ('FistBumpGive', 'FistBumpTake'),
 'hairtousle': ('HairTousleGive', 'HairTousleTake'),
 'hand_hold': ('', ''),
 'handshake': ('HandShakeGive', 'HandShakeTake'),
 'high_five': ('HighFiveGive', 'HighFiveTake'),
 'hug': ('HugTakeF', 'HugGiveF'),
 'playfight': ('PlayfightGive', 'PlayfightTake'),
 'princesscarry': ('', 'PrincessCarryJumpup'),
 'revolvingdance': ('RevolvingDanceGive', 'RevolvingDanceTake'),
 'secrethandshake': ('SecretHandshakeGive', 'SecretHandshakeTake'),
 'sidehug': ('SideHugGive', 'SideHugTake'),
 'whisper': ('WhisperGive', 'WhisperTake')}
_EXACT = {61: ('SideHugGive', 'SideHugTake', b'\x00\x00\x00'),
 72: ('RevolvingDanceGive', 'RevolvingDanceTake', b'\x01\x01\x00'),
 74: ('SecretHandshakeGive', 'SecretHandshakeTake', b'\x00\x00\x00'),
 86: ('HairTousleTake', 'HairTousleGive', b'\x00\x00\x00'),
 87: ('HairTousleTake_2', 'HairTousleGive_2', b'\x00\x00\x01')}

_ALIASES = {
    "hand_hold": ("牵手", "拉手", "牵牵", "handhold"),
    "hug": ("拥抱", "抱抱", "抱一下"),
    "high_five": ("击掌", "highfive"), "fist_bump": ("碰拳", "fistbump"),
    "carry": ("背背", "肩背", "背一下"), "hairtousle": ("摸头", "揉头"),
    "playfight": ("打闹",), "bearhug": ("熊抱",), "duetdance": ("双人舞",),
    "handshake": ("握手",), "sidehug": ("侧抱", "侧面拥抱"),
    "princesscarry": ("公主抱", "摇篮抱"), "duetbow": ("双人鞠躬", "鞠躬"),
    "duetbownfc": ("NFC双人鞠躬",), "whisper": ("说悄悄话", "悄悄话"),
    "revolvingdance": ("转圈舞", "旋转舞"), "secrethandshake": ("秘密握手",),
    "fortune_pony_carry": ("骑小马",),
}
# These choose a representative existing code, NOT a relationship level.
_PREFERRED = {family: min(c for c, f in _IDS.items() if f == family) for family in _NAMES}
_PREFERRED["hairtousle"] = 86  # explicit existing acceptance-pattern annotation
_DANCES = frozenset((56, 57, 94, 95, 70, 71, 76, 77, 72, 73, 78, 79))
_CARRIES = frozenset((40, 41, 63, 64))
_CAPTURE_FAMILIES = frozenset(("hand_hold", "hug", "bearhug", "carry"))
# Exact local capture annotation in social_accept.py:52-54: type 86 has the
# base HairTousle pair/tail 0; type 87 has both _2 names and final bounded code 1,
# explicitly recorded as level 2. Other code ordering/shared family pairs do NOT
# establish relationship levels, legacy aliases, or native-equivalent variants.
_LEVELS = {86: 1, 87: 2}
_LEVEL_EVIDENCE = "cn_capture_annotation_1031_0358_hairtousle_86_87"
_LEVEL_UNKNOWN = "等级映射尚未核实；可用动作名称选择默认版本，或使用专家互动编号。"

ACTION_CODES = tuple(sorted(_IDS))
OFFER_IDS = MappingProxyType(_IDS)
OFFER_KINDS = MappingProxyType(_NAMES)


# Frozen public fields from native /account/get_relationship_abilities, 2026-11-03.
# Only the existing non-resource allowlist is embedded. No runtime fetching.
# code -> ability, offer clip, local clip, remote clip, loop, loop-exit, particle, canonical raw.
_NATIVE_CONFIG = MappingProxyType({2: ('hand_hold', 'OfferHand', '', '', True, False, '', 0),
 3: ('hug', 'OfferHug', 'HugGiveF', 'HugTakeF', False, False, '', 0),
 4: ('high_five', 'OfferHighFive', 'HighFiveGive', 'HighFiveTake', False, False, '', 0),
 5: ('fist_bump', 'OfferFistBump', 'FistBumpGive', 'FistBumpTake', False, False, '', 0),
 26: ('hug', 'OfferHug', 'CineDeathStormEndAvatarEmbrace', 'CineDeathStormEndAvatarWhiteEmbrace', True, True, '', 0),
 35: ('high_five_2', 'OfferHighFive', 'HighFiveGive', 'HighFiveTake', False, False, 'Emote2', 0),
 36: ('hug_2', 'OfferHug', 'HugGiveF', 'HugTakeF', False, False, 'Emote2', 0),
 37: ('fist_bump_2', 'OfferFistBump', 'FistBumpGive', 'FistBumpTake', False, False, 'Emote2', 0),
 40: ('carry', 'OfferCarry', '', '', False, False, '', 0),
 41: ('carry_2', 'OfferCarry', '', '', False, False, '', 0),
 42: ('high_five_3', 'OfferDoubleFive', 'DoubleFiveGive', 'DoubleFiveTake', False, False, '', 0),
 43: ('high_five_4', 'OfferDoubleFive', 'DoubleFiveGive', 'DoubleFiveTake', False, False, 'Emote2', 0),
 44: ('hairtousle', 'OfferHairTousle', 'HairTousleTake', 'HairTousleGive', False, False, '', 0),
 45: ('hairtousle_3', 'OfferHairTousle_2', 'HairTousleGive_2', 'HairTousleTake_2', False, False, '', 0),
 46: ('playfight', 'OfferPlayfight', 'PlayfightTake', 'PlayfightGive', False, False, '', 0),
 47: ('playfight_3', 'OfferPlayfight_2', 'PlayfightTake_2', 'PlayfightGive_2', False, False, '', 0),
 49: ('bearhug', 'OfferBearHug', 'BearHugTake', 'BearHugGive', False, False, '', 0),
 50: ('bearhug_3', 'OfferBearHug_2', 'BearHugTake_2', 'BearHugGive_2', False, False, '', 0),
 56: ('duetdance', 'OfferDuetDance', 'DuetDanceTake', 'DuetDanceGive', True, True, '', 0),
 57: ('duetdance_3', 'OfferDuetDance_2', 'DuetDanceTake_2', 'DuetDanceGive_2', True, True, '', 0),
 58: ('handshake', 'OfferHandShake', 'HandShakeTake', 'HandShakeGive', True, False, '', 0),
 59: ('handshake_3', 'OfferHandShake_2', 'HandShakeTake_2', 'HandShakeGive_2', True, False, '', 0),
 61: ('sidehug', 'OfferSideHug', 'SideHugTake', 'SideHugGive', False, False, '', 0),
 62: ('sidehug_3', 'OfferSideHug_2', 'SideHugTake_2', 'SideHugGive_2', False, False, '', 0),
 63: ('princesscarry', 'OfferPrincessCarry', 'PrincessCarryTake', 'PrincessCarryGive', False, False, '', 0),
 64: ('princesscarry_2', 'OfferPrincessCarry_2', 'PrincessCarryTake_2', 'PrincessCarryGive_2', False, False, '', 0),
 65: ('duetbow', 'OfferDuetBow', 'DuetBowTake', 'DuetBowGive', False, False, '', 0),
 66: ('duetbow_3', 'OfferDuetBow_2', 'DuetBowTake_2', 'DuetBowGive_2', False, False, '', 0),
 67: ('hug_3', 'OfferLongHug', 'LongHugTake', 'LongHugGive', True, True, '', 0),
 68: ('duetbownfc', 'OfferDuetBowNFC', 'DuetBowNFCTake', 'DuetBowNFCGive', False, False, '', 0),
 69: ('duetbownfc_2', 'OfferDuetBowNFC_2', 'DuetBowNFCTake_2', 'DuetBowNFCGive_2', False, False, '', 0),
 70: ('whisper', 'OfferWhisper', 'WhisperTake', 'WhisperGive', True, True, '', 0),
 71: ('whisper_2', 'OfferWhisper', 'WhisperTake', 'WhisperGive', True, True, 'Emote2', 0),
 72: ('revolvingdance', 'OfferRevolvingDance', 'RevolvingDanceTake', 'RevolvingDanceGive', True, True, '', 0),
 73: ('revolvingdance_2', 'OfferRevolvingDance', 'RevolvingDanceTake', 'RevolvingDanceGive', True, True, 'Emote2', 0),
 74: ('secrethandshake', 'OfferSecretHandshake', 'SecretHandshakeTake', 'SecretHandshakeGive', False, False, '', 0),
 75: ('secrethandshake_2', 'OfferSecretHandshake', 'SecretHandshakeTake', 'SecretHandshakeGive', False, False, 'Emote2', 0),
 76: ('whisper_3', 'OfferWhisper_2', 'WhisperTake_2', 'WhisperGive_2', True, True, '', 0),
 77: ('whisper_4', 'OfferWhisper_2', 'WhisperTake_2', 'WhisperGive_2', True, True, 'Emote2', 0),
 78: ('revolvingdance_3', 'OfferRevolvingDance_2', 'RevolvingDanceTake_2', 'RevolvingDanceGive_2', True, True, '', 0),
 79: ('revolvingdance_4', 'OfferRevolvingDance_2', 'RevolvingDanceTake_2', 'RevolvingDanceGive_2', True, True, 'Emote2', 0),
 80: ('secrethandshake_3', 'OfferSecretHandshake_2', 'SecretHandshakeTake_2', 'SecretHandshakeGive_2', False, False, '', 0),
 81: ('secrethandshake_4', 'OfferSecretHandshake_2', 'SecretHandshakeTake_2', 'SecretHandshakeGive_2', False, False, 'Emote2', 0),
 84: ('playfight_2', 'OfferPlayfight', 'PlayfightTake', 'PlayfightGive', False, False, 'Emote2', 0),
 85: ('playfight_4', 'OfferPlayfight_2', 'PlayfightTake_2', 'PlayfightGive_2', False, False, 'Emote2', 0),
 86: ('hairtousle_2', 'OfferHairTousle', 'HairTousleTake', 'HairTousleGive', False, False, 'Emote2', 0),
 87: ('hairtousle_4', 'OfferHairTousle_2', 'HairTousleGive_2', 'HairTousleTake_2', False, False, 'Emote2', 0),
 90: ('bearhug_2', 'OfferBearHug', 'BearHugTake', 'BearHugGive', False, False, 'Emote2', 0),
 91: ('bearhug_4', 'OfferBearHug_2', 'BearHugTake_2', 'BearHugGive_2', False, False, 'Emote2', 0),
 92: ('handshake_2', 'OfferHandShake', 'HandShakeTake', 'HandShakeGive', True, False, 'Emote2', 0),
 93: ('handshake_4', 'OfferHandShake_2', 'HandShakeTake_2', 'HandShakeGive_2', True, False, 'Emote2', 0),
 94: ('duetdance_2', 'OfferDuetDance', 'DuetDanceTake', 'DuetDanceGive', True, True, 'Emote2', 0),
 95: ('duetdance_4', 'OfferDuetDance_2', 'DuetDanceTake_2', 'DuetDanceGive_2', True, True, 'Emote2', 0),
 96: ('sidehug_2', 'OfferSideHug', 'SideHugTake', 'SideHugGive', False, False, 'Emote2', 0),
 97: ('sidehug_4', 'OfferSideHug_2', 'SideHugTake_2', 'SideHugGive_2', False, False, 'Emote2', 0),
 100: ('duetbow_2', 'OfferDuetBow', 'DuetBowTake', 'DuetBowGive', False, False, 'Emote2', 0),
 101: ('duetbow_4', 'OfferDuetBow_2', 'DuetBowTake_2', 'DuetBowGive_2', False, False, 'Emote2', 0),
 102: ('hug_4', 'OfferLongHug', 'LongHugTake', 'LongHugGive', True, True, 'Emote2', 0),
 103: ('fist_bump_3', 'OfferFistBump', 'FistBumpTakeLv2', 'FistBumpGiveLv2', False, False, '', 0),
 104: ('fist_bump_4', 'OfferFistBump', 'FistBumpTakeLv2', 'FistBumpGiveLv2', False, False, 'Emote2', 0)})
_PARTICLE_CODES = MappingProxyType({'': 0, 'none': 0, 'Emote2': 1, 'Emote3': 2, 'Emote4': 4, 'max': 8})
# These complete tuples retain explicit captures / already-working behavior.
# They are NEVER spread to neighboring variants or assembled as a cross-product.
_PRESERVED_ACCEPT = {
    2: ('', '', (0, 0, 0)),
    3: ('HugTakeF', 'HugGiveF', (0, 0, 0)),
    40: ('', 'JumpOnShoulders_1', (0, 0, 0)),
    41: ('', 'JumpOnShoulders_1', (0, 0, 0)),
    49: ('BearHugGive', 'BearHugTake', (0, 0, 0)),
    56: ('DuetDanceGive', 'DuetDanceTake', (1, 1, 0)),
    61: ('SideHugGive', 'SideHugTake', (0, 0, 0)),
    63: ('', 'PrincessCarryJumpup', (0, 0, 0)),
    64: ('', 'PrincessCarryJumpup', (0, 0, 0)),
    72: ('RevolvingDanceGive', 'RevolvingDanceTake', (1, 1, 0)),
    74: ('SecretHandshakeGive', 'SecretHandshakeTake', (0, 0, 0)),
    86: ('HairTousleTake', 'HairTousleGive', (0, 0, 0)),
    87: ('HairTousleTake_2', 'HairTousleGive_2', (0, 0, 1)),
}
_SPECIAL_CODES = frozenset((2, 40, 41, 63, 64))
assert set(_NATIVE_CONFIG) == set(_IDS) - {252}


def _tuple_record(value):
    return {'animation_a': value[0], 'animation_b': value[1], 'tail': list(value[2])}


def _entry(code):
    family = _IDS[code]
    row = _NATIVE_CONFIG.get(code)
    supported = row is not None
    legacy_kind = 'hold' if code == 2 else 'carry' if code in _CARRIES else 'dance' if code in _DANCES else 'once'
    if family == 'fortune_pony_carry':
        legacy_kind = 'carry'
    profile = ('unsupported' if row is None else 'hold' if code == 2 else
               'carrier' if code in _CARRIES else 'loop' if row[4] else 'once')
    config_tuple = None if row is None else (row[2], row[3], (int(row[4]), int(row[5]), _PARTICLE_CODES[row[6]]))
    pairs, pair_evidence = [], []
    preserved = _PRESERVED_ACCEPT.get(code)
    if preserved is not None:
        pairs.append(_tuple_record(preserved))
        pair_evidence.append('existing_variant_compatibility' if code in (41, 63, 64) else
                             'cn_live_duet_verified' if code == 56 else 'cn_exact_capture_annotation')
    # Hold/carry have dedicated native producers; their config siblings do not
    # replace the observed empty/jump RPC tuple. In particular hold is000, not100.
    if config_tuple is not None and code not in _SPECIAL_CODES:
        candidate = _tuple_record(config_tuple)
        if candidate not in pairs:
            pairs.append(candidate)
            pair_evidence.append('cn_native_generic_local_remote_config')
    first = pairs[0] if pairs else None
    variant = {'classification': ('verified_level' if code in _LEVELS else
                'special' if family in ('duetbownfc', 'fortune_pony_carry') else
                'default_level_unverified' if code == _PREFERRED[family] else 'unclassified'),
               'expert_code': code, 'legacy': None,
               'level_evidence': _LEVEL_EVIDENCE if code in _LEVELS else None}
    return {'code': code, 'canonical': family + ':' + str(code), 'family': family,
            'name': _NAMES[family], 'aliases': list((family,) + _ALIASES[family]),
            'level': _LEVELS.get(code), 'level_status': 'verified' if code in _LEVELS else 'unknown',
            'selectable': supported and (code in _LEVELS or code == _PREFERRED[family]),
            'variant': variant, 'kind': legacy_kind, 'accepted_profile': profile,
            'supported': supported,
            'reason': 'candidate_active_invite_not_live_verified' if supported else 'acceptance_pattern_unknown',
            'offer_animation': row[1] if row is not None else None,
            'is_loop_anim': bool(row[4]) if row is not None else False,
            'has_loop_exit_anim': bool(row[5]) if row is not None else False,
            'particle_code': _PARTICLE_CODES[row[6]] if row is not None else None,
            'config_accept_pair': _tuple_record(config_tuple) if config_tuple is not None else None,
            'native_config_variant': None if row is None else {
                'ability': row[0], 'offer_animation': row[1], 'local_animation': row[2],
                'remote_animation': row[3], 'canonical_level_raw': row[7], 'particle_symbol': row[6]},
            # Wire A belongs to the first/offerer entity, not whichever name ends Give.
            'give_animation': (first['animation_a'] or None) if first is not None else None,
            'animation_role_verified': True, 'role_verified': True,
            'animation_role_evidence': 'cn_native_event_23188f0_consumer_14ed400',
            'duration': 1.2 if profile == 'once' else None,
            'duration_meaning': 'existing_once_partner_link_window_not_native_clip_length',
            'accept_pairs': pairs, 'accept_pair_evidence': pair_evidence,
            'acceptance_pattern_evidence': 'cn_native_config_and_explicit_complete_tuples' if supported else 'missing_acceptance_pattern',
            'initiation_live_verified': False, 'gift_or_unlock': False}


def catalog():
    """Return fresh JSON-safe entries; no live or permission state is implied."""
    return [_entry(code) for code in ACTION_CODES]


def resolve_action(value):
    """Resolve strict code, canonical family:code, or exact bounded alias.

    Generic aliases select a representative code only; they do not assert a
    level, ownership, unlocked relationship, or observed active-invite success.
    """
    if type(value) is int:
        code = value
    elif type(value) is str and 0 < len(value) <= 64 and all(ord(c) >= 32 for c in value):
        text = value.strip().casefold()
        if re.fullmatch(r"[0-9]{1,3}", text):
            code = int(text)
        elif re.fullmatch(r"[a-z_]+:[0-9]{1,3}", text):
            family, number = text.split(":")
            code = int(number)
            if _IDS.get(code) != family:
                raise ValueError("social_action_unknown")
        else:
            family = next((f for f, aliases in _ALIASES.items()
                           if text in tuple(a.casefold() for a in (f,) + aliases)), None)
            if family is None:
                raise ValueError("social_action_unknown")
            code = _PREFERRED[family]
    else:
        raise ValueError("social_action_invalid")
    if code not in _IDS:
        raise ValueError("social_action_unknown")
    return _entry(code)


def _explicit_code(value):
    return type(value) is int or (type(value) is str and
            re.fullmatch(r"(?:[0-9]{1,3}|[a-z_]+:[0-9]{1,3})", value.strip().casefold()) is not None)


def select_action(family_or_alias, level=None):
    """Select a known grade or a family's existing default without guessing.

    No level means the existing default, NOT an assertion that it is native L1.
    Explicit raw/canonical codes remain available for expert compatibility.
    Unknown explicit grades never silently downgrade to a default or a neighbor.
    """
    if level is not None and (type(level) is not int or not 1 <= level <= 9):
        raise ValueError("social_level_invalid")
    entry = resolve_action(family_or_alias)
    if level is None:
        return entry
    if _explicit_code(family_or_alias):
        if entry["level"] == level and entry["supported"]:
            return entry
        raise ValueError("social_level_unknown")
    for code, known_level in _LEVELS.items():
        if _IDS[code] == entry["family"] and known_level == level:
            result = _entry(code)
            if result["selectable"]:
                return result
    raise ValueError("social_level_unknown")


def display_catalog():
    """One user-facing row per family; only proven grades appear in levels.

    Unknown same-family codes are retained as expert_codes, not duplicate menu
    entries. Equal animation names are not proof of interchangeable abilities.
    """
    rows = []
    for family in sorted(_NAMES):
        default = _entry(_PREFERRED[family])
        codes = [code for code in ACTION_CODES if _IDS[code] == family]
        levels = [{"level": _LEVELS[code], "code": code, "selectable": _entry(code)["selectable"]}
                  for code in codes if code in _LEVELS]
        rows.append({"family": family,
                     "name": "NFC双人鞠躬" if family == "duetbownfc" else _NAMES[family],
                     "aliases": list((family,) + _ALIASES[family]),
                     "levels": sorted(levels, key=lambda item: item["level"]),
                     "default_code": default["code"], "default_level": default["level"],
                     "selectable": default["selectable"],
                     "unknownVariantCount": sum(code not in _LEVELS for code in codes),
                     "expert_codes": list(codes),
                     "level_unknown_reason": _LEVEL_UNKNOWN if any(code not in _LEVELS for code in codes) else None})
    return rows
