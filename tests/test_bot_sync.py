"""terrainfix.11 bot port regressions: pure offline, no account loading."""
from pathlib import Path
import copy
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / 'app'
sys.path[:0] = [str(APP), str(APP/'bot'), str(APP/'src')]
import anim_pose as AP
import placeable
import room_pose as RP
import stuck_recovery as SR
import wander
import xg_paths


class ContinuousEmoteTests(unittest.TestCase):
    def setUp(self):
        self.body = (ROOT/'tests/fixtures/actor_0168_candle.bin').read_bytes()
        fields = AP.read(self.body)
        self.previous = {key:fields[key] for key in ('gen','key','start')}

    def test_enter_then_indefinite_hold(self):
        event = AP.ContinuousEmoteEvent(100,101,self.previous,0)
        start = AP.read(event.post(self.body,10))
        self.assertEqual((start['mode'],start['emote_hold'],start['code90']), (8,1,1))
        held = AP.read(event.post(self.body,10000))
        self.assertEqual((held['mode'],held['emote_hold'],held['code90']), (8,1,0))
        self.assertEqual(event.phase(10000)[0], 'holding')

    def test_enter_timer_starts_on_first_actor(self):
        event = AP.ContinuousEmoteEvent(100,101,self.previous,0)
        event.post(self.body,100)
        self.assertEqual(event.phase(100.1)[0], 'entering')
        self.assertEqual(event.phase(100.2)[0], 'holding')

    def test_exit_three_phases_and_clear_hold(self):
        event = AP.EmoteExitEvent(101,self.previous,10)
        self.assertEqual(event.phase(10)[0],'mode7')
        self.assertEqual(event.phase(10.7)[0],'restoring')
        self.assertEqual(event.phase(12)[0],'idle')
        for now in (10,10.7):
            self.assertEqual(AP.read(event.post(self.body,now))['emote_hold'],0)
        idle = AP.read(event.post(self.body,12))
        self.assertEqual((idle['mode'],idle['code90']),(0,7))

    def test_resume_reenters_outer_apply_without_private_keyword(self):
        c = RP.PoseController.__new__(RP.PoseController)
        c._solo_lock = threading.RLock()
        c.stop, c.social = threading.Event(), None
        c._active_running = lambda:False
        c._solo_cancel_ground = lambda:None
        c.state = {'phase':'solo_exit'}
        c.log = lambda *a,**k:None
        event = AP.EmoteExitEvent(101,self.previous,0)
        event.post(self.body,2)
        c.event = event
        calls=[]
        c._apply=lambda cmd:calls.append(cmd)
        def _apply(*args,**kwargs):
            self.fail('inner unwrapped apply must not be invoked')
        gate={'event':event,'pending':(_apply,({'pose':'follow','target':'fixture'},),{'_home_command':4},1)}
        c._solo_gate=gate
        RP.PoseController._solo_resume(c,gate)
        self.assertEqual(calls,[{'pose':'follow','target':'fixture'}])
        self.assertEqual(c.event.key,0)
        self.assertIsNone(c._solo_gate)

    def test_resume_drops_stale_gate(self):
        c = RP.PoseController.__new__(RP.PoseController)
        c._solo_lock = threading.RLock()
        c._solo_gate = object()
        RP.PoseController._solo_resume(c, {'event':None})
        self.assertIsNotNone(c._solo_gate)


