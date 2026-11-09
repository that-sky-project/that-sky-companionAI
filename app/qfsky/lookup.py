# -*- coding: utf-8 -*-
"""按好友码 / 邀请二维码查**他人**身高与装扮。

链路（2026-10-05 重做，全部线上实测；旧实现为什么必然失败见文末）：

    ① 12 位好友码（游戏内「创建好友码」→「复制分享此邀请码」那串）
         POST /account/code/verify {"code": "XXXX-XXXX-XXXX"}
         → {"result":"ok","type":"i","code":"<邀请令牌 uuid>"}
       ★ 实测：**12 位码就是邀请令牌的短码** —— 本地 uuid_to_short_code(令牌)
         算出来的那串，服务端 code/verify 认，回的就是同一个令牌 uuid。
         （旧注释写「短码 ≠ invite_token_id」是错的，见 skycode.py 的更正。）
    ② 借「查询背号」接受这个令牌 → 立刻成为好友
         POST /account/accept_invite {"token_id": "<令牌uuid>", "invite_nickname": "查装扮"}
         → {"invite_accepted": true, "invite_result": "ok",
            "invite_friend_id": "<对方玩家 uuid>"}
    ③ 拉**星盘快照**：一次调用带回**全部好友**的完整装扮，离线可查、不要求对方在线
         POST /service/relationship/api/v1/get_friend_constellation {}
         → {"set_friend_statues": [{friend_id, nickname, given, recvd,
                                    outfit:{wing/body/hair/…, scale, height, …}}, …]}
       按 ② 的 friend_id 取行 —— 那一行的 outfit 与 get_remote_outfit **完全同构**。
    ④ 解析：身高走 outfit.parse_height(key="get_remote_outfit")，
       9 格部位走 outfit.parse_outfit_parts(...) + outfitcatalog 挂中文名与 CDN 图标。
    ⑤ ★ **保留**这次好友关系（不再软删）—— 见下。

★★★ 「查过一次，下次还能查」是怎么做到的（2026-10-06 定案）
   星盘快照是「一次拿回**全部好友**的 outfit」，所以只要**目标还在好友列表里**，
   就随时能读到他的最新数据，不需要任何令牌。于是：
     · 第一次查：走 ①→②→③，**读完之后保留好友关系**（不软删）。
     · 之后每次：`read_friend()` —— 纯星盘读，**不 accept、不消费令牌、不改关系**，
       而且拿到的是**实时**数据（不是缓存快照）。
   这正是用户提的那条：「既然都成好友了，那直接查这个好友不就行了，查完一次下次还能再查」。
   副产品：`already_friends` 不再是无解的 —— 只要这个号是我们以前加过的，
   身份映射（lookupstore 的 links）里就有他的玩家 uuid，直接走 `read_friend` 就行。

   代价与边界（诚实记录）：
     · 目标玩家会**长期**看到一条来自「查装扮」的好友（第一次查之后一直在）。
     · 背号的好友数会随查询量增长 → 超过 `_MAX_KEPT_FRIENDS` 时按加入时间
       清理最早的「查装扮」好友（只清昵称等于 `_BEFRIEND_NICK` 的，绝不碰真好友）。
     · 背号好友位被占满之前，同一台背号可以服务很多不同的人。

★★ 好友码仍然是**一次性**的（2026-10-05 三轮 + 跨背号实测）：
   accept 成功后令牌即被**全局**消费 —— 换另一台背号再用同一个码同样是
   `{"invite_result":"not_available"}`。所以「同一个码」的重复查询走的是
   上面的直读路径（靠身份映射），不是靠令牌。
   实测补充（2026-10-06）：`already_friends` / `own_invite` 这两种拒绝**不消费令牌** ——
   双向好友已存在时 accept 回 already_friends；把好友软删掉之后同一个令牌再 accept 直接 ok。
   所以 main.py 在这两种结果上会**换一台背号重试**。

⚠️ 本模块**会写**：`accept_invite`（给查询背号加一个好友，且**保留**）。
   用户自己的账号**完全不被触碰**：用户不需要登录，也不需要知道背号是谁。
   副作用：目标玩家会收到一次「查装扮」的加好友（此后长期保留）。

为什么旧链必然失败（留着，别再走回去）：
   旧实现调 `/account/get_remote_outfit {"target": uuid}`。实测只有 target =
   查询者**自己**时返回 200；对**任何**他人 target（好友、真码目标、伪造 uuid）
   一律 HTTP 400 `{"result":"not_recently_online"}` —— 伪造 uuid 与真人 uuid 返回
   **同一个**错误，说明服务端在评估 target 之前就拒了。已穷尽排除出口 IP（7 个）、
   请求头 A/B 矩阵、body 形态、会话、机位、目标在线/同房/是好友，并做过
   「真进房 + 保活」。真机抓包另证：客户端在游戏里读他人数据走 UDP 房间通道，
   不走这个 HTTP 接口。星盘才是正解 —— 它是「一次拿全部好友快照」的语义，
   不按 target 校验，所以离线、异房都能查。
"""
from __future__ import annotations

