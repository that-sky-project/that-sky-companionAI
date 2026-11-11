# -*- coding: utf-8 -*-
"""国服 Sky HTTP 封装：skyPost（RC4 + 自定义 CE 解码 + 统一头）。

移植自 SkyApi.executeSkyPost / buildHeaders / decodeCnResponse（仅国服分支）。
"""
from __future__ import annotations

import base64
import gzip
import json
import random
import string
import threading
import uuid

import requests

from . import fp, netcfg, version
from .util import rc4

CN_HOST = "https://live-as-sky-merge.game.163.com"
CN_LEVEL_ID = "3526133726"  # x-sky-level-id（国服 /account/ 类接口带）

# 可重试的 HTTP 状态。422 = 网易对出口 IP 的限流信号（见 SkyClient.post 注释）。
RETRY_STATUS = (422, 429)
RETRY_RATE_LIMIT = 422
RATE_LIMIT_ATTEMPTS = 5         # 撞上限流时自动加试到 5 次（约 0.9+1.8+2.7+3.6 秒退避）

# ★★ 0.16.5 网关双闸门（2026-09-24 真机内存取证定位）★★
# 用户情报「build-access-key 原本是空的，现在补上了」得到证实。
# 闸门 1：请求头 x-sky-build-access-key 必须等于官方 APK 编译期常量
#         com.tgc.sky.BuildConfig.SKY_BUILD_ACCESS_KEY（从重修的 ntp dex 用 androguard 读出）。
#         0.16.8 换新值，且不能进公开仓库；每次请求前由 fp.build_access_key() 解析。
#         空串 / "1" / 只取时间戳段 一律 HTTP 418。
# 闸门 2：User-Agent 必须是官方形态 Sky-Live-<数字>/<版本>.<build> (<设备>)，
#         且版本 ≥ 0.16.5。`Sky/<版本>` 旧形态与 0.15.8 版本号现均被拒（418）。
# 两者必须同时满足；满足后 /account/get_vars、/account/get_motd 立刻 200。
# 常量与 UA 助手统一放在 fp.py（cnlogin.py 也要用，避免循环导入）。
# 兼容旧导入名：不要把它当常量缓存；实际值在请求时重新解析。
CN_BUILD_ACCESS_KEY = ""

# 兜底常量：fp 关闭（QFLY_FP_OFF=1 或 netcfg.fp=false）时才用。
# 改造前**所有**账号都报这一个机型 —— 137 个号自称同一台小米 12S Ultra，
# 这是风控现成的聚合键。现在按账号取自 fp.device()。
# 2026-09-18：用户要求弃用 2203121C 那台（容易封号），兜底改为 fp.legacy_ua()
# —— 从 devrand 机型目录确定性取的一台，不再是那个已被风控记住的型号。
UA_DEVICE = fp.legacy_ua()


class SkyApiError(Exception):
    def __init__(self, status: int, path: str, body: str):
        super().__init__("sky api HTTP %s on %s: %s" % (status, path, body[:180]))
        self.status = status
        self.path = path
        self.body = body


def cn_user_agent(preferred: str = "", key: str = "") -> str:
    """国服 Sky API 的官方 UA：`Sky-Live-<9位数字>/<版本> (<设备>)`。

    ★ 0.16.5 起这是**唯一**能过网关的形态（见 CN_BUILD_ACCESS_KEY 注释）：
        Sky-Live-<数字>/0.16.5.412659 (Xiaomi 23013RK75C; android 36.0.0; en)  200 ✓
        Sky/0.16.5.412659                (HUAWEI BAL-AL80;   android 34.0.0; en)  418 ✗
        Sky-Live-<数字>/0.15.8.383089    (Xiaomi 23013RK75C; android 36.0.0; en)  418 ✗
        Sky-Live-<数字>/0.16.5.412659                        （无括号段）         418 ✗
    """
    v = (preferred or "").strip()
    if not v or v == "0":
        v = version.cn_version()
    elif v.count(".") < 3:
        # 残缺版本 → 用完整版本换 build 段
        b = version.build_from_version(v)
        base = version.cn_version()
        v = "%s.%s" % (base.rsplit(".", 1)[0], b) if b else base
    return fp.sky_cn_ua(key, v)


