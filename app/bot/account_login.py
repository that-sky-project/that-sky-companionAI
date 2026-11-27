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
import account_preferences
from xg_auth_client import Client, AuthError, atomic, cache_update, cache_delete, cache_is_deleted, cache_pending_deletions, cache_mark_delete_pending, cache_finish_delete


AUTH_ERROR_MESSAGES = {
    "account_bound_elsewhere": "这个游戏号已在另一位使用者的小光里登录。请让对方在小光账号列表里点‘删除’后再登录。（错误码：account_bound_elsewhere）",
    "account_identity_changed": "本次返回的游戏账号身份与原绑定不一致，已停止更新；请确认登录的是正确游戏账号。",
    "account_deleted": "这个账号已被删除，请重新登录。",
    "invalid_account_id": "账号标识无效，请刷新账号列表后重试。",
    "invalid_delete_receipt": "服务返回的删除确认与所选账号不一致，本地账号未清理。",
    "delete_confirmation_required": "删除游戏账号需要二次确认。",
    "account_delete_local_failed": "远端删除已确认，但本地清理未完成；请再次删除同一账号以完成清理。",
    "password_required": "请填写游戏邮箱密码。",
    "invalid_credentials": "游戏邮箱或密码不正确，请检查后重试。",
    "invalid_password": "游戏密码不正确或格式无效，请检查后重试。",
    "invalid_email": "游戏邮箱格式不正确，请检查后重试。",
    "account_locked": "游戏账号已被锁定，请先在官方客户端处理。",
    "account_banned": "游戏账号被限制登录，请先在官方客户端确认账号状态。",
    "renew_in_progress": "续期仍在处理中，请稍后刷新账号列表，避免重复点击。",
    "renew_failed": "续期未完成，请重新登录该账号。",
    "account_not_found": "账号已不存在，请刷新账号列表。",
    "not_found": "没有找到该账号，请刷新列表后重新登录。",
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
    "too_many_pending_logins": "已有 3 个未结束的登录任务。请先完成已有验证，或等旧任务过期释放名额后再试（等待验证任务有效期为 30 分钟）。重启软件或清缓存不会清除这些任务。（错误码：too_many_pending_logins）",
    "rate_limited": "请求过于频繁，请稍候再试，先不要连续发送或提交验证码。（错误码：rate_limited）",
    "login_cancelled": "这次登录任务已结束，请重新发起登录。（错误码：login_cancelled）",
    "upstream_unavailable": "登录服务暂时未能完成请求，请稍后再试。",
    "device_verification_required": "登录服务要求设备安全验证，请先在官方客户端完成验证后再试。",
    "device_context_missing": "本次登录的设备状态缺失，请重新发起登录。",
    "login_failed": "本次登录未成功，登录服务未返回更具体的原因。请核对当前登录方式的账号信息，并保留错误码排查。",
}


def error_result(code, fallback="操作未完成，请保留错误码排查。"):
    if not isinstance(code, str) or re.fullmatch(r"[a-z][a-z0-9_]{0,79}", code) is None:
        code = "login_failed"
    message = AUTH_ERROR_MESSAGES.get(code, fallback)
    if "错误码：" not in message and code != "account_deleted":
        message += "（错误码：" + code + "）"
    return {"ok": False, "error": code, "code": code, "message": message}


