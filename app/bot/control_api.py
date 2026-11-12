"""小光控制接口（W1 本机版）：只监听 127.0.0.1，必须带令牌。

和服务器版的区别
================
* 不再依赖 sky-play：静态页由这个进程直接提供（GET / 返回改过的 xiaoguang.html，
  /static/* 是 js/css）。API 路径仍然是前端现在用的 `/xiaoguang/...`，
  bot_api.py 原来做的转发这里自己实现；同时兼容不带前缀的 `/status`、`/accounts`。
* 启动时随机端口（`--port 0`）写 DATA/control/web.json；访问令牌由启动器生成，
  用环境变量 `XG_WEB_TOKEN` 传给子进程（不放命令行，进程列表看不到）。浏览器第一次
  带 `?t=<token>` 打开，服务端换成 HttpOnly cookie，之后只认 cookie；去掉 sky-play 登录。
* 首次运行（DATA 下没有 config.json）：`GET /` 先给风险告知页，勾选同意走
  `POST /api/consent`，写入同意时间后才显示面板。
* 进程/脚本操作全部走 xg_ctl、xg_proc、xg_runtime（不再 bash / pgrep / /proc）。
* 桌面版通过 `XG_PARENT_PID` + `XG_PARENT_CREATE_TIME` 传入宿主身份；看门狗每
  2.5 秒核对一次，宿主消失（崩溃/任务管理器结束/注销）就执行与 `/api/quit`
  相同的清理后退出，避免留下孤儿后台。

用法：python bot/control_api.py [--port 8791] [--token TOKEN] [--no-window]
"""
import argparse
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import unicodedata
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

_APP = Path(__file__).resolve().parent.parent
os.environ.setdefault("SKY_AI_ROOT", str(_APP))
if not os.environ.get("XG_DATA_DIR"):
    if os.name == "nt":
        _base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
        os.environ["XG_DATA_DIR"] = str(Path(_base) / "XiaoGuang" / "data")
    else:
        os.environ["XG_DATA_DIR"] = str(Path.home() / ".local/share/xiaoguang")
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(_APP))
sys.path.insert(0, str(_APP / "launcher"))

import map_catalog                                # noqa: E402  地图清单
import owner_title                                # noqa: E402
import xg_account                                 # noqa: E402
import xg_exec                                    # noqa: E402
import xg_paths                                   # noqa: E402
import xg_proc                                    # noqa: E402
import xg_runtime                                 # noqa: E402
import llm_providers                              # noqa: E402  W2：各家 AI 接口设置/测试
import autostart                                  # noqa: E402  W4：开机自启动
import capture_ctl                                # noqa: E402  W4：本机录图
import tasks                                      # noqa: E402  W4：后台长任务
import wizard                                     # noqa: E402  W4：首次安装向导

RISK_VERSION = 1
RISK_TEXT = (
    "第三方程序、直连网易服务器、账号可能被封（我们的号曾被判“使用外挂、脚本”封禁）、"
    "不得使用主号。请使用单独的账号，并自行承担风险。"
)
COOKIE = "xg_token"
LOG_EVENTS = {"heard", "said", "emote", "control", "not_replied", "offer_to_me", "offer_accept_requested",
              "offer_denied", "offer_expired", "friend_add", "friend_code_shared", "outfit_read", "reply_fixed"}
LOCK = threading.Lock()
PARENT_CHECK_SECONDS = 2.5
PARENT_PID_ENV = "XG_PARENT_PID"
PARENT_CREATE_ENV = "XG_PARENT_CREATE_TIME"
_SHUTDOWN_LOCK = threading.Lock()
_SHUTDOWN_STARTED = False
_SERVER = None

BOT = xg_paths.BOT_CODE
WEB = xg_paths.WEB
PINNED = {"service_account.js", "desktop.css", "desktop.js", "auth_service.js", "xiaoguang.html", "xiaoguang.js", "xiaoguang.css", "style.css", "risk.html",
          "standalone.css", "ai_settings.js", "w4b.js", "wizard.html", "wizard.js", "wizard.css"}


# ------------------------------------------------------------------ 状态
def consent_ok() -> bool:
    cfg = xg_paths.config()
    c = (cfg or {}).get("consent") or {}
    return bool(c.get("accepted")) and int(c.get("version") or 0) >= RISK_VERSION


def wizard_done() -> bool:
    return bool(((xg_paths.config() or {}).get("wizard") or {}).get("completed_at"))


def ai_status() -> dict:
    """AI 设置摘要（绝不含 key）。配置坏了也只返回错误，不让 /status 挂掉。"""
    try:
        return llm_providers.settings_status(llm_providers.load_settings())
    except Exception as exc:
        return {"ok": False, "configured": False, "has_key": False,
                "error": llm_providers._scrub(f"AI 设置读取失败：{type(exc).__name__}")}


def chat_running() -> bool:
    return bool(xg_proc.find_script("bot/ai_companion.py"))


def _accounts_doc():
    data = xg_paths.read_json(xg_paths.ACCOUNTS_JSON, None)
    if isinstance(data, dict):
        items = data.get("accounts", data)
    else:
        items = data
    if isinstance(items, dict):
        items = list(items.values())
    return [x for x in (items or []) if isinstance(x, dict)]


