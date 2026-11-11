# -*- coding: utf-8 -*-
"""每账号稳定设备指纹：终结「全站 137 个账号其实是同一台手机」。

问题（2026-09-14 线上实测）
--------------------------
封号原文全部是「您的角色ID:xxx 因使用外挂、脚本等辅助软件被禁止登录至…」，
而且**不同机主的账号在同一批里被同时封**：

    2026-09-10 12:24:46 「小号1」
    2026-09-10 12:24:51 「popi1117」
    2026-09-10 12:24:57 「facaiya9999」
    2026-09-10 12:25:07 「baihe6111」
    2026-09-14 12:34:45 / 12:34:50 / 12:35:00 / 12:35:21

四条不同机主的账号在 21 秒内一起被封 —— 这不是单账号行为检测，是**服务端按共同特征聚类批量封**。
共同特征只有两类：出口 IP（见 `netcfg.py`）与设备指纹。本模块解决后者。

改造前，全站每个账号对网易报的设备都是同一个常量：

    skyapi.UA_DEVICE      = "Xiaomi 2203121C; android 33.0.0; en"        ← 游戏 API 全量请求
    cnlogin.MPAY_UA       = "...(2203121C;31)"                           ← mpay 登录/验证码
    cnlogin.SKY_DEVICE_NAME = "Xiaomi 12S Ultra"                         ← mpay device_name 字段
    cnlogin.py:210        = "Sky-Live-.../x (Xiaomi 2203121C; ...)"       ← 第二处硬编码 UA

也就是：137 个账号全部自称「小米 12S Ultra / 2203121C / android 33」。真机的机型分布是长尾的，
全站收敛到 1 个值，等于给风控送了一个免费的聚合键。

本模块的做法
------------
按**账号维度**确定性派生一台「手机」：同一个账号永远是同一台（真机不会换机型），
不同账号拿到不同机型 / Android 版本。

    key = 光遇角色ID（acc.user_uuid）  → 游戏 API 全量请求
    key = "sms:00000000000" 等登录键   → mpay 登录链

确定性（而不是每次随机）是刻意的：**同一账号在不同会话里 UID 跳变，比固定值更像机器**。
不落库、不迁移、不改协议 —— UA 只是请求头，网易不拿它跟会话做绑定校验
（会话绑定的是 `deviceid`，见 `cnlogin.rand_udid` 注释：deviceid 与会话绑定校验 4010，不可随机）。

开关
----
`data/netcfg.json` 里 `{"fp": false}` 或环境变量 `QFLY_FP_OFF=1` → 立即退回一个固定档案，
无需改代码。**2026-09-18 起这个兜底不再是 `2203121C`**（用户要求弃用那台），
改为从 devrand 目录确定性取一台（见 `legacy_device()`）。
"""
from __future__ import annotations
import json

import hashlib
import os

from . import devrand, version