class HomeSeatTests(unittest.TestCase):
    def setUp(self):
        self.c=RP.PoseController.__new__(RP.PoseController)
        self.c.tracker=RP.RoomTracker()
        self.c.map_id=RP.HOME_MAP_ID
        self.c.room_since=100
        t=self.c.tracker
        t.ready=True
        t.confirmed_map_id=RP.HOME_MAP_ID
        t.room_generation=3
        t.seat_epoch=2
        t.own_uuid='own-fixture'
        t.slots={5:'witness-fixture'}
        t._slot_versions={5:7}
        t.seat_book={5547:((1,2,3),16,950)}
        t.seat_witnesses={5547:('witness-fixture',5,7,3,2,50)}
        self.wall=patch('room_pose.time.time',return_value=1000)
        self.mono=patch('room_pose.time.monotonic',return_value=100)
        self.wall.start();self.mono.start()
        self.addCleanup(self.wall.stop);self.addCleanup(self.mono.stop)

    def free(self):
        return self.c._home_seat_snapshot()[0]

    def test_current_witness_seat_eligible(self):
        self.assertEqual(set(self.free()),{5547})
        self.assertEqual(self.c._home_pick_seat((1,2,3))[0],5547)

    def test_no_neighbor_seat_inference(self):
        self.c.seats={5546:{'origin':[1,2,3],'yaw':16}}
        self.assertEqual(set(self.c._seat_candidates()[0]),{5547})

    def test_expired_wall_clock_rejected(self):
        self.c.tracker.seat_book[5547]=((1,2,3),16,399)
        self.assertEqual(self.free(),{})

    def test_expired_monotonic_rejected(self):
        self.c.tracker.seat_witnesses[5547]=('witness-fixture',5,7,3,2,-501)
        self.assertEqual(self.free(),{})

    def test_witness_departure_rejected(self):
        self.c.tracker.slots.clear()
        self.assertEqual(self.free(),{})

    def test_witness_slot_reuse_rejected(self):
        self.c.tracker._slot_versions[5]=8
        self.assertEqual(self.free(),{})

    def test_old_room_rejected(self):
        self.c.tracker.room_generation=4
        self.assertEqual(self.free(),{})

    def test_old_seat_epoch_rejected(self):
        self.c.tracker.seat_epoch=3
        self.assertEqual(self.free(),{})

    def test_self_observation_rejected(self):
        self.c.tracker.own_uuid='witness-fixture'
        self.assertEqual(self.free(),{})

    def test_occupied_seat_rejected(self):
        self.c.tracker.seated[5]=5547
        self.assertEqual(self.free(),{})

    def test_home_load_and_learn_do_not_touch_files(self):
        with patch.object(Path,'exists',side_effect=AssertionError('seat file probe')), patch.object(Path,'read_text',side_effect=AssertionError('seat file read')), patch.object(Path,'write_text',side_effect=AssertionError('seat file write')):
            self.assertEqual(RP.load_seats(RP.HOME_MAP_ID),{})
            self.c._learn_seats()

    def test_room_reset_clears_seat_and_prop_state(self):
        before=self.c.tracker.seat_epoch
        self.c.tracker._clear_room_locked()
        self.assertEqual(self.c.tracker.seat_epoch,before+1)
        self.assertEqual(self.c.tracker.seat_book,{})
        self.assertEqual(self.c.tracker.seat_witnesses,{})
        self.assertEqual(self.c.tracker.placeables.snapshot(),[])

    def test_default_control_uses_client_paths(self):
        self.assertEqual(RP.CONTROL_DIR,xg_paths.CONTROL)
        self.assertEqual(wander.SWITCH,xg_paths.CONTROL/'wander')


class WanderSeatStateTests(unittest.TestCase):
    def state(self):
        return {'map_id':RP.HOME_MAP_ID,'room':{'map_id':RP.HOME_MAP_ID,'connected':True,'room_generation':2},
            'home_seat_status':{'schema':'home-seat-live-v1','map_id':RP.HOME_MAP_ID,'home_session':'fixture',
                'room_generation':2,'seat_epoch':1,'observed_at':1000,'expires_at':1100,'count':1}}

    def test_current_context_accepted(self):
        with patch('wander.time.time',return_value=1001):
            self.assertEqual(wander._home_live_context(self.state()),{'home_session':'fixture','room_generation':2,'seat_epoch':1})

    def test_stale_state_rejected(self):
        with patch('wander.time.time',return_value=1010):
            self.assertIsNone(wander._home_live_context(self.state()))

    def test_old_counter_not_room_evidence(self):
        self.assertIsNone(wander._home_live_context({'map_id':RP.HOME_MAP_ID,'seats_here':100}))

    def test_expired_and_changed_room_rejected(self):
        st=self.state();st['home_seat_status']['expires_at']=999
        with patch('wander.time.time',return_value=1001):
            self.assertIsNone(wander._home_live_context(st))
            st=self.state();st['room']['room_generation']=3
            self.assertIsNone(wander._home_live_context(st))


