"""Single ADB endpoint allowlist for the Windows client's emulator features."""
from __future__ import annotations

import ipaddress
import re

_PORT = re.compile(r"[0-9]{1,5}")
_BRIDGED = tuple(ipaddress.IPv4Network(network) for network in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
))


def _valid_port(value):
    return bool(_PORT.fullmatch(value) and 0 < int(value) <= 65535)


def _host(serial):
    if not isinstance(serial, str):
        return None
    host, sep, port = serial.rpartition(":")
    return host if sep and _valid_port(port) else None


def _loopback_host(host):
    if host.casefold() in ("localhost", "[::1]"):
        return True
    try:
        return ipaddress.IPv4Address(host).is_loopback
    except ValueError:
        return False


def is_loopback_emulator_tcp_serial(serial):
    host = _host(serial)
    return host is not None and _loopback_host(host)


def is_supported_emulator_tcp_serial(serial):
    """Accept literal loopback and RFC 1918 addresses, not public/USB/DNS."""
    host = _host(serial)
    if host is None:
        return False
    if _loopback_host(host):
        return True
    try:
        address = ipaddress.IPv4Address(host)
    except ValueError:
        return False
    return any(address in subnet for subnet in _BRIDGED)


def is_supported_emulator_serial(serial):
    if not isinstance(serial, str):
        return False
    return ((serial.startswith("emulator-") and _valid_port(serial[9:]))
            or is_supported_emulator_tcp_serial(serial))