_SALT_DEFAULT = "qfly-fp-v1"
_CFG_FILE = os.path.join(os.path.abspath(os.environ.get("QFLY_DATA_DIR") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")), "netcfg.json")

# 真机 Android 版本分布（偏 13/14，与国服光遇玩家机型时间线对齐）
_ANDROIDS: list[tuple[str, str]] = [("12", "31"), ("13", "33"), ("13", "33"),
                                    ("14", "34"), ("14", "34"), ("15", "35")]

_FALLBACK_KEY = "legacy-fp-off"     # fp 关闭时的固定档案（不再是烧掉的 2203121C，见下）

# 已知机型的市场名（网易 `device_name` 字段真机发的是市场名而不是型号代码）。
# 目录里没登记的回退成「品牌 型号」—— 仍然是一台自洽的真机，只是命名颗粒度粗一点。
_MARKETING = {
    "PHY110": "OPPO Find X7", "PHY120": "OPPO Find X7 Ultra", "PGEM10": "OPPO Find X6 Pro",
    "PJD110": "OnePlus 12", "PJZ110": "OnePlus 13", "PHP110": "OnePlus Ace 3",
    "CPH2573": "OnePlus 12R", "CPH2581": "OPPO Find X7",
    "V2366GA": "iQOO 12", "V2309A": "vivo X100 Pro", "V2312A": "vivo X100",
    "V2324A": "iQOO Neo9", "V2337A": "vivo X100s", "V2331A": "iQOO Neo9 Pro",
    "V2254A": "iQOO 11", "V2185A": "vivo X80", "V2241HA": "iQOO 11 Pro",
    "23127PN0CC": "Redmi K70E", "23117RK66C": "Redmi K70", "23013RK75C": "Redmi Note 12 Turbo",
    "2311DRK48C": "Redmi K70 Pro", "24069PC21G": "Redmi K80", "23078RKD5C": "Redmi Note 13 Pro",
    "22081212C": "Redmi K50 Ultra", "2211133C": "Xiaomi 13", "2312DRA50C": "Redmi Note 13 Pro+",
    "24053PY09C": "Xiaomi 14", "23054RA19C": "Redmi Note 12T Pro",
    "BVL-AN20": "HONOR Magic6 Pro", "PGT-AN20": "HONOR Magic5 Pro", "ALN-AL10": "HONOR Magic6",
    "FCP-AN10": "HONOR 100 Pro", "RKY-AN10": "HONOR 90 GT", "ELI-AN00": "HONOR Magic5",
    "MAG-AN00": "HONOR 200", "BRC-AN00": "HONOR 100",
    "ALN-AL00": "HUAWEI Mate 60 Pro", "MNA-AL00": "HUAWEI P60 Pro", "CET-AL00": "HUAWEI Mate 60",
    "ADA-AL00": "HUAWEI Mate 60 Pro+", "LYA-AL00": "HUAWEI Mate 60 RS",
    "SM-S9210": "Samsung Galaxy S24", "SM-S9280": "Samsung Galaxy S24 Ultra",
    "SM-S9180": "Samsung Galaxy S23 Ultra", "SM-S9110": "Samsung Galaxy S23",
    "SM-F9460": "Samsung Galaxy Z Fold5", "SM-A5560": "Samsung Galaxy A55",
    "SM-S9360": "Samsung Galaxy S25", "SM-S9260": "Samsung Galaxy S24+",
    "RMX3820": "realme GT5", "RMX3706": "realme GT Neo5", "RMX3888": "realme GT5 Pro",
    "RMX3770": "realme GT Neo5 SE",
    "M2381": "Meizu 20", "M2391": "Meizu 21",
    "NX769J": "nubia Z60 Ultra", "NX729J": "nubia Z50",
    "A2023H": "ZTE Axon 50", "A2024H": "ZTE Axon 60",
    "GKWS6": "Google Pixel 8 Pro", "GC3VE": "Google Pixel 8",
    "XQ-DQ72": "Sony Xperia 1 V", "A065": "Nothing Phone (2a)",
}


_cfg_cache: dict | None = None
_cfg_sig = None


def _cfg() -> dict:
    """读 data/netcfg.json（与 netcfg.py 共用），坏文件一律当空配置。

    带 mtime 缓存：`device()` 会被每个出网请求调用一次（每次都要拼 UA），
    不能每请求都去解析一遍文件。
    """
    global _cfg_cache, _cfg_sig
    try:
        import json
        st = os.stat(_CFG_FILE)
        sig = (st.st_mtime_ns, st.st_size)
        if _cfg_cache is not None and _cfg_sig == sig:
            return _cfg_cache
        with open(_CFG_FILE, encoding="utf-8") as f:
            d = json.load(f)
        _cfg_cache = d if isinstance(d, dict) else {}
        _cfg_sig = sig
    except Exception:
        _cfg_cache = {}
        _cfg_sig = None
    return _cfg_cache or {}


def enabled() -> bool:
    if str(os.environ.get("QFLY_FP_OFF") or "").strip() in ("1", "true", "yes", "on"):
        return False
    return bool(_cfg().get("fp", True))


def salt() -> str:
    return str(_cfg().get("fp_salt") or _SALT_DEFAULT)


def _h(key: str, tag: str) -> int:
    raw = "%s|%s|%s" % (salt(), str(key or "anonymous"), tag)
    return int(hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16], 16)


def legacy_device() -> dict:
    """`fp` 关闭（QFLY_FP_OFF=1 / netcfg.fp=false）时的固定档案。

    改造前这个位置写死 `Xiaomi 2203121C / 小米 12S Ultra`。用户 2026-09-18 明确
    「不要 2203121C 这台设备了，容易封号」——那个型号已经被风控记住，不再当兜底。
    现在改成从 `devrand` 目录里确定性地取一台**没被烧过**的真机。

    仍然是「全站一台」：这正是 fp 关闭的语义（不伪装，只用于调试/对照），
    生产走上面的按账号派生分支。
    """
    c = devrand.CATALOG[_h(_FALLBACK_KEY, "model") % len(devrand.CATALOG)]
    tpl, brand, model = c[0], c[1], c[2]
    android, sdk = _ANDROIDS[_h(_FALLBACK_KEY, "os") % len(_ANDROIDS)]
    name = _MARKETING.get(model) or ("%s %s" % (brand, model))
    return {
        "template": "legacy:%s" % tpl, "brand": brand, "model": model,
        "android": android, "sdk": sdk, "device_name": name,
        "ua_device": "%s %s; android %s.0.0; en" % (brand, model, sdk),
        "spoofed": False,
    }


def device(key: str) -> dict:
    """账号 key → 稳定的设备档案（永远同一台）。"""
    if os.environ.get("QFLY_SINGLE_DEVICE_MODE") == "1":
        source = os.environ.get("QFLY_MPAY_DEVICE_ATTRS_FILE", "")
        if not source or not os.path.isfile(source):
            raise ValueError("单设备模式缺少当前设备属性文件，请先完成设备注册或导入")
        with open(source, encoding="utf-8") as f:
            record = json.load(f)
        a = record.get("attrs", record.get("attributes", record.get("fields", record)))
        dtype = str(a.get("device_type") or "").strip().lower()
        system_name = str(a.get("system_name") or "").strip()
        # W7：安装版注册的是如实的 Windows 独立客户端（device_type=other），
        # Windows 没有 Android API level，sdk_int 用 "0" 表示“不适用”（与
        # sky-play 注册表单 sv="0" 同义）。这种设备不能再按安卓指纹拼
        # "android N.0.0" 的 UA，否则等于把 Windows 伪装成安卓，前后矛盾。
        standalone = dtype == "other" or (system_name and system_name.lower() != "android")
        required = ("brand", "device_model", "system_version", "device_name")
        for field in required:
            if not str(a.get(field, "")):
                raise ValueError("当前设备属性缺少 " + field)
        if not standalone and not str(a.get("sdk_int", "")):
            raise ValueError("当前设备属性缺少 sdk_int")
        sdk = str(a.get("sdk_int") or ("0" if standalone else ""))
        if standalone:
            os_name = system_name or "Windows"
            return {"template": "observed-standalone-device", "brand": str(a["brand"]),
                    "model": str(a["device_model"]), "android": str(a["system_version"]),
                    "sdk": sdk, "device_name": str(a["device_name"]),
                    "system_name": os_name, "device_type": dtype or "other",
                    "ua_device": "%s %s; %s %s; en" % (
                        a["brand"], a["device_model"], os_name, a["system_version"]),
                    "ua_mpay": "SkyAccountConsole/1.0 (%s; standalone software client)" % os_name,
                    "spoofed": False}
        return {"template": "observed-current-device", "brand": str(a["brand"]),
                "model": str(a["device_model"]), "android": str(a["system_version"]),
                "sdk": sdk, "device_name": str(a["device_name"]),
                "ua_device": "%s %s; android %s.0.0; en" % (a["brand"], a["device_model"], sdk),
                "spoofed": False}
    if not enabled():
        return legacy_device()
    c = devrand.CATALOG[_h(key, "model") % len(devrand.CATALOG)]
    tpl, brand, model = c[0], c[1], c[2]
    android, sdk = _ANDROIDS[_h(key, "os") % len(_ANDROIDS)]
    name = _MARKETING.get(model) or ("%s %s" % (brand, model))
    return {
        "template": tpl, "brand": brand, "model": model,
        "android": android, "sdk": sdk, "device_name": name,
        "ua_device": "%s %s; android %s.0.0; en" % (brand, model, sdk),
        "spoofed": True,
    }


# ── 各调用点用的便捷函数 ──

def ua_device(key: str = "") -> str:
    s = device(key)["ua_device"]
    # 单设备模式注册的是 Windows 独立客户端（brand="Standalone software client"）。
    # /account/* 认这种 UA，但 /service/relationship/*（好友名单、星盘、送礼）会直接
    # 400 Invalid Request。机型段本来就"可任取"（见 sky_cn_ua 注释），这里换成一台
    # 确定性的安卓机型：登录用的设备凭据（device_id/key）完全不动，只换 UA 展示段。
    if "Standalone software client" in s or "WindowsPC" in s or "Windows" in s:
        return legacy_device()["ua_device"]
    return s


def legacy_ua() -> str:
    """fp 关闭时的 UA 机型段（`skyapi.UA_DEVICE` / `cnlogin.MPAY_UA` 用它组装）。"""
    return legacy_device()["ua_device"]


def sky_live_id(key: str = "") -> str:
    """历史遗留：旧 UA 的 `Sky-Live-<9 位数字>` 段。

    ★★ 2026-09-24 418 根因（已彻底定位并修复）★★
      网易网关用**正则拉黑了 `Sky-Live-\\d+/` 这个 UA 前缀模式**——实测：

        418 ✗  Sky-Live-000000000/0.16.5.412659 (HUAWEI BAL-AL80; android 34.0.0; en)
        418 ✗  Sky-Live-122191640/0.16.5.412659 (vivo V2312A; android 34.0.0; en)   ← 换机型/换数字仍 418
        418 ✗  Sky-Live-000000000/0.16.5.5119   (HUAWEI BAL-AL80; android 34.0.0; en) ← 换版本号仍 418
        200 ✓  Sky/0.16.5.412659                (HUAWEI BAL-AL80; android 34.0.0; en) ← 只换前缀立刻过
        200 ✓  Sky-Live-000000000 0.16.5.412659 (…)                                   ← 只去掉斜杠立刻过
        200 ✓  Sky-Live-000000000/0.16.5.412659 (HUAWEI BAL-AL80)                     ← 只截短括号段也过

      即：触发条件是 **`Sky-Live-<数字>/` 前缀 + `; android N.0.0; <lang>` 后缀**同时出现；
      单独出现任何一个都不触发。`Sky-Live-NNN/…` 是我们自己造的格式（官方客户端不用），
      被网易识别成脚本特征后整体拉黑 —— 162 个账号共用它，一次全挂。

      修复=改用官方 `Sky/<version> (<device>)` 形态（见 sky_ua）。本函数仅为兼容
      历史调用保留，新代码不要再拼进 UA。
    """
    return "%09d" % (_h(key, "liveid") % 1000000000) if enabled() else "000000000"


def sky_ua(key: str, version_str: str) -> str:
    """Sky 游戏 API 的 UA（**国际服**形态 `Sky/<version> (<device>)`）。

    ★ 国服 0.16.5 不要用这个 —— 国服网关现在只放行官方
      `Sky-Live-<9位数字>/<版本>.<build> (<设备>)` 形态，用本形态一律 418。
      国服请用 sky_cn_ua()。详见 sky_cn_ua / sky_live_id 的文档。
    """
    return "Sky/%s (%s)" % (version_str, ua_device(key))


# ★★ 国服网关双闸门之一：x-sky-build-access-key 每个构建号都会换 ★★
# 值 = 官方 APK 编译期常量 com.tgc.sky.BuildConfig.SKY_BUILD_ACCESS_KEY。
# 0.16.8 的新值不写进公开仓库。运行时由 qfsky.build_key 解析：
#   环境变量 SKY_BUILD_ACCESS_KEY_NETEASE
#   → runtime/private/build_access_key
#   → 私有登录服务下发后的本机缓存
# 旧 0.16.7 默认值已删除；缺失时 build_access_key() 抛中文错误，绝不发空串硬闯。
def build_access_key() -> str:
    from .build_key import resolve
    return resolve()


def sky_cn_ua(key: str, version_str: str) -> str:
    """**国服** Sky API 的官方 UA：`Sky-Live-<9位数字>/<版本> (<设备>)`。

    ★ 0.16.5 起这是唯一能过网易网关的形态（真机内存取证，2026-09-24）：

        Sky-Live-678472765/0.16.5.412659 (Xiaomi 23013RK75C; android 36.0.0; en)  200 ✓
        Sky/0.16.5.412659                (HUAWEI BAL-AL80;   android 34.0.0; en)  418 ✗
        Sky-Live-678472765/0.15.8.383089 (Xiaomi 23013RK75C; android 36.0.0; en)  418 ✗
        Sky-Live-678472765/0.16.5.412659                         （无括号段）      418 ✗
        Sky-Live-678472765 0.16.5.412659 (Xiaomi 23013RK75C; …)  （空格代斜杠）    418 ✗

    机型/机型品牌/9 位数字可任取（不影响判定），但**斜杠、括号段、版本 ≥0.16.5** 三者缺一不可。
    注：sky_live_id 文档里 2026-09-24 早些时候记的「`Sky-Live-<数字>/` 被拉黑、
    `Sky/` 才过」结论，已被同日真机内存取证推翻 —— 那次测试没有带正确的
    build-access-key，两处变量纠缠导致误判。现结论以本节为准。
    """
    return "Sky-Live-%s/%s (%s)" % (sky_live_id(key), version_str, ua_device(key))


def mpay_ua(key: str = "") -> str:
    """MPay 请求 UA。版本号与 cnlogin 的表单同源（见 docs/知识库/66）。

    末尾的 `;NN` 是**该设备指纹的 Android API 级别**，表单里的 `sv` 必须取同一个值
    （`cnlogin._form_base` 用 `version.mpay_sv(fp.device(key))`）——
    否则同一个请求的 UA 写 (机型;33)、表单写 sv=31，自己跟自己矛盾。
    """
    d = device(key)
    if d.get("ua_mpay"):
        # 独立软件客户端：sky-play 线上一直用这个 UA 打 mpay；不要把 Windows
        # 设备塞进官方安卓客户端的 "(型号;API)" 形态里。
        return str(d["ua_mpay"])
    return "com.netease.sky/%s NeteaseMobileGame/%s (%s;%s)" % (
        version.mpay_gv(), version.MPAY_CV, d["model"], d["sdk"])


def device_name(key: str = "") -> str:
    return device(key)["device_name"]


def payload(key: str) -> dict:
    """给前端/体检页看的精简档案。"""
    d = device(key)
    return {"model": d["model"], "brand": d["brand"], "android": d["android"], "name": d["device_name"]}
