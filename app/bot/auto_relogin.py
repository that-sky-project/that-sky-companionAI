import sys, json
from xg_auth_client import Client, AuthError
try:
    Client().renew(sys.argv[1])
    print(json.dumps({"ok": True}))
except AuthError as e:
    print(json.dumps({"ok": False, "error": e.code}))
