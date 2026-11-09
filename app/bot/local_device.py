"""The private broker owns device registration; clients retain no device keys."""
from xg_auth_client import read_config, Client, AuthError


def status(platform="android"):
    try:
        read_config()
        configured = True
    except AuthError:
        configured = False
    return {"ok": configured, "registered": configured, "managed": True, "platform": platform,
            "message": "设备由私有服务在登录时注册" if configured else "请先配置私有登录服务"}


def status_all():
    a, i = status("android"), status("ios")
    return {"ok": a["ok"], "current": a, "android": a, "ios": i, "devices": []}


def ensure(platform="android", force=False):
    Client().request("GET", "/v1/health")
    return status(platform)


def register(platform="android"):
    return ensure(platform)


def pool_summary():
    return {"total": 0, "android": 0, "ios": 0, "managed": True}


def key_for_device(_identity):
    raise AuthError("device_keys_are_server_private")
