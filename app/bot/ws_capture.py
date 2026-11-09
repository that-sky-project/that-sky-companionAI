"""账号 WebSocket（/account/ws）全量业务帧记录器，用于定位聊天下行。

复用 qfsky.skyws.WsSession 的握手，只替换接收循环：ping 照常回 pong，
其余文本/二进制帧完整写入 jsonl。断线后自动重连。
"""
import json
import socket
import struct
import threading
import time

from qfsky import skyws
from qfsky.skyapi import CN_BUILD_ACCESS_KEY, cn_user_agent


class CaptureWs(skyws.WsSession):
    def __init__(self, *args, out_path, **kwargs):
        super().__init__(*args, **kwargs)
        self.out_path = out_path
        self.business_frames = 0

    def _write(self, kind, data):
        with open(self.out_path, "a", encoding="utf-8") as f:
            row = {"t": round(time.time(), 3), "kind": kind, "len": len(data)}
            try:
                row["text"] = data.decode("utf-8")
            except UnicodeDecodeError:
                row["hex"] = data.hex()
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("eof")
            buf += chunk
        return buf

    def _loop(self):
        self.sock.settimeout(2.5)
        while self.alive:
            try:
                first = self._recv_exact(2)
                opcode = first[0] & 0x0F
                length = first[1] & 0x7F
                if length == 126:
                    length = struct.unpack(">H", self._recv_exact(2))[0]
                elif length == 127:
                    length = struct.unpack(">Q", self._recv_exact(8))[0]
                mask = self._recv_exact(4) if (first[1] & 0x80) else None
                data = self._recv_exact(length)
                if mask:
                    data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
                if opcode == 0x9:
                    self._send(data, 0xA)
                    self.pings += 1
                elif opcode == 0x8:
                    self._write("close", data)
                    self.alive = False
                elif opcode in (0x1, 0x2, 0x0):
                    self.business_frames += 1
                    self._write("text" if opcode == 0x1 else "binary", data)
            except socket.timeout:
                continue
            except Exception as exc:  # 断线：交给 run_forever 重连
                self._write("error", str(exc).encode())
                self.alive = False


def run_forever(account, out_path, stop_event):
    """保持一条记录用 WS，直到 stop_event 置位。"""
    from qfsky.skyapi import SkyClient
    client = SkyClient(account["user_uuid"], account["session"], account["session2"],
                       account.get("data_version") or "")
    ua = cn_user_agent(getattr(client, "client_version", "") or "", getattr(client, "fp_key", ""))
    backoff = 5
    while not stop_event.is_set():
        ws = CaptureWs(uid=getattr(client, "uid", ""), session=getattr(client, "session", ""),
                       ua=ua, access_key=CN_BUILD_ACCESS_KEY, out_path=out_path)
        if ws.connect():
            ws._write("open", b"connected")
            backoff = 5
            while ws.alive and not stop_event.is_set():
                stop_event.wait(1)
            ws.close()
        else:
            ws._write("connect_failed", ws.error.encode())
        stop_event.wait(backoff)
        backoff = min(120, backoff * 2)


def start(account, out_path, stop_event):
    t = threading.Thread(target=run_forever, args=(account, out_path, stop_event), daemon=True,
                         name="ws-capture")
    t.start()
    return t
