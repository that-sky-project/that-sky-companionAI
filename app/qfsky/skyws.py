# -*- coding: utf-8 -*-
"""送心的 WebSocket 保活通道（国服）。

★★ 2026-10-04 真机对照实验定案 ★★
================================================================
`POST /account/send_message` **必须有活跃的 `/account/ws` 连接**，否则一律回
`{"result":"not_online"}`（蜡烛不扣）。三组对照：

    A 不连 WS        → not_online ×3
    B 保持 WS 连接   → ok ×2（蜡烛 5356→5353→5350）
    C 关掉 WS 后再送 → not_online ×3

所以 `not_online` 的真实含义是「**发送方**没有活跃会话通道」，**不是**「对方不在线」。
—— 好友的 `is_online` 字段（`get_all_friend_gist`）实测 89/89 全 False，
是个不可用字段，别拿它做判断。

WS 帧在送心全程**没有业务收发**（对端只有 `ping`，我们回 `pong`），
所以这里只实现握手 + 心跳，不需要解析任何业务帧。

握手头（顺序与取值都实测过，少一个就 400/401）：
    Host / IsReconnecting / x-user-id / x-session-token / x-new-session /
    Connection / Upgrade / Sec-WebSocket-Key / Sec-WebSocket-Version
  * `x-session-token` = 账号的 `session`（**不是** session2/session3，用错回 401）
  * `x-new-session: true` 首连；重连用 `false`
  * 缺 `Host` → 400；缺 `Sec-WebSocket-Key` 或 `Sec-WebSocket-Version: 13` → 400
"""
from __future__ import annotations

import base64
import os
import socket
import ssl
import struct
import threading
import time

WS_HOST = "live-ws-sky-merge.game.163.com"
WS_PATH = "/account/ws"

# 建成后至少挂这么久再送：服务端要把「该 uid 在线」登记上，太快送会 still not_online
WS_SETTLE_SEC = 1.2
# 送完后保持连接的时间：礼物消息要在这条通道上推给对端，立刻断开可能丢
WS_LINGER_SEC = 0.8


