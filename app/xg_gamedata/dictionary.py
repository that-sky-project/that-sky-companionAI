# -*- coding: utf-8 -*-
"""从用户自己的 APK 提取 type-2 压缩帧用的 CompressDictionary.zst（W9）。

它是什么
========
`qfsky/type2_dictionary0167.load_type2_decoder` 用它解压下行 ``compression kind 2``
的 zstd 帧。文件是 **Zstd trained dictionary**：2 MiB、dictID=0x46f7ab50、
sha256 固定。0.16.8 / 421629 与 0.16.7 / 421589 的 APK 里都是同一份，就在
``assets/Data/Resources/CompressDictionary.zst``，不是抓包私有数据，也不含
账号/设备身份。

本模块只从用户 APK 复制这一个成员，校验尺寸/魔数/dictID/sha256 后原子写到
``DATA/runtime/protocol_results/handshake/resources/CompressDictionary.zst``。
不随包分发字典本身（APK 提取物按红线留在用户机）。
"""
from __future__ import annotations

import hashlib
import struct
import zipfile
from pathlib import Path

from .common import GameDataError, Progress, atomic_write, final, resolve_data

MEMBER = "assets/Data/Resources/CompressDictionary.zst"
SIZE = 2097152
DICT_ID = 0x46F7AB50
SHA256 = "18fc4ec8f4250e729f7277769b6f782f950c0f3798850ef2f2211d131a37efed"
MAGIC = bytes.fromhex("37a430ec")


def target_path(data: Path) -> Path:
    return (Path(data) / "runtime" / "protocol_results" / "handshake" / "resources" /
            "CompressDictionary.zst")


def validate(raw: bytes, *, expected_sha: str = SHA256, expected_size: int = SIZE,
             expected_id: int = DICT_ID) -> dict:
    if len(raw) != expected_size:
        raise GameDataError("压缩字典大小不对：%d 字节（应为 %d）" % (len(raw), expected_size),
                            "dictionary_size")
    if raw[:4] != MAGIC:
        raise GameDataError("压缩字典魔数不对（不是 zstd 字典帧）", "dictionary_magic")
    if struct.unpack_from("<I", raw, 4)[0] != expected_id:
        raise GameDataError("压缩字典 dictID 不对", "dictionary_id")
    digest = hashlib.sha256(raw).hexdigest()
    if expected_sha and digest != expected_sha:
        raise GameDataError("压缩字典 sha256 不对（不是国服 0.16.8 / 421629 的那份）",
                            "dictionary_sha256")
    return {"size": len(raw), "sha256": digest, "dict_id": expected_id,
            "magic": raw[:4].hex()}


def _read_member(apk: Path) -> tuple[str, bytes]:
    try:
        with zipfile.ZipFile(apk) as zf:
            names = zf.namelist()
            member = MEMBER
            if member not in names:
                candidates = [n for n in names
                              if n.endswith("/CompressDictionary.zst") or
                              n == "CompressDictionary.zst"]
                if len(candidates) != 1:
                    raise GameDataError(
                        "安装包里找不到唯一的 CompressDictionary.zst（找到 %d 个）。"
                        "请用完整的国服 0.16.8 / 421629 base.apk。" % len(candidates),
                        "dictionary_missing")
                member = candidates[0]
            return member, zf.read(member)
    except zipfile.BadZipFile:
        raise GameDataError("APK 打不开（zip 结构损坏）。", "dictionary_bad_zip") from None
    except OSError as exc:
        raise GameDataError("读 APK 失败：%s" % exc, "dictionary_apk_read") from None


def extract(apk: Path, dest: Path, *, expected_sha: str = SHA256,
            expected_size: int = SIZE, expected_id: int = DICT_ID,
            progress: Progress | None = None) -> dict:
    apk = Path(apk).expanduser().resolve()
    if not apk.is_file():
        raise GameDataError("APK 不存在：%s" % apk, "dictionary_apk_missing")
    if progress:
        progress.emit("从 APK 提取压缩字典…", done=0)
    member, raw = _read_member(apk)
    info = validate(raw, expected_sha=expected_sha, expected_size=expected_size,
                    expected_id=expected_id)
    dest = Path(dest)
    atomic_write(dest, raw)
    if hashlib.sha256(dest.read_bytes()).hexdigest() != info["sha256"]:
        raise GameDataError("压缩字典写入后校验失败：%s" % dest, "dictionary_write_verify")
    if progress:
        progress.emit("压缩字典已提取", done=1, total=1, force=True)
    return {"ok": True, "path": str(dest), "member": member, **info}


def ensure_for_data(apk, data: Path, progress: Progress | None = None) -> dict:
    """已经有合法字典就复用；否则从 APK 提取。失败返回 ok=False，不抛。"""
    dest = target_path(data)
    try:
        if dest.is_file():
            info = validate(dest.read_bytes())
            return {"ok": True, "path": str(dest), "existing": True, **info}
    except (GameDataError, OSError):
        pass
    if not apk:
        return {"ok": False, "path": str(dest), "code": "apk_missing",
                "error": "还没有用户 APK，无法提取压缩字典"}
    try:
        return extract(Path(apk), dest, progress=progress)
    except GameDataError as exc:
        return {"ok": False, "path": str(dest), "code": exc.code, "error": exc.message}
    except OSError as exc:
        return {"ok": False, "path": str(dest), "code": "dictionary_io", "error": str(exc)}


def cmd_dictionary(args) -> int:
    progress = Progress("dictionary", 2)
    try:
        data = resolve_data(args.data)
        result = extract(Path(args.apk), target_path(data), progress=progress)
        progress.finish("压缩字典已就绪")
        final(True, **{k: v for k, v in result.items() if k != "ok"})
        return 0
    except GameDataError as exc:
        final(False, error=exc.message, code=exc.code)
        return 1
    except OSError as exc:
        final(False, error="提取压缩字典失败：%s" % exc, code="dictionary_io")
        return 1