import re
import time

from . import outfit, skycode
from .skyapi import SkyApiError

# 已废弃：只对「查询者自己」有效，见顶部「为什么旧链必然失败」。保留常量只为可读性。
P_REMOTE_OUTFIT = "/account/get_remote_outfit"

P_CONSTELLATION = "/service/relationship/api/v1/get_friend_constellation"
P_ACCEPT_INVITE = "/account/accept_invite"
P_SOFT_DELETE = "/service/relationship/api/v1/batch_set_friend_soft_delete"

# 装扮数据在响应里的键。星盘行里的 outfit 与 get_remote_outfit 同构，
# 套进这个键就能直接复用 outfit.py 那套解析（解析层一行都不用改）。
REMOTE_KEY = "get_remote_outfit"

# 加好友的备注名：服务端硬上限 20 字，且**不能带空格**（带了会静默无视这次生成）。
# ★ 这个名字同时是「哪些好友是我们加的」的唯一判据 —— prune_ours() 靠它做清理，
#   所以**不要**改成空串或让用户自定义。
_BEFRIEND_NICK = "查装扮"

# 背号好友数上限：超过就按加入时间清理最早的「查装扮」好友（只清我们加的）。
# 保留好友是「下次还能查」的前提，但也不能让列表无限长。
_MAX_KEPT_FRIENDS = 300

# 加好友后星盘不是同一毫秒就同步 —— 给三次机会（实测通常第 0 次就命中）
_CONST_ATTEMPTS = 3
_CONST_GAP = 0.8

_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
# URL 里可能承载身份的查询参数（按可信度排序：码 > 令牌 > udid）
_URL_PARAM_KEYS = ("code", "invite_code", "invitecode", "token_id", "token", "invite", "udid")

# 12 位短码在正文里可能带横线/空格/全角横线，且玩家可能把 O/I/L 抄成 0/1。
# 关键：**只认「独立成组的 12 位」**，绝不在更长的字母数字串里切 12 位 ——
# 二维码正文里的 udid（22 位 base62）会被切出假码（实测踩过：2633EMMGQGIK）。
_CODE_GROUP_RE = re.compile(r"[0-9A-Z]{12,}")
_CODE_CHARS = skycode.ALPHABET + "OIL"      # O/I/L 是手抄常见误写，扫的时候先认下来
# 码里的分隔符：ASCII 连字符之外，还要吃全角/短/长破折号与各种减号 —— 玩家从游戏里
# 复制到聊天软件再转贴，全角横线（U+FF0D）是高频形态，别在这上面栽跟头。
_DASHES = "-\u2010\u2011\u2012\u2013\u2014\u2015\u2212\ufe58\ufe63\uff0d"
_CODE_SEP_RE = re.compile("[%s\\s]+" % _DASHES)


def _as_code(value: str) -> str:
    """看起来是 12 位码就归一化，否则原样返回（udid 这类不能被改）。"""
    flat = _CODE_SEP_RE.sub("", str(value or "").upper())
    if len(flat) == 12 and skycode.is_short_code(flat):
        return _norm_code(flat)
    return str(value or "")


def _norm_code(value: str) -> str:
    """把码归一成规范形态（复用 skycode 的规则：补横线、吃 O→0 / I→1 / L→1）。"""
    return skycode.normalize_friend_code(value)


