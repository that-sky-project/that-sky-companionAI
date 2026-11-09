# -*- coding: utf-8 -*-
"""好友管理：概览列表（get_all_friend_gist）/ 改名（set_friend_name）/ 批量软删。
移植 SkyHeartApi / SkyGameInfoApi 国服分支。
"""
from __future__ import annotations

from .skyapi import SkyApiError
from .skycode import try_uuid_to_short_code

P_FRIEND_GIST = "/service/relationship/api/v1/get_all_friend_gist"
P_SET_FRIEND_NAME = "/account/set_friend_name"
P_BATCH_SOFT_DELETE = "/service/relationship/api/v1/batch_set_friend_soft_delete"

# ── 加好友（邀请令牌链，2026-10 线上实测）──────────────────────────────
# 不在游戏里、不需要同房、不需要同时在线、不花任何资源：
#   发起方 POST /account/create_invite {"invite_nickname": 备注}  → invite_token_id（= 好友码）
#   接收方 POST /account/accept_invite {"token_id": 码, "invite_nickname": 备注}
#          → {"invite_accepted": true, "invite_result": "ok", "invite_friend_id": 发起方 uid}
#   关系立即双向落地（实测 A 193→194、B 24→25）。
# 坑：create_invite **必须**带 invite_nickname，缺了或带别的字段名一律 HTTP 404；
#     check_invite 用发起方自己查会返回 invite_result="own_invite"（不是没生成）；
#     /account/code/redeem 不存在（HTTP 404）。
# ⚠ 更正（2026-10-02）：`/account/code/verify` **不是**这条链的一部分，但它是
#   **短码 → uuid 的服务端查表接口**（玩家手里的 12 位码走它，见 qfsky/skycode.py）。
#   之前记成"不在链上"是因为拿 uuid / 乱码去打，才得到 not_found；
#   真码实测 {"code":"AFMF-A04M-GDEJ"} → {"result":"ok","type":"i","code":"e1ce3396-..."}。
#   短码**不能**直接打 check_invite（服务端按 uuid 解析，短码 HTTP 500）。
#   所以加好友入口有两条：`invite_token_id`（本站生成）与**短码**（玩家持码，先 verify 换 uuid）。
P_CREATE_INVITE = "/account/create_invite"
P_ACCEPT_INVITE = "/account/accept_invite"
P_CHECK_INVITE = "/account/check_invite"
P_GET_INVITES = "/account/get_invites"
P_DELETE_INVITE = "/account/delete_invite"

# invite_result → 中文（线上见过的取值）
INVITE_ZH = {
    "ok": "可用",
    "own_invite": "这是自己生成的码",
    "not_found": "码不存在或已被使用",
    "expired": "码已过期",
    "accepted": "码已被使用",
    "friend_exists": "已经是好友了",
}


def invite_reason(result: str) -> str:
    return INVITE_ZH.get(str(result or ""), str(result or "未知"))


# 备注名硬上限（线上实测 21 字符起 HTTP 400）
NICK_MAX = 20

# 服端 HTTP 状态 → 管理员能直接照做的中文
_API_ZH = {
    400: "服务端不接受这次请求（HTTP 400）：要么备注名超长，"
         "要么这个机位的登录已失效（/service/* 对失效会话回 400 Invalid Request）",
    401: "这个机位的登录已经失效（HTTP 401），去机位列表重新登录再试",
    404: "服务端没有这个接口 / 参数形态不对（HTTP 404）",
    422: "网易出口限流中（HTTP 422）：同一出口 IP 短时间请求太多，"
         "等几秒再点一次就好；重试已自动做过 5 次，连着 422 说明限流窗口还没过",
    429: "请求太频繁，被光遇限流了（HTTP 429），等几秒再试",
}


def api_zh(e) -> str:
    """把 SkyApiError 翻成管理员能照做的中文（保留原始状态码便于对日志）。"""
    st = getattr(e, "status", None)
    base = _API_ZH.get(st) or "光遇接口报错"
    return "%s（原报错：sky api HTTP %s）" % (base, st)


def _post(client, path, body, **kw):
    """统一入口：把 SkyApiError 转成带中文解释的 RuntimeError，供路由回 400。"""
    try:
        return client.post(path, body, **kw)
    except SkyApiError as e:
        raise RuntimeError(api_zh(e)) from e

# gist 行里 id / 名字的可能键名（HeartTaskApi.kt:355-363 兼容序）
_ID_KEYS = ("friend_id", "id", "user_id", "uuid", "user_uuid")
_NAME_KEYS = ("nickname", "nick_name", "name", "display_name")


def _first(row: dict, keys) -> str:
    for k in keys:
        v = row.get(k)
        if v not in (None, ""):
            return str(v)
    return ""


