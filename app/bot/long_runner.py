"""光遇 0.16.8 当前地图长连接常驻运行器（claudework 测试副本）。

直接复用 r5 的 qfsky.connection0167.run_connection，不经网页；连接意外结束时按退避自动重连。
用法：long_runner.py --account <账号ID> --map <map_id> [--hours 1] [--max-retries 5]
"""
import argparse
import json
import os
import signal
import sys
import threading
import time
import uuid
from pathlib import Path

import xg_paths


def _load_map_state_class():
    """Only opt-in v31 uses its pinned release sibling, never ROOT's module."""
    if os.environ.get('SKY_ESCORT_SAME_PEER_V31') != '1':
        from map_state import MapSessionState
        return MapSessionState
    import importlib.util
    path = Path(__file__).resolve().with_name('map_state.py')
    spec = importlib.util.spec_from_file_location('_sky_escort_v31_map_state', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules[spec.name] = module
    return module.MapSessionState

ROOT = xg_paths.DATA
RT = xg_paths.RUNTIME
SERVER = xg_paths.QFSKY_CODE.parent
sys.path.insert(0, str(xg_paths.BOT_CODE))
sys.path.insert(0, str(xg_paths.APP))

os.environ.update({
    "QFLY_DATA_DIR": str(xg_paths.RUNTIME_DATA),
    "QFLY_JOIN_MODE": "current",
    "QFLY_JOIN_CONFIG": str(RT / "private/connection0167_profiles.local.json"),
    "QFLY_RESOURCE_REVISION": "V76318",
    "QFLY_SINGLE_DEVICE_MODE": "1",
    "QFLY_MPAY_DEVICE_ATTRS_FILE": str(RT / "runtime-data/registered-device.local.json"),
    "QFLY_SCHEDULER_ENABLED": "0",
    "QFLY_LOCAL_NETWORK_MODE": "enabled",
    "CLAUDEWORK_LONG": "1",
    "CLAUDEWORK_MAX_DURATION": str(24 * 3600),
})
sys.path.insert(0, str(SERVER))
os.chdir(SERVER)

if os.environ.get('SKY_ESCORT_SAME_PEER_V31') == '1':
    overlay=xg_paths.OVERLAY
    if not (overlay/'qfsky/connection0167.py').is_file():raise RuntimeError('same_peer_overlay_missing')
    sys.path.insert(0,str(overlay))
from qfsky import connection0167 as conn, version  # noqa: E402

LOG = ROOT / "logs/long_runner.jsonl"
# 这些结束原因说明是凭据或输入问题，重连也没用
NO_RETRY = {"unexpected_http_endpoint"}


def log(event, **fields):
    row = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "event": event, **fields}
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(json.dumps(row, ensure_ascii=False), flush=True)


REJOIN_AFTER = 30.0        # JOIN 目标连续这么久不在房间里 → 重新拉票回去
REJOIN_MAX = 600.0         # 回去了还是找不到（对方下线/换图）就逐次加倍，最多 10 分钟一次
REJOIN_GRACE = 30.0        # 每段开头先等这么久（名单要入服后才有）
FOLLOW_DELAY = 1.0         # 跟着哥哥换图：等他先进新房间再拉票
FOLLOW_FAST = 120.0        # 换图后这段时间内找不到哥哥就快速重拉
FOLLOW_GRACE = 4.0
FOLLOW_MISSING = 3.0
FOLLOW_NO_ROSTER = 25.0    # 快速模式：这么久还没收到新房间名单才重拉


def watch_segment(cancel, seg_cancel, rejoin, tracker, join_players, state):
    """本段监视：全局 cancel → 结束本段；设置了 JOIN 时，目标连续 interval 秒不在名单里 → 标记 rejoin 并结束本段。
    服务器偶尔会把小光单独 Move 到别的实例（实测 22:44），真人玩家会跟好友在一起，她不会，所以要自己回去。"""
    started, missing_since = time.monotonic(), None
    fast = time.monotonic() < state.get("fast_until", 0)      # 刚跟着哥哥换图：快点回到他身边
    grace, step = (FOLLOW_GRACE, 2) if fast else (REJOIN_GRACE, 5)
    while not seg_cancel.wait(step):
        if cancel.is_set():
            seg_cancel.set()
            return
        if state.get("leader_same_peer") or not join_players or tracker is None or time.monotonic() - started < grace:
            continue
        if fast and getattr(tracker, "roster_at", 0) <= started:          # 新房间名单还没来（大图入服慢）
            if time.monotonic() - started < FOLLOW_NO_ROSTER:
                continue
        with tracker.lock:
            here = bool(join_players & set(tracker.slots.values()))
        if cancel.is_set() or seg_cancel.is_set():
            return
        if here:
            missing_since = None
            state["interval"] = REJOIN_AFTER          # 找到了：间隔复位
            if fast and state.get("switched_at"):
                log("follow_arrived", seconds=round(time.monotonic() - state["switched_at"], 1),
                    tickets=state.get("tickets", 1), delay=FOLLOW_DELAY)
                state["switched_at"] = None
            continue
        missing_since = missing_since or time.monotonic()
        if time.monotonic() - missing_since >= (FOLLOW_MISSING if fast else state["interval"]):
            if fast and state.get("switched_at"):
                state["tickets"] = state.get("tickets", 1) + 1      # 这次拉票没分到哥哥身边
            state["interval"] = min(REJOIN_MAX, state["interval"] * 2)
            rejoin.set()
            seg_cancel.set()
            return


