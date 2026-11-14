"""Offline tests for the shipped AI source; never import its credential/network bootstrap."""
import ast
import copy
import json
from pathlib import Path
import re
import threading
import time
import types
import unittest


SOURCE = Path(__file__).resolve().parents[1] / 'app' / 'bot' / 'ai_companion.py'
TEXT = SOURCE.read_text(encoding='utf-8')
TREE = ast.parse(TEXT)


class MemoryPath:
    def __init__(self, doc=None, error=None):
        self.doc, self.error, self.writes = doc, error, []
        self.parent = self

    def __truediv__(self, _):
        return self

    def mkdir(self, **_):
        pass

    def read_text(self, **_):
        if self.error:
            raise self.error
        return json.dumps(self.doc)

    def write_text(self, value, **_):
        self.writes.append(json.loads(value))


def load_subset(doc=None):
    fn_names = {'_chat_move_intent', '_pose_state', '_pose_sitting', '_pose_action_note', 'situation'}
    constant_names = {'MOVES', 'CHAT_MOVE_GUIDANCE', 'EMOTE_TAG', 'STANCE_TAG'}
    nodes = []
    for node in TREE.body:
        if isinstance(node, ast.FunctionDef) and node.name in fn_names:
            nodes.append(copy.deepcopy(node))
        elif isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in constant_names for t in node.targets):
            nodes.append(copy.deepcopy(node))
        elif isinstance(node, ast.ClassDef) and node.name == 'Companion':
            subset = copy.deepcopy(node)
            subset.body = [n for n in subset.body if isinstance(n, ast.FunctionDef)
                           and n.name in {'_respond_locked', 'emote', 'move'}]
            nodes.append(subset)
    pose = MemoryPath(doc if doc is not None else {})
    logs, threads = [], []

    class Thread:
        def __init__(self, target, args=(), **_):
            self.target, self.args = target, args

        def start(self):
            threads.append(self)

    never = re.compile(r'(?!)')
    env = {
        're': re, 'json': json, 'POSE_STATE': pose, 'ROOT': pose,
        'POSE_CONTROL': MemoryPath(), 'OWNER': 'owner', 'OWNER_TITLE': '哥哥',
        'MAP_NAMES': {1: '测试地图'}, 'map_catalog': types.SimpleNamespace(all_maps=lambda: {}),
        'motion_catalog': types.SimpleNamespace(prompt_lists=lambda: ('挥手,鞠躬', '默认')),
        'game_context': types.SimpleNamespace(system_note=lambda *_: '', query_intent=lambda _: None),
        'travel_chat_v25': types.SimpleNamespace(route_pending=lambda *_: types.SimpleNamespace(handled=False, remaining=None)),
        'social_commands': types.SimpleNamespace(classify=lambda _: None, semantic_candidate=lambda _: False),
        'personas': types.SimpleNamespace(active=lambda: {'prompt': '测试人设'}),
        'RULES': '', 'TOOLS': [], 'TOOL_ROUNDS': 1,
        'web_tools': types.SimpleNamespace(TOOL_DEFINITIONS=[]),
        'RENAME_HINT_RE': never, 'ASK_RE': never, 'CLAIM_RE': never,
        'STANCE_ASK_RE': never, 'NUMBER_RE': never, 'BANNED': never,
        'time': types.SimpleNamespace(monotonic=lambda: 10000.0, time=lambda: 0,
                                      sleep=lambda _: None, strftime=time.strftime, gmtime=time.gmtime),
        'threading': types.SimpleNamespace(Thread=Thread), 'EMOTE_COOLDOWN': 6.0,
        'log': lambda *a, **kw: logs.append((a, kw)), 'logs': logs, 'threads': threads,
    }
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), str(SOURCE), 'exec'), env)
    return env