def random_trace() -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=7))


class SkyClient:
    """一个账号一个 client：持有 uid / session / session2(RC4 key)，版本号自动跟随全局。"""

    def __init__(self, uid: str, session: str, session2: str, client_version: str = "",
                 fp_key: str = ""):
        self.uid = uid
        self.session = session
        self.session2 = session2
        self.client_version = client_version  # 空 = 用全局版本
        # 指纹键默认 = 光遇角色ID：同一账号永远同一台手机，不同账号不同机型。
        self.fp_key = str(fp_key or uid or "")
        # 出口：未配代理 = 裸 Session（与改造前一致）；配了 = 该账号粘性绑定的出口。
        self._local = netcfg.session(self.fp_key)
        self._lock = threading.Lock()
        # 当前所在地图（聊天进程按自己所在房间设置）：/service/* 会校验这个 level。
        self.level_id = None

    # ── 核心请求 ──
    def post(
        self,
        path: str,
        inner: dict,
        include_level_id: bool = True,
        level_id: str | None = None,
        ua_version: str | None = None,
        timeout: float = 25.0,
        attempts: int = 3,
    ) -> dict:
        """skyPost：国服 RC4 包裹 + 响应 CE 分派解码。非 2xx 抛 SkyApiError。

        可重试状态：`429`、`422`、任意 `>=500`、连接层异常。
        **`422` 是网易对出口 IP 的限流信号**（与 cnlogin.LOGIN_TRANSIENT_STATUS 同口径）：
        被限时 `/account/*` 会成片 422，而限流窗口通常只有几秒，等一下就过。
        2026-10-02 实测：面板一次操作里 create_invite / check_invite / get_invites
        齐刷刷 422，就是这里没重试、把瞬时限流原样报给了用户。
        真·会话失效是 `401`（不可重试，立刻抛）。
        """
        last_err: Exception | None = None
        n = max(1, int(attempts))
        attempt = 0
        while attempt < n:
            attempt += 1
            retry_sleep = 0.0
            try:
                return self._post_once(path, inner, include_level_id, level_id, ua_version, timeout)
            except SkyApiError as e:
                last_err = e
                if e.status == RETRY_RATE_LIMIT and n < RATE_LIMIT_ATTEMPTS:
                    n = RATE_LIMIT_ATTEMPTS     # 限流：自动补救到 5 次，别让用户手动重试
                elif e.status not in RETRY_STATUS and e.status < 500:
                    raise
                retry_sleep = 0.9 if e.status in RETRY_STATUS else 0.6
            except (requests.RequestException, OSError) as e:
                last_err = e
                retry_sleep = 0.6
            if attempt < n:
                import time
                time.sleep(retry_sleep * attempt)
        raise last_err if isinstance(last_err, Exception) else OSError("sky api request failed")

    def _post_once(self, path, inner, include_level_id, level_id, ua_version, timeout) -> dict:
        key = self.session2.encode("ascii")
        body_inner = dict(inner)
        body_inner.setdefault("user", self.uid)
        body_inner.setdefault("user_id", self.uid)
        body_inner.setdefault("session", self.session)
        cipher = rc4(key, json.dumps(body_inner, separators=(",", ":")).encode("utf-8"))
        outer = {"user": self.uid, "body": base64.b64encode(cipher).decode("ascii")}
        headers = {
            "Host": CN_HOST.split("://", 1)[1],
            "Content-Type": "application/json",
            "User-Agent": cn_user_agent(ua_version or self.client_version, self.fp_key),
            "X-Session-ID": str(uuid.uuid4()),
            "trace-id": random_trace(),
            "x-sky-build-access-key": fp.build_access_key(),
            "session": self.session,
            "x-session-token": self.session,
            "user-id": self.uid,
            "x-user-id": self.uid,
            "Accept-Encoding": "identity",
        }
        if level_id is not None:
            lid = level_id
        elif include_level_id:
            lid = getattr(self, "level_id", None) or CN_LEVEL_ID
        else:
            lid = None
        if lid:
            headers["x-sky-level-id"] = str(lid)
        r = self._local.post(CN_HOST + path, data=json.dumps(outer).encode("utf-8"),
                             headers=headers, timeout=timeout)
        ce = (r.headers.get("Content-Encoding") or "").strip().lower()
        raw = r.content or b""
        if not r.ok:
            text = self._decode(key, raw, ce)
            raise SkyApiError(r.status_code, path, text[:300])
        text = self._decode(key, raw, ce)
        if not text.strip():
            return {}
        return json.loads(text)

    @staticmethod
    def _decode(key: bytes, raw: bytes, ce: str) -> str:
        try:
            if "gzip-session-valid" in ce:
                return gzip.decompress(rc4(key, raw)).decode("utf-8", "ignore")
            if "session-valid" in ce:
                return rc4(key, raw).decode("utf-8", "ignore")
            if ce in ("gzip", "x-gzip"):
                return gzip.decompress(raw).decode("utf-8", "ignore")
        except Exception:
            pass
        return raw.decode("utf-8", "ignore")

    # ── 业务快捷封装 ──
    def get_currency(self) -> dict:
        return self.post("/account/get_currency", {})

    def get_friends(self) -> dict:
        return self.post("/service/relationship/api/v1/get_all_friend_gist", {})

    def get_collectibles(self) -> dict:
        return self.post("/account/get_collectibles", {})

    def get_wing_buffs(self) -> dict:
        return self.post("/account/wing_buffs/get", {})

    def find_previous_or_empty(
        self,
        server_version: int,
        client_version: int,
        level_hash: int,
        level_id: int,
        rejoin_uid: str | None = None,
        *, single_attempt: bool = False, timeout: float = 25.0,
    ) -> dict:
        body = {
            "server_version": server_version,
            "client_version": client_version,
            "force_empty": False,
            "net_flags": 0,
            "net_exclusive_flags": 1,
            "level_hash": level_hash,
        }
        rejoin = [rejoin_uid] if rejoin_uid else []
        body["quick_rejoin_players"] = rejoin
        body["recent_players"] = rejoin
        # 探针验证过的行为：拉票头 x-sky-level-id 用目标图 map_id（cn_full_probe 实测）
        if single_attempt:
            # A verification attempt must never silently request a second ticket.
            return self._post_once("/account/find_previous_or_empty", body, True, str(level_id), None, timeout)
        return self.post("/account/find_previous_or_empty", body, include_level_id=True, level_id=str(level_id), timeout=timeout)

    def collect_pickup_batch(self, map_id: int, pickup_ids: list[int],
                             global_pickup_ids: list[int] | None = None,
                             emitters: list[dict] | None = None) -> dict:
        """批量拾取。

        global_pickup_ids = 不挂在图上的全局拾取物（遇境神坛每日季蜡在这里，见 seasonhome.py）。
        emitters = 动态发射器拾取物（见 eventtoken.py）：
            {"emitter_name": str, "event_name": str, "chunk_seq": int, "pickup_seq": int}
        实测（09-25，月光节图 1694829989）：**必须先 enter_map 进图**，否则本接口
        一律返回 `{}` 空响应（服务端说该图当前没有拾取物），进图后立即返回逐项 batch_results。

        ⚠ 不要传空的 user/user_id/session：_post_once 用 setdefault 注入真实值，
        传空串会把它们钉死成空串。
        """
        return self.post(
            "/account/collect_pickup_batch",
            {"level_id": map_id, "pickup_ids": list(pickup_ids),
             "global_pickup_ids": list(global_pickup_ids or []),
             "emitters": [dict(e) for e in (emitters or [])]},
            include_level_id=False,
        )