MOVE_LOG = ROOT / "logs/ws_move.jsonl"   # ai_companion 记下的 WS move（服务器推的入服票）
MOVE_MAX_AGE = 20.0        # 只用这么新的票（move_ts 是服务器时间，票过期就没用了）


FRIEND_POLL = 0.5          # 跟着过图：每这么久问一次哥哥在哪间房
FRIEND_WAIT = 10.0         # 最多问这么久（哥哥加载慢）


def friend_ticket(acc_id, friend, level=None, wait=FRIEND_WAIT, cancel=None):
    """POST /account/join_friend_game：好友此刻所在房间的入服票（星盘传送同款）。
    level 给了就等到好友进了这张图才返回；问不到返回 None。"""
    from qfsky.skyapi import SkyClient
    end = time.monotonic() + wait
    last = None
    while True:
        if cancel is not None and cancel.is_set():
            return None
        try:
            a = load_account(acc_id)
            c = SkyClient(a["user_uuid"], a["session"], a["session2"], a.get("data_version") or "")
            if cancel is not None and cancel.is_set():
                return None
            r = c._post_once("/account/join_friend_game", {"friend_id": friend}, True,
                             str(level or 0), None, 6.0)
            if cancel is not None and cancel.is_set():
                return None
            d = r.get("data") if isinstance(r.get("data"), dict) else r
            m = d.get("move_to_game") if isinstance(d, dict) else None
            if isinstance(m, dict) and m.get("public_ip") and m.get("port") and m.get("sig") and m.get("move_ts"):
                last = m.get("level")
                if level is None or m.get("level") == level:
                    return {"uri": f"{m['public_ip']}:{int(m['port'])}", "move_ts": m["move_ts"], "sig": m["sig"],
                            "platform_filter": m.get("platform_filter", 0), "level": m.get("level"),
                            "players": [p for p in (m.get("players") or []) if isinstance(p, str)][:7]}
        except Exception as exc:
            last = "error:" + str(exc)[:80]
        if time.monotonic() >= end or (cancel is not None and cancel.is_set()):
            log("friend_ticket_miss", want=level, last=last)
            return None
        if cancel is None:
            time.sleep(FRIEND_POLL)
        elif cancel.wait(FRIEND_POLL):
            return None


def use_friend_ticket(t, state, cancel=None):
    """把好友票装给下一段入服（others 带上要找的人）；state["friend_try"] 让段结束时判断成没成。"""
    from qfsky.join0167_profile import uuid_raw
    if (cancel is not None and cancel.is_set()) or not t or not t.get("players"):
        return False
    conn.TICKET_OVERRIDE["ticket"] = {k: t[k] for k in ("uri", "move_ts", "sig", "platform_filter")}
    conn.TICKET_OVERRIDE["fields"] = {"others": [uuid_raw(p) for p in t["players"]]}
    state["friend_try"] = True
    return True


def owner_uuid():
    try:
        return (ROOT / "control/owner_uuid").read_text().strip()
    except OSError:
        return ""


def has_capture(uuid, map_id):
    try:
        d = json.loads((RT / "private/connection0167_profiles.local.json").read_text())
        cap = (d["maps"].get(str(map_id)) or {}).get("current_capture") or {}
        if uuid in (cap.get("by_account") or {}) or cap.get("account") == uuid:
            return True
        from qfsky.asset_bootstrap_runtime_v32 import asset_trial_available
        if asset_trial_available(uuid, map_id):
            return True
        from qfsky.asset_catalog_runtime_v32c import catalog_trial_available
        return catalog_trial_available(uuid, map_id)
    except (OSError, ValueError, KeyError):
        return False


def fnv32(s):
    h = 0x811c9dc5
    for c in s.encode():
        h = ((h ^ c) * 0x01000193) & 0xFFFFFFFF
    return h


