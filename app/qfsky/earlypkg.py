# -*- coding: utf-8 -*-
"""网易大神「提前包 / 预约包」守望。

═══════════════════════════════════════════════════════════════════════════
它解决什么问题
═══════════════════════════════════════════════════════════════════════════
新版本正式发布前，大神游戏中心会先挂出「新版本预约」活动，
`versionDownloadInfo.downloadPackageUrl` 就是**提前包直链**。这个链接出现的
时间远早于下载窗口开启 —— 实测光遇 2026 年那一期：预约窗 8/26 开、直链当场
下发、包体 8/28 已上传 CDN（HEAD 200 / 1.92GB），而 App 里的下载按钮 9/4 才亮。
**比正式窗口早 7 天就能下。**

解法很简单：定期 POST 一次 `gameDetail` 就知道链接出没出。

    POST https://god.gameyw.netease.com/v1/app/gameCenter/gameDetail
    {"squareId": "<SQUARE_ID>"}

实测该端点**不需要登录、不需要 GL-CheckSum、不需要真机**，`GL-DeviceId` 填全零
也能返回 200 与完整 GameInfoEntity —— 所以这个模块**不接触任何账号凭证**，
对机位账号零风险。

═══════════════════════════════════════════════════════════════════════════
四种状态
═══════════════════════════════════════════════════════════════════════════
  NONE  尚未挂出            本期还没挂预约活动
  WAIT  已出链接·窗口未开    ★ 最有价值：App 里按钮是灰的，但 CDN 上能下
  OK    可下载              下载窗口已开
  （超时后 download_open=false，但 state 仍是 OK，链接依然有效）

═══════════════════════════════════════════════════════════════════════════
落盘
═══════════════════════════════════════════════════════════════════════════
  data/early_pkg.json  {"watched": [squareId...], "state": {squareId: 快照}}
"""
from __future__ import annotations

import json
import os
import random
import time

from . import netcfg

