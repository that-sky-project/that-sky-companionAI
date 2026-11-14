"""Real chat/submit/receiver functions; fixture files only, no accounts/network."""
from __future__ import annotations

import ast
import copy
import importlib.util
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
BOT = ROOT / 'app/bot'
if str(BOT) not in sys.path:
    sys.path.insert(0, str(BOT))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


with mock.patch.dict(sys.modules, {'xg_paths': types.ModuleType('xg_paths')}):
    travel = load('escort_acceptance7_travel', BOT / 'travel_chat_v25.py')
runtime_module = load('escort_acceptance7_runtime', BOT / 'leader_travel_runtime.py')
SPEAKER = '11111111-1111-4111-8111-111111111111'
OWN = '22222222-2222-4222-8222-222222222222'
SESSION = '33333333-3333-4333-8333-333333333333'
OTHER_SESSION = '44444444-4444-4444-8444-444444444444'
SCENE = 'Dawn'
MAP_ID = runtime_module.fnv32(SCENE)


class Clock:
    wall = 1000000.0
    mono = 1000.0

    def sleep(self, delay):
        self.wall += delay
        self.mono += delay


def json_write(path, doc):
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(doc, ensure_ascii=False), encoding='utf-8')
    tmp.replace(path)


class EscortAcceptanceTests(unittest.TestCase):
    def setUp(self):
        for target in ('socket.socket.connect', 'socket.create_connection'):
            guard = mock.patch(target, side_effect=AssertionError('network forbidden'))
            guard.start(); self.addCleanup(guard.stop)
        self.tmp = tempfile.TemporaryDirectory(prefix='escort7-fixture-', dir=ROOT / 'tests')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.control = self.root / 'control'; self.control.mkdir()
        self.clock = Clock()
        self.logs, self.effects, self.sent, self.events = [], [], [], []
        self.captured = True
        tracker = types.SimpleNamespace(own_uuid=OWN, lock=threading.RLock(),
            slots={'1': OWN, '2': SPEAKER}, _slot_versions={'2': 1},
            _attachment_events=[], room_generation=7,
            _own_social_binding_locked=lambda: '1')
        self.ctl = types.SimpleNamespace(path=self.control / 'pose.json', tracker=tracker,
            position=(0, 0, 0), map_id=1, base={}, follow=None, after_arrive=None,
            route=[], motion=None, social={}, _active_offer=None,
            _set_override=lambda value: self.effects.append(('override', value)),
            log=lambda event, **kw: self.logs.append((event, kw)),
            _apply=lambda cmd: self.effects.append(('other', cmd)),
            _follow_tick=lambda: None, _active_end=lambda *a, **kw: None)
        self.runtime = runtime_module.LeaderTravelRuntime(self.ctl,
            session_deadline=self.clock.wall + 180,
            has_capture=lambda *_: self.captured,
            rpc_port=types.SimpleNamespace(cancel_pending=lambda: None),
            clock=lambda: self.clock.mono, wall=lambda: self.clock.wall)
        self.destination = {'id': 'dawn', 'sceneName': SCENE, 'sceneHash': MAP_ID}
        resolver_module = types.ModuleType('destination_resolver_v25')
        resolver_module.load_default = lambda: types.SimpleNamespace(resolve=lambda _: {
            'status': 'resolved', 'destination': self.destination})
        guard = mock.patch.dict(sys.modules, {'travel_chat_v25': travel,
                                             'destination_resolver_v25': resolver_module})
        guard.start(); self.addCleanup(guard.stop)
        self.on_sleep = lambda: None

        def sleep(delay):
            # Publication lock is never held while receiver acceptance is awaited.
            self.assertTrue(self.port.lock.acquire(blocking=False))
            self.port.lock.release()
            self.clock.sleep(delay)
            self.on_sleep()
        self.port = travel.PoseControlPort(self.root, clock=lambda: self.clock.wall,
            monotonic=lambda: self.clock.mono, sleep=sleep)
        json_write(self.port.flag, {'version': 1, 'enabled': True,
                                   'session_deadline': self.clock.wall + 180})
        json_write(self.port.state_path, {'generated_at': self.clock.wall, 'room': {
            'connected': True, 'map_id': 1, 'generated_at': self.clock.wall,
            'slots': {'2': SPEAKER}, 'players': {'2': {'uuid': SPEAKER,
            'position': {'freshness': 'fresh', 'age_seconds': 0, 'value': [0, 0, 0]}}}}})
        self.router = travel.Router(resolver=lambda _: travel.DestinationRef('dawn', SCENE, MAP_ID),
                                    port=self.port, clock=lambda: self.clock.mono)
        source = (BOT / 'ai_companion.py').read_text(encoding='utf-8')
        cls = next(n for n in ast.parse(source).body if isinstance(n, ast.ClassDef) and n.name == 'Companion')
        node = copy.deepcopy(cls)
        node.body = [copy.deepcopy(n) for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in ('_respond_locked', 'send')]
        env = {'travel_chat_v25': travel,
               'time': types.SimpleNamespace(monotonic=lambda: self.clock.mono, sleep=self.clock.sleep),
               'FORBIDDEN_WORDS': {}, '_pose_sitting': lambda _: True, '_pose_state': lambda: {},
               'personas': types.SimpleNamespace(active=lambda: {'display_name': 'fixture'}),
               'log': lambda *a, **kw: None, 'SkyApiError': type('FixtureApiError', (Exception,), {})}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), 'ai_companion.py', 'exec'), env)
        self.bot = env['Companion']()
        self.bot.lock = threading.RLock()
        self.bot.last_reply, self.bot.global_last = {}, 0
        self.bot._travel_chat_session, self.bot._travel_ingress_revision = SESSION, 1
        self.bot.escort_travel_router = self.router
        self.bot.send = lambda text, *a, **kw: self.sent.append(text)

    def origin(self, raw='/带我去晨岛', revision=1):
        return travel.Origin(SPEAKER, raw, 'line-' + str(revision), 'local', SESSION,
                             revision, self.clock.mono)

    def receive(self):
        self.on_sleep = lambda: None
        self.assertEqual(self.sent, [], 'no departure phrase before receiver decision')
        result = self.runtime.accept(json.loads(self.port.path.read_text(encoding='utf-8')))
        self.events.append(('receiver', result))

    def respond(self, raw='/带我去晨岛'):
        origin = self.origin(raw, self.bot._travel_ingress_revision)
        self.bot.pending = {(SPEAKER, 'local'): [{'text': raw, 'travel_origin': origin,
                                               'line_id': origin.line_id, 'ch': 'local'}]}
        self.bot._respond_locked(SPEAKER, 'local')

    def submit(self):
        origin = self.origin()
        receipt = self.port.submit(origin, travel.parse(origin.raw_message),
            travel.DestinationRef('dawn', SCENE, MAP_ID), still_current=lambda: True)
        self.assertTrue(receipt.submitted)
        return origin, receipt

    def accepted(self):
        origin, receipt = self.submit()
        self.assertTrue(self.runtime.accept(json.loads(self.port.path.read_text(encoding='utf-8'))))
        return origin, receipt

    def feedback(self):
        return json.loads(self.port.feedback_path.read_text(encoding='utf-8'))

    def actual_send(self):
        self.bot.send = types.MethodType(type(self.bot).send, self.bot)
        self.bot.send_lock = threading.Lock()
        self.bot.dry_run = False
        self.bot.pass_review = lambda text: text
        self.bot.reply_local = types.SimpleNamespace(context={})
        self.bot.map_id = 1
        self.bot.account = {'user_uuid': OWN}  # synthetic identifier, not account configuration
        self.bot.name_of = lambda _: 'fixture'
        self.bot.memory = types.SimpleNamespace(add_line=lambda *a, **kw: None)
        def post(url, payload, **kwargs):
            self.assertEqual(url, '/account/chat/send')
            self.sent.append(payload['msg'])
            return {'result': 'ok'}
        self.bot.client = types.SimpleNamespace(post=post)

    def test_actual_receiver_accepts_before_chat_departure_reply(self):
        self.on_sleep = self.receive
        self.respond()
        self.assertEqual(self.events, [('receiver', True)])
        self.assertEqual(self.sent, ['好呀，牵好再出发。'])
        self.assertEqual(self.feedback()['results'][-1]['status'], 'accepted')
        self.assertEqual(self.feedback()['results'][-1]['reason'], 'runtime_accepted')

    def test_actual_missing_account_capture_rejects_with_resource_guidance(self):
        self.captured = False
        self.on_sleep = self.receive
        self.respond()
        self.assertEqual(self.events, [('receiver', False)])
        self.assertEqual(self.sent, ['本账号缺少目的地图目录，请到资源页重新构建。'])
        self.assertIsNone(self.runtime.current)
        self.assertEqual(self.feedback()['results'][-1]['reason'], 'destination_capture_unavailable')

    def test_submission_alone_times_out_without_success_or_resubmission(self):
        with mock.patch.object(self.port, 'submit', wraps=self.port.submit) as submit:
            self.respond()
        self.assertEqual(submit.call_count, 1)
        self.assertEqual(self.sent, ['带路尚未确认，暂时不承诺出发。'])
        self.assertLessEqual(self.clock.mono - 1000, 4.1)
        self.assertEqual(self.feedback()['results'], [])

    def test_unknown_receiver_exception_is_not_exposed(self):
        self.runtime._validate = mock.Mock(side_effect=ValueError('/private/arbitrary/secret text'))
        self.on_sleep = self.receive
        self.respond()
        self.assertEqual(self.sent, ['这次带路没有被接下，稍后再试试。'])
        self.assertEqual(self.feedback()['results'][-1]['reason'], 'runtime_rejected')
        self.assertNotIn('/private', self.port.feedback_path.read_text(encoding='utf-8'))

    def test_receipt_binding_rejects_every_other_request_identity(self):
        origin, receipt = self.accepted()
        valid = self.feedback()
        for key, value in [('request_id', 'b' * 32), ('requester', OWN),
                           ('issued_at', receipt.issued_at - 1), ('chat_session', OTHER_SESSION),
                           ('ingress_revision', 2), ('channel', 'private'), ('operation', 'other')]:
            with self.subTest(key=key):
                doc = copy.deepcopy(valid); doc['results'][0][key] = value
                json_write(self.port.feedback_path, doc)
                self.assertIsNone(self.port._acceptance(origin, receipt))

    def test_stale_expired_future_and_malformed_receipts_are_ignored(self):
        origin, receipt = self.accepted(); valid = self.feedback()
        mutations = [lambda d: d.update(generated_at=self.clock.wall - 16),
                     lambda d: d.update(generated_at=self.clock.wall + 1),
                     lambda d: d['results'][0].update(expires_at=self.clock.wall),
                     lambda d: d['results'][0].update(expires_at=self.clock.wall + 100),
                     lambda d: d['results'][0].update(observed_at=receipt.issued_at - 1),
                     lambda d: d['results'][0].update(observed_at=self.clock.wall + 1),
                     lambda d: d.update(results='not a list')]
        for mutate in mutations:
            doc = copy.deepcopy(valid); mutate(doc); json_write(self.port.feedback_path, doc)
            self.assertIsNone(self.port._acceptance(origin, receipt))
        self.port.feedback_path.write_text('{broken', encoding='utf-8')
        self.assertIsNone(self.port._acceptance(origin, receipt))

    def test_old_receipt_does_not_prevent_later_matching_acceptance(self):
        origin, receipt = self.submit()
        self.runtime._publish_feedback({**json.loads(self.port.path.read_text(encoding='utf-8')),
                                       'request_id': 'b' * 32}, 'rejected', 'runtime_rejected')
        self.on_sleep = self.receive
        result = self.port.await_acceptance(origin, receipt, still_current=lambda: True)
        self.assertTrue(result.accepted)
        self.assertEqual(result.request_id, receipt.request_id)

    def test_active_request_and_controller_revision_must_match(self):
        origin, receipt = self.accepted(); valid = self.feedback()
        for key, value in [('active_request_id', 'b' * 32), ('active_revision', 999)]:
            doc = copy.deepcopy(valid); doc[key] = value; json_write(self.port.feedback_path, doc)
            self.assertFalse(self.port.acceptance_current(origin, receipt))

    def test_new_queued_action_invalidates_old_acceptance_before_runtime_poll(self):
        origin, receipt = self.accepted()
        json_write(self.port.path, {'pose': 'follow', 'target': SPEAKER})
        self.assertFalse(self.port.acceptance_current(origin, receipt))

    def test_runtime_preemption_invalidates_acceptance(self):
        origin, receipt = self.accepted()
        self.runtime.preempt('other_command')
        self.assertFalse(self.port.acceptance_current(origin, receipt))
        self.assertEqual(self.feedback()['results'][-1]['status'], 'superseded')

    def test_feedback_update_failure_best_effort_removes_old_acceptance(self):
        origin, receipt = self.accepted()
        with mock.patch.object(runtime_module.os, 'replace', side_effect=OSError('fixture atomic replace denied')):
            self.runtime.preempt('other_command')
        self.assertFalse(self.port.feedback_path.exists())
        self.assertFalse(self.port.acceptance_current(origin, receipt))
        self.assertIn('escort_feedback_write_failed', [row[0] for row in self.logs])

    def test_unremovable_feedback_fault_has_documented_fifteen_second_limit(self):
        origin, receipt = self.accepted()
        with mock.patch.object(runtime_module.os, 'replace', side_effect=OSError('fixture locked')), \
             mock.patch.object(Path, 'unlink', side_effect=OSError('fixture locked')):
            self.runtime.preempt('other_command')
        # Deliberately document the residual filesystem fault, not an atomic IPC
        # guarantee: genuine prior acceptance can remain readable until its TTL.
        self.assertTrue(self.port.acceptance_current(origin, receipt))
        self.assertIn('escort_feedback_invalidation_failed', [row[0] for row in self.logs])
        self.clock.sleep(15.1)
        self.assertFalse(self.port.acceptance_current(origin, receipt))

    def test_unsafe_feedback_path_never_attempts_invalidation_unlink(self):
        self.accepted()
        with mock.patch.object(Path, 'is_symlink', return_value=True), \
             mock.patch.object(Path, 'unlink') as unlink:
            self.runtime._write_feedback()
        unlink.assert_not_called()

    def test_new_ingress_while_waiting_suppresses_old_turn_reply(self):
        self.on_sleep = lambda: setattr(self.bot, '_travel_ingress_revision', 2)
        self.respond()
        self.assertEqual(self.sent, [])

    def test_new_session_while_waiting_suppresses_old_turn_reply(self):
        self.on_sleep = lambda: setattr(self.bot, '_travel_chat_session', OTHER_SESSION)
        self.respond()
        self.assertEqual(self.sent, [])

    def test_send_guard_rechecks_new_ingress_after_router_returns(self):
        route = travel.route_pending
        def replaced(*args):
            result = route(*args)
            self.bot._travel_ingress_revision += 1
            return result
        self.on_sleep = self.receive
        with mock.patch.object(travel, 'route_pending', side_effect=replaced):
            self.respond()
        self.assertEqual(self.events, [('receiver', True)])
        self.assertEqual(self.sent, [])

    def test_send_guard_rechecks_new_action_after_router_returns(self):
        route = travel.route_pending
        def replaced(*args):
            result = route(*args)
            json_write(self.port.path, {'pose': 'follow'})
            return result
        self.on_sleep = self.receive
        with mock.patch.object(travel, 'route_pending', side_effect=replaced):
            self.respond()
        self.assertEqual(self.sent, [])

    def test_new_runtime_request_cannot_reuse_old_acceptance(self):
        origin, receipt = self.accepted()
        cmd = json.loads(self.port.path.read_text(encoding='utf-8'))
        cmd.update(request_id='b' * 32, ingress_revision=2)
        json_write(self.port.path, cmd)
        self.assertTrue(self.runtime.accept(cmd))
        self.assertFalse(self.port.acceptance_current(origin, receipt))
        self.assertEqual(self.feedback()['active_request_id'], 'b' * 32)

    def test_current_rejection_does_not_reuse_previous_accepted_trip(self):
        self.accepted()
        self.captured = False
        self.bot._travel_ingress_revision = 2
        self.on_sleep = self.receive
        self.respond()
        self.assertEqual(self.sent, ['本账号缺少目的地图目录，请到资源页重新构建。'])

    def test_runtime_cancel_invalidates_previous_acceptance(self):
        origin, receipt = self.accepted()
        self.assertTrue(self.runtime.cancel({'raw_command': '/取消带路', 'requester': SPEAKER,
                                            'target': SPEAKER, 'src': 'chat'}))
        self.assertFalse(self.port.acceptance_current(origin, receipt))

    def test_feedback_is_bounded_and_expired_entries_are_pruned(self):
        self.submit(); cmd = json.loads(self.port.path.read_text(encoding='utf-8'))
        for i in range(25):
            self.runtime._publish_feedback({**cmd, 'request_id': f'{i:032x}'}, 'rejected', 'runtime_rejected')
        self.assertEqual(len(self.feedback()['results']), 16)
        self.assertLess(self.port.feedback_path.stat().st_size, 65536)
        self.assertNotIn('raw_command', self.port.feedback_path.read_text(encoding='utf-8'))
        self.clock.sleep(16)
        self.runtime.tick()
        self.assertEqual(self.feedback()['results'], [])

    def test_runtime_accept_without_successful_feedback_write_is_not_chat_success(self):
        self.on_sleep = self.receive
        with mock.patch.object(self.runtime, '_write_feedback', return_value=None):
            self.respond()
        self.assertEqual(self.events, [('receiver', True)])
        self.assertEqual(self.sent, ['带路尚未确认，暂时不承诺出发。'])

    def test_disabled_port_does_not_submit_or_announce_departure(self):
        self.port.flag.unlink()
        self.respond()
        self.assertEqual(self.sent, ['带路还没接好，暂时不会出发。'])
        self.assertFalse(self.port.path.exists())

    def test_cancel_chat_semantics_are_unchanged(self):
        self.respond('/取消带路')
        self.assertEqual(self.sent, ['好，先停下。'])
        self.assertEqual(json.loads(self.port.path.read_text(encoding='utf-8'))['pose'], 'escort_cancel')

    def test_nontravel_follow_and_sit_still_fall_through(self):
        for raw in ('/跟着我', '/坐我身边'):
            decision = travel.route_pending(self.bot, SPEAKER, 'local',
                [{'text': raw, 'travel_origin': self.origin(raw)}])
            self.assertFalse(decision.handled)
        self.assertFalse(self.port.path.exists())

    def test_installed_runtime_delegates_other_actions_unchanged(self):
        self.accepted()
        with mock.patch.object(runtime_module, 'LeaderTravelRuntime', return_value=self.runtime):
            runtime_module.install(self.ctl, has_capture=lambda *_: True,
                rpc_port=self.runtime.rpc, session_deadline=runtime_module.time.time() + 180)
        command = {'pose': 'follow', 'target': SPEAKER}
        self.ctl._apply(command)
        self.assertIn(('other', command), self.effects)
        self.assertEqual(self.feedback()['results'][-1]['status'], 'superseded')

    def test_missing_acceptance_interface_is_not_success(self):
        self.router.port = types.SimpleNamespace(available=True, submit=lambda *a, **kw:
            travel.SubmitResult('a' * 32, True, 'queued_not_arrived'))
        self.respond()
        self.assertEqual(self.sent, ['带路尚未确认，暂时不承诺出发。'])

    def test_restart_replaces_old_process_feedback_with_empty_new_runtime_id(self):
        origin, receipt = self.accepted()
        old = self.feedback()['runtime_id']
        runtime_module.LeaderTravelRuntime(self.ctl, session_deadline=self.clock.wall + 180,
            has_capture=lambda *_: True, rpc_port=self.runtime.rpc,
            clock=lambda: self.clock.mono, wall=lambda: self.clock.wall)
        self.assertNotEqual(self.feedback()['runtime_id'], old)
        self.assertEqual(self.feedback()['results'], [])
        self.assertFalse(self.port.acceptance_current(origin, receipt))

    def test_actual_transport_receives_refusal_even_if_review_invents_departure(self):
        self.actual_send()
        self.bot.pass_review = lambda _: '好呀，牵好再出发。'
        self.captured = False; self.on_sleep = self.receive
        self.respond()
        self.assertEqual(self.sent, ['本账号缺少目的地图目录，请到资源页重新构建。'])

    def test_actual_transport_preserves_early_disabled_and_submit_failure_replies(self):
        self.actual_send()
        self.bot.pass_review = lambda _: '好呀，我现在就带你去晨岛！'
        self.port.flag.unlink()
        self.respond()
        self.assertEqual(self.sent, ['带路还没接好，暂时不会出发。'])
        json_write(self.port.flag, {'version': 1, 'enabled': True, 'session_deadline': self.clock.wall + 180})
        self.bot._travel_ingress_revision = 2
        with mock.patch.object(self.port, 'submit', side_effect=OSError('fixture write failed')):
            self.respond()
        self.assertEqual(self.sent[-1], '这次没接上，先等等我。')

    def test_actual_transport_rechecks_acceptance_after_review_wait(self):
        self.actual_send()
        def review(text):
            self.runtime.preempt('other_command')
            return text
        self.bot.pass_review = review
        self.on_sleep = self.receive
        self.respond()
        self.assertEqual(self.events, [('receiver', True)])
        self.assertEqual(self.sent, [])

    def test_actual_transport_filters_post_review_model_promise_only_in_travel_turn(self):
        self.actual_send()
        self.bot.pass_review = lambda _: '我现在出发。'
        self.bot.send('地图列表', unaccepted_travel_payloads=[])
        self.assertEqual(self.sent, ['还没确认带路，先选好目的地吧。'])
        self.bot.send('普通聊天')
        self.assertEqual(self.sent[-1], '我现在出发。')


