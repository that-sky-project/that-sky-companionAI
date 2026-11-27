"""Keep Windows ADB clients from auto-replacing another app's ADB server."""
from __future__ import annotations

import functools
import ipaddress
import os
import re
import socket
import subprocess

import xg_exec

SERVER_CONFLICT = "xg_adb_server_version_conflict"
_UNKNOWN = object()


def _endpoint(environment):
    address = environment.get("ANDROID_ADB_SERVER_ADDRESS") or "127.0.0.1"
    port = environment.get("ANDROID_ADB_SERVER_PORT") or "5037"
    server = environment.get("ADB_SERVER_SOCKET")
    if server:
        if not server.startswith("tcp:"):
            return None
        entry = server[4:]
        address, port = entry.rsplit(":", 1) if ":" in entry else ("127.0.0.1", entry)
    if address.casefold() == "localhost":
        address = "127.0.0.1"
    try:
        host = ipaddress.ip_address(address.strip("[]"))
        if 0 < int(port) <= 65535 and host.is_loopback and "%" not in str(host):
            return str(host), int(port)
    except ValueError:
        pass
    return None


def _read_exact(connection, size):
    data = bytearray()
    while len(data) < size:
        chunk = connection.recv(size - len(data))
        if not chunk:
            return None
        data.extend(chunk)
    return bytes(data)


def _server_protocol(endpoint):
    """ADB's read-only host version handshake; never start a server or scan ports."""
    try:
        connection = socket.create_connection(endpoint, timeout=0.2)
    except ConnectionRefusedError:
        return None  # No server. Let the selected ADB start normally.
    except OSError:
        return _UNKNOWN
    try:
        with connection:
            connection.sendall(b"000chost:version")
            if _read_exact(connection, 4) != b"OKAY" or _read_exact(connection, 4) != b"0004":
                return _UNKNOWN
            version = _read_exact(connection, 4)
            if version and re.fullmatch(rb"[0-9a-fA-F]{4}", version):
                return int(version, 16)
    except OSError:
        pass
    return _UNKNOWN


@functools.lru_cache(maxsize=32)
def _client_protocol(executable, identity):
    try:
        response = xg_exec.run([executable, "version"], capture_output=True, timeout=2)
        result = re.search(rb"Android Debug Bridge version\s+1\.0\.(\d+)\b", response.stdout or b"")
        if response.returncode == 0 and result:
            return int(result[1])
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def run(prefix, args, **kwargs):
    """Run an argv prefix, allowing compatible or absent local ADB servers only.

    The source-side wrappers used by tests are not native ADB clients; POSIX
    keeps its existing behavior. No restart or kill-server is issued here.
    """
    prefix = list(prefix)
    command = [*prefix, *args]
    if os.name == "nt" and len(prefix) == 1:
        environment = kwargs.get("env")
        endpoint = _endpoint(os.environ if environment is None else environment)
        server = _server_protocol(endpoint) if endpoint else _UNKNOWN
        if server is not None:
            client = None
            if server is not _UNKNOWN:
                try:
                    info = os.stat(prefix[0])
                    identity = (info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                except OSError:
                    identity = None
                client = _client_protocol(prefix[0], identity)
            if client != server:
                textual = kwargs.get("text") or kwargs.get("encoding") or kwargs.get("errors") or kwargs.get("universal_newlines")
                return subprocess.CompletedProcess(
                    command, 1, "" if textual else b"", SERVER_CONFLICT if textual else SERVER_CONFLICT.encode("ascii"))
    return xg_exec.run(command, **kwargs)
