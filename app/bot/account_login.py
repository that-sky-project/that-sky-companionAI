"""Keep the 1.0.5 web commands; delegate login to the private HTTPS service."""
from __future__ import annotations
import hashlib
import os
import re
from pathlib import Path
import json
import sys
import time

import xg_paths
from xg_auth_client import Client, AuthError, atomic, cache_update


AUTH_ERROR_MESSAGES = {
    "auth_service_not_configured": "请先配置私有登录服务",
    "unauthorized": "服务令牌无效或已撤销",
    "account_in_use": "该账号正在另一客户端使用",
    "verification_required": "请继续完成安全验证",
    "reauth_required": "账号需要重新完成登录验证",
    "auth_service_unavailable": "暂时连接不上私有登录服务",
    "login_expired": "这次验证请求已过期。请刷新账号列表；若还没有该账号，再重新登录。",
    "login_not_waiting": "这次验证请求的状态已变化，请刷新账号列表后查看结果。",
    "service_restarted": "登录服务刚刚重启，请重新登录。",
    "service_busy": "登录服务正忙，请稍后再试。",
    "too_many_verification_attempts": "本次验证尝试次数已用完，请重新发起登录。",
    "invalid_code": "请检查验证码格式后再试。",
    "confirmation_required": "请完成短信操作后点击确认。",
}


def active_id():
    try:
        return xg_paths.ACTIVE_ACCOUNT.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def view(account):
    return {**account, "active": account.get("id") == active_id(), "remember": True,
            "created": time.strftime("%m-%d %H:%M", time.localtime(account.get("created_at", 0)))}


def result(job, client):
    job = client.wait_job(job)
    state = job["status"]
    if state == "ready":
        acc = client.session(job["account_id"])
        return {"ok": True, "account": view({k: acc.get(k) for k in ("id", "alias", "platform", "status", "created_at")}),
                "remember": True, "message": "登录成功，已启用自动续期"}
    if state in ("needs_sms", "needs_code"):
        challenge = job.get("challenge", {})
        if challenge.get("kind") == "upload_sms":
            return {"ok": False, "risk": "sms_verify",
                    "message": ("短信验证还在同步，请稍等几秒再点确认。"
                                if job.get("error") == "verification_pending" else "请按提示完成网易安全验证"),
                    "verify": {"kind": "upload_sms", "ticket": job["job_id"], "number": challenge.get("number", ""),
                               "content": challenge.get("content", ""), "tips": "完成后点击确认"}}
        return {"ok": False, "pending": job["job_id"], "message": "请填写短信验证码",
                "verify": {"ticket": job["job_id"], "kind": challenge.get("kind", "sms")}}
    if state == "running":
        return {"ok": False, "pending": job["job_id"], "message": "登录仍在处理中，请稍后查看"}
    error = job.get("error") or "login_failed"
    return {"ok": False, "error": error, "message": AUTH_ERROR_MESSAGES.get(error, "登录未完成，请重试或完成账号验证")}


def cmd_list(_req):
    out = Client().request("GET", "/v1/accounts")
    for row in out["accounts"]:
        cache_update(row["id"], {k: row[k] for k in ("alias", "platform", "status", "created_at", "last_renew_at") if k in row})
    return {"ok": True, "active": active_id(), "accounts": [view(a) for a in out["accounts"]]}


def cmd_email(req):
    c = Client()
    # Password exists only in this local request. No raw password crosses the
    # service boundary or is saved to the local remember-password store.
    digest = req.get("password_md5")
    if digest is None:
        password = str(req.get("password", ""))
        if not password or len(password) > 1024:
            raise AuthError("password_required")
        digest = hashlib.md5(password.encode("utf-8")).hexdigest()
    job = c.request("POST", "/v1/login-jobs", {"method": "email", "email": req.get("email", ""),
                    "password_md5": digest, "platform": req.get("platform", "android")})
    return result(job, c)


def cmd_send(req):
    c = Client()
    job = c.wait_job(c.request("POST", "/v1/login-jobs", {"method": "phone", "mobile": req.get("mobile", ""),
                                                    "platform": req.get("platform", "android")}))
    if job["status"] in ("needs_code", "needs_sms"):
        ch = job.get("challenge", {})
        manual = {k: ch.get(k, "") for k in ("number", "content")} if ch.get("kind") == "upload_sms" else None
        return {"ok": True, "pending": job["job_id"], "manual": manual,
                "message": "请发送提示的验证短信" if manual else "验证码已发送"}
    return result(job, c)


def continue_result(identity, payload):
    c = Client()
    path = "/v1/login-jobs/" + str(identity)
    job = c.request("GET", path)
    if job.get("status") not in ("needs_code", "needs_sms"):
        return result(job, c)
    return result(c.request("POST", path + "/continue", payload), c)


def cmd_verify(req):
    return continue_result(req.get("pending", ""), {"code": str(req.get("code", ""))})


def cmd_confirm(req):
    return continue_result(req.get("ticket", ""), {"confirm": True})