def _spawn_reference(prepared):
    """目录/标注给出的意图高度；只当真实地形选层参考，不是最终 y。"""
    try:
        import terrain_spawn
        raw = getattr(getattr(prepared, "current_inputs", None), "_placement", None)
        return terrain_spawn.placement_reference(json.loads(raw)) if raw else None
    except Exception:
        return None


ROOM_LEVEL_STABLE = 20.0  # 名单里哥哥的地图和小光以为的不一样，持续这么久就换过去
ROOM_LEVEL_GRACE = 30.0   # 进房后这么久内不判断：刚过图时名单里哥哥那行的地图还是旧的


def watch_moves(cancel, seg_cancel, pending, pose_ctl, own_uuid, current_map, tracker=None, acceptor=None, same_peer_publish=None):
    """跟着哥哥过图/回遇境。两条路：
    1) 带路方发来 ChangeLevelWithFadeRpc（牵着时按回遇境/过图，实测走这条）→ 换图，拉票带上哥哥；
    2) WS move（服务器推的票）：哥哥正牵着/背着（或刚松开），或目的房间里有哥哥 → 用这张票过去。"""
    def stopped():
        return cancel.is_set() or seg_cancel.is_set()

    def publish(**fields):
        # The container belongs only to this segment; never publish after cancellation.
        if stopped():
            return False
        pending.update(fields)
        seg_cancel.set()
        return True

    try:
        pos = MOVE_LOG.stat().st_size
    except OSError:
        pos = 0
    seen = time.monotonic()
    mismatch_since = None
    while not seg_cancel.wait(0.3):
        if stopped():
            return
        # Explicit leader travel is published only after the exact reliable
        # ChangeLevel frame committed through this live engine.
        escort = getattr(pose_ctl, "_escort_travel", None) if pose_ctl is not None else None
        if escort is not None:
            move = escort.take_pending_move()
            if move is not None:
                if same_peer_publish is not None and (move.get('same_peer_handhold') is True
                                                      or move.get('same_map_relocate')):
                    same_peer_publish(move, escort)
                    continue
                if publish(**move):
                    log("escort_map_switch_requested", map_id=move["level"])
                return
        current_map = pose_ctl.map_id if pose_ctl is not None else current_map
        if acceptor is not None and time.monotonic() - seen >= ROOM_LEVEL_GRACE:
            owner = owner_uuid()                  # 小光以为的地图 ≠ 哥哥实际所在（同一房间）：换过去
            try:
                real = acceptor.room_level(uuid.UUID(owner).bytes)
            except Exception:
                real = None
            if stopped():
                return
            if pose_ctl is not None and pose_ctl.linked_with(owner):
                real = None                       # 牵着/背着时跟随走 ChangeLevel，这里不插手
            if real and real != current_map:
                mismatch_since = mismatch_since or time.monotonic()
                if time.monotonic() - mismatch_since >= ROOM_LEVEL_STABLE:
                    if has_capture(own_uuid, real):
                        if publish(level=real, ticket=None, join=owner, why="名单显示实际在哥哥那张图"):
                            log("room_level_mismatch", believed=current_map, real=real)
                        return
                    log("room_level_mismatch", believed=current_map, real=real, why="这个号没有这张图的录制")
                    acceptor = None               # 本段不再提示
            else:
                mismatch_since = None
        if tracker is not None and pose_ctl is not None:
            owner = owner_uuid()
            with tracker.lock:
                changes = [c for c in tracker.level_changes if c[0] > seen]
                slots = dict(tracker.slots)
            if stopped():
                return
            for t, slot, scene in changes:
                if stopped():
                    return
                seen = max(seen, t)
                who = slots.get(slot)
                level = fnv32(scene)
                if who != owner or not pose_ctl.linked_with(owner):
                    log("level_change_ignored", scene=scene, by=(who or "")[:8], why="不是牵着/背着小光的哥哥")
                    continue
                if not has_capture(own_uuid, level):
                    log("level_change_ignored", scene=scene, why="这个号没有这张图的录制")
                    continue
                why = f"被牵着去 {scene}"
                if publish(level=level, ticket=None, join=owner, why=why):
                    log("move_follow", level=level, scene=scene, why=why)
                return
        try:
            with MOVE_LOG.open(encoding="utf-8") as f:
                f.seek(pos)
                lines = f.readlines()
                pos = f.tell()
        except OSError:
            continue
        for line in lines:
            if stopped():
                return
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            level, owner = ev.get("level"), owner_uuid()
            if not isinstance(level, int) or not all(isinstance(ev.get(k), (str, int)) for k in ("uri", "sig", "move_ts")):
                continue
            if abs(time.time() - int(ev["move_ts"])) > MOVE_MAX_AGE:
                continue
            linked = pose_ctl is not None and pose_ctl.linked_with(owner)
            with_owner = owner in (ev.get("other_players") or [])
            if level == current_map or not (linked or with_owner):
                continue                          # 同图换服务器的票不理（会把小光从哥哥身边拆走）
            escort = getattr(pose_ctl,"_escort_travel",None) if pose_ctl is not None else None
            if (escort is not None and escort.ignore_source_ws_advisory(level,current_map)):
                log("escort_source_ws_move_ignored",source_map=level,target_map=current_map,
                    reason="committed_leader_same_peer_intent")
                continue
            if not has_capture(own_uuid, level):
                log("move_ignored", level=level, why="这个号没有这张图的录制")
                continue
            why = "牵着/背着" if linked else "去哥哥所在的房间"
            if publish(level=level, ticket={k: ev[k] for k in ("uri", "move_ts", "sig", "platform_filter") if k in ev},
                       why=why):
                log("move_follow", level=level, why=why, uri=ev["uri"])
            return