class RecoveryTests(unittest.TestCase):
    def tracker(self):
        t=SR.RecoveryTracker();t.observe('follow-fixture',(0,0,0),0,moving=True)
        return t

    def test_goal_failure_not_stuck(self):
        t=self.tracker();t.note_failure('goal_support_missing',1,token='a')
        self.assertEqual(SR.classify_failure('goal_support_missing'),'invalid_goal')
        self.assertIsNone(t.trigger(100))

    def test_worker_and_permission_failures_ignored(self):
        for text in ('worker_unavailable','cold','permission_denied','social_owned'):
            self.assertEqual(SR.classify_failure(text),'ignore')

    def test_cached_failure_not_recounted(self):
        t=self.tracker()
        for now in (0,1,2):t.note_failure('start_overlaps_collision',now,token='same')
        self.assertEqual(t.invalid_failures,1)
        self.assertIsNone(t.trigger(2))

    def test_new_failures_trigger_and_cooldown(self):
        t=self.tracker();t.note_failure('start_overlaps_collision',0,token='a');t.note_failure('start_overlaps_collision',1,token='b')
        self.assertEqual(t.trigger(1),'invalid_start')
        self.assertTrue(t.begin_attempt(1))
        self.assertFalse(t.begin_attempt(2))
        self.assertTrue(t.begin_attempt(10))
        self.assertTrue(t.exhausted)
        self.assertFalse(t.rearm_after_cooldown(30))
        self.assertTrue(t.rearm_after_cooldown(41))
        t.note_failure('start_overlaps_collision',42,token='a')
        self.assertEqual(t.invalid_failures,0)

    def test_policy_rejects_unbounded_search(self):
        for kwargs in ({'max_attempts':20},{'search_radius':100},{'search_step':.01}):
            with self.assertRaises(ValueError):SR.RecoveryPolicy(**kwargs)


class PlaceableRegistryTests(unittest.TestCase):
    def event(self):
        return {'kind':'place','id':'fixture/prop','name':'TableXmas','owner_slot':5,'owner_uuid':'fixture',
                'position':[1,2,3],'rotation':[0,0,0,1],'scale':1,'state':0,'tag':'aabbcc'}

    def test_owner_binding_and_idempotent_update(self):
        r=placeable.PlaceableRegistry({'TableXmas'})
        self.assertEqual(r.apply([self.event()],{}),[{'reason':'prop_owner_not_in_roster'}])
        self.assertEqual(r.snapshot(),[])
        self.assertEqual(r.apply([self.event()],{5:'fixture'}),[])
        revision=r.revision
        r.apply([self.event()],{5:'fixture'})
        self.assertEqual(r.revision,revision)
        event=self.event();event['position'][0]=4;r.apply([event],{5:'fixture'})
        self.assertGreater(r.revision,revision)

    def test_snapshot_is_detached_and_departure_clears(self):
        r=placeable.PlaceableRegistry({'TableXmas'});r.apply([self.event()],{5:'fixture'})
        snapshot=r.snapshot();snapshot[0]['position'][0]=100
        self.assertEqual(r.snapshot()[0]['position'][0],1)
        self.assertEqual(r.retain_owners([]),1)
        self.assertEqual(r.snapshot(),[])

    def test_destroy_only_matching_owner_tag(self):
        r=placeable.PlaceableRegistry({'TableXmas'});r.apply([self.event()],{5:'fixture'})
        destroy={'kind':'destroy','owner_slot':5,'owner_uuid':'fixture','tag':'wrong'}
        r.apply([destroy],{5:'fixture'});self.assertEqual(len(r.snapshot()),1)
        destroy['tag']='aabbcc';r.apply([destroy],{5:'fixture'});self.assertEqual(r.snapshot(),[])

    def test_malformed_body_rejected(self):
        with self.assertRaises(ValueError):placeable.parse_placeable_body(b'bad')


if __name__=='__main__':
    unittest.main()