class ModelDepartureTests(unittest.TestCase):
    def setUp(self):
        harness = load('escort7_choice_harness', Path(__file__).with_name('test_travel_choice_sync.py'))
        self.case = harness.AITravelReplyIntegrationTests('test_real_reply_and_tool_register_successful_visible_text')
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def test_actual_send_removes_model_promise_but_keeps_selectable_names(self):
        _, sent, _ = self.case.respond(model='恶作剧密室；螃蟹的恶搞派对。\n好呀，牵好再出发。')
        self.assertIn('恶作剧密室；螃蟹的恶搞派对', sent[0])
        self.assertIn('还没确认带路', sent[0])
        self.assertNotIn('牵好再出发', sent[0])
        self.assertEqual(len(self.case.router.choice_proposals[(SPEAKER, 'local')]['candidates']), 2)
        result = self.case.route('/第二个')
        self.assertEqual(result.reason, 'submitted_not_completed')
        self.assertEqual(len(self.case.port.calls), 1)

    def test_actual_send_in_nontravel_model_turn_is_unchanged(self):
        _, sent, _ = self.case.respond(model='我们明天出发看风景。', use_tool=False)
        self.assertEqual(sent, ['我们明天出发看风景。'])

    def test_compression_rewrite_is_filtered_before_actual_send(self):
        _, sent, _ = self.case.respond(model='恶作剧密室；螃蟹的恶搞派对。',
                                       parts=['恶作剧密室；螃蟹的恶搞派对。', '我现在出发。'])
        self.assertEqual(sent[-1], '还没确认带路，先选好目的地吧。')
        self.assertEqual(len(self.case.router.choice_proposals[(SPEAKER, 'local')]['candidates']), 2)

    def test_map_names_and_conditional_or_quoted_descriptions_are_not_promises(self):
        text = '如果你选好了，我们再出发。出发前先看地图。她说“我现在出发”。'
        self.assertEqual(travel.guard_model_departure(text), text)
        payload = [{'maps': [{'名称': '我现在出发', '其它叫法': []}]}]
        self.assertEqual(travel.guard_model_departure('我现在出发。', payload), '我现在出发。')

    def test_compound_adverbs_do_not_escape_current_turn_model_guard(self):
        for text in ['好呀，我现在就带你去晨岛！', '我已经带你到晨岛了。', '我们现在就出发。']:
            with self.subTest(text=text):
                _, sent, _ = self.case.respond(model=text)
                self.assertNotEqual(sent, [text])
                self.assertIn('还没确认带路', sent[0])
                self.case.reset_context()


if __name__ == '__main__':
    unittest.main()
