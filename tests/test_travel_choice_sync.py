"""Travel-choice offline regression. No AI bootstrap, credentials, network or game.

Fixtures are unchanged destination JSON supplied with inputs/6-travel-choice/orig.
Model, sender, clock and the submission port are explicit fixtures. The parser,
registry resolver, Router, pending selection, AI reply function and tool function
are the real packaged source (AI methods are extracted by AST, never imported).
"""
from __future__ import annotations
import ast
import copy
from dataclasses import replace
import importlib.util
import json
from pathlib import Path
import re
import socket
import sys
import threading
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
BOT = ROOT / "app/bot"
FIXTURES = Path(__file__).with_name("fixtures_travel6")
for path in (ROOT / "app", BOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


def load_file(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


travel = load_file("travel_choice_sync_under_test", BOT / "travel_chat_v25.py")
import destination_resolver_v25 as destinations

SPEAKER = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
SESSION = "33333333-3333-4333-8333-333333333333"
SESSION2 = "44444444-4444-4444-8444-444444444444"


class FixturePort:
    def __init__(self):
        self.available = True
        self.calls = []
        self.reason = None
        self.before_submit = lambda: None
        self.accepted_requests = set()

    def submit(self, origin, intent, destination, *, still_current):
        self.before_submit()
        if not still_current():
            return travel.SubmitResult(None, False, "fixture_superseded")
        self.calls.append((origin, intent, destination))
        if self.reason:
            return travel.SubmitResult(None, False, self.reason)
        return travel.SubmitResult("a" * 32, True, "queued_not_arrived")

    def await_acceptance(self, origin, receipt, *, still_current):
        # Patch 7: a fake write is not a fake receiver acceptance. Explicitly
        # acknowledge this exact fixture request; retain all original assertions.
        if not still_current():
            return travel.AcceptanceResult(receipt.request_id, False, "superseded")
        self.accepted_requests.add((origin, receipt.request_id))
        return travel.AcceptanceResult(receipt.request_id, True, "runtime_accepted")

    def acceptance_current(self, origin, receipt):
        return (origin, receipt.request_id) in self.accepted_requests


class TravelCase(unittest.TestCase):
    def setUp(self):
        self.guard = mock.patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden"))
        self.guard.start(); self.addCleanup(self.guard.stop)
        self.guard2 = mock.patch.object(socket, "create_connection", side_effect=AssertionError("network forbidden"))
        self.guard2.start(); self.addCleanup(self.guard2.stop)
        self.registry = destinations.DestinationResolver.from_file(FIXTURES / "destination-records-v25.json")
        patch = mock.patch.object(destinations, "load_default", return_value=self.registry)
        patch.start(); self.addCleanup(patch.stop)
        self.runtime_stub = types.SimpleNamespace(startable_maps=lambda: [
            {"scene": r["sceneName"]} for r in self.registry._records.values() if r.get("sceneName")])
        patch = mock.patch.dict(sys.modules, {"xg_runtime": self.runtime_stub})
        patch.start(); self.addCleanup(patch.stop)
        self.reset_context()

    def reset_context(self):
        self.now = 1000.0
        self.port = FixturePort()
        self.resolver = travel.RegistryResolver(FIXTURES / "escort_travel.destinations.json")
        self.router = travel.Router(resolver=self.resolver, port=self.port, clock=lambda: self.now)
        self.bot = types.SimpleNamespace(lock=threading.Lock(), escort_travel_router=self.router,
            _travel_chat_session=SESSION, _travel_ingress_revision=0)

    def row(self, raw, *, sender=SPEAKER, channel="local", session=None, age=0):
        self.bot._travel_ingress_revision += 1
        origin = travel.Origin(sender, raw, str(self.bot._travel_ingress_revision), channel,
            session or self.bot._travel_chat_session, self.bot._travel_ingress_revision, self.now - age)
        return {"text": raw, "line_id": origin.line_id, "ch": channel, "travel_origin": origin}

    def route(self, raw, **kw):
        row = self.row(raw, **kw)
        return travel.route_pending(self.bot, kw.get("sender", SPEAKER), kw.get("channel", "local"), [row])

    def listed(self, raw="/带我去万圣节的图"):
        decision = self.route(raw)
        self.assertEqual(decision.reason, "destination_choices")
        return self.router.choice_proposals[(SPEAKER, "local")]["candidates"]

    def payload(self, candidates):
        return {"ok": True, "maps": [{"名称": c["name"], "其它叫法": list(c["aliases"])} for c in candidates]}


class TravelChoiceRoutingTests(TravelCase):
    def test_direct_router_list_preexists_delivery_upstream_behavior(self):
        self.listed()
        # Direct deterministic Router replies are recorded before the caller
        # sends the text. Unlike model list_maps registration, no receipt is
        # involved here. Preserve and disclose this upstream boundary.
        self.assertIn((SPEAKER, "local"), self.router.choice_proposals)
        self.assertFalse(self.port.calls)

    def test_all_eight_ordinals_and_last_preserve_display_order(self):
        for text, index in [(f"/第{i}个", i - 1) for i in range(1, 9)] + [("/最后那个", 7)]:
            with self.subTest(text=text):
                self.reset_context()
                candidates = travel._find_choices(self.router, "scene")
                origin = self.row("/有哪些地图")["travel_origin"]
                travel._remember_choices(self.router, origin, candidates)
                self.assertEqual(self.route(text).reason, "submitted_not_completed")
                self.assertEqual(self.port.calls[-1][2], candidates[index]["ref"])

    def test_series_requests_and_order(self):
        for query in ("/带我去万圣节地图", "/带我去万圣节的图", "/背我去万圣节系列"):
            with self.subTest(query=query):
                self.reset_context()
                candidates = self.listed(query)
                self.assertEqual([c["name"] for c in candidates], ["螃蟹的恶搞派对", "恶作剧密室"])
                self.assertFalse(self.port.calls)

    def test_eight_selection_phrasings(self):
        for text, scene in [
            ("/恶作剧密室吧", "Event_DaysOfMischief_Escape"),
            ("/就密室那个", "Event_DaysOfMischief_Escape"),
            ("/第二个", "Event_DaysOfMischief_Escape"),
            ("/第2个", "Event_DaysOfMischief_Escape"),
            ("/最后那个", "Event_DaysOfMischief_Escape"),
            ("/螃蟹的恶搞派对吧", "Event_DaysOfMischief"),
            ("/螃蟹派对", "Event_DaysOfMischief"),
            ("/螃蟹那个吧", "Event_DaysOfMischief"),
        ]:
            with self.subTest(text=text):
                self.reset_context(); self.listed()
                result = self.route(text)
                self.assertEqual(result.reason, "submitted_not_completed")
                self.assertEqual(self.port.calls[-1][2].scene, scene)
                self.assertEqual(len(self.port.calls), 1)
                self.assertNotIn((SPEAKER, "local"), self.router.choice_proposals)

    def test_carry_kind_inheritance(self):
        for verb, kind in [("带我", "handhold"), ("背我", "piggyback"), ("抱我", "princess_carry")]:
            with self.subTest(kind=kind):
                self.reset_context(); self.listed("/" + verb + "去万圣节地图")
                self.assertEqual(self.route("/第二个").reason, "submitted_not_completed")
                self.assertEqual(self.port.calls[-1][1].carry_kind, kind)

    def test_find_choices_maximum_eight_preserves_catalogue_order(self):
        catalogue = travel._choice_catalogue(self.router)
        # All fixture IDs start with scene:/landmark:, so 'scene' matches many.
        candidates = travel._find_choices(self.router, "scene")
        expected = [c for c in catalogue if any("scene" in travel._loose(a) for a in c["aliases"])][:8]
        self.assertEqual(len(candidates), 8)
        self.assertEqual(candidates, expected)
        listing = travel._choice_listing(candidates)
        self.assertTrue(all(f"{i}、{c['name']}" in listing for i, c in enumerate(candidates, 1)))

    def test_only_executable_registry_rows_are_choices(self):
        allowed = self.resolver("恶作剧密室")
        self.router.resolver = lambda q: allowed if self.resolver(q) == allowed else None
        choices = travel._find_choices(self.router, "万圣节")
        self.assertEqual([c["ref"] for c in choices], [allowed])
        self.assertEqual(self.route("/带我去万圣节地图").reason, "suggested_correction")
        self.assertIsNotNone(self.router.proposal)
        self.assertFalse(self.port.calls)

    def test_120_second_boundary(self):
        for elapsed, expected in [(119.999, 1), (120.0, 0), (121.0, 0)]:
            with self.subTest(elapsed=elapsed):
                self.reset_context(); self.listed(); self.now += elapsed
                self.route("/第二个")
                self.assertEqual(len(self.port.calls), expected)

    def test_different_speaker_and_channel_do_not_select(self):
        for sender, channel in [(OTHER, "local"), (SPEAKER, "team")]:
            with self.subTest(sender=sender, channel=channel):
                self.reset_context(); self.listed()
                self.route("/第二个", sender=sender, channel=channel)
                self.assertFalse(self.port.calls)
                self.assertIn((SPEAKER, "local"), self.router.choice_proposals)

    def test_new_session_discards_old_proposal(self):
        self.listed(); self.bot._travel_chat_session = SESSION2
        self.route("/第二个")
        self.assertFalse(self.port.calls)
        self.assertNotIn((SPEAKER, "local"), self.router.choice_proposals)

    def test_missing_ingress_does_not_select(self):
        self.listed()
        travel.route_pending(self.bot, SPEAKER, "local", [{"text": "/第二个"}])
        self.assertFalse(self.port.calls)

    def test_stale_and_superseded_ingress_rejected(self):
        for kind in ("stale", "superseded", "speaker", "channel", "line", "session"):
            with self.subTest(kind=kind):
                self.reset_context(); self.listed(); row = self.row("/第二个")
                o = row["travel_origin"]
                if kind == "stale": o = replace(o, observed_mono=self.now - 31)
                if kind == "superseded": self.bot._travel_ingress_revision += 1
                if kind == "speaker": o = replace(o, speaker_uuid=OTHER)
                if kind == "channel": o = replace(o, channel="team")
                if kind == "line": o = replace(o, line_id="")
                if kind == "session": o = replace(o, chat_session="invalid")
                row["travel_origin"] = o
                d = travel.route_pending(self.bot, SPEAKER, "local", [row])
                self.assertEqual(d.reason, "unbound_or_expired_choice_origin")
                self.assertFalse(self.port.calls)

    def test_replay_does_not_submit_twice(self):
        self.listed(); row = self.row("/第二个")
        travel.route_pending(self.bot, SPEAKER, "local", [row])
        travel.route_pending(self.bot, SPEAKER, "local", [row])
        self.assertEqual(len(self.port.calls), 1)
        origin, intent, _ = self.port.calls[0]
        d = self.router.route(origin, intent, still_current=lambda: True)
        self.assertEqual(d.reason, "superseded_or_consumed")

    def test_disabled_port(self):
        self.listed(); self.port.available = False
        self.assertEqual(self.route("/第二个").reason, "execution_disabled")
        self.assertFalse(self.port.calls)

    def test_unslashed_text_has_no_ingress_and_does_not_select(self):
        self.listed()
        self.assertIsNone(travel.capture_ingress(self.bot, SPEAKER, "第二个", "local", "no-slash"))
        travel.route_pending(self.bot, SPEAKER, "local", [{"text": "第二个"}])
        self.assertFalse(self.port.calls)
        self.assertIn((SPEAKER, "local"), self.router.choice_proposals)

    def test_submit_exception_and_invalid_receipt(self):
        for mode in ("exception", "dict", "bad-id"):
            with self.subTest(mode=mode):
                self.reset_context(); self.listed()
                if mode == "exception":
                    self.port.submit = mock.Mock(side_effect=RuntimeError("fixture failure"))
                elif mode == "dict":
                    self.port.submit = lambda *a, **kw: {"submitted": True}
                else:
                    self.port.submit = lambda *a, **kw: travel.SubmitResult("bad", True, "invalid_fixture_id")
                d = self.route("/第二个")
                self.assertNotEqual(d.reason, "submitted_not_completed")
                self.assertIsNone(self.router.last_submitted)

    def test_port_rejection_not_reported_as_success(self):
        self.listed(); self.port.reason = "fixture_denied"
        d = self.route("/第二个")
        self.assertEqual(d.reason, "fixture_denied")
        self.assertIsNone(self.router.last_submitted)

    def test_port_submit_rechecks_current_ingress(self):
        self.listed()
        self.port.before_submit = lambda: setattr(self.bot, "_travel_ingress_revision", 999)
        self.assertEqual(self.route("/第二个").reason, "fixture_superseded")
        self.assertFalse(self.port.calls)

    def test_destination_is_revalidated_at_selection(self):
        self.listed(); self.router.resolver = lambda q: None
        self.assertEqual(self.route("/第二个").reason, "choice_destination_changed")
        self.assertFalse(self.port.calls)

    def test_declines_clear_proposal(self):
        for text in ("/不去了", "/算了", "/都不要", "/取消", "/哪也不去"):
            with self.subTest(text=text):
                self.reset_context(); self.listed()
                self.assertEqual(self.route(text).reason, "choice_declined")
                self.assertNotIn((SPEAKER, "local"), self.router.choice_proposals)
                self.assertFalse(self.port.calls)

    def test_unrelated_questions_and_negative_mentions_preserve_proposal(self):
        for text in ("/今天天气不错", "/密室好玩吗", "/不要去恶作剧密室", "/他说第二个", "/恶作剧密室在哪里"):
            with self.subTest(text=text):
                self.reset_context(); self.listed(); self.route(text)
                self.assertFalse(self.port.calls)
                self.assertIn((SPEAKER, "local"), self.router.choice_proposals)

    def test_new_explicit_command_replaces_choice(self):
        self.listed(); d = self.route("/背我回遇境")
        self.assertEqual(d.reason, "submitted_not_completed")
        self.assertEqual(self.port.calls[-1][2].scene, "CandleSpace")
        self.assertEqual(self.port.calls[-1][1].carry_kind, "piggyback")
        self.assertNotIn((SPEAKER, "local"), self.router.choice_proposals)

    def test_ambiguity_is_asked_only_once(self):
        self.listed()
        first = self.route("/第一个和第二个")
        second = self.route("/第一个和第二个")
        self.assertEqual(first.reason, "choice_ambiguous")
        self.assertTrue(first.reply)
        self.assertEqual(second.reason, "choice_ambiguous")
        self.assertIsNone(second.reply)
        self.assertFalse(self.port.calls)

    def test_merged_new_command_or_decline_does_not_revive_old_choice(self):
        for prior in ("/带我回遇境", "/不去了"):
            with self.subTest(prior=prior):
                self.reset_context(); self.listed()
                rows = [self.row(prior), self.row("/第二个")]
                travel.route_pending(self.bot, SPEAKER, "local", rows)
                self.assertFalse(self.port.calls)
                self.assertNotIn((SPEAKER, "local"), self.router.choice_proposals)

    def test_capture_ingress_strips_trailing_newline(self):
        with mock.patch.object(travel, "time", types.SimpleNamespace(monotonic=lambda: self.now)):
            origin = travel.capture_ingress(self.bot, SPEAKER, " /第二个\r\n", "local", "line-1")
        self.assertEqual(origin.raw_message, "/第二个")
        self.assertEqual(origin.ingress_revision, self.bot._travel_ingress_revision)

    def test_existing_explicit_travel_and_cancel_replies(self):
        for command, reply in [("/带我回遇境", "好呀，牵好就回遇境。"),
            ("/背我去恶作剧密室", "好呀，背好再出发。"),
            ("/抱我去恶作剧密室", "好呀，抱好再出发。"), ("/取消带路", "好，先停下。")]:
            with self.subTest(command=command):
                self.reset_context(); self.assertEqual(self.route(command).reply, reply)


class MapReplyRegistrationTests(TravelCase):
    def client_fallback_router(self):
        scope = {"ROOT": ROOT}
        exec("def control(self, command=None):\n    return None\n", scope)
        client = types.SimpleNamespace()
        client.control = types.MethodType(scope["control"], client)
        router = travel.default_router(client)
        self.assertEqual(router.resolver.path, ROOT / "app/config/spawn_points.json")
        router.port = self.port
        router.clock = lambda: self.now
        self.router = router
        self.bot.escort_travel_router = router
        return router

    def test_actual_client_spawn_fallback_and_directory_payload(self):
        self.client_fallback_router()
        source = BOT / "xg_runtime.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        names = {"directory_rows", "list_maps_payload", "_json", "_records_path", "region_of"}
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names
                 or isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "REGIONS" for t in n.targets)]
        env = {"Path": Path, "re": re, "xg_paths": travel.xg_paths,
               "TARGET_RECORDS": FIXTURES / "destination-records-v25.json"}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), env)
        records = {r.get("sceneName"): r for r in self.registry._records.values() if r.get("kind") == "scene"}
        actual_spawn = json.loads(self.router.resolver.path.read_text(encoding="utf-8"))
        selected = [r for r in actual_spawn["destinations"] if r["scene"].startswith("Event_DaysOfMischief")]
        self.assertEqual(len(selected), 2)
        # Availability is a fixture, not an account/profile read. Identity and
        # aliases come from actual shipped spawn points + supplied real names.
        maps = [{"id": r["map_id"], "scene": r["scene"], "name": records[r["scene"]]["displayName"],
                 "region": env["region_of"](r["scene"]), "kind": "fixture_available"} for r in selected]
        rows = env["directory_rows"](ROOT, maps)
        payload = env["list_maps_payload"](rows, "Event_DaysOfMischief")
        self.assertEqual(payload["count"], 2)
        origin = self.row("/有哪些万圣节地图")
        reply = "；".join(r["名称"] for r in payload["maps"])
        self.assertTrue(travel.record_list_maps_choices(self.bot, SPEAKER, "local", [origin], reply, [payload]))
        self.assertEqual(self.route("/第二个").reason, "submitted_not_completed")
        self.assertEqual(self.port.calls[-1][2].scene, selected[1]["scene"])
        self.assertEqual(self.port.calls[-1][2].map_id, selected[1]["map_id"])

    def test_missing_generated_names_has_no_guessed_choices(self):
        self.client_fallback_router()
        with mock.patch.object(destinations, "load_default", side_effect=FileNotFoundError("names not generated")):
            self.assertEqual(self.route("/带我去万圣节地图").reason, "unknown_destination")
            self.assertFalse(self.router.choice_proposals)
            self.assertFalse(self.port.calls)
            # Explicit identities from shipped spawn points still use the old path.
            self.assertEqual(self.route("/带我去恶作剧密室").reason, "submitted_not_completed")

    def test_sent_order_is_selection_order(self):
        candidates = travel._find_choices(self.router, "万圣节")
        row = self.row("/有哪些万圣节地图")
        self.assertTrue(travel.record_list_maps_choices(self.bot, SPEAKER, "local", [row],
            "恶作剧密室；螃蟹的恶搞派对", [self.payload(candidates)]))
        self.assertEqual(self.route("/第一个").reason, "submitted_not_completed")
        self.assertEqual(self.port.calls[-1][2].scene, "Event_DaysOfMischief_Escape")

    def test_unmentioned_and_nonpayload_names_are_not_registered(self):
        candidates = travel._find_choices(self.router, "万圣节")
        row = self.row("/有哪些地图")
        self.assertTrue(travel.record_list_maps_choices(self.bot, SPEAKER, "local", [row],
            "恶作剧密室、遇境", [self.payload(candidates)]))
        stored = self.router.choice_proposals[(SPEAKER, "local")]["candidates"]
        self.assertEqual([c["name"] for c in stored], ["恶作剧密室"])

    def test_bad_payload_and_old_turn_do_not_register(self):
        candidates = travel._find_choices(self.router, "万圣节")
        for mode in ("failed", "empty", "old", "no_ingress"):
            with self.subTest(mode=mode):
                self.reset_context(); row = self.row("/有哪些地图")
                payloads = [self.payload(candidates)]
                if mode == "failed": payloads[0]["ok"] = False
                if mode == "empty": payloads = []
                if mode == "old": self.bot._travel_ingress_revision += 1
                if mode == "no_ingress": row.pop("travel_origin")
                self.assertFalse(travel.record_list_maps_choices(self.bot, SPEAKER, "local", [row], "恶作剧密室", payloads))
                self.assertFalse(self.router.choice_proposals)

    def test_over_eight_visible_choices_clear_old_list_instead_of_truncating(self):
        self.listed()
        candidates = []
        for c in travel._choice_catalogue(self.router):
            if all(c["name"] not in p["name"] and p["name"] not in c["name"] for p in candidates):
                candidates.append(c)
            if len(candidates) == 9: break
        self.assertEqual(len(candidates), 9)
        row = self.row("/有哪些地图")
        self.assertFalse(travel.record_list_maps_choices(self.bot, SPEAKER, "local", [row],
            "；".join(c["name"] for c in candidates), [self.payload(candidates)]))
        self.assertNotIn((SPEAKER, "local"), self.router.choice_proposals)

    def test_list_registration_inherits_existing_carry_mode(self):
        candidates = self.listed("/抱我去万圣节地图")
        row = self.row("/再列一下")
        self.assertTrue(travel.record_list_maps_choices(self.bot, SPEAKER, "local", [row],
            "恶作剧密室", [self.payload(candidates)]))
        self.route("/第一个")
        self.assertEqual(self.port.calls[-1][1].carry_kind, "princess_carry")