def _account_view(a, active_id):
    note = a.get("status_note") or ""
    ban = xg_account.BAN_RE.search(note)
    banned, until = xg_account.ban_status(a)
    uuid = a.get("user_uuid") or ""
    try:
        maps = len(xg_account.recorded_maps(uuid))
    except Exception:
        maps = 0
    ready = bool(uuid) and maps > 0 and not banned
    why = ""
    if not uuid:
        why = "账号库里没有 user_uuid"
    elif banned:
        why = "这个号被封了（禁止登录%s）" % (("至 " + until) if until else "")
    elif maps == 0:
        why = xg_account.NO_CAPTURE_NOTE
    return {"id": a.get("id"), "alias": a.get("alias") or "", "uuid": uuid[:8],
            "platform": a.get("platform") or "", "status": a.get("status") or "unknown",
            "note": note[:120], "bannedUntil": ban.group(1) if ban else None,
            "created": time.strftime("%m-%d %H:%M", time.localtime(a["created_at"])) if a.get("created_at") else None,
            "active": a.get("id") == active_id,
            "ready": ready, "readyNote": why, "maps": maps}


def accounts_payload():
    return account_cmd("list")


def account_status():
    aid = xg_account.active_id()
    acc = xg_account.account_by_id(aid)
    if not acc:
        return {"state": "unknown", "note": "没有当前账号"}
    return {"state": acc.get("status") or "unknown", "note": (acc.get("status_note") or "")[:120]}


def tail_jsonl(path, n):
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()[-max(n * 6, 400):]
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def logs(limit):
    rows = []
    for e in tail_jsonl(xg_paths.LOGS / "companion.jsonl", limit):
        ev = e.get("event")
        if ev not in LOG_EVENTS:
            continue
        who = e.get("name") or (e.get("sender") or e.get("target") or "")[:8]
        text = e.get("msg") or e.get("note") or e.get("reason") or ""
        if ev == "emote":
            text = e.get("name", "")
        if ev == "control":
            text = f"{e.get('kind') or e.get('pose')}"
        if ev.startswith("offer"):
            text = e.get("name") or str(e.get("type", ""))
        rows.append({"ts": e.get("ts", ""), "event": ev,
                     "who": who if ev not in ("said", "emote", "control") else "小光",
                     "text": str(text)[:200]})
    return rows[-limit:]


def status():
    owner = xg_account.owner_uuid()
    lv = xg_runtime.live(owner)
    chat = chat_running()
    ai = ai_status()
    maps = [{"id": m["id"], "name": m["name"], "region": m["region"], "scene": m["scene"], "ready": True}
            for m in xg_runtime.startable_maps()]
    st = xg_paths.read_json(xg_paths.POSE_STATE, None) or {}
    room = st.get("room") or {}
    offers = st.get("offers") or []
    return {
        "online": lv["online"], "runner": lv["runner"], "chat": chat,
        "startupError": lv.get("startupError"),
        "phase": "observing" if lv["online"] else None,
        "map": lv["map"], "mapName": lv["mapName"], "followOwner": True,
        "changing": lv["changing"], "pendingMap": lv["pendingMap"], "pendingName": lv["pendingName"],
        "going": lv["going"], "windowEnd": lv["windowEnd"], "home": xg_runtime.HOME,
        "segment": None, "since": lv["since"], "stopped": None,
        "room": {"players": lv["players"], "ownerHere": lv["ownerHere"], "pose": lv["pose"],
                 "offers": len(offers), "updated": st.get("ts")},
        "session": account_status(), "maps": maps, "owner": owner, "v": 2,
        "stable": xg_runtime.stable_info(), "ownerTitle": owner_title.get(),
        "autoRenew": {"lastTry": None, "lastOk": None, "result": None, "note": "", "lastRenewAt": None},
        "consent": consent_ok(),
        "aiConfigured": bool(ai.get("configured")),
        "aiProvider": ai.get("provider"),
        "wizardCompleted": wizard_done(),
        "captureState": capture_ctl.status().get("state"),
    }


# ------------------------------------------------------------------ 动作
def _run(args, stdin=None, timeout=120):
    try:
        r = xg_exec.run([str(xg_paths.python_exe()), "-B", *map(str, args)], cwd=str(xg_paths.APP),
                        input=stdin, capture_output=True, text=True, timeout=timeout,
                        env=dict(os.environ, PYTHONPATH=os.pathsep.join(
                            [str(xg_paths.BOT_CODE), str(xg_paths.QFSKY_CODE.parent),
                             str(xg_paths.SRC), str(xg_paths.APP)])))
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return -1, str(exc)