def cmd_select(req):
    identity = str(req.get("account", ""))
    Client().session(identity)
    atomic(xg_paths.ACTIVE_ACCOUNT, (identity + "\n").encode())
    return {"ok": True, "active": identity, "message": "已切换账号"}


def cmd_resume(req):
    c = Client()
    return result(c.request("GET", "/v1/login-jobs/" + str(req.get("pending", ""))), c)


ROOT = xg_paths.DATA
OWNER_FILE = ROOT / "control/owner_uuid"      # 哥哥（主号）的用户 uuid：跟小光用哪个号无关，切号也认得
DEFAULT_OWNER = os.environ.get("XG_DEFAULT_OWNER") or ""          # 默认空：用户自己选哥哥
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def owner_uuid():
    try:
        v = OWNER_FILE.read_text().strip()
        return v if UUID_RE.fullmatch(v) else DEFAULT_OWNER
    except OSError:
        return DEFAULT_OWNER


def _active_friends():
    from qfsky import friends, store
    from qfsky.skyapi import SkyClient
    acc = next((a for a in store.list_accounts() if a.get("id") == active_id()), None)
    if acc is None:
        raise LookupError("当前没有选账号")
    c = SkyClient(acc["user_uuid"], acc["session"], acc["session2"], acc.get("data_version") or "")
    rows = []
    for r in friends.get_friend_gist(c):
        fid = r.get("friend_id") or r.get("id") or ""
        if fid:
            rows.append({"uuid": fid, "nickname": r.get("nickname") or r.get("name") or fid[:8]})
    return rows


def _friends_failure(exc, owner):
    from qfsky.skyapi import SkyApiError
    status = exc.status if isinstance(exc, SkyApiError) else None
    invalid_request = False
    if status == 400 and exc.path == "/service/relationship/api/v1/get_all_friend_gist":
        try:
            body = json.loads(exc.body)
            invalid_request = isinstance(body, dict) and body.get("status") == "Invalid Request"
        except (ValueError, TypeError):
            pass
    if invalid_request or status in (401, 403, 423):
        return {"ok": False, "owner": owner, "error": "session_maybe_stale",
                "recoverable": True, "http_status": status,
                "message": "好友请求被拒绝（HTTP %s），当前登录会话可能已失效或被另一次登录更新。"
                           "请先退出 MuMu 中的小光账号，再点“恢复会话并重试”。" % status}
    if status == 400:
        return {"ok": False, "owner": owner, "error": "friends_request_rejected",
                "recoverable": False, "http_status": 400,
                "message": "好友服务不接受本次请求（HTTP 400），尚未确认是会话问题；请保留错误信息排查。"}
    return {"ok": False, "owner": owner, "error": "friends_unavailable",
            "recoverable": False, "http_status": status,
            "message": "读取好友列表失败：" + str(exc)[:120]}


def cmd_friends(_req):
    """当前小光号的好友列表（哥哥置顶）；读取本身不自动续期。"""
    owner = owner_uuid()
    try:
        rows = _active_friends()
    except Exception as exc:
        return _friends_failure(exc, owner)
    for r in rows:
        r["owner"] = r["uuid"] == owner
    rows.sort(key=lambda r: (not r["owner"], r["nickname"]))
    return {"ok": True, "owner": owner, "ownerIsFriend": any(r["owner"] for r in rows), "friends": rows}


def cmd_owner(req):
    uid = str(req.get("uuid") or "")
    if not UUID_RE.fullmatch(uid):
        return {"ok": False, "message": "这个人无效"}
    try:
        if uid not in {r["uuid"] for r in _active_friends()}:
            return {"ok": False, "message": "只能把当前小光号的好友设为哥哥"}
    except Exception as exc:
        return {"ok": False, "message": "读不到好友列表：" + str(exc)[:120]}
    OWNER_FILE.parent.mkdir(parents=True, exist_ok=True)
    OWNER_FILE.write_text(uid + "\n")
    return {"ok": True, "owner": uid, "message": "已设为哥哥"}



# Friend/owner commands are appended from the original 1.0.5 implementation by
# the integration script. They use only the current runtime session, not SDK credentials.


COMMANDS = {"list": cmd_list, "email": cmd_email, "send": cmd_send, "verify": cmd_verify,
            "confirm": cmd_confirm, "select": cmd_select, "resume": cmd_resume, "friends": cmd_friends, "owner": cmd_owner}


def main(argv):
    try:
        cmd = argv[1] if len(argv) > 1 else "list"
        req = json.loads(sys.stdin.read() or "{}")
        out = COMMANDS[cmd](req)
    except AuthError as exc:
        code = "login_expired" if exc.code == "not_found" and cmd in ("confirm", "verify", "resume") else exc.code
        out = {"ok": False, "error": code, "message": AUTH_ERROR_MESSAGES.get(code, "操作未完成，请稍后重试")}
    except Exception:
        out = {"ok": False, "error": "request_failed", "message": "操作未完成"}
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
