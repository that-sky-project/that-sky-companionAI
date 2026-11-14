"""Offline terrainfix.11 props regression tests; no account/config/network access."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
for path in (APP, APP / "src"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import xg_paths
from skyterrain import props
from skyterrain.dynamic_props import PlaceableBinding
from prop_synthetic_fixtures import write_catalog, READY_COUNT, TOTAL_COUNT


class PropRuntimeSyncTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="props-synthetic-", dir=ROOT/"tests")
        cls.addClassCleanup(cls.temp.cleanup)
        cls.fixture_root = write_catalog(Path(cls.temp.name)/"props")
        cls.catalog = props.load_generated_catalog(cls.fixture_root, expected_build=421629)
        cls.names = props.available_prop_names(catalog=cls.catalog)

    def test_asset_path_is_local_generated_and_centralized(self):
        self.assertEqual(xg_paths.PROP_ASSETS, xg_paths.GAME_DIR / "props")
        self.assertEqual(xg_paths.ASSETS, APP / "assets")

    def test_all_synthetic_ready_preload_with_no_errors(self):
        self.assertIsInstance(self.catalog, props.IndexedPropCatalog)
        self.assertEqual(len(self.names), READY_COUNT)
        self.assertEqual(len(props.known_prop_names(catalog=self.catalog)), TOTAL_COUNT)
        self.assertEqual(self.catalog["load_errors"], [])
        info = self.catalog.cache_info()
        self.assertEqual(info["preloaded"], READY_COUNT)
        self.assertLessEqual(info["raw_bytes"], props.MAX_PRELOAD_RAW_BYTES)
        self.assertLessEqual(info["compressed_bytes"], props.MAX_PRELOAD_COMPRESSED_BYTES)

    def test_every_runtime_shard_matches_catalog_pin(self):
        for name in self.names:
            with self.subTest(name=name):
                entry = self.catalog["props"][name]
                raw = (self.fixture_root / entry["shard"]).read_bytes()
                self.assertEqual(len(raw), entry["byte_length"])
                self.assertEqual(hashlib.sha256(raw).hexdigest(), entry["sha256"])

    def test_hot_decode_uses_ram_only_and_bounded_lru(self):
        catalog = copy.deepcopy(self.catalog)
        with mock.patch.object(Path, "open", side_effect=AssertionError("hot path disk I/O")):
            for name in self.names:
                self.assertIsNotNone(catalog.resolve(name))
            catalog.resolve(self.names[-1])
        info = catalog.cache_info()
        self.assertEqual(info["capacity"], 32)
        self.assertEqual(info["decoded"], 32)
        self.assertEqual(info["misses"], READY_COUNT)
        self.assertEqual(info["hits"], 1)
        self.assertEqual(info["evictions"], READY_COUNT - 32)

    def test_catalog_deepcopy_and_resolve_are_isolated(self):
        left, right = copy.deepcopy(self.catalog), copy.deepcopy(self.catalog)
        a = left.resolve(self.names[0])
        original = a["positions"][0]
        a["positions"][0] = 987654.0
        self.assertEqual(left.resolve(self.names[0])["positions"][0], original)
        self.assertEqual(right.cache_info()["decoded"], 0)
        self.assertEqual(right.resolve(self.names[0])["positions"][0], original)

    def test_runtime_geometry_preserves_server_float32_gate(self):
        catalog = copy.deepcopy(self.catalog)
        # Mathematical float64 triangle collapses in float32; preserve the real worker gate.
        identity_rejected = {"Float32Thin"}
        accepted = 0
        with mock.patch.object(Path, "open", side_effect=AssertionError("hot path disk I/O")):
            for name in self.names:
                with self.subTest(name=name):
                    colliders, rejected = props.build_colliders([{
                        "id": "offline-" + name, "name": name,
                        "position": [0, 0, 0], "rotation": [0, 0, 0, 1], "scale": 1,
                    }], catalog=catalog)
                    if name in identity_rejected:
                        self.assertEqual(colliders, [])
                        self.assertEqual(rejected, [{"id": "offline-" + name,
                            "reason": "prop_worker_float32_degenerate_triangle"}])
                    else:
                        self.assertEqual(rejected, [])
                        self.assertEqual(len(colliders), 1)
                        self.assertTrue(colliders[0]["standable"])
                        accepted += 1
        self.assertEqual(accepted, READY_COUNT - 1)

    def test_unknown_and_nonready_props_do_not_invent_geometry(self):
        for name in ("UnknownOfflineProp", "SyntheticMissing"):
            colliders, rejected = props.build_colliders([{
                "id": "unknown", "name": name,
                "position": [0, 0, 0], "rotation": [0, 0, 0, 1], "scale": 1,
            }], catalog=self.catalog)
            self.assertEqual(colliders, [])
            self.assertTrue(rejected)

    def test_room_revision_invalidates_applied_geometry(self):
        state = {"room_generation": 7, "revision": 1, "map_id": "CandleSpace",
                 "validated": True, "props": [{"id": "room-prop", "name": "TableXmas",
                 "position": [0, 0, 0], "rotation": [0, 0, 0, 1], "scale": 1}]}
        binding = PlaceableBinding(lambda: copy.deepcopy(state),
            lambda: (state["room_generation"], state["revision"]),
            room_generation=7, map_id="CandleSpace", catalog=self.catalog, require_focus=True)
        class Transport:
            generation = 9
            calls = []
            def request(self, op, **payload):
                self.calls.append((op, payload))
                return {"status": "ready", "colliderEpoch": payload["expectedLoadEpoch"],
                        "colliderRevision": payload["revision"], "colliderCount": len(payload["colliders"])}
        transport = Transport()
        binding.on_load(3)
        binding.set_focus((-2, 0, 0), (2, 0, 0))
        self.assertTrue(binding.sync(transport, 3))
        self.assertTrue(binding.applied_current(9))
        self.assertEqual(transport.calls[-1][0], "setColliders")
        state["revision"] += 1
        self.assertFalse(binding.applied_current(9))
        self.assertTrue(binding.sync(transport, 3))
        binding.on_load(4)
        self.assertFalse(binding.applied_current(9))

    def test_backend_keeps_client_hidden_process_wrapper(self):
        source = (APP / "src/skyterrain/backend.py").read_text(encoding="utf-8")
        self.assertIn("proc=xg_exec.popen(args,", source)
        self.assertIn("MAX_COLLIDER_FRAME=524288", source)
        self.assertIn("'setColliders','addColliders','removeColliders'", source)


class DynamicWorkerSyncTests(unittest.TestCase):
    def run_node(self, *args, input_text=None):
        return subprocess.run([str(ROOT / "runtime/node/node.exe"), *map(str, args)],
            input=input_text, capture_output=True, text=True, encoding="utf-8", timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def test_real_worker_ping_and_unloaded_collider_rejection(self):
        data = '\n'.join(json.dumps(q) for q in [
            {"id": "ping-1", "op": "ping"},
            {"id": "props-1", "op": "setColliders", "colliders": [], "revision": 1, "expectedLoadEpoch": 0},
        ]) + '\n'
        run = self.run_node(APP / "engine/staged_worker.mjs", "--assets", xg_paths.ASSETS, "--offline", input_text=data)
        self.assertEqual(run.returncode, 0, run.stderr)
        replies = [json.loads(line) for line in run.stdout.splitlines()]
        self.assertEqual(replies[0]["status"], "ready")
        self.assertEqual(replies[0]["dynamicCollidersProtocol"], 1)
        self.assertIn("asset_not_loaded", replies[1]["reason"])

    def test_client_cli_missing_assets_diagnostic(self):
        run = self.run_node(APP / "engine/staged_worker.mjs")
        self.assertEqual(run.returncode, 3)
        self.assertIn("缺资产", run.stderr)
        self.assertIn("usage:", run.stderr)

    def test_node_collider_transactions_and_bounds(self):
        url = (APP / "engine/dynamic_colliders.mjs").as_uri()
        script = r'''
import assert from 'node:assert/strict';
import {ColliderSet} from '__MODULE__';
const triangle = {id:'one',name:'fixture',positions:[0,0,0, 0,0,1, 1,0,0],triangles:[0,1,2],source_kind:'render_mesh_candidate',standable:true};
const original=new ColliderSet();
const p=original.propose('setColliders',[triangle],undefined,1);
assert.equal(original.size,0); assert.equal(p.changed,true); assert.equal(p.set.size,1);
assert.equal(p.set.propose('setColliders',[triangle],undefined,1).changed,false);
assert.throws(()=>p.set.propose('setColliders',[{...triangle,positions:[0,0,0,0,0,1,2,0,0]}],undefined,1),/revision_reuse/);
assert.throws(()=>p.set.propose('setColliders',[triangle,triangle],undefined,2),/duplicate_id/);
assert.throws(()=>p.set.propose('setColliders',[{...triangle,positions:[0,0,0,0,0,0,0,0,0]}],undefined,2),/degenerate_triangle/);
assert.throws(()=>p.set.propose('setColliders',[{...triangle,triangles:[0,1,3]}],undefined,2),/index/);
assert.equal(p.set.size,1);
const merged=p.set.merge({positions:new Float32Array([5,0,0,5,0,1,6,0,0]),indices:new Uint32Array([0,1,2])});
assert.equal(merged.positions.length,18);assert.deepEqual([...merged.indices],[0,1,2,3,4,5]);
assert.equal(p.set.propose('removeColliders',undefined,['one'],2).set.size,0);
console.log('dynamic-collider-transaction-checks: 12 passed');
'''.replace('__MODULE__', url)
        run = self.run_node("--input-type=module", "-e", script)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("12 passed", run.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