class InputError(RuntimeError):
    """**用户输入**的问题（认不出 / 形态不对 / 空）—— 换背号也没用，直接回 400。

    与之相对，`RuntimeError` 其余场合（限流 422、机位 401、HTTP 5xx）都是
    **背号/出口**的问题：换个背号很可能就成了，所以路由要**重试下一台**而不是直接报错。
    线上实测踩到过：一台背号的出口挂了（0.0 秒回 `{"status":"Invalid Request"}`），
    路由当时是 `except Exception → 502`，于是那一次查询白白失败 —— 而另一台背号是好的。
    """


def verify_code(client, value: str) -> dict:
    """任意形态的码 → 邀请令牌 uuid（服务端查表）。**不校验形态**，交给服务端判。

    返回 {"found","uuid","type","code","result","zh"}；
    输入问题抛 `InputError`，背号/出口问题抛 `RuntimeError`（路由据此换台重试）。
    """
    raw = str(value or "").strip()
    if not raw:
        raise InputError("请先填写好友码 / 邀请码")
    try:
        resp = client.post(skycode.P_CODE_VERIFY, {"code": raw}) or {}
    except SkyApiError as e:
        st = getattr(e, "status", None)
        if st in (422, 429):
            raise RuntimeError("网易出口限流中（HTTP %s），等几秒再试" % st) from e
        if st == 401:
            raise RuntimeError("查询机位的登录已经失效（HTTP 401），换个机位再试") from e
        raise RuntimeError("查这个码失败（sky api HTTP %s）" % st) from e

    result = str(resp.get("result") or "")
    uuid = str(resp.get("code") or "").strip()
    if result == "ok" and uuid:
        return {"found": True, "uuid": uuid, "type": str(resp.get("type") or ""),
                "code": raw, "result": result, "zh": "码有效"}
    if result == "throttle":
        secs = int(resp.get("cooldown") or 0)
        return {"found": False, "uuid": "", "type": "", "code": raw, "result": result,
                "zh": "查得太快了，等 %d 秒再试" % secs}
    _ZH = {
        "not_found": "服务端查不到这个码。只有真人玩家账号/邀请码在服务端表里",
        "expired": "这个码已经过期了",
        "failed": "服务端没有接受这个码",
    }
    return {"found": False, "uuid": "", "type": str(resp.get("type") or ""),
            "code": raw, "result": result,
            "zh": _ZH.get(result, result or "服务端没有给出结果")}


def extract_candidates(text: str) -> list[dict]:
    """从用户输入（可能是码、uuid、邀请链接或扫出来的一整段正文）里抽身份候选。

    优先级：uuid > URL 参数（code/udid/…） > 正文里的 12 位短码。
    返回 [{"kind","value"}]，kind ∈ {"uuid","param","code"}，已去重。
    """
    raw = str(text or "").strip()
    if not raw:
        return []
    out: list[dict] = []
    seen: set[tuple] = set()

    def push(kind: str, value: str) -> None:
        v = str(value or "").strip().strip('"\'')
        if not v:
            return
        key = (kind, v)
        if key in seen:
            return
        seen.add(key)
        out.append({"kind": kind, "value": v})

    # ① 整串就是一个 uuid（最常见：用户直接把光遇 ID 贴进来）
    m = _UUID_RE.search(raw)
    if m:
        push("uuid", m.group(0))

    # ② URL 查询参数（邀请二维码正文就是这种形态）
    if "?" in raw or "&" in raw:
        for pair in re.split(r"[?&#]", raw):
            if "=" not in pair:
                continue
            k, _, v = pair.partition("=")
            k = k.strip().lower()
            if k in _URL_PARAM_KEYS:
                push("param", v)

    # ③ 正文里的 12 位短码（先剥掉 URL/uuid，免得把 uuid 片段或 udid 当成码）
    body = _CODE_SEP_RE.sub("", raw).upper()                # 先吃掉横线/空格，码才连成一组
    for u in _UUID_RE.findall(raw):
        body = body.replace(u.replace("-", "").upper(), " ")
    for run in _CODE_GROUP_RE.findall(body):
        if len(run) == 12 and skycode.is_short_code(run):
            push("code", _norm_code(run))

    return out


