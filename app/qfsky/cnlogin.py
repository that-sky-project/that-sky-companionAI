"""Compatibility adapter. All NetEase login and renewal run privately."""
from xg_auth_client import Client, AuthError


LoginError = AuthError


def renew(credentials):
    identity = credentials.get("broker_account_id")
    if not identity:
        raise AuthError("private_account_reference_required")
    return Client().renew(identity)


def is_transient_login_error(error):
    return getattr(error, "code", "") in ("auth_service_unavailable", "renew_backoff", "retry_wait", "service_busy", "rate_limited")


def is_platform_login_block(error):
    return False


def is_platform_rest_down(error):
    return False