class AITravelReplyIntegrationTests(TravelCase):
    def respond(self, *, receipts=None, parts=None, model="螃蟹的恶搞派对；恶作剧密室", use_tool=True, web=False, supersede=False):
        harness = load_file("travel_ai_subset_harness", Path(__file__).with_name("test_ai_sync.py"))
        env = harness.load_subset({"pose": "stand"})
        env["travel_chat_v25"] = travel
        env["OWNER"] = SPEAKER
        env["TOOL_ROUNDS"] = 2
        cls = next(n for n in harness.TREE.body if isinstance(n, ast.ClassDef) and n.name == "Companion")
        fn = copy.deepcopy(next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "tool_list_maps"))
        exec(compile(ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])), str(harness.SOURCE), "exec"), env)
        setattr(env["Companion"], "tool_list_maps", env["tool_list_maps"])
        bot = env["Companion"]()
        original = self.bot
        bot.lock = threading.Lock()
        bot.escort_travel_router = self.router
        bot._travel_chat_session = original._travel_chat_session
        bot._travel_ingress_revision = original._travel_ingress_revision
        self.bot = bot
        row = self.row("/有哪些万圣节地图")
        bot.pending = {(SPEAKER, "local"): [row]}
        bot.last_reply, bot.global_last = {}, 0
        bot.name_of = lambda _: "fixture-speaker"
        bot.web = types.SimpleNamespace(begin_turn=lambda _: {"allowed": web})
        bot.names, bot.map_id, bot.account = {}, 1, {"user_uuid": "fixture"}
        bot._speaker_memory = lambda _: ""
        bot._chat_context_history = lambda *a, **kw: []
        bot.reply_local = types.SimpleNamespace()
        candidates = travel._find_choices(self.router, "万圣节")
        payload = self.payload(candidates)
        self.runtime_stub.directory_rows = lambda *a: payload["maps"]
        self.runtime_stub.list_maps_payload = lambda *a: copy.deepcopy(payload)
        bot.run_tool = lambda name, args, turn: bot.tool_list_maps(turn, "万圣节")
        model_calls = []
        def chat(messages, **kwargs):
            model_calls.append(messages)
            if use_tool and len(model_calls) == 1:
                return {"tool_calls": [{"id": "tool-1", "function": {"name": "list_maps", "arguments": "{}"}}]}
            if supersede: bot._travel_ingress_revision += 1
            return {"content": model}
        bot.llm = types.SimpleNamespace(chat=chat)
        bot.format_reply = lambda raw: list(parts) if parts is not None else [raw]
        sent = []
        def send(part, *args, **kwargs):
            index = len(sent); sent.append(part)
            return receipts[index] if receipts is not None else {"status": "submitted", "text": part}
        bot.send = send
        bot.emote = lambda *args: None
        bot._respond_locked(SPEAKER, "local")
        return bot, sent, model_calls

    def test_real_reply_and_tool_register_successful_visible_text(self):
        bot, sent, calls = self.respond()
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(self.router.choice_proposals[(SPEAKER, "local")]["candidates"]), 2)
        self.assertEqual(self.route("/第二个").reason, "submitted_not_completed")

    def test_send_failure_or_missing_actual_text_does_not_register(self):
        for receipt in ({"status": "failed", "text": "恶作剧密室"}, {"status": "submitted"}, None):
            with self.subTest(receipt=receipt):
                self.reset_context(); self.respond(receipts=[receipt])
                self.assertFalse(self.router.choice_proposals)

    def test_partial_delivery_records_only_submitted_parts(self):
        self.respond(parts=["螃蟹的恶搞派对", "恶作剧密室"], receipts=[
            {"status": "failed", "text": "螃蟹的恶搞派对"}, {"status": "submitted", "text": "恶作剧密室"}])
        self.assertEqual([c["name"] for c in self.router.choice_proposals[(SPEAKER, "local")]["candidates"]], ["恶作剧密室"])

    def test_review_rewrite_uses_final_text_not_model_draft(self):
        self.respond(receipts=[{"status": "submitted", "text": "恶作剧密室；螃蟹的恶搞派对"}])
        self.route("/第一个")
        self.assertEqual(self.port.calls[-1][2].scene, "Event_DaysOfMischief_Escape")

    def test_review_removes_names_no_registration(self):
        self.respond(receipts=[{"status": "submitted", "text": "这次先聊点别的吧"}])
        self.assertFalse(self.router.choice_proposals)

    def test_no_tool_web_and_old_turn_do_not_register(self):
        for kwargs in ({"use_tool": False}, {"web": True}, {"supersede": True}):
            with self.subTest(kwargs=kwargs):
                self.reset_context(); self.respond(**kwargs)
                self.assertFalse(self.router.choice_proposals)

    def test_selection_returns_before_model_and_no_reprompt(self):
        self.respond()
        row = self.row("/恶作剧密室吧")
        self.bot.pending = {(SPEAKER, "local"): [row]}
        self.bot.llm.chat = lambda *a, **kw: self.fail("selection must not call model")
        sent = []
        self.bot.send = lambda text, *a, **kw: sent.append(text)
        self.bot._respond_locked(SPEAKER, "local")
        self.assertEqual(len(self.port.calls), 1)
        self.assertEqual(sent, ["好呀，牵好再出发。"])

    def test_runtime_payload_guidance_is_updated(self):
        source = BOT / "xg_runtime.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "list_maps_payload")
        env = {}
        exec(compile(ast.Module(body=[fn], type_ignores=[]), str(source), "exec"), env)
        payload = env["list_maps_payload"]([{"名称": "恶作剧密室", "其它叫法": [], "区域": "万圣节", "内部名": "Event_DaysOfMischief_Escape"}], "万圣节")
        self.assertIn("候选最多列8个全名", payload["note"])
        self.assertIn("不要要求重发指令", payload["note"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