def resolve(client, text: str) -> dict:
    """把任意输入解析成目标**邀请令牌** uuid。

    返回 {"found","uuid","kind","used","code","zh","tried":[...]}。
    只有 udid 而服务端不认时，zh 会给出「去复制那串 12 位邀请码」的可照做提示。

    ★ 返回的 uuid 是**邀请令牌**（`accept_invite` 要的就是它），不是玩家 uuid。
      12 位码走 code/verify 换出来的就是令牌 uuid（实测与本地
      uuid_to_short_code(令牌) 算出的码完全对上）。
      用户直接贴 32 位光遇 ID 时这里也照收 —— 但那是玩家 ID 不是令牌，
      到 `befriend` 会被服务端拒，由 _ACCEPT_ZH 给出可照做的说明。

    ★ 为什么**不**把 udid 这类非码形态丢给 `code/verify` 碰碰运气：
      文档 47 实测「32 位 uuid 当 code 传」是 **HTTP 422**（服务端对码做了形态校验），
      而传输层把 422 当限流信号**强制加试到 5 次**（skyapi.RATE_LIMIT_ATTEMPTS，
      约 0.9+1.8+2.7+3.6 秒）—— 结果是白等 9 秒、最后还给用户一句误导的「限流中」。
      所以非码形态直接跳过网络调用，只记进 tried 供排查。
    """
    cands = extract_candidates(text)
    if not cands:
        raise InputError("没认出好友码 / 邀请码：可以直接贴 12 位码（XXXX-XXXX-XXXX）、"
                         "32 位光遇 ID，或上传游戏内邀请二维码")

    tried = []
    sent_any = False
    for c in cands:
        kind, value = c["kind"], c["value"]
        if kind == "uuid":
            return {"found": True, "uuid": value, "kind": "uuid", "used": value,
                    "code": value, "zh": "直接按光遇 ID 查询", "tried": tried}
        # param / code 都交给服务端查表：码的形态千奇百怪，形态校验只会误伤
        norm = _as_code(value)
        if len(norm.replace("-", "")) != 12:
            tried.append({"kind": kind, "value": value, "result": "not_code_shaped"})
            continue
        sent_any = True
        r = verify_code(client, norm)
        tried.append({"kind": kind, "value": value, "result": r.get("result") or ""})
        if r.get("found"):
            return {"found": True, "uuid": r["uuid"], "kind": kind, "used": value,
                    "code": r.get("code") or value, "type": r.get("type") or "",
                    "zh": r.get("zh") or "", "tried": tried}

    zh = "服务端查不到这个码。只有真人玩家账号/邀请码在服务端表里"
    if not sent_any:
        # 二维码正文里的分享标识（type=i&udid=…）没有对外反查口子 —— 指路而不是含糊报错
        zh = ("这张二维码里只有游戏内的分享标识（type=i&udid=…），服务端不提供反查。"
              "请在游戏里点『复制分享此邀请码』，把那串 12 位邀请码（如 4A1C-FD16-DEWZ）"
              "贴到上面的输入框再查一次。")
    return {"found": False, "uuid": "", "kind": "", "used": "", "code": "", "zh": zh,
            "tried": tried}


# ── 星盘：一次拿全部好友的装扮快照（离线可查）────────────────────────────

def constellation_rows(client) -> list:
    """星盘快照 `set_friend_statues`：**一次调用带回全部好友**的装扮/身高/关系计数。

    与 `get_all_friend_gist` 的分工（friends.py 已实测）：gist 给全量好友名单
    + `is_online`（实测 89/89 全 False，没用），星盘只含有星盘关系的那些人，
    但**只有星盘带 outfit**。本功能要的就是 outfit，所以走星盘。
    """
    r = client.post(P_CONSTELLATION, {"include_level_id": False}, include_level_id=False) or {}
    rows = r.get("set_friend_statues")
    return rows if isinstance(rows, list) else []


def _friend_ids(client) -> set:
    return {str(x.get("friend_id")) for x in constellation_rows(client)
            if isinstance(x, dict) and x.get("friend_id")}


def row_outfit(rows: list, friend_id: str) -> dict:
    """从星盘行里取某个好友的 outfit 对象（拿不到回空 dict）。"""
    want = str(friend_id or "")
    if not want:
        return {}
    for row in rows:
        if isinstance(row, dict) and str(row.get("friend_id") or "") == want:
            so = row.get("outfit")
            return so if isinstance(so, dict) else {}
    return {}


