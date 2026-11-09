# -*- coding: utf-8 -*-
"""随机机型目录：userdev 注册设备不再绑死 9 台 MuMu 模板，改为目录内随机取值。

为什么要随机
------------
2026-09-12 之前 `userdev.register()` 的 model/brand 取自 `devpool.DEVICES`（9 台真机模板）。
只有 9 个取值意味着**全站所有用户的设备卡片翻来覆去就是那 9 个机型**——这本身就是
设备聚集特征：正常玩家的机型分布是长尾的，不会全站收敛到 9 个值上。

为什么随机是安全的（实测 + 代码级证据）
---------------------------------------
`model`/`brand` **从不进入任何出网请求**，只用于网页设备选择器的展示：

1. 9393 模板（`sky9393.build_upload_plain`）只字节替换
   `platform / source_platform / sdkuid / sessionid / deviceid / udid` 六个字段，
   模板 JSON 里根本没有机型字段 —— 实现里没有任何 `_replace_field(text, "model", ...)`。
2. 各渠道取设备的唯一入口是 `device["udid"]` / `device["urs_udid"]`：
   `cnlogin._dev_udid()`、`cnlogin._form_base(urs_udid=...)`、
   `hwqr.generate_qr(udid=...)`、`honorqr.generate_qr(udid=...)`。
3. 真机 SDK 里唯一会外发的机型是华为 `_hw_uni_sauth` 的 `sdklog.device_model`
   （写死 `ALN-AL10`）和荣耀 `honor.uni_sauth` 的 `sdklog.device_model`（写死 `23013RK75C`），
   都是常量，与 userdev 无关。
4. 9393 服务端实测不校验 udid 值（`cnlogin.rand_udid` 注释：2026-09-09 p/b/m 模板随机 16hex 全过）。

因此机型的唯一约束是「**看起来像真机**」——所以目录全部用真实上市机型的型号代码，
而不是任意字符串。

目录结构
--------
每项 `(key, brand, model, resolution)`：key 落库进 `template` 字段（保留旧字段，便于追踪这一台从哪来）；
`resolution` 是真实上市机型的主屏物理分辨率，供 `devreg` 合成注册报文用。
型号代码为各厂商公开的 `ro.product.model`，覆盖 OPPO/vivo/小米/红米/荣耀/华为/三星/
一加/realme/魅族/努比亚/中兴/Google 等——和光遇国服玩家实际机型分布对齐。
"""
from __future__ import annotations

import secrets