def get_friend_gist(client) -> list[dict]:
    """好友概览：[{friend_id, nickname, sky_code, tier?, given?, recvd?}]。

    sky_code = friend_id 的 12 位短码（玩家自己看到的那个码，本地算）。
    玩家开口要的就是短码，uuid 给他也没用，所以列表直接带上。

    ★ 2026-10-04 起**叠加星盘的 given/recvd**（今日已送/已收心数）：
      `get_all_friend_gist` 只有 89 个好友 + `is_online`（实测 89/89 全 False，
      不可用），不带送礼计数；`get_friend_constellation`（星盘）才带
      `given`/`recvd`，但只有 69 个（它只含「有星盘关系」的）。
      两个都拉，**以 gist 为主键、把星盘的计数并进去** —— 这样列表既不缺人，
      又有「今天送过没有」的状态。星盘拉失败不影响主流程（计数留 None）。
    """
    resp = client.get_friends()
    data = resp.get("data") if isinstance(resp.get("data"), dict) else resp
    arr = data.get("friends") or data.get("gist") or []
    if not isinstance(arr, list):
        arr = []

    # 星盘：只取 friend_id → (given, recvd)
    counts: dict[str, tuple] = {}
    try:
        const = client.post("/service/relationship/api/v1/get_friend_constellation",
                            {"include_level_id": False}, include_level_id=False)
        for row in (const or {}).get("set_friend_statues") or []:
            if not isinstance(row, dict):
                continue
            fid = row.get("friend_id")
            if fid:
                counts[str(fid)] = (row.get("given"), row.get("recvd"))
    except Exception:  # noqa: BLE001 —— 星盘挂了也要能出好友列表
        pass

    out = []
    for row in arr:
        if not isinstance(row, dict):
            continue
        fid = _first(row, _ID_KEYS)
        if not fid:
            continue
        item = {"friend_id": fid, "nickname": _first(row, _NAME_KEYS) or "(未取到昵称)"}
        sc = try_uuid_to_short_code(fid)
        if sc:
            item["sky_code"] = sc
        tier = row.get("tier")
        if isinstance(tier, (int, float)) and not isinstance(tier, bool):
            item["tier"] = int(tier)
        g, rc = counts.get(fid, (None, None))
        if isinstance(g, int):
            item["given"] = g
        if isinstance(rc, int):
            item["recvd"] = rc
        out.append(item)

    # 星盘里有、gist 里没有的（少数）也补进来，别漏掉能送心的对象
    known = {x["friend_id"] for x in out}
    for fid, (g, rc) in counts.items():
        if fid in known:
            continue
        item = {"friend_id": fid, "nickname": "(未取到昵称)", "constellation_only": True}
        sc = try_uuid_to_short_code(fid)
        if sc:
            item["sky_code"] = sc
        if isinstance(g, int):
            item["given"] = g
        if isinstance(rc, int):
            item["recvd"] = rc
        out.append(item)
    return out


def set_friend_name(client, friend_id: str, name: str) -> None:
    """好友备注名。"""
    client.post(P_SET_FRIEND_NAME, {"friend": str(friend_id), "name": str(name)})


def soft_delete_friends(client, friend_ids: list[str], soft_deleted: bool = True) -> None:
    """批量（软）删除好友。"""
    ids = [str(x) for x in (friend_ids or []) if str(x).strip()]
    if not ids:
        raise RuntimeError("未选择要删除的好友")
    client.post(P_BATCH_SOFT_DELETE, {"friend_ids": ids, "soft_deleted": bool(soft_deleted)})


# ── 加好友 ──────────────────────────────────────────────────────────────

def _invite_row(row) -> dict:
    if not isinstance(row, dict):
        return {}
    return {
        "token_id": str(row.get("token_id") or ""),
        "nickname": str(row.get("nickname") or ""),
        "inviter_user_id": str(row.get("inviter_user_id") or ""),
        "friend_id": str(row.get("friend_id") or ""),
        "created": int(row.get("created") or 0),
    }


def _invites_of(resp) -> list[dict]:
    """从 create/delete/get_invites 的响应里取待接受邀请列表。"""
    arr = resp.get("invites") if isinstance(resp, dict) else None
    if not isinstance(arr, list):
        return []
    return [r for r in (_invite_row(x) for x in arr) if r.get("token_id")]