class WsSession:
    """一个账号的 WS 保活连接。用 `with` 或 try/finally 保证 close。"""

    def __init__(self, uid: str, session: str, ua: str = "", access_key: str = "",
                 host: str = WS_HOST):
        self.uid = str(uid or "")
        self.session = str(session or "")
        self.ua = ua
        self.access_key = access_key
        self.host = host
        self.sock = None
        self.alive = False
        self.thread: threading.Thread | None = None
        self.pings = 0
        self.frames: list[str] = []
        self.error = ""
        self.reconnects = 0

    # ── 连接 ──
    def connect(self, timeout: float = 15.0) -> bool:
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            raw = socket.create_connection((self.host, 443), timeout=timeout)
            self.sock = ctx.wrap_socket(raw, server_hostname=self.host)

            lines = [
                "GET %s HTTP/1.1" % WS_PATH,
                "Host: %s" % self.host,
                "IsReconnecting: false",
                "x-user-id: %s" % self.uid,
                "x-session-token: %s" % self.session,
                "x-new-session: true",
            ]
            if self.ua:
                lines.append("User-Agent: %s" % self.ua)
            if self.access_key:
                lines.append("x-sky-build-access-key: %s" % self.access_key)
            lines += [
                "Connection: Upgrade",
                "Upgrade: websocket",
                "Sec-WebSocket-Key: %s" % base64.b64encode(os.urandom(16)).decode(),
                "Sec-WebSocket-Version: 13",
            ]
            self.sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())

            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = self.sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
            head = buf.split(b"\r\n\r\n", 1)[0].decode("latin1", "replace")
            first = head.split("\r\n")[0] if head else ""
            if "101" not in first:
                self.error = head[:200]
                self.close()
                return False

            self.alive = True
            self.thread = threading.Thread(target=self._loop, daemon=True)
            self.thread.start()
            return True
        except Exception as e:  # noqa: BLE001 —— 连不上就退化成「无 WS」，由调用方决定
            self.error = str(e)[:160]
            self.close()
            return False

    # ── 帧 ──
    def _send(self, payload: bytes, opcode: int = 0x1) -> None:
        if not self.sock:
            return
        n = len(payload)
        head = bytearray([0x80 | opcode])
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head.append(0x80 | 126)
            head += struct.pack(">H", n)
        else:
            head.append(0x80 | 127)
            head += struct.pack(">Q", n)
        mask = os.urandom(4)
        head += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        try:
            self.sock.sendall(bytes(head) + masked)
        except Exception:  # noqa: BLE001
            self.alive = False

    def _loop(self) -> None:
        try:
            self.sock.settimeout(2.5)
        except Exception:  # noqa: BLE001
            self.alive = False
            return
        while self.alive:
            try:
                first = self.sock.recv(2)
                if len(first) < 2:
                    self.alive = False
                    break
                opcode = first[0] & 0x0F
                length = first[1] & 0x7F
                if length == 126:
                    length = struct.unpack(">H", self.sock.recv(2))[0]
                elif length == 127:
                    length = struct.unpack(">Q", self.sock.recv(8))[0]
                mask = self.sock.recv(4) if (first[1] & 0x80) else None
                data = b""
                while len(data) < length:
                    chunk = self.sock.recv(length - len(data))
                    if not chunk:
                        break
                    data += chunk
                if mask:
                    data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))

                if opcode == 0x9:            # ping → pong（服务端每 3~5s 一次）
                    self._send(data, 0xA)
                    self.pings += 1
                elif opcode == 0x8:          # close
                    self.alive = False
                    break
                elif opcode == 0xA:          # pong（我们主动 ping 的回音）
                    pass
                else:
                    # 业务下行：送心链上实测为空，留着只为诊断
                    self.frames.append(data[:200].decode("utf-8", "replace"))
            except socket.timeout:
                continue
            except Exception:  # noqa: BLE001
                self.alive = False
                break

    def close(self) -> None:
        self.alive = False
        try:
            if self.sock:
                self.sock.close()
        except Exception:  # noqa: BLE001
            pass
        self.sock = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def keepalive_for(client, seconds: float | None = None):
    """按 `SkyClient` 造一条已连接并 settle 好的 WS。失败返回 None（调用方自行降级）。

    `seconds` 给了就原地保持这么久（简单用法：送心链跑完再退）。
    """
    from .skyapi import CN_BUILD_ACCESS_KEY, cn_user_agent

    ws = WsSession(
        uid=getattr(client, "uid", ""),
        session=getattr(client, "session", ""),
        ua=cn_user_agent(getattr(client, "client_version", "") or "",
                         getattr(client, "fp_key", "")),
        access_key=CN_BUILD_ACCESS_KEY,
    )
    if not ws.connect():
        return None
    time.sleep(WS_SETTLE_SEC)
    if seconds:
        time.sleep(max(0.0, float(seconds)))
    return ws


def ensure_alive(ws, client=None, settle: float | None = None):
    """确保 `ws` 仍然活着；断了就（按 `client`）重连。

    ★ 2026-10-05 实测：批量送心里**第一个请求走旧连接是 ok，后面的却回
      `not_online`** —— 同一条 WS 在两次送礼之间会掉（服务端主动断 / 空闲回收）。
      所以批量循环**每次送礼前**都要调本函数，别指望一条连接从头发到尾。

    返回可用的 `WsSession` 或 None（连不上 → 调用方会拿到 not_online，
    上层的 `ws_ok` / 错误码会如实反映）。
    """
    if ws is not None and getattr(ws, "alive", False) and ws.sock is not None:
        return ws
    if client is None:
        return ws if (ws is not None and getattr(ws, "alive", False)) else None
    fresh = keepalive_for(client)
    if fresh is None:
        return ws          # 重连失败：留着旧的，让调用方看到 not_online
    fresh.reconnects = getattr(ws, "reconnects", 0) + 1 if ws is not None else 1
    if ws is not None:
        try:
            ws.close()
        except Exception:  # noqa: BLE001
            pass
    if settle:                       # 重连后多等一会儿再发
        time.sleep(max(0.0, float(settle)))
    return fresh