def befriend(client, token_id: str) -> dict:
    """接受好友码 → **查询背号**立刻和对方成为好友（双向，立即生效）。

    返回 {"accepted", "result", "friend_id"}；`friend_id` 是**对方玩家 uuid**
    （这才是要查身高的对象，不是传进来的令牌 uuid）。
    """
    tok = str(token_id or "").strip()
    if not tok:
        raise InputError("请先填写好友码")
    resp = client.post(P_ACCEPT_INVITE, {"token_id": tok,
                                         "invite_nickname": _BEFRIEND_NICK}) or {}
    return {
        "accepted": bool(resp.get("invite_accepted")),
        "result": str(resp.get("invite_result") or ""),
        "friend_id": str(resp.get("invite_friend_id") or ""),
    }


def unfriend(client, friend_ids) -> None:
    """软删好友（只用于清理我们加的「查装扮」好友 / 读取失败时回滚）。"""
    ids = [str(x) for x in (friend_ids or []) if str(x).strip()]
    if not ids:
        return
    client.post(P_SOFT_DELETE, {"friend_ids": ids, "soft_deleted": True})


def prune_ours(client, log=None, rows=None) -> int:
    """背号好友数超上限时，清掉最早那些**我们自己加的**「查装扮」好友。

    判据只有一个：`nickname == _BEFRIEND_NICK`。绝不会碰到背号自己的真好友
    （昵称不是「查装扮」）。任何异常都吞掉 —— 这是维护动作，不该影响查询结果。

    `rows` 可以传入**已经拉过的**星盘快照（`lookup()` 就是这么用的），
    省掉每次查询都多打一次星盘、多拉一整份好友列表。
    """
    try:
        if rows is None:
            rows = constellation_rows(client)
        if len(rows) < _MAX_KEPT_FRIENDS:
            return 0
        mine = [x for x in rows if isinstance(x, dict)
                and str(x.get("nickname") or "") == _BEFRIEND_NICK]
        if not mine:
            return 0

        def born(x: dict) -> float:
            try:
                return float(x.get("when_created") or 0)
            except (TypeError, ValueError):
                return 0.0

        # 多清一个，保证这次加完不会又立刻超限
        need = len(rows) - _MAX_KEPT_FRIENDS + 1
        drop = [str(x.get("friend_id")) for x in sorted(mine, key=born)[:need]
                if x.get("friend_id")]
        if not drop:
            return 0
        unfriend(client, drop)
        if log:
            log("背号好友 %d 个超过上限 %d，清理了最早的 %d 个「%s」好友"
                % (len(rows), _MAX_KEPT_FRIENDS, len(drop), _BEFRIEND_NICK))
        return len(drop)
    except Exception:                                       # noqa: BLE001
        return 0


def _ok_result(friend_id: str, s: dict, code: str = "", kind: str = "", tried=None) -> dict:
    return {"ok": True, "zh": "", "uuid": friend_id, "code": code, "kind": kind,
            "tried": tried or [], "height": s.get("height"),
            "parts": s.get("parts") or [], "part_count": s.get("part_count") or 0,
            "wingless": bool(s.get("wingless"))}


def read_friend(client, friend_id: str, log=None) -> dict:
    """**已知玩家 uuid** → 直接从星盘读（不 accept、不消费令牌、不改任何关系）。

    这是「保留好友关系」换来的能力：查过一次之后，同一个玩家可以反复查，
    而且每次都是**实时**数据 —— 不是缓存快照。
    """
    want = str(friend_id or "").strip()
    if not want:
        return _fail("没有可查的玩家 ID", result="no_id")
    so = row_outfit(constellation_rows(client), want)
    if not so:
        return _fail("这个玩家不在查询账号的好友星盘里（好友关系可能已被删除）。"
                     "用新的好友码再查一次即可重新建立。",
                     uuid=want, result="not_friend")
    s = summarize({REMOTE_KEY: so})
    if not s.get("parts") and not s.get("height"):
        return _fail("星盘里有这个好友，但里面既没有部位也没有身高数据。",
                     uuid=want, result="empty_outfit")
    return _ok_result(want, s, kind="friend")