def create_invite(client, nickname: str) -> dict:
    """生成「好友码」。

    nickname = 发起方给对方的备注名（服务端强制要求，缺了直接 404）。
    线上实测边界（2026-10-02，`scratch/_probe_invite_422.py`）：
      · 1~20 字符 → 200；**21 字符起 → HTTP 400 `{}`**（上限就是 20，不是 40）
      · 含空格的备注（"给 对方"）→ 200 但**空回执 `{}`**，服务端静默不做
    返回 {"token_id", "nickname", "invites": [待接受邀请...]}。
    """
    name = str(nickname or "").strip()
    if not name:
        raise RuntimeError("请先填写给对方的备注名")
    if len(name) > NICK_MAX:
        raise RuntimeError("备注名最长 %d 个字（服务端硬上限，超了会被拒）" % NICK_MAX)
    if any(ch.isspace() for ch in name):
        raise RuntimeError("备注名不能带空格，服务端会直接无视这次生成")
    resp = _post(client, P_CREATE_INVITE, {"invite_nickname": name})
    token = str((resp or {}).get("invite_token_id") or "")
    if not token:
        # 200 但没 token：服务端要么静默拒绝（空格等），要么返回了别的形状
        raise RuntimeError("服务端没有给出好友码（备注名可能不被接受，换个纯文字短备注再试）")
    return {"token_id": token, "nickname": name, "invites": _invites_of(resp)}


def list_invites(client) -> list[dict]:
    """待接受的邀请（自己发出去的码）。"""
    return _invites_of(_post(client, P_GET_INVITES, {}))


def check_invite(client, token_id: str) -> dict:
    """查一个好友码还有没有效（接收方查；发起方自己查会得到 own_invite）。"""
    tok = str(token_id or "").strip()
    if not tok:
        raise RuntimeError("请先填写好友码")
    resp = _post(client, P_CHECK_INVITE, {"token_id": tok}) or {}
    return {
        "exists": bool(resp.get("invite_exists")),
        "result": str(resp.get("invite_result") or ""),
        "zh": invite_reason(resp.get("invite_result")),
    }


def delete_invite(client, token_id: str) -> dict:
    """撤销自己发出去、还没被接受的邀请。"""
    tok = str(token_id or "").strip()
    if not tok:
        raise RuntimeError("缺少好友码")
    resp = _post(client, P_DELETE_INVITE, {"token_id": tok}) or {}
    return {"deleted": bool(resp.get("invite_deleted")), "invites": _invites_of(resp)}


def resolve_friend_code(client, code: str, nickname: str = "") -> dict:
    """把玩家给的「好友码」变成一条能直接落地的加好友动作。

    玩家手里只有 12 位短码（长 uuid 他们用不了）。短码**不是** invite_token_id
    （实测：自造 token 编码成短码再 verify → not_found），它是**玩家账号的码**。
    所以分派：

      输入 = 短码 → `code/verify` 换出对方 uuid
                   → 拿这个 uuid 打 accept_invite（对方的邀请令牌就用 uuid 当 token，
                     实测 check_invite{"token_id": <对方uuid>} → invite_exists:true / ok）
      输入 = 32 位 uuid → 同样直接 accept（对方自己的码形态）
      输入 = invite_token_id（create_invite 发的） → 原样走 accept

    返回归一化结果（形状与 accept_invite 一致）+ kind / resolved_uuid / code。
    """
    from .skycode import code_to_uuid, is_short_code, normalize_friend_code

    raw = str(code or "").strip()
    if not raw:
        raise RuntimeError("请先填写好友码")

    norm = normalize_friend_code(raw)
    if is_short_code(norm):
        info = code_to_uuid(client, norm)
        if not info.get("found"):
            return {
                "accepted": False, "result": info.get("result") or "not_found",
                "friend_id": "", "nickname": str(nickname or ""),
                "kind": "short_code", "resolved_uuid": "", "code": norm,
                "zh": info.get("zh") or "查不到这个码",
            }
        uuid = info["uuid"]
        r = accept_invite(client, uuid, nickname)
        r["kind"] = "short_code"
        r["resolved_uuid"] = uuid
        r["code"] = norm
        if not r.get("accepted"):
            r.setdefault("zh", invite_reason(r.get("result")))
        return r

    # uuid（玩家码的 32 位形态）或 invite_token_id：都直接交给 accept_invite
    r = accept_invite(client, norm, nickname)
    r["kind"] = "uuid" if len(norm.replace("-", "")) == 32 else "invite"
    r["resolved_uuid"] = norm if r["kind"] == "uuid" else (r.get("friend_id") or "")
    return r


def accept_invite(client, token_id: str, nickname: str = "") -> dict:
    """接受别人的好友码 → 立刻成为好友（双向，立即生效）。

    返回 {"accepted", "result", "friend_id", "nickname"}。
    """
    tok = str(token_id or "").strip()
    if not tok:
        raise RuntimeError("请先填写好友码")
    name = str(nickname or "").strip()
    if len(name) > NICK_MAX:
        raise RuntimeError("备注名最长 %d 个字（服务端硬上限）" % NICK_MAX)
    body = {"token_id": tok}
    if name:
        body["invite_nickname"] = name
    resp = _post(client, P_ACCEPT_INVITE, body) or {}
    return {
        "accepted": bool(resp.get("invite_accepted")),
        "result": str(resp.get("invite_result") or ""),
        "friend_id": str(resp.get("invite_friend_id") or ""),
        "nickname": name,
    }
