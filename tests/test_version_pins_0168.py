"""0.16.8 版本常量一致性（terrainfix.11）。

terrainfix.6~9 曾漏改三处仍停留在 0.16.7 的值，导致 0.16.8 下一上线就退出：
跟随配置的程序哈希、anim_pose 的角色状态地址、scene_migration 的目标哈希。
这里把运行时会用到的版本常量互相对照，防止以后再漏。
"""
import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
for p in (APP, APP / "bot", APP / "escort-v31-server"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

NATIVE_0167 = "0b81f1da54d9c6f28c08746b2ec19519d8eb78c4f1b3c5e4e420ff726dc25321"


def _text(rel):
    return (APP / rel).read_text(encoding="utf-8")


def _const(rel, name):
    m = re.search(r"^%s\s*=\s*['\"]([0-9a-f]{64})['\"]" % re.escape(name), _text(rel), re.M)
    if not m:
        raise AssertionError("%s 里找不到 %s" % (rel, name))
    return m.group(1)


class VersionPins0168(unittest.TestCase):
    def setUp(self):
        self.native = _const("qfsky/join0167_profile.py", "NATIVE_SHA256")

    def test_hop_config_codec_matches_runtime(self):
        cfg = json.loads(_text("config/experimental-hop-trial.runtime.json"))
        self.assertEqual(cfg["hopV20"]["codecSHA256"],
                         _const("bot/hop_actor_profile_v20.py", "CODEC_SHA256"))
        self.assertEqual(cfg["hopV20"]["codecSHA256"], self.native)

    def test_runtime_native_pins_agree(self):
        for rel, name in (("bot/hop_actor_profile_v20.py", "CODEC_SHA256"),
                          ("capture/native_profile.py", "NATIVE_SHA256"),
                          ("qfsky/long0167/codec/scene_wire.py", "PUBLIC_ELF_SHA256_0168")):
            self.assertEqual(_const(rel, name), self.native, rel)

    def test_anim_pose_avatar_state_is_0168(self):
        m = re.search(r'^SEATED, SIT_ENTER, SEAT_ID, AVATAR_STATE = (.+)$',
                      _text("bot/anim_pose.py"), re.M)
        self.assertIsNotNone(m)
        self.assertIn('"0x21de944"', m.group(1))
        self.assertNotIn('"0x21df140"', m.group(1))

    def test_scene_migration_targets_0168(self):
        src = _text("qfsky/map_long0167/scene_migration.py")
        self.assertIn("NEW_SHA=scene.PUBLIC_ELF_SHA256_0168", src)
        self.assertIn("verified_target_build=421629", src)

    def test_no_0167_native_in_runtime_config(self):
        for p in (APP / "config").glob("*.json"):
            self.assertNotIn(NATIVE_0167, p.read_text(encoding="utf-8"), p.name)


if __name__ == "__main__":
    unittest.main()
