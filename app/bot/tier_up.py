"""解锁好友树的“大节点”（信任等级提升）：POST /service/trust/api/v1/tier_up {"target": 好友 uuid}。

做法照搬线上礼包后端 sync_friend_trust（/opt/sky-gift-api/current/backend.py，只读参考）：
被送烛的一方在对方送够蜡烛后主动“点大节点” = 提交一次已挣到的信任等级提升。服务端自己判断够不够，
不够就返回 success=false，不会误升。该接口不接受 x-sky-level-id 头。
用法：python tier_up.py <账号ID> <好友 uuid>
"""
import json
import sys
from pathlib import Path

import xg_paths

ROOT = xg_paths.DATA
sys.path.insert(0, str(ROOT / "runtime/source/online/03_网页前后端源码/server"))
from qfsky.skyapi import SkyClient  # noqa: E402


def tier_up(client, target):
    r = client.post("/service/trust/api/v1/tier_up", {"target": target}, include_level_id=False, attempts=1)
    return {k: r.get(k) for k in ("success", "reason", "tier", "old_tier", "xp", "target", "type")}


def client_for(account):
    return SkyClient(account["user_uuid"], account["session"], account["session2"], account.get("data_version") or "")


def load_account(acc_id):
    data = json.load(open(ROOT / "runtime/runtime-data/accounts.json", encoding="utf-8"))
    items = data if isinstance(data, list) else data.get("accounts", data)
    items = list(items.values()) if isinstance(items, dict) else items
    return next(x for x in items if isinstance(x, dict) and x.get("id") == acc_id)


if __name__ == "__main__":
    print(json.dumps(tier_up(client_for(load_account(sys.argv[1])), sys.argv[2]), ensure_ascii=False))
