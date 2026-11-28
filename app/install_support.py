"""Actionable local installation errors shared by the desktop host and wizard."""
from __future__ import annotations

import errno
import os
from pathlib import Path
import re
import shutil


REEXTRACT = "关闭小光，把整个压缩包重新解压到有写权限的短路径（例如 D:\\XiaoGuang）；保留 runtime 和 app，再重试。"
WEBVIEW = ("请通过 Microsoft 官方渠道安装或修复 Microsoft Edge WebView2 Evergreen Runtime，"
           "然后重新打开小光；受管理的电脑可请管理员安装。")
DOTNET = ("小光的窗口组件需要 Microsoft .NET Framework 4.8（Windows 10/11 一般自带）。"
          "请从微软官网下载安装 .NET Framework 4.8（或用“启用或关闭 Windows 功能”勾选 .NET Framework 4.8 高级服务），"
          "并安装最新的 Microsoft Visual C++ 2015-2022 x64 运行库，重启电脑后再打开小光。")
DATA_WRITE = ("请确认当前 Windows 用户对小光数据目录有写入权限；使用默认的 %LOCALAPPDATA% 目录，"
              "或换到自己可写的本地目录后重试。无需以管理员身份启动小光。")
DISK = "请清理小光数据目录所在盘及系统临时目录所在盘的空间，或迁移到有足够空间的磁盘，再重试。"
MEMORY = "请关闭占内存的软件和闲置的 MuMu 实例，确认 Windows 虚拟内存没有关闭，再重试。"
SECURITY = ("请在 Windows 安全中心 → 病毒和威胁防护 → 保护历史记录中检查拦截；"
            "核对安装包来源后重新解压。公司电脑请联系管理员检查应用控制策略。")
BACKEND = "本机后台没有启动。请退出旧版小光，确认完整解压，并查看数据目录下 logs/desktop-backend.log，再重试。"


class InstallProblem(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def problem(error=None, *, stage="", default=None):
    """Convert OS/runtime failures to a short UI message, without showing stack traces."""
    if isinstance(error, InstallProblem):
        return {"code": error.code, "message": str(error)}
    winerror = getattr(error, "winerror", None)
    err = getattr(error, "errno", None)
    reason = str(error or "").casefold()
    diagnostic = lambda code, message: {"code": code, "message": message}
    if winerror in (39, 112) or err in (errno.ENOSPC, getattr(errno, "EDQUOT", -1)) or re.search(
        r"\benospc\b|no space left on device|not enough space on the disk|磁盘空间不足|磁盘已满", reason
    ):
        return diagnostic("disk_full", DISK)
    if isinstance(error, MemoryError) or winerror in (8, 14, 1450, 1455) or err == errno.ENOMEM or re.search(
        r"out of memory|memoryerror:|paging file is too small|内存不足|页面文件太小", reason
    ):
        return diagnostic("memory_low", MEMORY)
    if winerror in (225, 226, 577, 1260) or re.search(
        r"contains a virus|potentially unwanted software|blocked by group policy|包含病毒或潜在的垃圾软件", reason
    ):
        return diagnostic("security_blocked", SECURITY)
    if winerror == 206 or err == errno.ENAMETOOLONG or re.search(
        r"filename or extension is too long|file name too long|文件名或扩展名太长", reason
    ):
        return diagnostic("path_too_long", REEXTRACT + " 如果是数据目录太深，请另选较短的数据目录。")
    if winerror in (193, 216, 1150) or re.search(
        r"not a valid win32 application|bad exe format|不是有效的 win32 应用程序", reason
    ):
        return diagnostic("runtime_arch", "请在受支持的 64 位 Windows 10/11 上使用对应架构的完整客户端。")
    if stage == "webview":
        if isinstance(error, ModuleNotFoundError):
            return diagnostic("desktop_runtime_missing", "桌面宿主缺少界面组件。" + REEXTRACT)
        chain = reason
        cause = getattr(error, "__cause__", None) or getattr(error, "__context__", None)
        if cause is not None:
            chain += " " + str(cause).casefold()
        if re.search(r"\.net runtime|python\.runtime|clr_loader|clrloader|netfx|pythonnet|mscoree|coreclr", chain):
            return diagnostic("dotnet_unavailable", DOTNET)
        if winerror in (5, 126, 127, 14001) or re.search(r"webview2|edgechromium|corewebview|0x80070002|0x8007007e", reason):
            return diagnostic("webview2_unavailable", WEBVIEW)
        return diagnostic("desktop_view_failed", WEBVIEW + " 若仍失败，请查看本机 desktop-lifecycle.log。")
    if isinstance(error, PermissionError) or winerror in (5, 32) or err in (errno.EACCES, errno.EPERM):
        return diagnostic("data_write_denied" if stage == "data" else "access_denied",
                          DATA_WRITE if stage == "data" else "请确认完整解压到自己可写的目录后重试。" + SECURITY)
    if isinstance(error, FileNotFoundError) or winerror in (2, 3, 126, 127):
        return diagnostic("runtime_missing", "可能有安装文件或运行库缺失。" + REEXTRACT)
    if default:
        return diagnostic("local_operation_failed", default)
    return None


def wizard_warnings(install_root, data_dir):
    """Advisory only: do not make a full disk or missing Node block repair steps."""
    warnings = []
    if os.name == "nt" and not (Path(install_root) / "runtime/node/node.exe").is_file():
        warnings.append("完整包缺少随附的 Node 运行时。地形和道具生成可能失败。" + REEXTRACT)
    path = Path(data_dir)
    try:
        if path.exists() and shutil.disk_usage(path).free < 512 * 1024 * 1024:
            warnings.append("小光数据盘可用空间不足 512 MiB，拉取安装包或生成资源前请先释放空间。")
    except OSError:
        # The actual write reports concrete permission or I/O errors later.
        pass
    return warnings