_DATA_DIR = os.path.abspath(os.environ.get("QFLY_DATA_DIR") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"))
FILE = os.path.join(_DATA_DIR, "early_pkg.json")

# ── 端点契约（来自大神 App 字节码）────────────────────────────────────────
API_PATH = "/v1/app/gameCenter/gameDetail"
HOST = "god.gameyw.netease.com"
GL_CLIENT_TYPE = "50"
GL_VERSION = "4.23.0"
GL_CHANNEL = "wyds_dl_homeholdh5"
NULL_DEVICE_ID = "00000000-0000-0000-0000-000000000000"
UA = "okhttp/4.9.0"

# 光·遇 —— 默认关注，也是这个功能存在的主要理由
SKY_SQUARE_ID = "5cb546a0d5456870b97d9424"
DEFAULT_WATCHED = [SKY_SQUARE_ID]

MAX_WATCHED = 30          # 关注上限：一次检查 = N 个上游请求，别让面板变成扫描器
PROBE_TIMEOUT = 25        # HEAD 探测 CDN 的秒数（包体 1.9G，只取头，很快）
API_TIMEOUT = 20
_cache = {"mtime": None, "data": None}


# ══════════════════════════════════════════════════════════════════════════
# 落盘
# ══════════════════════════════════════════════════════════════════════════
def _ensure_dir() -> None:
    os.makedirs(_DATA_DIR, exist_ok=True)


def load() -> dict:
    """带 mtime 缓存的读取；坏文件回落空档案，绝不抛。"""
    try:
        st = os.stat(FILE)
        key = (st.st_mtime_ns, st.st_size)
        if _cache["mtime"] == key and _cache["data"] is not None:
            return _cache["data"]
    except OSError:
        key = None
    data = {}
    if key is not None:
        try:
            with open(FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                data = {}
        except Exception:                                         # noqa: BLE001
            data = {}
    # watched 必须是字符串数组：脏档案里若是个字符串，逐字符迭代会造出一堆垃圾
    # squareId 去查上游，所以这里先卡类型再过滤空值。
    raw_w = data.get("watched")
    if not isinstance(raw_w, list):
        raw_w = []
    out = {"watched": [s.strip() for s in raw_w if isinstance(s, str) and s.strip()],
           "state": data.get("state") if isinstance(data.get("state"), dict) else {}}
    if not out["watched"]:
        out["watched"] = list(DEFAULT_WATCHED)
    _cache["mtime"] = key
    _cache["data"] = out
    return out


def save(data: dict) -> None:
    _ensure_dir()
    tmp = FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, FILE)
    try:
        os.chmod(FILE, 0o600)
    except Exception:                                             # noqa: BLE001
        pass
    _cache["mtime"] = None
    _cache["data"] = None


# ══════════════════════════════════════════════════════════════════════════
# 游戏表
# ══════════════════════════════════════════════════════════════════════════
def presets() -> list:
    """内置预设表（79 个网易游戏的 squareId）。"""
    try:
        from .earlypkg_games import GAMES
        return list(GAMES)
    except Exception:                                             # noqa: BLE001
        return [{"name": "光·遇", "appKey": "ma75", "squareId": SKY_SQUARE_ID}]


def name_of(square_id: str) -> str:
    for g in presets():
        if g.get("squareId") == square_id:
            return g.get("name") or square_id
    return square_id


def set_watched(square_ids) -> list:
    """设置关注列表。保留顺序、去重、过滤空值，超上限抛 ValueError。"""
    seen, out = set(), []
    for s in (square_ids or []):
        s = str(s or "").strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    if len(out) > MAX_WATCHED:
        raise ValueError("最多关注 %d 个游戏" % MAX_WATCHED)
    d = load()
    d["watched"] = out or list(DEFAULT_WATCHED)
    save(d)
    return d["watched"]


# ══════════════════════════════════════════════════════════════════════════
# 网络
# ══════════════════════════════════════════════════════════════════════════
def _headers() -> dict:
    now = int(time.time() * 1000)
    return {
        "Content-Type": "application/json; charset=utf-8",
        "GL-ClientType": GL_CLIENT_TYPE,
        "GL-CurTime": str(now),
        "GL-DeviceId": NULL_DEVICE_ID,
        "GL-Nonce": "%d_%d" % (now, random.getrandbits(63)),
        "GL-Version": GL_VERSION,
        "GL-Channel": GL_CHANNEL,
        "User-Agent": UA,
    }


def fetch(square_id: str, timeout: int = API_TIMEOUT, retries: int = 3) -> dict:
    """取 GameInfoEntity。失败抛 RuntimeError。

    出口走 netcfg.shared_session（与全站一致）：代理启用时按游戏粘性绑出口，
    未启用时就是普通 Session，与脚本单跑逐字节同行为。
    """
    url = "https://%s%s" % (HOST, API_PATH)
    s = netcfg.shared_session("earlypkg:" + square_id)
    last = None
    for attempt in range(retries):
        try:
            r = s.post(url, json={"squareId": square_id}, headers=_headers(), timeout=timeout)
            if r.status_code != 200:
                last = "HTTP %s %s" % (r.status_code, (r.text or "")[:160])
            else:
                payload = r.json()
                if "result" not in payload:
                    raise RuntimeError("响应无 result 字段: %s" % json.dumps(
                        payload, ensure_ascii=False)[:200])
                return payload["result"] or {}
        except Exception as e:                                    # noqa: BLE001
            last = "%s: %s" % (type(e).__name__, e)
        if attempt < retries - 1:
            time.sleep(1.5 * (attempt + 1) + random.random())
    raise RuntimeError(last or "未知错误")


def head_probe(url: str, timeout: int = PROBE_TIMEOUT) -> dict:
    """HEAD 探测 CDN：确认真能下、拿真实大小。"""
    try:
        s = netcfg.shared_session("earlypkg:probe")
        r = s.head(url, headers={"User-Agent": UA}, timeout=timeout, allow_redirects=True)
        h = {k.lower(): v for k, v in r.headers.items()}
        try:
            length = int(h.get("content-length", "0") or 0)
        except ValueError:
            length = 0
        return {"status": r.status_code, "length": length,
                "type": h.get("content-type", ""),
                "parent_file": h.get("x-parent-file", ""),
                "last_modified": h.get("last-modified", "")}
    except Exception as e:                                        # noqa: BLE001
        return {"status": -1, "error": "%s: %s" % (type(e).__name__, e)}


# ══════════════════════════════════════════════════════════════════════════
# 业务判定
# ══════════════════════════════════════════════════════════════════════════
def collect_links(result: dict) -> list:
    """挖出全部下载直链 —— [{tag, url}]，去重保序。"""
    out, seen = [], set()

    def add(tag, u):
        if isinstance(u, str) and u.startswith("http") and u not in seen:
            seen.add(u)
            out.append({"tag": tag, "url": u})

    vdi = result.get("versionDownloadInfo") or {}
    add("提前包主链接", vdi.get("downloadPackageUrl"))
    for r in (vdi.get("downLoadResources") or []):
        if isinstance(r, dict):
            add("提前包备用链 %s" % r.get("gameDownloadChannel", "?"), r.get("url"))

    gsi = result.get("gameStartupInfo") or {}
    add("常包 newApkDownload", gsi.get("newApkDownload"))
    add("常包 apkDownload1", gsi.get("apkDownload1"))
    for r in (gsi.get("downLoadResources") or []):
        if isinstance(r, dict):
            add("常包渠道链 %s" % r.get("gameDownloadChannel", "?"), r.get("url"))
    return out


def analyse(result: dict, now_ms: int = None) -> dict:
    """把一个 GameInfoEntity 压成可比较的状态快照。"""
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    vdi = result.get("versionDownloadInfo") or {}
    gb = result.get("gameBasicInfo") or {}
    gsi = result.get("gameStartupInfo") or {}

    has_vdi = bool(vdi)
    enabled = bool(vdi.get("enableVersionDownload"))
    main_url = vdi.get("downloadPackageUrl") or ""
    d_start, d_end = vdi.get("downloadStartTime"), vdi.get("downloadEndTime")
    b_start, b_end = vdi.get("bookStartTime"), vdi.get("bookEndTime")

    if has_vdi and enabled and main_url:
        if isinstance(d_start, int) and isinstance(d_end, int):
            state = "OK" if d_start <= now_ms <= d_end else "WAIT"
        else:
            state = "WAIT"
    else:
        state = "NONE"

    return {
        "state": state,
        "checked_at": now_ms,
        "name": result.get("name"),
        "appKey": result.get("appKey"),
        "squareId": result.get("squareId"),
        "store_version": gb.get("version"),
        "store_versionCode": gb.get("versionCode"),
        "size": gb.get("size"),
        "packageName": gsi.get("packageName"),
        "miniPackageName": gsi.get("miniPackageName"),
        "has_vdi": has_vdi,
        "enableVersionDownload": enabled,
        "bookUniqueId": vdi.get("bookUniqueId") or "",
        "bookName": vdi.get("name"),
        "bookType": vdi.get("bookType"),
        "bookVersion": vdi.get("version"),
        "bookStartTime": b_start,
        "bookEndTime": b_end,
        "downloadStartTime": d_start,
        "downloadEndTime": d_end,
        "pushTime": vdi.get("pushTime"),
        "downloadPackageUrl": main_url,
        "links": collect_links(result),
        "book_open": bool(isinstance(b_start, int) and isinstance(b_end, int)
                          and b_start <= now_ms <= b_end),
        "download_open": bool(isinstance(d_start, int) and isinstance(d_end, int)
                              and d_start <= now_ms <= d_end),
    }


# ══════════════════════════════════════════════════════════════════════════
# 检查
# ══════════════════════════════════════════════════════════════════════════
def check(square_ids=None, probe: bool = False, timeout: int = API_TIMEOUT) -> dict:
    """检查一轮并落库。返回 {squareId: 快照}；单个游戏失败不拖垮整体。

    快照里额外带：
      changed    本期 bookUniqueId 与上次不同 → 新一期
      first_seen 从「尚未挂出」第一次变成有链接
      probe      本次 HEAD 探测结果 {url: {...}}
      error      该游戏本轮失败原因（成功则无此键）
    """
    d = load()
    ids = [str(s).strip() for s in (square_ids or d["watched"]) if str(s or "").strip()]
    out = {}
    for sq in ids:
        prev = d["state"].get(sq) or {}
        try:
            snap = analyse(fetch(sq, timeout=timeout))
        except Exception as e:                                    # noqa: BLE001
            stale = dict(prev) if prev else {"squareId": sq, "state": "NONE"}
            stale["error"] = str(e)
            stale["checked_at"] = int(time.time() * 1000)
            out[sq] = stale
            d["state"][sq] = stale
            continue

        snap["changed"] = bool(prev.get("bookUniqueId")
                               and prev["bookUniqueId"] != snap["bookUniqueId"])
        snap["first_seen"] = bool(snap["state"] != "NONE" and not prev.get("bookUniqueId"))
        if snap["state"] != "NONE" and not prev.get("first_seen_at"):
            snap["first_seen_at"] = snap["checked_at"]
        else:
            snap["first_seen_at"] = prev.get("first_seen_at")

        if probe:
            snap["probe"] = {l["url"]: head_probe(l["url"]) for l in snap["links"]}
        out[sq] = snap
        d["state"][sq] = snap

    # 关注列表里已经删掉的游戏，状态留着但排到后面；面板按 watched 顺序渲染
    save(d)
    return out


def snapshot(probe: bool = False) -> dict:
    """给管理面板的完整视图：预设表 + 关注列表 + 当前状态。只读，不发起检查。"""
    d = load()
    watched = d["watched"]
    state = d["state"]
    games = []
    for sq in watched:
        snap = dict(state.get(sq) or {"squareId": sq, "state": "NONE"})
        snap.setdefault("name", name_of(sq))
        games.append(snap)
    return {
        "host": HOST,
        "max_watched": MAX_WATCHED,
        "skysquare": SKY_SQUARE_ID,
        "watched": watched,
        "games": games,
        "presets": presets(),
        "checked_any": any(s.get("checked_at") for s in games),
    }


def status() -> dict:
    """给 /api/admin/risk 一类的体检用：只报计数，不泄露链接。"""
    d = load()
    st = d["state"]
    return {"watched": len(d["watched"]), "max": MAX_WATCHED,
            "with_link": sum(1 for s in st.values() if s.get("downloadPackageUrl")),
            "waiting": sum(1 for s in st.values() if s.get("state") == "WAIT"),
            "file": "early_pkg.json"}