RENEW_WAITS = (30, 60, 120, 240)   # 续期失败后的重试间隔（秒）：网易出口限流 422 一般几分钟内就过去


def try_renew(acc_id, attempts=1, cancel=None):
    """用账号库里存的凭据续期（qfsky.tasks._renew_mark，和网页控制台“续期”按钮同一套；不涉及输入密码）。
    成功会把新 session 写回账号库，下一段 load_account 就能用上。2026-10-31：会话过期导致 01:59 掉线后加；
    同日改为可重试多次（之前续期碰上 422 限流就直接停了）。"""
    from qfsky import tasks
    for i in range(attempts):
        try:
            tasks._renew_mark(load_account(acc_id), lambda *a: None)
            log("renewed", account=acc_id, attempt=i + 1)
            return True
        except Exception as exc:
            log("renew_failed", attempt=i + 1, error=str(exc)[:200])
            if "禁止登录" in str(exc):               # 号被封：重试没有意义，反复登录只会更糟
                return False
        if i + 1 < attempts:
            wait = RENEW_WAITS[min(i, len(RENEW_WAITS) - 1)]
            if cancel is not None:
                if cancel.wait(wait):
                    return False
            else:
                time.sleep(wait)
    return False


JOINABLE_EVERY = 60.0      # 礼包线上同款：在房间里每 60 秒重新声明一次可加入


def set_joinable(acc_id, map_id):
    """POST /account/set_joinable {"joinable": true}：好友才能用星盘传送到小光身边。失败只记日志。"""
    try:
        from qfsky.skyapi import SkyClient
        a = load_account(acc_id)
        c = SkyClient(a["user_uuid"], a["session"], a["session2"], a.get("data_version") or "")
        r = c.post("/account/set_joinable", {"joinable": True}, include_level_id=True, level_id=str(map_id),
                   timeout=8.0, attempts=2)
        data = r.get("data") if isinstance(r.get("data"), dict) else r
        ok = data.get("joinable") is True
        log("set_joinable", ok=ok, **({} if ok else {"reply": str(r)[:160]}))
        return ok
    except Exception as exc:
        log("set_joinable_failed", error=str(exc)[:200])
        return False


def joinable_loop(acc_id, map_id, cancel):
    # 2026-11-06：带路换图后要按“小光现在所在的图”声明，否则服务器一直以为她还在开局那张图，
    # 好友星盘传送只在遇境能成功。换图后立刻补一次，平时仍 60 秒一次。
    last_map, last_at = None, time.monotonic()
    while not cancel.wait(5.0):
        try:
            cur = map_id() if callable(map_id) else map_id
        except Exception:
            continue
        if cur != last_map or time.monotonic() - last_at >= JOINABLE_EVERY:
            if cur != last_map and last_map is not None:
                log("joinable_map_changed", old=last_map, new=cur)
            set_joinable(acc_id, cur)
            last_map, last_at = cur, time.monotonic()