def _last_json(text):
    for line in reversed((text or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return None


def account_cmd(cmd, payload=None):
    code, out = _run([BOT / "account_login.py", cmd], stdin=json.dumps(payload or {}, ensure_ascii=False), timeout=390 if cmd == "renew" else 195)
    return _last_json(out) or {"ok": False, "message": "账号工具没有返回结果：" + out[-160:]}


def persona_cmd(*args, stdin=None):
    code, out = _run([BOT / "personas.py", *args], stdin=stdin, timeout=30)
    return _last_json(out) or {"ok": False, "message": "人设工具没有返回结果：" + out[-160:]}


def kill_chats(timeout=3.0):
    return xg_proc.stop_all(("bot/ai_companion.py",), grace=timeout)


def account_process_matches(row, identity):
    argv = xg_proc.argv_of(row["pid"])
    for i, item in enumerate(argv):
        if item == "--account" and i + 1 < len(argv):
            return str(argv[i + 1]) == identity
        if str(item).startswith("--account="):
            return str(item).split("=", 1)[1] == identity
    return identity == xg_account.active_id()


def renew_account(identity):
    # Rotate offline credentials only; never silently disconnect a live game.
    if not xg_account.account_by_id(identity):
        return {"ok": False, "message": "没有这个账号，请先刷新账号列表"}
    running = xg_proc.find_any_script(xg_runtime.RUN_SCRIPTS + xg_runtime.GO_SCRIPTS)
    if any(account_process_matches(row, identity) for row in running):
        return {"ok": False, "message": "该账号正在上线、换图或在线，请先让小光下线，再续期。"}
    # A failed runner may leave its chat retry loop alive. Stop only this account's chat.
    for row in xg_proc.find_script("bot/ai_companion.py"):
        if account_process_matches(row, identity):
            xg_proc.terminate(row["pid"], grace=3.0)
    out = account_cmd("renew", {"account": identity})
    if out.get("ok"):
        xg_account.cache_clear()
    return out


def renew(attempts=1):
    from xg_auth_client import Client, AuthError
    try:
        Client().renew(xg_account.active_id())
        return True, "私有服务已完成续期，运行凭据已同步"
    except AuthError as exc:
        return False, "续期未完成：" + exc.code


def stop_little_light():
    kill_chats()
    did = xg_runtime.stop_all()
    try:
        from xg_auth_client import Client
        identity = xg_account.active_id()
        if identity:
            Client().request("POST", "/v1/accounts/" + identity + "/release", timeout=3)
    except Exception:
        pass  # Local stop always succeeds even if the private service is offline.
    return did


def go_map(map_id):
    if xg_proc.find_script("bot/long_runner.py"):
        stop_little_light()
        time.sleep(1)
    return xg_runtime.go(int(map_id))


# ------------------------------------------------------------------ 生命周期
def _lifecycle_event(event, **fields):
    """One append-only JSON log line for startup/exit diagnosis; no secrets."""
    row = {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event}
    row.update(fields)
    try:
        xg_paths.LOGS.mkdir(parents=True, exist_ok=True)
        with (xg_paths.LOGS / "control_api.log").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _parent_from_env(environ=None):
    """Return {pid, create_time} supplied by desktop.py, or None when unset/invalid."""
    env = os.environ if environ is None else environ
    raw_pid = env.get(PARENT_PID_ENV)
    if not raw_pid:
        return None
    try:
        pid = int(raw_pid)
        create_time = float(env.get(PARENT_CREATE_ENV) or "")
    except (TypeError, ValueError):
        _lifecycle_event("parent_watchdog_disabled", reason="invalid_parent_identity",
                         parent_pid=str(raw_pid)[:32])
        return None
    if pid <= 0 or create_time <= 0:
        _lifecycle_event("parent_watchdog_disabled", reason="invalid_parent_identity",
                         parent_pid=pid)
        return None
    return {"pid": pid, "create_time": round(create_time, 6)}


def _parent_alive(parent):
    """PID plus creation time: a recycled PID is not our parent."""
    row = xg_proc.identity(parent["pid"])
    if row is None or row.get("state") == "Z":
        return False
    try:
        return abs(float(row["create_time"]) - float(parent["create_time"])) <= 1e-3
    except (KeyError, TypeError, ValueError):
        return False


def _request_shutdown(reason, **fields):
    """Run the same cleanup as /api/quit, exactly once, then stop HTTP serving."""
    global _SHUTDOWN_STARTED
    with _SHUTDOWN_LOCK:
        if _SHUTDOWN_STARTED:
            return False
        _SHUTDOWN_STARTED = True
    _lifecycle_event("shutdown_requested", reason=reason, pid=os.getpid(), **fields)

    def _finish():
        time.sleep(0.8)  # Let /api/quit flush its response before teardown.
        try:
            import launcher
            launcher.stop(xg_paths, all_proc=True)
        except Exception as exc:  # noqa: BLE001 - HTTP shutdown must still run
            _lifecycle_event("shutdown_cleanup_failed", reason=reason,
                             error=type(exc).__name__)
        finally:
            srv = _SERVER
            if srv is not None:
                try:
                    srv.shutdown()
                except Exception:  # noqa: BLE001
                    pass
        _lifecycle_event("shutdown_cleanup_done", reason=reason)

    threading.Thread(target=_finish, name="xg-shutdown", daemon=True).start()
    return True


def _watch_parent(stop_evt, parent):
    """Poll every 2.5 s; the normal /api/quit cleanup runs if the host disappears."""
    if not _parent_alive(parent):
        _lifecycle_event("parent_missing_at_startup", parent_pid=parent["pid"])
        _request_shutdown("parent_exited", parent_pid=parent["pid"])
        return
    while not stop_evt.wait(PARENT_CHECK_SECONDS):
        if not _parent_alive(parent):
            _lifecycle_event("parent_exited", parent_pid=parent["pid"],
                             parent_create_time=parent["create_time"])
            _request_shutdown("parent_exited", parent_pid=parent["pid"])
            return


def _start_parent_watchdog(stop_evt):
    parent = _parent_from_env()
    if parent is None:
        return None
    threading.Thread(target=_watch_parent, args=(stop_evt, parent),
                     name="xg-parent-watchdog", daemon=True).start()
    _lifecycle_event("parent_watchdog_started", parent_pid=parent["pid"],
                     parent_create_time=parent["create_time"],
                     interval=PARENT_CHECK_SECONDS)
    return parent


# ------------------------------------------------------------------ HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "xiaoguang-local"
    protocol_version = "HTTP/1.1"
    server_token = ""
    started_at = time.time()
    token_used = False

    def log_message(self, fmt, *args):
        return  # No URLs, bootstrap tokens or credential-bearing request logs.

    # -- 输出
    def reply(self, code, obj, headers=None):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def reply_file(self, path, ctype, headers=None):
        try:
            body = Path(path).read_bytes()
        except OSError:
            return self.reply(404, {"error": "not_found"})
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def redirect(self, location, headers=None):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()

    # -- 鉴权
    def token(self):
        return self.server_token or Handler.server_token

    def cookie_token(self):
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE:
                return v
        return ""

    def authed(self):
        return bool(self.token()) and hmac.compare_digest(self.cookie_token(), self.token())

    def same_origin(self):
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = self.headers.get("Host") or ""
        return origin in (f"http://{host}", f"https://{host}")

    def set_cookie_headers(self):
        return {"Set-Cookie": f"{COOKIE}={self.token()}; HttpOnly; SameSite=Strict; Path=/",
                "X-Content-Type-Options": "nosniff"}

    def do_GET(self):
        u = urlsplit(self.path)
        path = u.path
        q = parse_qs(u.query)

        # 一次性 token 换 cookie（启动器打开浏览器时带的 ?t=）
        if q.get("t") and hmac.compare_digest(q["t"][0], self.token()):
            if Handler.token_used and not self.authed():
                return self.reply(403, {"error": "token_used"})
            Handler.token_used = True
            target = path or "/"
            return self.redirect(target, self.set_cookie_headers())

        if path.startswith("/static/"):
            name = path[len("/static/"):]
            if name not in PINNED:
                return self.reply(404, {"error": "not_found"})
            ctype = {".js": "text/javascript; charset=utf-8",
                     ".css": "text/css; charset=utf-8"}.get(Path(name).suffix, "text/plain; charset=utf-8")
            return self.reply_file(WEB / name, ctype)

        if path in ("/", "/index.html", "/xiaoguang"):
            if not consent_ok():
                return self.reply_file(WEB / "risk.html", "text/html; charset=utf-8")
            return self.reply_file(WEB / "xiaoguang.html", "text/html; charset=utf-8")

        if path in ("/desktop", "/desktop/"):
            return self.reply_file(WEB / "desktop.html", "text/html; charset=utf-8")

        if path in ("/wizard", "/wizard/"):
            if not consent_ok():
                return self.reply_file(WEB / "risk.html", "text/html; charset=utf-8")
            return self.reply_file(WEB / "wizard.html", "text/html; charset=utf-8")

        if path == "/api/consent":
            return self.reply(200, {"ok": consent_ok()})

        if path == "/auth/csrf":
            return self.reply(200, {"csrfToken": ""})

        if not self.authed():
            return self.reply(403, {"error": "forbidden"})

        api = path[len("/xiaoguang"):] if path.startswith("/xiaoguang") else path
        if api.startswith("/api/service-account/"):
            import xg_service_identity
            busy = bool(xg_proc.find_script("bot/long_runner.py") or chat_running())
            code, result = xg_service_identity.handle(api[len("/api/service-account/"):], method="GET", busy=busy)
            return self.reply(code, result)
        if api == "/api/auth-service/status":
            from xg_auth_client import read_config, AuthError
            try:
                cfg = read_config()
                return self.reply(200, {"ok": True, "configured": True, "url": cfg["url"]})
            except AuthError:
                return self.reply(200, {"ok": True, "configured": False})
        if api == "/api/auth-service/accounts":
            return self.reply(200, account_cmd("list"))

        if api == "/ai":
            return self.reply(200, ai_status())
        if api == "/ai/providers":
            return self.reply(200, {"ok": True, "providers": llm_providers.preset_list(),
                                    "stored_key": ai_status().get("key_hint")})
        if api == "/status":
            return self.reply(200, status())
        if api == "/accounts":
            return self.reply(200, accounts_payload())
        if api == "/api/device":
            # W7：本机设备摘要（永远不含 key）。
            try:
                import local_device
                doc = local_device.status_all()
                doc["pool"] = local_device.pool_summary()
                return self.reply(200, doc)
            except Exception as exc:
                return self.reply(500, {"ok": False,
                                        "message": "读取本机设备失败：" + str(exc)[:160]})
        if api == "/chat/trigger":
            path = xg_paths.CONTROL / "chat_trigger.json"
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                doc = {}
            if not isinstance(doc, dict):
                doc = {}
            return self.reply(200, {"ok": True,
                                    "mode": "all" if doc.get("mode") == "all" else "prefix",
                                    "prefix": (doc.get("prefix") if isinstance(doc.get("prefix"), str) and doc.get("prefix").strip() else "/")})
        if api == "/personas":
            return self.reply(200, persona_cmd("list"))
        if api == "/friends":
            return self.reply(200, account_cmd("friends"))
        if api == "/logs":
            n = q.get("limit", ["60"])[0]
            return self.reply(200, {"rows": logs(max(1, min(200, int(n) if n.isdigit() else 60)))})
        if api == "/api/wizard":
            return self.reply(200, wizard.summary())
        if api == "/api/wizard/task":
            try:
                doc = tasks.current()
                wizard.after_task(doc)
                wizard.ensure_catalog()
            except Exception as exc:
                doc = {"ok": False, "running": False,
                       "error": f"任务状态读取失败：{type(exc).__name__}"}
            return self.reply(200, doc)
        if api in ("/api/capture", "/api/capture/status"):
            # 采集状态只把与当前游戏版本一致的录制算作有效；旧录制单独标成 stale。
            return self.reply(200, wizard.capture_status())
        if api == "/api/autostart":
            return self.reply(200, autostart.status())
        return self.reply(404, {"error": "not_found"})

    def do_POST(self):
        u = urlsplit(self.path)
        path = u.path
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if 0 <= n <= 2 * 1024 * 1024 else b""
            data = json.loads(raw or b"{}")
        except (ValueError, OSError):
            return self.reply(400, {"error": "bad_json"})

        if not isinstance(data, dict):
            return self.reply(400, {"error": "bad_json"})

        if path == "/api/consent":
            if consent_ok():
                return self.reply(200, {"ok": True, "already": True})
            if data.get("accepted") is not True:
                return self.reply(400, {"ok": False, "message": "要先勾选同意才能继续"})
            cfg = xg_paths.config() or {}
            cfg["consent"] = {"accepted": True, "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                              "version": RISK_VERSION, "text": RISK_TEXT}
            cfg.setdefault("web", {"port": self.server.server_address[1]})
            xg_paths.save_config(cfg)
            return self.reply(200, {"ok": True}, self.set_cookie_headers())

        if not self.authed():
            return self.reply(403, {"error": "forbidden"})
        if not self.same_origin():
            return self.reply(403, {"error": "origin"})

        api = path[len("/xiaoguang"):] if path.startswith("/xiaoguang") else path
        if api.startswith("/api/service-account/"):
            import xg_service_identity
            busy = bool(xg_proc.find_script("bot/long_runner.py") or chat_running())
            code, result = xg_service_identity.handle(api[len("/api/service-account/"):], payload=data, busy=busy)
            return self.reply(code, result)
        if api.startswith("/api/auth-service/"):
            from xg_auth_client import Client, AuthError, configure
            try:
                if api == "/api/auth-service/config":
                    if xg_proc.find_script("bot/long_runner.py") or chat_running():
                        return self.reply(409, {"ok": False, "message": "请先停止小光，再切换登录服务"})
                    cfg = {"url": str(data.get("url", "")), "token": str(data.get("token", ""))}
                    Client(cfg).request("GET", "/v1/accounts")
                    configure(cfg["url"], cfg["token"])
                    return self.reply(200, {"ok": True})
                if api == "/api/auth-service/job":
                    out = Client().request("GET", "/v1/login-jobs/" + str(data.get("job_id", "")))
                    return self.reply(200, out)
                if api == "/api/auth-service/continue":
                    if data.get("kind") == "upload_sms":
                        out = account_cmd("confirm", {"ticket": data.get("job_id", "")})
                    else:
                        out = account_cmd("verify", {"pending": data.get("job_id", ""), "code": data.get("code", "")})
                    return self.reply(200 if out.get("ok") else 400, out)
            except AuthError as exc:
                return self.reply(400, {"ok": False, "error": exc.code})


        if api == "/api/quit":
            _request_shutdown("api_quit")
            return self.reply(200, {"ok": True, "message": "小光和后台正在退出，可以关掉这个页面了。"})

        if not LOCK.acquire(blocking=False):
            return self.reply(409, {"error": "busy", "message": "上一个操作还没完成"})
        try:
            if api == "/chat/trigger":
                # Keep legacy all-chat mode readable/writable; the desktop only
                # exposes explicit-prefix replies. Never coerce an empty prefix.
                mode = data.get("mode", "prefix")
                prefix = data.get("prefix")
                if mode not in ("prefix", "all"):
                    return self.reply(400, {"ok": False, "message": "触发方式无效"})
                if (not isinstance(prefix, str)
                        or any(unicodedata.category(ch).startswith("C")
                               or unicodedata.category(ch) in ("Zl", "Zp") for ch in prefix)):
                    return self.reply(400, {"ok": False, "message": "前缀须为 1～3 个字符，不能含控制字符或换行"})
                prefix = prefix.strip()
                if not 1 <= len(prefix) <= 3:
                    return self.reply(400, {"ok": False, "message": "前缀须为 1～3 个非空字符"})
                try:
                    xg_paths.write_json(xg_paths.CONTROL / "chat_trigger.json",
                                        {"mode": mode, "prefix": prefix})
                except OSError:
                    return self.reply(500, {"ok": False, "message": "保存前缀失败，原设置未更改"})
                return self.reply(200, {"ok": True, "mode": mode, "prefix": prefix,
                                        "message": "已保存，下一句聊天生效"})
            if api in ("/api/device/ensure", "/api/device/register"):
                import local_device
                platform = str(data.get("platform") or "android")
                if api == "/api/device/register" and data.get("confirm") is not True:
                    return self.reply(400, {"ok": False,
                                            "message": "重新注册设备需要二次确认（confirm=true）"})
                try:
                    if api == "/api/device/register":
                        st = local_device.register(platform)
                    else:
                        st = local_device.ensure(platform)
                except Exception as exc:
                    msg = " ".join(str(exc).split())[:200] or "本机设备注册失败"
                    return self.reply(400, {"ok": False, "device": False,
                                            "message": "本机设备注册失败：" + msg})
                out = {"ok": True, "device": st}
                out.update({k: v for k, v in local_device.status_all().items() if k != "ok"})
                out["pool"] = local_device.pool_summary()
                out["message"] = st.get("message") or ""
                return self.reply(200, out)
            if api == "/api/wizard/mumu":
                r = wizard.check_mumu(data.get("adb") or None)
                return self.reply(200 if r.get("ok") else 400, r)
            if api == "/api/wizard/apk/pull":
                r = wizard.pull_apk()
                return self.reply(200 if r.get("ok") else 409, r)
            if api == "/api/wizard/apk/check":
                r = wizard.check_apk(data.get("path"))
                return self.reply(200 if r.get("ok") else 400, r)
            if api == "/api/wizard/data/start":
                r = wizard.start_data(bool(data.get("resume")))
                return self.reply(200 if r.get("ok") else 409, r)
            if api == "/api/wizard/login/done":
                r = wizard.login_done()
                return self.reply(200 if r.get("ok") else 400, r)
            if api == "/api/wizard/capture/start":
                r = wizard.start_capture()
                return self.reply(200 if r.get("ok") else 500, r)
            if api == "/api/wizard/capture/stop":
                return self.reply(200, wizard.stop_capture())
            if api == "/api/wizard/capture/register":
                r = wizard.register_capture(data.get("dir") or "")
                return self.reply(200 if r.get("ok") else 400, r)
            if api == "/api/wizard/catalog/start":
                r = wizard.start_catalog(data.get("account"), data.get("apk"))
                return self.reply(200 if r.get("ok") else 409, r)
            if api == "/api/wizard/ai/done":
                r = wizard.ai_done()
                return self.reply(200 if r.get("ok") else 400, r)
            if api == "/api/wizard/owner":
                r = wizard.owner_done(data.get("uuid"), data.get("title") or "",
                                      data.get("persona") or "")
                return self.reply(200 if r.get("ok") else 400, r)
            if api == "/api/wizard/finish":
                r = wizard.finish()
                return self.reply(200 if r.get("ok") else 400, r)
            if api == "/api/wizard/task/stop":
                return self.reply(200, tasks.stop())
            if api == "/api/autostart":
                want = bool(data.get("enabled"))
                r = autostart.enable() if want else autostart.disable()
                return self.reply(200 if r.get("ok") else 500, r)
            if api in ("/start", "/go"):
                target = int(data.get("map") or xg_runtime.HOME)
                try:
                    m = go_map(target)
                except (ValueError, RuntimeError) as exc:
                    return self.reply(409, {"ok": False, "message": str(exc)})
                msg = ("小光正在回遇境，大约半分钟" if target == xg_runtime.HOME
                       else f"小光正在前往「{m['name']}」，大约半分钟")
                return self.reply(200, {"ok": True, "going": target, "message": msg})
            if api == "/api/restart_service":
                # 原地重启后台进程：**必须沿用当前端口**。启动器是用 --port 0（随机端口）
                # 起的，端口记在 control/web.json；直接复用 argv 会换到新随机端口，
                # 页面就变成"无法访问此网站"。
                try:
                    port_now = int(self.server.server_address[1])
                except Exception:
                    port_now = 0
                argv = []
                skip_next = False
                for a in sys.argv[1:]:
                    if skip_next:
                        skip_next = False
                        continue
                    if a == "--port":
                        skip_next = True
                        continue
                    argv.append(a)
                if port_now:
                    argv += ["--port", str(port_now)]
                # 令牌必须原样带上：启动器是用环境变量 XG_WEB_TOKEN 传的，而 main() 启动时
                # 已经把它 pop 掉了；execv 后进程读不到，会退回去读 control/api.token，
                # 结果和页面里的 cookie 对不上 → “本机令牌失效了”。
                try:
                    tokn = self.token()
                except Exception:
                    tokn = ""
                if tokn:
                    argv += ["--token", tokn]
                # 让新进程起来后把小光也重启一遍（按钮语义 = 重启小光 + 后台）
                try:
                    (xg_paths.CONTROL / "restart_bot_pending").write_text("1", encoding="utf-8")
                except OSError:
                    pass
                script = str(Path(__file__).resolve())

                def _reexec():
                    try:
                        sys.stdout.flush()
                        sys.stderr.flush()
                    except Exception:
                        pass
                    try:
                        os.execv(sys.executable, [sys.executable, "-B", script] + argv)
                    except Exception as exc:  # noqa: BLE001
                        try:
                            (xg_paths.LOGS / "control_api.log").open("a", encoding="utf-8").write(
                                json.dumps({"event": "restart_service_exec_failed", "error": str(exc)[:200]}) + chr(10))
                        except Exception:
                            pass
                threading.Timer(1.0, _reexec).start()
                return self.reply(200, {"ok": True, "port": port_now,
                                        "message": "后台正在重启（端口 %d），3 秒后自动刷新页面" % port_now})
            if api == "/restart":
                cur = status()
                target = int(data.get("map") or cur["map"] or xg_runtime.HOME)
                stop_little_light()
                time.sleep(1)
                try:
                    go_map(target)
                except (ValueError, RuntimeError) as exc:
                    return self.reply(409, {"ok": False, "message": str(exc)})
                return self.reply(200, {"ok": True, "going": target, "message": "小光正在原地重连，大约半分钟"})
            if api == "/stop":
                did = stop_little_light()
                return self.reply(200, {"ok": True, "message": "已下线" if did else "本来就不在线"})
            if api == "/ai":
                try:
                    st = llm_providers.save_settings(data)
                except llm_providers.ProviderError as exc:
                    return self.reply(400, {"ok": False, "message": exc.message})
                except Exception as exc:
                    return self.reply(500, {"ok": False,
                                            "message": llm_providers._scrub(f"保存失败：{type(exc).__name__}")})
                running = chat_running()
                out = {"ok": True, **llm_providers.settings_status(st),
                       "chat_running": running, "restart_required": running}
                if running:
                    out["message"] = "已保存。聊天进程正在运行，点“重启聊天生效”让新接口马上生效。"
                elif st.configured:
                    out["message"] = "已保存。下次启动聊天就会用这个接口。"
                else:
                    out["message"] = "已保存，但还没配齐（服务商/地址/模型/Key），聊天暂时用不了。"
                return self.reply(200, out)
            if api == "/ai/models":
                return self.reply(200, llm_providers.models_for(data))
            if api == "/ai/test":
                return self.reply(200, llm_providers.test_for(data))
            if api == "/chat/restart":
                code_, out = _run([xg_paths.LAUNCHER / "xg_ctl.py", "restart-chat"], timeout=90)
                doc = _last_json(out) or {}
                ok = code_ == 0 and doc.get("ok") is not False
                return self.reply(200 if ok else 500, {
                    "ok": ok, "message": "聊天已重启，新设置已生效" if ok else "重启聊天失败",
                    "detail": re.sub(r"\s+", " ", out)[-300:] if not ok else ""})
            if api in ("/renew", "/accounts/renew"):
                identity = str(data.get("account") or xg_account.active_id()) if api == "/renew" else str(data.get("account") or "")
                out = renew_account(identity)
                return self.reply(200 if out.get("ok") else 409, out)
            if api == "/accounts/remark":
                out = account_cmd("remark", {"account": data.get("account"), "nickname": data.get("nickname", "")})
                return self.reply(200 if out.get("ok") else 400, out)
            if api == "/memory/reset":
                scope = "all" if data.get("scope") == "all" else "chat"
                if xg_proc.find_script("bot/ai_companion.py"):
                    (xg_paths.CONTROL / "memory_reset").write_text(json.dumps({"scope": scope}))
                else:
                    mf = xg_paths.LOGS / "memory.json"
                    try:
                        m = json.loads(mf.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        m = {}
                    m.update(recent=[], summary="", events=[])
                    if scope == "all":
                        m["people"] = {}
                    mf.parent.mkdir(parents=True, exist_ok=True)
                    mf.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
                return self.reply(200, {"ok": True, "scope": scope,
                                        "message": "已清空" + ("全部记忆" if scope == "all" else "聊天上下文")})
            if api in ("/personas/select", "/personas/save", "/personas/delete"):
                if api == "/personas/select":
                    out = persona_cmd("select", str(data.get("persona", "")))
                elif api == "/personas/delete":
                    out = persona_cmd("delete", str(data.get("persona", "")))
                else:
                    out = persona_cmd("save", stdin=json.dumps(
                        {"id": data.get("persona") or "", "name": data.get("name") or "",
                         "prompt": data.get("prompt") or ""}, ensure_ascii=False))
                return self.reply(200 if out.get("ok") else 400, out)
            if api == "/owner/title":
                t = str(data.get("title") or "")
                if data.get("check") is True:
                    ok, why = owner_title.check(t)
                    return self.reply(200, {"ok": ok, "message": why})
                ok, why = owner_title.set_title(t)
                if not ok:
                    return self.reply(400, {"ok": False, "message": why})
                running = chat_running()
                message = why + ("。点“重启聊天生效”使用新称呼，不影响游戏连接。" if running
                                 else "。下次启动聊天生效。")
                return self.reply(200, {"ok": True, "message": message, "title": owner_title.get(),
                                        "chat_running": running, "restart_required": running})
            if api == "/owner":
                uuid = str(data.get("uuid") or "").strip()
                if not xg_account.UUID_RE.fullmatch(uuid):
                    return self.reply(400, {"ok": False, "message": "uuid 形状不对"})
                xg_paths.OWNER_UUID.parent.mkdir(parents=True, exist_ok=True)
                xg_paths.OWNER_UUID.write_text(uuid + "\n", encoding="utf-8")
                return self.reply(200, {"ok": True, "message": "已设为哥哥，下次上线生效"})
            if api == "/accounts/select":
                from xg_auth_client import Client, AuthError
                try:
                    Client().session(str(data.get("account", "")))
                except AuthError as exc:
                    return self.reply(400, {"ok": False, "message": exc.code})
                acc_id = str(data.get("account") or "")
                row = xg_account.account_by_id(acc_id)
                if row is None:
                    return self.reply(400, {"ok": False, "message": "没有这个账号"})
                banned, until = xg_account.ban_status(row)
                if banned:
                    return self.reply(409, {"ok": False, "message": "这个号被封了，不能切"})
                xg_paths.ACTIVE_ACCOUNT.parent.mkdir(parents=True, exist_ok=True)
                xg_paths.ACTIVE_ACCOUNT.write_text(acc_id + "\n", encoding="utf-8")
                xg_account.cache_clear()
                xg_runtime.refresh(acc_id)
                kill_chats()
                return self.reply(200, {"ok": True, "active": acc_id,
                                        "message": f"已切换到 {row.get('alias') or acc_id}，点上线就用新号"})
            routes = {"/login/send-sms": ("send", ("mobile", "platform")),
                      "/login/verify": ("verify", ("pending", "code")),
                      "/login/email": ("email", ("email", "password", "platform", "remember")),
                      "/login/confirm-sms": ("confirm", ("ticket",))}
            if api in routes:
                cmd, keys = routes[api]
                out = account_cmd(cmd, {k: data[k] for k in keys if k in data})
                return self.reply(200 if out.get("ok") else 400, out)
            return self.reply(404, {"error": "not_found"})
        except (ValueError, subprocess.TimeoutExpired) as exc:
            return self.reply(400, {"ok": False, "message": str(exc)[:200]})
        finally:
            LOCK.release()


def _write_web_json(port, token):
    doc = {"port": int(port), "token": token, "cookie_name": COOKIE, "pid": os.getpid(),
           "create_time": (xg_proc.identity(os.getpid()) or {}).get("create_time"),
           "started": time.strftime("%Y-%m-%dT%H:%M:%S"), "version": 1}
    xg_paths.CONTROL.mkdir(parents=True, exist_ok=True)
    xg_paths.write_json(xg_paths.WEB_JSON, doc)


def _monitor_loop(stop_evt):
    """后台巡检：刷新长任务、录图完成后自动起 catalog。不依赖页面是否开着。"""
    while not stop_evt.is_set():
        try:
            doc = tasks.current()
            wizard.after_task(doc)
            wizard.ensure_catalog()
        except Exception:
            pass
        stop_evt.wait(1.5)


def main(argv=None):
    global _SERVER
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8791)
    ap.add_argument("--token", default="")
    a = ap.parse_args(argv)
    xg_paths.ensure_dirs()
    token = a.token or os.environ.pop("XG_WEB_TOKEN", "")
    if not token:
        # 服务器兼容：sky-play 读 DATA/control/api.token（同一份）。没有就生成。
        try:
            token = (xg_paths.CONTROL / "api.token").read_text().strip()
        except OSError:
            token = ""
        if not token:
            token = secrets.token_urlsafe(24)
            (xg_paths.CONTROL / "api.token").write_text(token + "\n", encoding="utf-8")
            xg_paths.private(xg_paths.CONTROL / "api.token")
    Handler.server_token = token
    Handler.started_at = time.time()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    _SERVER = srv
    port = srv.server_address[1]
    _write_web_json(port, Handler.server_token)
    # 上次点“重启后台”留下的标记：起来后把小光也重启一遍
    try:
        _flag = xg_paths.CONTROL / "restart_bot_pending"
        if _flag.exists():
            _flag.unlink()
            def _restart_bot_soon():
                time.sleep(2.0)
                try:
                    stop_little_light()
                    time.sleep(1.0)
                    _cur = status()
                    go_map(int(_cur.get("map") or xg_runtime.HOME))
                except Exception:
                    pass
            threading.Thread(target=_restart_bot_soon, daemon=True).start()
    except Exception:
        pass
    print(json.dumps({"event": "control_api_started", "port": port, "pid": os.getpid(),
                      "web": str(xg_paths.WEB_JSON), "data": str(xg_paths.DATA)}, ensure_ascii=False), flush=True)
    stop_evt = threading.Event()
    threading.Thread(target=_monitor_loop, args=(stop_evt,), daemon=True).start()
    _start_parent_watchdog(stop_evt)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_evt.set()
        _SERVER = None
        try:
            doc = xg_paths.read_json(xg_paths.WEB_JSON, None)
            if isinstance(doc, dict) and doc.get("pid") == os.getpid():
                xg_paths.WEB_JSON.unlink()
        except OSError:
            pass
        try:
            srv.server_close()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