# (key, brand, model)
CATALOG: list[tuple[str, str, str, str]] = [
    # ---- OPPO ----
    ("oppo_phy110", "OPPO", "PHY110", "1240*2772"),
    ("oppo_phy120", "OPPO", "PHY120", "1240*2772"),
    ("oppo_pgem10", "OPPO", "PGEM10", "1440*3168"),
    ("oppo_pgu110", "OPPO", "PGU110", "1240*2772"),
    ("oppo_pffm20", "OPPO", "PFFM20", "1240*2772"),
    ("oppo_pfjm10", "OPPO", "PFJM10", "1240*2772"),
    ("oppo_pjv110", "OPPO", "PJV110", "1240*2772"),
    ("oppo_pjw110", "OPPO", "PJW110", "1240*2772"),
    ("oppo_cph2581", "OPPO", "CPH2581", "1440*3168"),
    ("oppo_cph2609", "OPPO", "CPH2609", "1080*2412"),
    ("oppo_pht110", "OPPO", "PHT110", "1080*2412"),
    ("oppo_pkd110", "OPPO", "PKD110", "1080*2400"),
    # ---- OnePlus ----
    ("oneplus_pjd110", "OnePlus", "PJD110", "1440*3168"),
    ("oneplus_pjz110", "OnePlus", "PJZ110", "1440*3168"),
    ("oneplus_php110", "OnePlus", "PHP110", "1240*2772"),
    ("oneplus_cph2573", "OnePlus", "CPH2573", "1240*2772"),
    ("oneplus_pja110", "OnePlus", "PJA110", "1240*2772"),
    # ---- vivo ----
    ("vivo_v2366ga", "vivo", "V2366GA", "1260*2800"),
    ("vivo_v2309a", "vivo", "V2309A", "1260*2800"),
    ("vivo_v2312a", "vivo", "V2312A", "1080*2400"),
    ("vivo_v2324a", "vivo", "V2324A", "1260*2800"),
    ("vivo_v2337a", "vivo", "V2337A", "1260*2800"),
    ("vivo_v2331a", "vivo", "V2331A", "1080*2400"),
    ("vivo_v2254a", "vivo", "V2254A", "1080*2400"),
    ("vivo_v2185a", "vivo", "V2185A", "1440*3200"),
    ("vivo_v2241ha", "vivo", "V2241HA", "1080*2400"),
    ("vivo_v2280a", "vivo", "V2280A", "1080*2400"),
    ("vivo_pd2309", "vivo", "PD2309", "1260*2800"),
    ("vivo_v2352a", "vivo", "V2352A", "1260*2800"),
    ("vivo_v2415a", "vivo", "V2415A", "1260*2800"),
    # ---- Xiaomi ----
    ("mi_23127pn0cc", "Xiaomi", "23127PN0CC", "1200*2670"),
    ("mi_23117rk66c", "Xiaomi", "23117RK66C", "1220*2712"),
    ("mi_23013rk75c", "Xiaomi", "23013RK75C", "1220*2712"),
    ("mi_2311drk48c", "Xiaomi", "2311DRK48C", "1220*2712"),
    ("mi_24069pc21g", "Xiaomi", "24069PC21G", "1440*3200"),
    ("mi_23078rkd5c", "Xiaomi", "23078RKD5C", "1220*2712"),
    ("mi_22081212c", "Xiaomi", "22081212C", "1440*3200"),
    ("mi_2211133c", "Xiaomi", "2211133C", "1440*3200"),
    ("mi_2312dra50c", "Xiaomi", "2312DRA50C", "1220*2712"),
    ("mi_24053py09c", "Xiaomi", "24053PY09C", "1200*2670"),
    ("mi_2312drc48c", "Xiaomi", "2312DRC48C", "1220*2712"),
    ("mi_23054ra19c", "Xiaomi", "23054RA19C", "1080*2400"),
    # ---- HONOR ----
    ("honor_bvl_an20", "HONOR", "BVL-AN20", "1080*2400"),
    ("honor_pgt_an20", "HONOR", "PGT-AN20", "1080*2400"),
    ("honor_aln_al10", "HONOR", "ALN-AL10", "1080*2400"),
    ("honor_fcp_an10", "HONOR", "FCP-AN10", "1224*2700"),
    ("honor_rky_an10", "HONOR", "RKY-AN10", "1200*2664"),
    ("honor_eli_an00", "HONOR", "ELI-AN00", "1224*2700"),
    ("honor_mag_an00", "HONOR", "MAG-AN00", "1080*2376"),
    ("honor_brc_an00", "HONOR", "BRC-AN00", "1224*2700"),
    ("honor_rea_an00", "HONOR", "REA-AN00", "1224*2700"),
    ("honor_any_an00", "HONOR", "ANY-AN00", "1080*2400"),
    # ---- HUAWEI ----
    ("hw_aln_al00", "HUAWEI", "ALN-AL00", "1080*2400"),
    ("hw_mna_al00", "HUAWEI", "MNA-AL00", "1224*2700"),
    ("hw_cet_al00", "HUAWEI", "CET-AL00", "1224*2700"),
    ("hw_ada_al00", "HUAWEI", "ADA-AL00", "1224*2700"),
    ("hw_lya_al00", "HUAWEI", "LYA-AL00", "1224*2700"),
    ("hw_bal_al80", "HUAWEI", "BAL-AL80", "1080*2400"),
    # ---- samsung ----
    ("ss_sm_s9210", "samsung", "SM-S9210", "1080*2340"),
    ("ss_sm_s9280", "samsung", "SM-S9280", "1440*3120"),
    ("ss_sm_s9180", "samsung", "SM-S9180", "1440*3088"),
    ("ss_sm_s9110", "samsung", "SM-S9110", "1080*2340"),
    ("ss_sm_f9460", "samsung", "SM-F9460", "1812*2176"),
    ("ss_sm_a5560", "samsung", "SM-A5560", "1080*2340"),
    ("ss_sm_s9360", "samsung", "SM-S9360", "1080*2340"),
    ("ss_sm_s9260", "samsung", "SM-S9260", "1080*2340"),
    # ---- realme ----
    ("realme_rmx3820", "realme", "RMX3820", "1240*2772"),
    ("realme_rmx3706", "realme", "RMX3706", "1080*2412"),
    ("realme_rmx3888", "realme", "RMX3888", "1240*2772"),
    ("realme_rmx3770", "realme", "RMX3770", "1080*2412"),
    # ---- Meizu ----
    ("meizu_m2381", "Meizu", "M2381", "1080*2400"),
    ("meizu_m2391", "Meizu", "M2391", "1080*2400"),
    # ---- nubia ----
    ("nubia_nx769j", "nubia", "NX769J", "1116*2480"),
    ("nubia_nx729j", "nubia", "NX729J", "1116*2480"),
    # ---- ZTE ----
    ("zte_a2023h", "ZTE", "A2023H", "1080*2400"),
    ("zte_a2024h", "ZTE", "A2024H", "1080*2400"),
    # ---- google ----
    ("google_gkws6", "google", "GKWS6", "1080*2400"),
    ("google_gc3ve", "google", "GC3VE", "1344*2992"),
    # ---- Sony ----
    ("sony_xq_dq72", "Sony", "XQ-DQ72", "1080*2520"),
    # ---- Nothing ----
    ("nothing_a065", "Nothing", "A065", "1080*2400"),
    # ---- OPPO ----
    ("oppo_pkb110", "OPPO", "PKB110", "1080*2412"),
    ("oppo_pjy110", "OPPO", "PJY110", "1240*2772"),
    ("oppo_phv110", "OPPO", "PHV110", "1240*2772"),
    ("oppo_pgt110", "OPPO", "PGT110", "1240*2772"),
    ("oppo_pku110", "OPPO", "PKU110", "1240*2772"),
    ("oppo_cph2557", "OPPO", "CPH2557", "1240*2772"),
    ("oppo_cph2525", "OPPO", "CPH2525", "1240*2772"),
    ("oppo_phb110", "OPPO", "PHB110", "1440*3216"),
    # ---- OnePlus ----
    ("oneplus_pjx110", "OnePlus", "PJX110", "1240*2772"),
    ("oneplus_pjh110", "OnePlus", "PJH110", "1240*2772"),
    # ---- vivo ----
    ("vivo_v2307a", "vivo", "V2307A", "1260*2800"),
    ("vivo_v2318a", "vivo", "V2318A", "1260*2800"),
    ("vivo_v2342a", "vivo", "V2342A", "1260*2800"),
    ("vivo_v2329a", "vivo", "V2329A", "1260*2800"),
    ("vivo_v2302a", "vivo", "V2302A", "1260*2800"),
    ("vivo_v2334a", "vivo", "V2334A", "1260*2800"),
    ("vivo_i2301", "vivo", "I2301", "1260*2800"),
    ("vivo_i2201", "vivo", "I2201", "1080*2400"),
    ("vivo_v2308a", "vivo", "V2308A", "1080*2400"),
    ("vivo_v2405a", "vivo", "V2405A", "1260*2800"),
    # ---- Xiaomi ----
    ("mi_2312draabc", "Xiaomi", "2312DRAABC", "1220*2712"),
    ("mi_24069ra21c", "Xiaomi", "24069RA21C", "1220*2712"),
    ("mi_23090ra98c", "Xiaomi", "23090RA98C", "1220*2712"),
    ("mi_2210132c", "Xiaomi", "2210132C", "1440*3200"),
    ("mi_2304fpn6dc", "Xiaomi", "2304FPN6DC", "1220*2712"),
    ("mi_24018rpacc", "Xiaomi", "24018RPACC", "1440*3200"),
    ("mi_23129raa4c", "Xiaomi", "23129RAA4C", "1220*2712"),
    ("mi_24072px77c", "Xiaomi", "24072PX77C", "1080*2400"),
    ("mi_23124ra7eo", "Xiaomi", "23124RA7EO", "1080*2400"),
    # ---- HONOR ----
    ("honor_fne_an00", "HONOR", "FNE-AN00", "1200*2664"),
    ("honor_maa_an10", "HONOR", "MAA-AN10", "1224*2700"),
    ("honor_lra_an00", "HONOR", "LRA-AN00", "1224*2700"),
    ("honor_mag_an10", "HONOR", "MAG-AN10", "1224*2700"),
    ("honor_rvl_an00", "HONOR", "RVL-AN00", "1224*2700"),
    ("honor_aln_al80", "HONOR", "ALN-AL80", "1080*2400"),
    ("honor_brp_an00", "HONOR", "BRP-AN00", "1224*2700"),
    ("honor_elp_an00", "HONOR", "ELP-AN00", "1224*2700"),
    # ---- HUAWEI ----
    ("hw_lna_al00", "HUAWEI", "LNA-AL00", "1224*2700"),
    ("hw_clt_al00", "HUAWEI", "CLT-AL00", "1224*2700"),
    ("hw_ele_al00", "HUAWEI", "ELE-AL00", "1224*2700"),
    ("hw_hbn_al00", "HUAWEI", "HBN-AL00", "1224*2700"),
    ("hw_mrr_w29", "HUAWEI", "MRR-W29", "1840*2800"),
    # ---- samsung ----
    ("ss_sm_s9310", "samsung", "SM-S9310", "1080*2340"),
    ("ss_sm_s9380", "samsung", "SM-S9380", "1440*3120"),
    ("ss_sm_f9560", "samsung", "SM-F9560", "1856*2160"),
    ("ss_sm_a5660", "samsung", "SM-A5660", "1080*2340"),
    ("ss_sm_s7210", "samsung", "SM-S7210", "1080*2340"),
    ("ss_sm_g9910", "samsung", "SM-G9910", "1080*2340"),
    # ---- realme ----
    ("realme_rmx3850", "realme", "RMX3850", "1240*2772"),
    ("realme_rmx3997", "realme", "RMX3997", "1080*2400"),
    ("realme_rmx3920", "realme", "RMX3920", "1240*2772"),
    # ---- Meizu ----
    ("meizu_m2481", "Meizu", "M2481", "1080*2400"),
    ("meizu_m3481", "Meizu", "M3481", "1224*2700"),
    # ---- nubia ----
    ("nubia_nx779j", "nubia", "NX779J", "1116*2480"),
    ("nubia_nx749j", "nubia", "NX749J", "1116*2480"),
    # ---- ZTE ----
    ("zte_a2025h", "ZTE", "A2025H", "1080*2400"),
    # ---- google ----
    ("google_g3nz6", "google", "G3NZ6", "1080*2400"),
    ("google_gb7n6", "google", "GB7N6", "1080*2400"),
    ("google_g9s9b", "google", "G9S9B", "1344*2992"),
    # ---- Sony ----
    ("sony_xq_de72", "Sony", "XQ-DE72", "1080*2520"),
    ("sony_xq_ct72", "Sony", "XQ-CT72", "1644*3840"),
    # ---- Nothing ----
    ("nothing_a063", "Nothing", "A063", "1080*2400"),
    ("nothing_a142", "Nothing", "A142", "1080*2400"),
]

_BY_KEY = {c[0]: c for c in CATALOG}


def pick(used_models: set[str]) -> dict:
    """随机挑一个「该用户还没用过」的机型；用户已用遍目录时才允许重复（保底回绕）。

    真随机（`secrets.choice`）而不是哈希取模：哈希取模会让**同一个 user_id 得到
    固定序列**，也就是全站只有 len(CATALOG) 条不同的「用户 → 机型序列」——
    随机取值才能让两台设备的组合空间真正展开。
    """
    pool = [c for c in CATALOG if c[2] not in used_models]
    if not pool:                       # 该用户把目录用遍（5 台上限下不可能，保底）
        pool = CATALOG
    c = secrets.choice(pool)
    return {"template": c[0], "brand": c[1], "model": c[2], "resolution": c[3]}


def get(key: str) -> dict | None:
    c = _BY_KEY.get(str(key))
    if not c:
        return None
    return {"template": c[0], "brand": c[1], "model": c[2], "resolution": c[3]}


def size() -> int:
    return len(CATALOG)