# accept_invite 的拒绝原因 → 用户能照做的中文
_ACCEPT_ZH = {
    "not_available": ("这个好友码已经被用过了 —— **一个码只能用一次**。\n"
                      "想再看同一个玩家：直接贴**上次那串码**或**它的 32 位光遇 ID**"
                      "（查过一次就一直能查）。要看**别人**，就回游戏里重新创建一个码。"),
    "not_found": ("服务端不认这个好友码。注意：**32 位光遇 ID 不是好友码** —— "
                  "请回游戏里「创建好友码」复制那串 12 位码（XXXX-XXXX-XXXX）。"),
    "expired": "这个好友码已经过期了，回游戏里重新创建一个再查。",
    "accepted": "这个好友码已经被别人用掉了，回游戏里重新创建一个再查。",
    "own_invite": ("这个好友码是查询背号自己生成的，服务端不接受自己加自己。"
                   "请确认贴的是**对方**在游戏里创建的好友码。"),
    "already_friends": (
        "这个号**之前已经查过了** —— 查询账号还是它的好友，所以服务端不再走「接受邀请」"
        "这一步。而**新码只能靠这一步才能认出码属于谁**（2026-10-07 实测："
        "`check_invite` 与 `accept_invite` 的所有变体都只回一个 `invite_result`，"
        "不带对方 ID —— 没有别的口子能把码换成玩家 ID）。\n"
        "照下面任一条做都能立刻查到：\n"
        "· 贴**它的 32 位光遇 ID**（最稳）；\n"
        "· 或者贴**上次那串码**。\n"
        "这两个都走「直接读星盘」，不受这一步影响。"),
}

# ★ 这两个拒绝是**背号相关**的 —— 换一台背号就可能成功。main.py 会据此重试。
#   实测（2026-10-05 单对决定性测试）：already_friends **不消费令牌** ——
#   双向好友已存在时 accept 回 already_friends；把好友软删掉之后，**同一个令牌
#   再 accept 直接 ok**。所以「换背号重试」真的能救回来，不是碰运气。
#   own_invite 同理（令牌没被消费，只是那一台背号不能接受自己的邀请）。
BACKER_RETRY_RESULTS = ("already_friends", "own_invite")


def summarize(resp: dict) -> dict:
    """把（套了 get_remote_outfit 键的）装扮对象压成前端要的形状。"""
    summary = outfit.summarize_outfit(resp, key=REMOTE_KEY)
    from . import outfitcatalog
    try:
        outfitcatalog.enrich_summary(summary)
    except Exception:                                       # noqa: BLE001
        pass                                                # 目录拉不到只掉图标，不影响身高
    return summary


def _fail(zh: str, uuid: str = "", code: str = "", kind: str = "",
          tried=None, result: str = "") -> dict:
    """统一的业务失败形状（ok:False，前端直接展示 zh）。"""
    return {"ok": False, "zh": zh, "uuid": uuid, "code": code, "kind": kind,
            "tried": tried or [], "result": result,
            "height": None, "parts": [], "part_count": 0, "wingless": False}


def _recover_fid(rows) -> str:
    """accept_invite 没回 `invite_friend_id` 时的兜底：找刚加上的那个「查装扮」好友。

    ★ 只在 friend_id 为空时才会被调用 —— 线上实测 accept_invite **一直**会给
      invite_friend_id，所以这是纯粹的保险，正常路径**零额外调用**。
      （旧版是「accept 之前先拉一次星盘做前后差集」，那会让每次查询都多打一次
      星盘、多拉一整份好友快照。已改成这个事后判定。）

    判据三条同时成立：昵称 == `_BEFRIEND_NICK`、`when_created` 在 120 秒内、
    而且这样的候选**唯一**。任何一条不满足就返回空串（宁可报错也不猜错人）。
    """
    now = time.time()
    cands = []
    for x in (rows or []):
        if not isinstance(x, dict) or not x.get("friend_id"):
            continue
        if str(x.get("nickname") or "") != _BEFRIEND_NICK:
            continue
        try:
            born = float(x.get("when_created") or 0)
        except (TypeError, ValueError):
            born = 0.0
        if born and abs(now - born) <= 120:
            cands.append((born, str(x["friend_id"])))
    return max(cands)[1] if len(cands) == 1 else ""


def _rollback(client, friend_id: str, log=None) -> None:
    """读失败时把这次加的好友删掉（留着也没用，白占一个好友位）。

    只在**没读到数据**时调用 —— 读成功是**保留**关系的（见 lookup 的说明）。
    friend_id 缺失时用 `_recover_fid` 兜底找回来。失败一律吞掉。
    """
    try:
        fid = str(friend_id or "")
        if not fid:
            fid = _recover_fid(constellation_rows(client))
        if not fid:
            return
        unfriend(client, [fid])
        if log:
            log("已回滚这次加的好友关系（%s）" % fid[:8])
    except Exception as e:                                  # noqa: BLE001
        if log:
            log("回滚失败（不影响结果）：%s" % str(e)[:80])


