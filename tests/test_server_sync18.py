"""terrainfix.18：11-19～11-22 服务器修复同步后的离线检查（不联网、不读真实账号数据）。"""
import ast
import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"


def _func(path, name):
    """从源码里只取出一个顶层函数来测，避免导入整个运行器。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    ns = {}
    exec(compile(ast.Module([node], []), str(path), "exec"), ns)
    return ns[name]


class AffinityT100(unittest.TestCase):
    def test_body_matches_native_layout(self):
        body = _func(APP / "bot/long_runner.py", "_t100_affinity_body")
        self.assertEqual(body(5, [(3, 255)]).hex(), "0705000103ff")
        self.assertEqual(body(1, []).hex(), "07010000")
        self.assertEqual(len(body(2, [(i, 9) for i in range(12)])), 4 + 2 * 8)   # 最多 8 条

    def test_loop_has_switch_and_outbox(self):
        src = (APP / "bot/long_runner.py").read_text(encoding="utf-8")
        self.assertIn('SKY_T100_AFFINITY", "1"', src)
        self.assertIn("map_long_engine.OUTBOX.append", src)
        eng = (APP / "escort-v31-server/qfsky/map_long0167/map_long_engine.py").read_text(encoding="utf-8")
        self.assertIn("if OUTBOX:", eng)          # 客户端不设 CLAUDEWORK_LONG 也要发出


class PendingT101(unittest.TestCase):
    def setUp(self):
        os.environ.pop("SKY_T101_PENDING_IGNORE", None)
        import qfsky.handoff0167 as h
        import qfsky.receive0167 as r
        self.h, self.M = h, (lambda t: r.ServerMessage(message_type=t, body=b"\x00" * 5))

    def act(self, seq):
        return self.h.preflight_pending_batch([self.M(t) for t in seq]).action

    def test_notice_before_enter_is_ignored(self):
        self.assertEqual(self.act([14, 8, 14]), "wait")
        self.assertEqual(self.act([8, 17]), "validate-enter")
        self.assertEqual(self.act([14, 8, 17, 8]), "validate-enter")

    def test_rejections_still_veto(self):
        self.assertEqual(self.act([2, 8]), "veto")
        self.assertEqual(self.act([9]), "veto")

    def test_switch_off_restores_old_rule(self):
        os.environ["SKY_T101_PENDING_IGNORE"] = ""
        try:
            self.assertEqual(self.act([14, 8, 14]), "veto")
        finally:
            os.environ.pop("SKY_T101_PENDING_IGNORE", None)


class ClientPaths(unittest.TestCase):
    FILES = ["bot/long_runner.py", "bot/room_pose.py", "bot/tour_runtime.py", "bot/ai_companion.py",
             "bot/leader_travel_rpc.py", "bot/leader_travel_runtime.py", "bot/owner_rejoin.py",
             "bot/same_map_move_policy.py", "qfsky/home_handoff_runtime0167.py", "qfsky/join0167_profile.py",
             "qfsky/handoff0167.py", "qfsky/long0167/candle_rpc.py", "qfsky/long0167/scene_edit.py",
             "escort-v31-server/qfsky/connection0167.py",
             "escort-v31-server/qfsky/map_long0167/scene_only_draft.py"]

    def test_no_server_paths(self):
        for rel in self.FILES:
            src = (APP / rel).read_text(encoding="utf-8")
            code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
            self.assertNotIn("/root/", code, rel)
            self.assertNotIn("claudework/sky-ai", code, rel)

    def test_bundled_tables_parse(self):
        for name in ("tour_routes.json", "spawn_y_fix.json", "candle-tables.json",
                     "darkstone-slots.json", "change_level_tail.json"):
            json.loads((APP / "config" / name).read_text(encoding="utf-8"))

    def test_tour_routes_fallback_to_bundle(self):
        import tour_runtime
        with tempfile.TemporaryDirectory() as d:
            p = tour_runtime._routes_path(d)
            self.assertTrue(Path(p).is_file())
            doc = tour_runtime.load_routes(p)
            self.assertTrue(doc)

    def test_scene_blob_dir_under_data_dir(self):
        src = (APP / "qfsky/join0167_profile.py").read_text(encoding="utf-8")
        self.assertIn("'runtime', 'private', 'scene_blobs'", src)

    def test_no_scene_blob_data_shipped(self):
        self.assertFalse(list(ROOT.rglob("scene_blobs/*.bin")))
        self.assertFalse(list(ROOT.rglob("t90_dayhub_blob.bin")))


if __name__ == "__main__":
    unittest.main()
