"""Local account labels. Never change the game nickname or the service alias."""
import unicodedata

import xg_paths
from xg_auth_client import cache_get


def labels():
    value = xg_paths.read_json(xg_paths.CONTROL / "account_nicknames.json", {})
    return value if isinstance(value, dict) else {}


def nickname(identity):
    value = labels().get(str(identity), "")
    return value if isinstance(value, str) else ""


def known_account(identity):
    row = cache_get(str(identity))
    if not row:
        raise ValueError("没有这个账号，请先刷新账号列表")
    return row


def save(identity, value):
    known_account(identity)
    if not isinstance(value, str):
        raise ValueError("备注昵称必须是文字")
    # Reject control characters before trimming, including invisible bidi controls.
    if any(unicodedata.category(c).startswith("C") for c in value):
        raise ValueError("备注昵称不能包含换行、控制字符或不可见字符")
    value = unicodedata.normalize("NFC", value).strip()
    if len(value) > 40:
        raise ValueError("备注昵称最多 40 个字")
    current = labels()
    if value:
        current[str(identity)] = value
    else:
        current.pop(str(identity), None)
    xg_paths.write_json(xg_paths.CONTROL / "account_nicknames.json", current)
    return value