def load_account(acc_id):
    from xg_auth_client import Client
    return Client().session(acc_id)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", required=True)
    ap.add_argument("--map", type=int, required=True)
    ap.add_argument("--hours", type=float, default=1.0)
    ap.add_argument("--max-retries", type=int, default=5)
    ap.add_argument("--capture", help="抓包目录：记录 UDP 下行与账号 WebSocket 业务帧")
    ap.add_argument("--control", help="姿态控制文件（JSON）：写入 candle/idle 切换小光的姿态")
    ap.add_argument("--accept-offers", action="store_true", help="自动接受别人在好友树递来的蜡烛")
    args = ap.parse_args()
    if os.environ.get('SKY_ESCORT_SAME_PEER_V31') == '1':
        from qfsky.map_long0167 import map_long_engine as loaded_engine
        expected=xg_paths.OVERLAY/'qfsky'
        if (Path(conn.__file__).resolve()!=expected/'connection0167.py'
                or Path(loaded_engine.__file__).resolve()!=expected/'map_long0167/map_long_engine.py'):
            raise RuntimeError('same_peer_overlay_import_mismatch')
        log('same_peer_v31_installed',enabled=True,handhold_only=True,isolated_overlay=True)

    cancel = threading.Event()
    from xg_auth_client import heartbeat
    heartbeat(args.account, cancel, log)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: cancel.set())
    sys.path.insert(0, str(xg_paths.BOT_CODE))

    if args.capture:
        cap = Path(args.capture)
        if not cap.is_absolute():          # 进程已 chdir 到 server 目录，相对路径按项目根解析
            cap = ROOT / cap
        cap.mkdir(parents=True, exist_ok=True)
        os.environ["CLAUDEWORK_CAPTURE"] = str(cap / "udp.jsonl")
        import ws_capture
        ws_capture.start(load_account(args.account), str(cap / "ws.jsonl"), cancel)
        log("capture_start", dir=str(cap))

    pose_ctl = tracker = None
    if args.control:
        import room_pose
        prepared0 = conn.prepare_current_long(load_account(args.account), version.cn_build(),
                                              args.map, variant=getattr(args, "variant", None))
        seed = prepared0.current_inputs.actor_bytes()
        control = Path(args.control) if Path(args.control).is_absolute() else ROOT / args.control
        tracker, pose_ctl = room_pose.install(control, seed, log, cancel, map_id=args.map,
                                              own_uuid=load_account(args.account)["user_uuid"],
                                              ground_ref=_spawn_reference(prepared0),
                                              variant=getattr(args, "variant", None),
                                              account=args.account)   # T24-2：换装/叫声按这个号存
        # T21：同图传送失败回滚本地落点时，runner 的 args.variant 也要回退，
        # 否则下一次重连 prepare 仍会按千鸟城种子进来。
        pose_ctl._on_relocation_reverted = lambda variant: setattr(args, "variant", variant)
        from leader_travel_port import install_engine_port
        from leader_travel_runtime import install as install_leader_travel
        install_leader_travel(pose_ctl, has_capture=has_capture,
                             rpc_port=install_engine_port(pose_ctl))
        log("pose_control_start", file=str(control))

    if args.accept_offers:
        import offer_accept
        from qfsky import home_handoff_runtime0167 as pump_mod
        import tier_up as tier

        def on_complete(sender):
            """收完一个好友树节点：播接烛动作；3 秒后替这位好友点一下“大节点”（够了就升，不够服务端回 false）。"""
            if pose_ctl:
                pose_ctl.play("TakeCandle")

            def unlock():
                try:
                    r = tier.tier_up(tier.client_for(load_account(args.account)), sender)
                    log("tier_up", friend=sender[:8], **{k: v for k, v in r.items() if k != "target"})
                except Exception as exc:
                    log("tier_up_failed", target=sender[:8], error=str(exc)[:160])
            threading.Timer(3.0, unlock).start()
        acceptor = offer_accept.OfferAcceptor(load_account(args.account), lambda: args.map, log,
                                              on_complete=on_complete)       # 换图后按当前地图认送方
        pump_mod.CLAUDEWORK_LISTENERS.append(acceptor)
        log("offer_accept_start", take_candle_anim=pose_ctl is not None)

    acceptor = locals().get("acceptor")
    own_uuid = load_account(args.account)["user_uuid"]
    join_players = {u.strip() for u in os.environ.get("CLAUDEWORK_JOIN_PLAYERS", "").split(",") if u.strip()}
    rejoin_state = {"interval": REJOIN_AFTER, "last": 0.0}
    current_map_id = lambda: (getattr(pose_ctl, "map_id", None) or args.map) if pose_ctl is not None else args.map
    threading.Thread(target=joinable_loop, args=(args.account, current_map_id, cancel), daemon=True,
                     name="joinable").start()
    MapSessionState = _load_map_state_class()
    map_state = MapSessionState(ROOT / "control")
    pending_relink = {}
    end_at = time.monotonic() + args.hours * 3600
    retries, segment = 0, 0
    renewed_once = False
    while not cancel.is_set():
        remaining = int(end_at - time.monotonic())
        if remaining < 15:
            log("finished", reason="planned_end")
            return
        # 每段都重读账号，网页续期后的新会话能直接生效
        account = load_account(args.account)
        if cancel.is_set():
            break
        prepared = conn.prepare_current_long(account, version.cn_build(), args.map,
                                             variant=getattr(args, "variant", None))
        if cancel.is_set():
            break
        segment += 1
        map_state.begin(args.map, segment)
        if tracker is not None:
            tracker.begin_segment(args.map)
        last_emit = [0.0]
        latest = {}

        def emit(state, segment_id=segment, latest_state=latest, emit_at=last_emit):
            if os.environ.get("SKY_ESCORT_SAME_PEER_V31") == "1":
                if not map_state.observe(segment_id, state, report_accepted=True):
                    return
            else:
                map_state.observe(segment_id, state)
            latest_state.update(state)
            now = time.monotonic()
            if now - emit_at[0] >= 30:
                emit_at[0] = now
                log("progress", segment=segment_id, **{k: latest_state.get(k) for k in (
                    "phase", "elapsed_seconds", "move_completed", "long_actor_frames",
                    "reply_count", "crc_failures")})

        if time.monotonic() >= rejoin_state.get("fast_until", 0):
            os.environ.pop("CLAUDEWORK_JOIN_QUICK", None)
        if segment == 1 and owner_uuid() in join_players:     # 上线时哥哥就在这张图：直接进他那间房
            use_friend_ticket(friend_ticket(args.account, owner_uuid(), args.map, wait=0, cancel=cancel),
                              rejoin_state, cancel=cancel)
        if cancel.is_set():
            map_state.finish(segment)
            if tracker is not None:
                tracker.end_segment()
            break
        set_joinable(args.account, args.map)    # 每段进图前声明可加入（被 Move/重连后服务器那边可能已经不算了）
        log("segment_start", segment=segment, map_id=args.map, duration=remaining)
        if pose_ctl is not None and segment > 1:
            pose_ctl.reset("reconnect")   # 重连 = 新房间：旧房间的坐/举烛/走动状态不能带过去（墓土曾因此连环掉线）
        if pose_ctl is not None and pending_relink:
            now = time.monotonic()
            pose_ctl.relink = {**pending_relink, "after": now, "until": now + 150}
            pending_relink.clear()
        started = time.monotonic()
        seg_cancel = threading.Event()        # 本段专用：全局 cancel 或“回到 JOIN 目标房间”都会结束本段
        rejoin = threading.Event()
        # Preserve backoff across segments, but never share live mutable state with old watchers.
        segment_rejoin_state = dict(rejoin_state)
        watcher = threading.Thread(target=watch_segment, daemon=True,
                                   args=(cancel, seg_cancel, rejoin, tracker, join_players, segment_rejoin_state))
        watcher.start()
        # A slow previous watcher keeps only its old container, never the next segment's.
        pending_move = {}
        scene_publish = {'map_id': args.map, 'revision': 0}
        same_peer_moves = None
        same_peer_publish = None
        if os.environ.get('SKY_ESCORT_SAME_PEER_V31') == '1':
            import queue
            same_peer_moves = queue.Queue(maxsize=1)
            def same_peer_publish(move, escort, segment_id=segment, scene_state=scene_publish):
                if not escort.same_peer_move_current(move):return
                if move.get('same_map_relocate'):
                    # 同图换命名落点：不拆连接、不做 same-peer level update（那条路
                    # 对同一 map_id 是明确拒绝的），只换种子 + 原生传送。
                    try:
                        prepared = conn.prepare_current_long(load_account(args.account),
                                                             version.cn_build(), move['level'],
                                                             variant=move.get('variant'))
                        if prepared is None:
                            log('escort_same_map_relocate_failed', reason='prepare_unavailable')
                            return
                        if (cancel.is_set() or seg_cancel.is_set()
                                or not escort.same_peer_move_current(move)):
                            return
                        if pose_ctl is not None:
                            # T21：传送前记 ctl 快照；传送后登记实际落点。
                            # 游戏/对端拒绝同图传送时，协调器会回滚这份快照。
                            escort.capture_same_map_snapshot()
                            pose_ctl.relocate(prepared.current_inputs.actor_bytes(), move['level'],
                                              variant=move.get('variant'),
                                              ground_ref=_spawn_reference(prepared))
                            escort.record_scene_target(pose_ctl.position)
                        args.variant = move.get('variant')
                        log('escort_same_map_relocated', map_id=move['level'],
                            variant=move.get('variant'))
                    except Exception as exc:
                        log('escort_same_map_relocate_failed', error=str(exc)[:160])
                    return
                ws_intent_token = escort.begin_same_peer_ws_intent(move)
                segment_rejoin_state['leader_same_peer'] = True
                def before_target(target):
                    if cancel.is_set() or seg_cancel.is_set() or not escort.same_peer_move_current(move):
                        raise RuntimeError('same_peer_request_cancelled')
                    if pose_ctl is not None:
                        pose_ctl.switch_map(target.current_inputs.actor_bytes(), target.map_id,
                                            ground_ref=_spawn_reference(target),
                                            variant=move.get('variant'),
                                            source='same_peer_level_update')
                    if tracker is not None:tracker.begin_segment(target.map_id)
                def after_target(target, expected_map=scene_state['map_id'],
                                 expected_revision=scene_state['revision']):
                    # The transport segment is unchanged; advance only its scene.
                    revision = map_state.announce_same_peer(target.map_id, segment_id,
                        expected_map_id=expected_map, expected_revision=expected_revision)
                    scene_state.update(map_id=target.map_id, revision=revision)
                    args.map=target.map_id
                    args.variant=move.get('variant')
                    if acceptor is not None:acceptor.map_id=target.map_id
                    # T22：真的换图后旧图的同图传送快照不能再用。
                    escort.drop_same_map_snapshot('same_peer_level_update')
                    escort.activate_same_peer_ws_intent(ws_intent_token,target.map_id)
                same_peer_moves.put_nowait(dict(level=move['level'], variant=move.get('variant'),
                    guard=lambda:not cancel.is_set() and not seg_cancel.is_set() and escort.same_peer_move_current(move),
                    commit_guard=lambda:not cancel.is_set() and not seg_cancel.is_set() and escort.is_current(move['_binding']),
                    before_install=before_target,after_install=after_target,
                    report=lambda detail:log('escort_same_peer_level_update',**detail)))
        move_watcher = threading.Thread(target=watch_moves, daemon=True, name="moves",
                                       args=(cancel, seg_cancel, pending_move, pose_ctl, own_uuid, args.map, tracker,
                                             None, same_peer_publish))   # 原follower路径不变；leader显式同连接队列
        move_watcher.start()
        try:
            if cancel.is_set():
                break
            final = (conn.run_connection(prepared, remaining, seg_cancel, emit, same_peer_moves=same_peer_moves) if same_peer_moves is not None else conn.run_connection(prepared, remaining, seg_cancel, emit)) or latest
        except Exception as exc:  # run_connection 自己会兜底，这里只防意外
            final = {**latest, "phase": "failed", "error_code": type(exc).__name__}
        finally:
            seg_cancel.set()
            # A blocked I/O operation must not hold up the connection loop forever.
            for worker in (watcher, move_watcher):
                worker.join(timeout=0.25)
            rejoin_state.update(dict(segment_rejoin_state))
            map_state.finish(segment)
            if tracker is not None:
                tracker.end_segment()
        lived = round(time.monotonic() - started, 1)
        log("segment_end", segment=segment, lived=lived, **{k: final.get(k) for k in (
            "phase", "reason", "error_code", "http_status", "move_completed", "move_forced_closed",
            "remote_disconnect_code", "long_actor_frames", "reply_count", "crc_failures")})

        if final.get('same_peer_level_update_failed') or final.get('error_code') == 'same_peer_level_update_failed':
            log('stopped',reason='same_peer_level_update_failed_no_room_retry')
            return
        if rejoin_state.pop("friend_try", False):
            conn.TICKET_OVERRIDE.pop("fields", None)
            if lived < 3 and final.get("phase") == "failed":
                log("friend_join_failed", error=final.get("error_code"))
                continue                          # 立刻退回普通拉票
            log("friend_join_ok", lived=lived)
        if pending_move and not cancel.is_set():   # 跟着哥哥过图/回遇境：换图，下一段用服务器推的票入服
            mv = dict(pending_move)
            pending_move.clear()
            try:
                if mv["level"] != args.map:
                    prepared = conn.prepare_current_long(load_account(args.account), version.cn_build(),
                                                         mv["level"], variant=mv.get("variant"))
                    seed = prepared.current_inputs.actor_bytes()
                    if cancel.is_set():
                        break
                    args.map = mv["level"]
                    args.variant = mv.get("variant")
                    if pose_ctl is not None:
                        relink = (pose_ctl.last_social if not mv.get("escort_leader")
                                  and pose_ctl.linked_with(owner_uuid()) else None)
                        # T22：换图前丢掉旧图的同图传送快照，防止失败回滚写回旧种子。
                        if (pose_ctl is not None and getattr(pose_ctl, "_escort_travel", None) is not None):
                            pose_ctl._escort_travel.drop_same_map_snapshot("move_switch")
                        pose_ctl.switch_map(seed, args.map,
                                            ground_ref=_spawn_reference(prepared),
                                            variant=args.variant,
                                            source="move_switch")
                        if relink:                    # 进新图后哥哥一出现就恢复牵手/背背（新段 reset 之后才装上）
                            pending_relink.update(who=relink["who"], kind=relink["kind"])
                    if acceptor is not None:
                        acceptor.map_id = args.map
                if cancel.is_set():
                    break
                if mv.get("ticket"):
                    conn.TICKET_OVERRIDE["ticket"] = mv["ticket"]
                if mv.get("join") and mv["join"] not in os.environ.get("CLAUDEWORK_JOIN_PLAYERS", ""):
                    os.environ["CLAUDEWORK_JOIN_PLAYERS"] = mv["join"]     # 拉票时带上哥哥：进他所在的实例
                    join_players.add(mv["join"])
                os.environ["CLAUDEWORK_JOIN_QUICK"] = "1"   # 退回拉票时带 quick_rejoin_players
                rejoin_state["fast_until"] = time.monotonic() + FOLLOW_FAST
                rejoin_state["switched_at"], rejoin_state["tickets"] = time.monotonic(), 1
                log("move_switch", map_id=args.map, why=mv["why"])
                if mv.get("join") and not mv.get("ticket"):     # 等哥哥进了那张图，直接拿他房间的票
                    t = friend_ticket(args.account, mv["join"], args.map, cancel=cancel)   # 只当信号：哥哥进了这张图
                    if t:
                        log("friend_arrived", level=t["level"], waited=round(time.monotonic() - rejoin_state["switched_at"], 1))
                        use_friend_ticket(t, rejoin_state, cancel=cancel)   # 好友票、others 带上哥哥：直接进他那间房
                    else:
                        cancel.wait(FOLLOW_DELAY)
            except Exception as exc:
                conn.TICKET_OVERRIDE.pop("ticket", None)
                log("move_switch_failed", error=str(exc)[:200])
            continue
        if rejoin.is_set() and not cancel.is_set():
            log("rejoin", reason="JOIN 目标不在房间里（多半是服务器把小光单独 Move 走了），重新拉票回去")
            owner = owner_uuid()
            t = friend_ticket(args.account, owner, args.map, wait=2.0, cancel=cancel) if owner in join_players else None
            if owner in join_players and t is None:
                t = friend_ticket(args.account, owner, None, wait=0, cancel=cancel)   # 哥哥可能在别的图
                if t and t.get("level") and t["level"] != args.map:
                    if has_capture(own_uuid, t["level"]):
                        prepared = conn.prepare_current_long(load_account(args.account), version.cn_build(),
                                                             t["level"])
                        seed = prepared.current_inputs.actor_bytes()
                        if cancel.is_set():
                            break
                        log("rejoin_other_map", believed=args.map, real=t["level"])
                        args.map = t["level"]
                        args.variant = None
                        if pose_ctl is not None:
                            escort = getattr(pose_ctl, "_escort_travel", None)
                            if escort is not None:
                                escort.drop_same_map_snapshot("rejoin_other_map")
                            pose_ctl.switch_map(seed, args.map,
                                                ground_ref=_spawn_reference(prepared),
                                                variant=None,
                                                source="rejoin_other_map")
                        if acceptor is not None:
                            acceptor.map_id = args.map
                    else:
                        log("rejoin_other_map", believed=args.map, real=t["level"], why="这个号没有这张图的录制")
                        t = None
            if not (t and use_friend_ticket(t, rejoin_state, cancel=cancel)):
                cancel.wait(3)
            continue
        if cancel.is_set() or final.get("phase") == "completed":
            continue
        if final.get("error_code") in NO_RETRY or final.get("http_status") not in (None, 200):
            # 多半是登录态过期：先用账号里存的凭据自动续期一次（= 网页控制台的“续期”按钮），成功就接着连
            if not renewed_once and try_renew(args.account, attempts=5, cancel=cancel):
                renewed_once = True
                cancel.wait(5)
                continue
            log("stopped", reason="需要人工处理（凭据或票据请求被拒，自动续期也没成功）")
            return
        renewed_once = False
        # 活过 5 分钟的段算稳定，重置退避计数
        retries = 0 if lived >= 300 else retries + 1
        if retries > args.max_retries:
            log("stopped", reason="连续重连失败次数过多")
            return
        wait = min(120, 5 * 2 ** max(0, retries - 1))
        log("reconnect_wait", seconds=wait, retries=retries)
        cancel.wait(wait)


if __name__ == "__main__":
    main()
