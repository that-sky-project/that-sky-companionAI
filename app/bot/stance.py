"""小光切换站姿（2026-11-01，Codex 调研 + 实测）：站姿 = 装扮里的 attitude（客户端枚举 0~14）。
set_outfit 是整套覆盖：先拉完整装扮、备份、只改 attitude、整套写回。
  stance.py show            # 当前装扮的 attitude
  stance.py set <0~14|名字>  # 改站姿（自动备份到 control/outfit_backup_*.json）
  stance.py restore <备份>   # 用备份整套写回
"""
import copy
import json
import sys
import time
from pathlib import Path

import xg_paths

sys.path.insert(0, str(xg_paths.BOT_CODE))
import long_runner as L  # noqa: E402
from qfsky import outfit as O  # noqa: E402
from qfsky.skyapi import SkyClient  # noqa: E402
import xg_account  # noqa: E402  T24-2：备份按号存

ROOT = xg_paths.DATA
# Codex 从客户端反射表读出的枚举（kAttitude_*）；中文是检索名，不全是官方叫法
STANCES = {0: "普通", 1: "畏寒", 2: "张开双臂", 3: "骄傲", 4: "警惕", 5: "强硬", 6: "庄重", 7: "妖娆",
           8: "慵懒", 9: "智慧", 10: "内秀", 11: "拘谨", 12: "受伤", 13: "低落", 14: "警戒"}


STANCE_NAMES = ["普通", "骄傲", "妖娆", "警惕", "强硬", "庄重", "慵懒", "智慧", "内秀", "拘谨", "低落", "警戒", "张开双臂"]
ALIASES = {"默认": 0, "正常": 0, "俏皮": 7, "自信": 5, "害羞": 10, "驼背": 13, "难过": 13, "沮丧": 13, "受伤": 12, "畏寒": 1}


def resolve(name):
    """中文站姿名 → 编号；不认识返回 None。先用 Codex 的 motion_catalog，再用这里的口语兜底（未确认）。"""
    try:
        import motion_catalog
        r = motion_catalog.resolve_stance(name)
        if r:
            return int(r["id"])
    except Exception:
        pass
    name = str(name).replace("站姿", "").replace("姿势", "").strip()
    for k, n in STANCES.items():
        if n == name:
            return k
    return ALIASES.get(name)


def client():
    a = L.load_account((ROOT / "control/active_account").read_text().strip())
    return SkyClient(a["user_uuid"], a["session"], a["session2"], a.get("data_version") or "")


def current(c):
    r = c.post(O.P_OUTFIT, {})
    return r, O.outfit_set(r)


def set_stance(value):
    c = client()
    r, o = current(c)
    if o is None:
        raise RuntimeError("读不到当前装扮")
    bk = xg_account.state_dir() / f"outfit_backup_{time.strftime('%m%d%H%M%S')}.json"
    bk.parent.mkdir(parents=True, exist_ok=True)
    bk.write_text(json.dumps(o, ensure_ascii=False))
    new = copy.deepcopy(o)
    new["attitude"] = int(value)   # 要发整数（发字符串 "3" 服务器回 ok 但不改；读回来是字符串）
    resp = c.post(O.P_SET_OUTFIT, {"outfit": new})   # 要包一层 outfit（不包 404）
    _r2, after = current(c)
    return {"backup": str(bk), "before": o.get("attitude"), "after": after.get("attitude") if after else None,
            "resp": str(resp)[:200], "slots_same": all(after.get(k) == o.get(k) for k, _ in O.OUTFIT_SLOTS) if after else None}


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["show"]:
        _r, o = current(client())
        print(json.dumps({"attitude": o.get("attitude"), "keys": list(o)}, ensure_ascii=False))
    elif a[:1] == ["set"] and len(a) == 2:
        v = int(a[1]) if a[1].isdigit() else next(k for k, n in STANCES.items() if n == a[1])
        print(json.dumps(set_stance(v), ensure_ascii=False))
    elif a[:1] == ["restore"] and len(a) == 2:
        o = json.loads(Path(a[1]).read_text())
        print(json.dumps(client().post(O.P_SET_OUTFIT, {"outfit": o}), ensure_ascii=False)[:300])
    else:
        print(__doc__)