def active_id():
    try:
        return xg_paths.ACTIVE_ACCOUNT.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def view(account):
    remark = account_preferences.nickname(account.get("id", ""))
    return {**account, "alias": remark or account.get("alias", ""),
            "nickname": remark, "service_alias": account.get("alias", ""),
            "active": account.get("id") == active_id(), "remember": True,
            "created": time.strftime("%m-%d %H:%M", time.localtime(account.get("created_at") or 0))}


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
        # A rejected submission can retain the original SMS challenge. That
        # is a retryable failure, not evidence of a new verification step.
        # Explicit upload_sms challenges above still use the verification UI.
        error = job.get("error")
        kind = challenge.get("kind")
        if error and (kind in ("sms", "sms_code") or (state == "needs_code" and not kind)):
            if not isinstance(error, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", error):
                error = "login_failed"
            out = error_result(error, "本次登录未完成，登录服务未返回更具体的原因。请保留错误码排查。")
            out["pending"] = job["job_id"]
            return out
        return {"ok": False, "pending": job["job_id"], "message": "请填写短信验证码",
                "verify": {"ticket": job["job_id"], "kind": challenge.get("kind", "sms")}}
    if state == "running":
        return {"ok": False, "pending": job["job_id"], "message": "登录仍在处理中，请稍后查看"}
    error = job.get("error") or "login_failed"
    return error_result(error, "登录未完成，请检查当前登录方式的账号信息并保留错误码排查。")


def cmd_list(_req):
    pending = cache_pending_deletions()
    remote_error = None
    try:
        out = Client().request("GET", "/v1/accounts")
    except AuthError as exc:
        if not pending:
            raise
        out = {"accounts": []}
        remote_error = exc.code
    for row in out["accounts"]:
        try:
            cache_update(row["id"], {k: row[k] for k in ("alias", "platform", "status", "created_at", "last_renew_at") if k in row})
        except AuthError as exc:
            if exc.code != "account_deleted":
                raise
    # A delayed list reply from before deletion must not re-display the old ID.
    pending = cache_pending_deletions()
    rows = [view(a) for a in out["accounts"] if not cache_is_deleted(a["id"]) and a["id"] not in pending]
    rows.extend({"id": identity, "alias": "本地清理待重试", "status": "cleanup_pending",
                 "cleanup_pending": True, "active": False, "remember": False}
                for identity in pending)
    result = {"ok": True, "active": active_id() if active_id() not in pending else "", "accounts": rows}
    if remote_error:
        result.update(partial=True, error=remote_error, message="远端列表暂不可用；仍可重试已确认删除账号的本地清理。")
    return result


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
        v = OWNER_FILE.read_text(encoding="utf-8").strip()
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
    OWNER_FILE.write_text(uid + "\n", encoding="utf-8")
    return {"ok": True, "owner": uid, "message": "已设为哥哥"}



# Friend/owner commands are appended from the original 1.0.5 implementation by
# the integration script. They use only the current runtime session, not SDK credentials.


def cmd_remark(req):
    identity = str(req.get("account") or "")
    row = account_preferences.known_account(identity)
    account_preferences.save(identity, req.get("nickname", ""))
    safe = {k: row.get(k) for k in ("id", "alias", "platform", "status", "created_at")}
    return {"ok": True, "account": view(safe), "message": "备注已保存" if req.get("nickname", "").strip() else "已恢复原名称"}


def cmd_renew(req):
    identity = str(req.get("account") or "")
    account_preferences.known_account(identity)
    row = Client().renew(identity)
    safe = {k: row.get(k) for k in ("id", "alias", "platform", "status", "created_at")}
    return {"ok": True, "account": view(safe), "message": "续期成功，下次上线会使用新的登录状态。"}


def cmd_delete(req):
    identity = req.get("account")
    if req.get("confirm") is not True:
        raise AuthError("delete_confirmation_required")
    # Also allow retry after a partial local commit, but never an arbitrary ID.
    pending = identity in cache_pending_deletions()
    if not pending and not cache_is_deleted(identity):
        account_preferences.known_account(identity)
    # A persisted pending ID is proof of the previous remote confirmation.
    # Retrying local-only cleanup must also work while the service is offline.
    receipt = {"not_found": False} if pending else Client().delete(identity)
    try:
        if not pending:
            cache_mark_delete_pending(identity)
        cache_delete(identity)
        if active_id() == identity:
            atomic(xg_paths.ACTIVE_ACCOUNT, b"")
        account_preferences.remove(identity)
        cache_finish_delete(identity)
    except (OSError, ValueError, AuthError):
        out = error_result("account_delete_local_failed")
        out.update(remote_deleted=True, account=identity)
        return out
    return {"ok": True, "deleted": True, "account": identity, "active": active_id(),
            "remote_not_found": receipt["not_found"],
            "message": "已删除该游戏账号及本机登录缓存、备注；录制目录、人设和聊天记忆已保留。"}


COMMANDS = {"delete": cmd_delete, "remark": cmd_remark, "renew": cmd_renew, "list": cmd_list, "email": cmd_email, "send": cmd_send, "verify": cmd_verify,
            "confirm": cmd_confirm, "select": cmd_select, "resume": cmd_resume, "friends": cmd_friends, "owner": cmd_owner}


def main(argv):
    try:
        cmd = argv[1] if len(argv) > 1 else "list"
        req = json.loads(sys.stdin.read() or "{}")
        out = COMMANDS[cmd](req)
    except AuthError as exc:
        code = "login_expired" if exc.code == "not_found" and cmd in ("confirm", "verify", "resume") else exc.code
        out = error_result(code)
    except ValueError as exc:
        out = {"ok": False, "error": "invalid_input", "message": str(exc) if cmd == "remark" else "账号信息不完整，请刷新列表后重试"}
    except Exception:
        out = {"ok": False, "error": "request_failed", "message": "操作未完成"}
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
