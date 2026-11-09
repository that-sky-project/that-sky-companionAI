# -*- coding: utf-8 -*-
"""跨平台文件锁（替代直接 import fcntl）。

- Linux/macOS：fcntl.flock(LOCK_EX)，阻塞。
- Windows：msvcrt.locking(LK_NBLCK) 锁文件第 1 个字节；默认阻塞重试。

用法：
    from xg_lock import locked
    with locked(path):
        ...
或
    lock = FileLock(path)
    lock.acquire(); ...; lock.release()
"""
from __future__ import annotations

import os
import time
from pathlib import Path

try:                     # POSIX
    import fcntl as _fcntl
except ImportError:      # Windows
    _fcntl = None

try:
    import msvcrt as _msvcrt
except ImportError:
    _msvcrt = None


class FileLock:
    def __init__(self, path, timeout: float | None = None):
        self.path = Path(path)
        self.timeout = timeout
        self._fh = None
        self.kind = None

    def _open(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+b")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def acquire(self):
        if self._fh is not None:
            return self
        self._open()
        if _fcntl is not None:
            _fcntl.flock(self._fh.fileno(), _fcntl.LOCK_EX)
            self.kind = "fcntl"
            return self
        if _msvcrt is None:
            self.kind = "none"
            return self
        deadline = None if self.timeout is None else time.monotonic() + self.timeout
        while True:
            try:
                self._fh.seek(0)
                _msvcrt.locking(self._fh.fileno(), _msvcrt.LK_NBLCK, 1)
                self.kind = "msvcrt"
                return self
            except OSError:
                if deadline is not None and time.monotonic() >= deadline:
                    self._fh.close()
                    self._fh = None
                    raise TimeoutError(f"lock timeout: {self.path}")
                time.sleep(0.05)

    def release(self):
        fh = self._fh
        if fh is None:
            return
        try:
            if self.kind == "fcntl":
                _fcntl.flock(fh.fileno(), _fcntl.LOCK_UN)
            elif self.kind == "msvcrt":
                fh.seek(0)
                _msvcrt.locking(fh.fileno(), _msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        finally:
            fh.close()
            self._fh = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()
        return False


def locked(path, timeout: float | None = None) -> FileLock:
    """`with locked(p): ...` 的简写。"""
    return FileLock(path, timeout=timeout)


if __name__ == "__main__":
    import sys
    import tempfile

    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.gettempdir()) / "xg_lock_demo.lock"
    lock = locked(target)
    lock.acquire()
    print(f"locked {target} via {lock.kind}")
    lock.release()