def lookup(client, text: str, log=None) -> dict:
    """一次完整查询：解析好友码 → 背号加好友 → 读星盘 → **保留好友关系** → 解析身高/装扮。

    返回固定形状（前端直接渲染）：
      {"ok", "zh", "uuid", "code", "kind", "height":{max,min,current},
       "parts":[{slot,label,id,name?,icon?}], "part_count", "wingless"}
    查不到（码无效 / 已用过 / 没快照）返回 ok:False + zh，**不抛**（前端原样展示）。

    ★ 读成功之后**不删好友** —— 这是「查过一次，下次还能查」的前提：
      目标留在星盘里，之后每次查询走 `read_friend()`，纯读、实时、不烧令牌。
      只有**读失败**时才回滚掉这次加的好友（留着也没用，白占一个好友位）。
    """
    def _log(m: str) -> None:
        if log:
            log(m)

    r = resolve(client, text)
    if not r.get("found"):
        return _fail(r.get("zh") or "查不到这个码", tried=r.get("tried") or [])

    token = r["uuid"]
    code = r.get("code") or ""
    kind = r.get("kind") or ""
    tried = r.get("tried") or []

    friend_id = ""
    try:
        acc = befriend(client, token)
        if not acc.get("accepted"):
            result = acc.get("result") or ""
            _log("加好友被拒：%s" % (result or "空"))
            return _fail(_ACCEPT_ZH.get(result,
                                        "服务端没有接受这个好友码（result=%s）" % (result or "空")),
                         uuid=token, code=code, kind=kind, tried=tried, result=result)

        friend_id = acc.get("friend_id") or ""
        _log("已把目标加成好友（friend_id=%s），开始读星盘" % (friend_id[:8] or "?"))

        so = {}
        for i in range(_CONST_ATTEMPTS):
            if i:
                time.sleep(_CONST_GAP * i)
            rows = constellation_rows(client)
            if not friend_id:
                friend_id = _recover_fid(rows)
            so = row_outfit(rows, friend_id)
            if so:
                break
            _log("星盘第 %d 次没命中（friend_id=%s），重试" % (i + 1, friend_id[:8] or "?"))

        if not so:
            _log("星盘没读到装扮，回滚这次加的好友")
            _rollback(client, friend_id, _log)
            return _fail("已经把这个号加成好友了，但星盘里没有它的装扮快照"
                         "（对方可能从未进过游戏，或账号数据还没同步到星盘）。",
                         uuid=friend_id or token, code=code, kind=kind, tried=tried,
                         result="no_outfit")

        s = summarize({REMOTE_KEY: so})
        if not s.get("parts") and not s.get("height"):
            _log("星盘行里没有可用数据，回滚这次加的好友")
            _rollback(client, friend_id, _log)
            return _fail("星盘返回了装扮行，但里面既没有部位也没有身高数据。",
                         uuid=friend_id, code=code, kind=kind, tried=tried,
                         result="empty_outfit")

        # ★ 读到了 → **保留**好友关系（不软删）。之后这个玩家可以反复直读。
        _log("已保留好友关系（%s），下次可直接读星盘" % friend_id[:8])
        prune_ours(client, log, rows=rows)      # 复用刚拉的那份快照，不再多打一次星盘
        return _ok_result(friend_id, s, code=code, kind=kind, tried=tried)
    except Exception:                                       # noqa: BLE001
        # 中途炸了（网络/解码）：这次加的好友如果读不到就回滚，别白占好友位
        _rollback(client, friend_id, _log)
        raise


def cache_key(text: str) -> str:
    """缓存键：按「规范化后的目标身份」而不是原始输入，同一目标多种写法命中同一份。"""
    cands = extract_candidates(text)
    if not cands:
        return "raw:" + str(text or "").strip().lower()
    c = cands[0]
    if c["kind"] == "uuid":
        return "uuid:" + c["value"].lower()
    return "%s:%s" % (c["kind"], _as_code(c["value"]).lower())