class AISyncTests(unittest.TestCase):
    def test_follow_natural_requests(self):
        parse = load_subset()['_chat_move_intent']
        for text in ['/跟我走', '/走啦走啦，跟上我呀', '/咱俩一起走吧', '小光呀，跟着我走好吗',
                     '能不能跟我走', '你还坐着呀，跟我走吧', '跟紧我', '随我走吧', '我带你走']:
            with self.subTest(text=text):
                self.assertEqual(parse(text)[0], 'follow')

    def test_sit_near_speaker_requests(self):
        parse = load_subset()['_chat_move_intent']
        for text in ['/来我边上坐下呗', '/嘻嘻，过来，来我边上坐', '/来来来，过来坐，坐我附近吧',
                     '坐我旁边', '能不能坐到我身边', '过来坐一会儿', '挨着我坐下吧']:
            with self.subTest(text=text):
                self.assertEqual(parse(text)[0], 'sit')

    def test_negations(self):
        parse = load_subset()['_chat_move_intent']
        for text, blocked in [('/别过来坐', 'sit'), ('/不用坐了', 'sit'), ('/你坐着别动', 'sit'),
                              ('/别跟着我', 'follow'), ('不用跟', 'follow'), ('停下别跟了', 'follow')]:
            with self.subTest(text=text):
                intent, prohibited = parse(text)
                self.assertIsNone(intent)
                self.assertIn(blocked, prohibited)

    def test_quotes_reports_questions_are_not_commands(self):
        parse = load_subset()['_chat_move_intent']
        for text in ['他跟我说过', '坐过山车好玩吗', '他说跟我走', '如果跟我走呢', '“跟我走”是什么意思',
                     '你已经坐下了吗', '他跟着我', '我正在跟着你', '跟我说说话', '他坐我旁边了']:
            with self.subTest(text=text):
                self.assertIsNone(parse(text)[0])

    def test_conflicting_actions_keep_model_decision(self):
        parse = load_subset()['_chat_move_intent']
        for text in ['来我旁边坐下，然后跟我走', '跟我走，站起来', '坐我旁边，然后点火']:
            with self.subTest(text=text):
                self.assertIsNone(parse(text)[0])

    def test_pose_notes(self):
        note = load_subset()['_pose_action_note']
        for doc, expected in [({'sitting': True, 'seat': 123}, '座位号 123'), ({'pose': 'sit'}, '正坐着'),
                              ({'pose': 'follow'}, '跟随中'), ({'pose': 'stand'}, '站着'),
                              ({'sitting': False}, '站着'), ({'pose': 'anim'}, '表情动作中'),
                              ({'pose': 'anim', 'name': 'BaskEnter'}, '持续性未确认')]:
            with self.subTest(doc=doc):
                self.assertIn(expected, note(doc))
        for doc in [None, {}, [], {'pose': 'unknown'}]:
            with self.subTest(doc=doc):
                self.assertEqual(note(doc), '')

    def test_state_reader_uses_only_read(self):
        env = load_subset({'pose': 'sit', 'sitting': True, 'seats_here': 2})
        self.assertTrue(env['_pose_state']()['sitting'])
        text = env['situation'](1)
        self.assertIn(env['CHAT_MOVE_GUIDANCE'], text)
        self.assertIn('正坐着', text)
        self.assertEqual(env['POSE_STATE'].writes, [])

    def test_missing_state_does_not_invent_standing(self):
        env = load_subset()
        env['POSE_STATE'].error = OSError('offline missing')
        self.assertEqual(env['_pose_state'](), {})
        self.assertNotIn('你现在是站着的', env['situation'](1))

    def test_non_object_state_is_unknown(self):
        env = load_subset(['invalid'])
        self.assertEqual(env['_pose_state'](), {})
        self.assertNotIn('动作状态：', env['situation'](1))

    def test_sit_failure_and_pending_notes_preserved(self):
        env = load_subset({'pose': 'stand', 'sit_pending': True, 'sit_failed': {'reason': 'no_seat'}})
        text = env['situation'](1)
        self.assertIn('no_seat', text)
        self.assertIn('正在走向座位', text)

    def respond(self, said, model_text='来啦[动作:站起来]', *, sender='owner', web=False, doc=None, history=None):
        env = load_subset(doc or {'pose': 'sit', 'sitting': True})
        bot = env['Companion']()
        messages = []
        bot.lock = threading.Lock()
        bot.pending = {(sender, 'local'): said if isinstance(said, list) else [said]}
        bot.last_reply, bot.global_last = {}, 0
        bot.name_of = lambda _: '测试说话人'
        bot.web = types.SimpleNamespace(begin_turn=lambda _: {'allowed': web})
        bot.names, bot.map_id, bot.account = {}, 1, {'user_uuid': 'fixture'}
        bot._speaker_memory = lambda _: ''
        bot._chat_context_history = lambda *_, **kw: history or []
        bot.reply_local = types.SimpleNamespace()
        def chat(msgs, **_):
            messages.append(copy.deepcopy(msgs))
            return {'content': model_text}
        bot.llm = types.SimpleNamespace(chat=chat)
        bot.format_reply = lambda raw: [raw]
        bot.send = lambda *_, **kw: {'status': 'submitted'}
        actions = []
        bot.emote = lambda name, who: actions.append((name, who))
        bot._respond_locked(sender, 'local')
        return env, bot, actions, messages

    def test_real_respond_method_injects_pose_and_guidance(self):
        env, _, actions, messages = self.respond('/跟我走')
        system = messages[0][0]['content']
        self.assertIn(env['CHAT_MOVE_GUIDANCE'], system)
        self.assertIn('正坐着', system)
        self.assertEqual(actions, [('跟着走', 'owner')])

    def test_missing_tag_still_follows(self):
        self.assertEqual(self.respond('走啦走啦，跟上我呀', model_text='来啦')[2], [('跟着走', 'owner')])

    def test_correct_tag_executes_once(self):
        self.assertEqual(self.respond('跟我走', model_text='来啦[动作:跟着走]')[2], [('跟着走', 'owner')])

    def test_sit_fallback_uses_speaker(self):
        self.assertEqual(self.respond('来我边上坐下呗', model_text='来啦')[2], [('坐下', 'owner')])

    def test_negative_model_tag_removed(self):
        self.assertEqual(self.respond('别跟着我', model_text='好[动作:跟着走]')[2], [])
        self.assertEqual(self.respond('不用坐了', model_text='好[动作:坐下]')[2], [])

    def test_nonowner_is_not_overridden(self):
        self.assertEqual(self.respond('跟我走', sender='other')[2], [('站起来', 'other')])

    def test_web_turn_does_not_schedule_action(self):
        self.assertEqual(self.respond('跟我走', web=True)[2], [])

    def test_history_does_not_create_intent(self):
        history = [{'role': 'user', 'content': '跟我走'}]
        self.assertEqual(self.respond('你好', model_text='你好', history=history)[2], [])

    def test_current_message_dicts_are_joined(self):
        self.assertEqual(self.respond([{'text': '嘻嘻，过来', 'line_id': '1'},
                                       {'text': '来我边上坐', 'ch': 'local'}], model_text='来啦')[2], [('坐下', 'owner')])

    def test_last_model_label_remains_when_intents_conflict(self):
        self.assertEqual(self.respond('跟我走，站起来', model_text='来啦[动作:跟着走][动作:站起来]')[2], [('站起来', 'owner')])

    def test_move_follow_and_sit_payloads(self):
        env = load_subset()
        bot = env['Companion']()
        actions, watches = [], []
        bot.control = lambda cmd, **_: actions.append(cmd)
        bot._start_sit_watch = lambda *args: watches.append(args)
        bot.move('follow', 'owner')
        bot.move('sit', 'owner')
        self.assertEqual(actions, [{'pose': 'follow', 'target': 'owner', 'distance': 1.5},
                                   {'pose': 'sit', 'near': 'owner'}])
        self.assertEqual(watches, [('owner', 'local')])

    def test_continuous_emote_has_no_timed_exit(self):
        env = load_subset()
        env['resolve_emote_all'] = lambda _: (['BaskEnter', 'BaskExit'], [1, 1])
        bot = env['Companion']()
        bot.dry_run = False
        bot.emote('晒太阳')
        self.assertEqual(env['POSE_CONTROL'].writes, [{'pose': 'anim', 'name': 'BaskEnter', 'seconds': 1}])
        self.assertEqual(env['threads'], [])

    def test_noncontinuous_multistage_emote_keeps_sequence(self):
        env = load_subset()
        env['resolve_emote_all'] = lambda _: (['First', 'Second'], [1, 2])
        bot = env['Companion']()
        bot.dry_run = False
        bot.emote('两段')
        self.assertEqual(len(env['threads']), 1)
        env['threads'][0].target()
        self.assertEqual([x['name'] for x in env['POSE_CONTROL'].writes], ['First', 'Second'])

    def test_unmatched_enter_does_not_become_continuous(self):
        env = load_subset()
        env['resolve_emote_all'] = lambda _: (['FirstEnter', 'OtherExit'], [1, 2])
        bot = env['Companion']()
        bot.dry_run = False
        bot.emote('两段')
        self.assertEqual(len(env['threads']), 1)

    def test_client_specific_paths_and_features_remain(self):
        for fragment in ['ROOT = xg_paths.DATA', 'import llm_providers', 'def trigger_match(',
                         'xg_paths.load_name_table', 'from xg_auth_client import Client, heartbeat',
                         'access_key=_fp.build_access_key()', 'def service_retry(', 'def unlock_friend_tree(']:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, TEXT)
        self.assertNotIn(('/root/cl' + 'audework'), TEXT)
        self.assertNotIn('seats_{bot.map_id}.json', TEXT)


if __name__ == '__main__':
    unittest.main()
