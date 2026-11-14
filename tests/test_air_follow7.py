"""Patch 7: real in-memory geometry and synthetic controllers; no account/config IO."""
from pathlib import Path
import copy
import math
import sys
import threading
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / 'app'
sys.path[:0] = [str(APP), str(APP/'bot'), str(APP/'src')]
import air_follow as AF
import air_follow_motion as FM
import air_follow_runtime as RT
import air_motion as AM
import air_planner as PL
import air_terrain as AT
import anim_pose as AP
import room_pose as RP
import stuck_recovery as SR
from skyterrain.ground import GroundSurface
from skyterrain.capsule import CapsuleProbe


def terrain(walls=(), floor=0., size=100.):
    vertices = [(-size,floor,-size), (size,floor,-size), (size,floor,size), (-size,floor,size)]
    triangles = [(0,1,2), (0,2,3)]
    for x, bottom, top in walls:
        i = len(vertices)
        vertices += [(x,bottom,-10), (x,top,-10), (x,top,10), (x,bottom,10)]
        triangles += [(i,i+1,i+2), (i,i+2,i+3)]
    return AT.TerrainView(GroundSurface(vertices, triangles))


def phase(a, b, duration=.2, name='climb'):
    return {'phase':name, 'mode':'linear', 'duration':duration, 'from':a, 'to':b}


def observation(point=(3.,3.,0.), now=0., mode='airborne', velocity=(0.,0.,0.)):
    return dict(position=point, observed_at=now, mode=mode, velocity=velocity, heading=0.)


def motion(scene=None, now=0.):
    scene = scene or terrain()
    p = (0.,3.,0.)
    plan = dict(kind='fly', reason='air_follow_takeoff', start=p, goal=p, target=(3.,3.,0.),
                landing=p, phases=[phase(p,p,.05)], air_follow_session=True,
                air_follow_observation=observation(now=now), end_velocity=(0.,0.,0.))
    result = FM.AirFollowMotion(plan, now, clips=NS(pick=lambda key:None), terrain=scene)
    result.advance(now+.05)
    return result


def controller(scene=None, now=10.):
    scene = scene or terrain()
    engine = NS(_closed=False)
    tracker = NS(lock=threading.RLock(), ready=True, _engine=engine, slots={7:'synthetic-target'},
                 origins={7:(3.,3.,0.)}, position_observed={7:(0,now)}, _slot_versions={7:2},
                 room_generation=4, epoch=6, level_changes=[])
    c = NS(tracker=tracker, follow={'target':'synthetic-target'}, map_id=1,
           position=(0.,0.,0.), heading=0., _air_motion=None, motion=None,
           _air_cfg={'enabled':True,'follow':True}, _command_seq=8,
           _terrain_adapter=None, sitting=None, social=None, holding=False, candle=None,
           candle_reply=None, anim_lock=threading.Lock(), event=None,
           _active_running=lambda:False, _hop_running=lambda:False)
    c._air_terrain=lambda:scene
    c.logs=[]
    c.log=lambda event,**fields:c.logs.append((event,fields))
    c._air_block_reason=lambda stamp:RP.PoseController._air_block_reason(c,stamp)
    def start(plan,stamp):
        c.motion=c._air_motion=RT._new_motion(plan,stamp,clips=NS(pick=lambda key:None),terrain=scene)
        return True
    c._air_start=start
    return c


def bind(c, air):
    snap,why=RT._snapshot(c,10.)
    assert why is None
    RT._bind_motion(air,dict(binding=snap['binding'],target=snap['target'],level_change=None),10.)
    c.motion=c._air_motion=air
    c.position=air.pos(10.)
    return snap


class FlightObservationTests(unittest.TestCase):
    def setUp(self):
        self.scene=terrain()
        self.tracker=AF.TargetTracker()

    def sample(self,t,p=(0.,1.5,0.),binding=('room',1),stamp=None):
        return self.tracker.observe(t,p,self.scene,binding=binding,observed_at=stamp)

    def test_small_jump_duration_does_not_confirm_flight(self):
        for i in range(41):
            t=i*.05
            row=self.sample(t,(0.,.362*max(0.,math.sin(math.pi*t/1.7)),0.))
            self.assertNotEqual(row['mode'],'airborne')

    def test_fast_height_requires_fresh_confirmation(self):
        self.assertEqual(self.sample(0.)['mode'],'candidate')
        self.assertEqual(self.sample(.1)['mode'],'candidate')
        self.assertEqual(self.sample(.2)['mode'],'airborne')

    def test_lower_air_height_requires_longer_confirmation(self):
        for t in (0.,.2,.4):self.assertNotEqual(self.sample(t,(0.,.8,0.))['mode'],'airborne')
        self.assertEqual(self.sample(.7,(0.,.8,0.))['mode'],'airborne')

    def test_duplicate_observation_cannot_accumulate_air_time(self):
        self.sample(0.)
        self.assertEqual(self.sample(.8,stamp=0.)['mode'],'candidate')
        self.assertEqual(len(self.tracker.samples),1)

    def test_stale_and_future_observation_reset(self):
        for t,stamp in ((2.,0.),(0.,.2)):
            row=self.sample(t,stamp=stamp)
            self.assertEqual(row['reason'],'stale_or_invalid_observation')
            self.assertEqual(self.tracker.samples,[])

    def test_binding_change_requires_new_confirmation(self):
        self.sample(0.);self.assertEqual(self.sample(.2)['mode'],'airborne')
        self.assertEqual(self.sample(.3,binding=('room',2))['mode'],'candidate')
        self.assertEqual(len(self.tracker.samples),1)

    def test_teleport_resets_velocity_and_air_age(self):
        self.sample(0.)
        row=self.sample(.1,(50.,1.5,0.))
        self.assertNotEqual(row['mode'],'airborne')
        self.assertEqual(row['velocity'],(0.,0.,0.))

    def test_landing_needs_stable_ground_window(self):
        self.sample(0.);self.sample(.2)
        modes=[self.sample(.3+i*.1,(0.,0.,0.))['mode'] for i in range(23)]
        self.assertNotEqual(modes[0],'grounded')
        self.assertEqual(modes[-1],'grounded')

    def test_unknown_support_cannot_start_but_confirmed_can_continue(self):
        unsupported=NS(hits=lambda x,z:[])
        row=self.tracker.observe(0.,(0.,4.,0.),unsupported,binding=1)
        self.assertEqual(row['mode'],'unknown')
        self.sample(.1);self.sample(.3)
        row=self.tracker.observe(.4,(0.,4.,0.),unsupported,binding=('room',1))
        self.assertEqual(row['mode'],'airborne')

    def test_support_does_not_choose_overhead_or_steep_surface(self):
        scene=NS(hits=lambda x,z:[(1,0.,1.),(2,4.,1.),(3,1.8,.2)])
        self.assertEqual(AF.support_below(scene,(0.,2.,0.)),0.)

    def test_history_is_bounded(self):
        for i in range(70):self.sample(i*.1)
        self.assertEqual(len(self.tracker.samples),32)


class StaticOverlapTests(unittest.TestCase):
    def setUp(self):
        self.scene=terrain(walls=[(0.,0.,4.)])
        self.start=(.1,0.,0.)
        self.end=(.4,0.,0.)
        self.phases=[phase(self.start,self.end)]

    def test_bounded_initial_contact_escape_proof(self):
        proof,reason=SR.validate_static_takeoff(self.scene,self.phases)
        self.assertIsNone(reason)
        self.assertEqual(proof['max_distance'],.5)
        self.assertEqual(proof['max_seconds'],.6)
        self.assertEqual(proof['capsule'],dict(radius=.25,height=1.5,skin=.015))

    def test_clear_start_does_not_receive_exemption(self):
        proof,reason=SR.validate_static_takeoff(self.scene,[phase(self.end,(1.,0.,0.))])
        self.assertIsNone(reason)
        self.assertIsNone(proof)

    def test_new_wall_is_not_initial_contact_exempt(self):
        scene=terrain(walls=[(0.,0.,4.),(.8,0.,4.)])
        proof,reason=SR.validate_static_takeoff(scene,[phase(self.start,(1.,0.,0.),.4)])
        self.assertIsNone(proof)
        self.assertEqual(reason,'static_overlap_new_contact')

    def test_initial_wall_cannot_be_crossed_inward(self):
        proof,reason=SR.validate_static_takeoff(self.scene,[phase(self.start,(-.4,0.,0.))])
        self.assertIsNone(proof)
        self.assertEqual(reason,'static_overlap_deeper_penetration')

    def test_cleared_contact_cannot_be_reentered(self):
        proof,reason=SR.validate_static_takeoff(self.scene,self.phases+[phase(self.end,self.start)])
        self.assertIsNone(proof)
        self.assertEqual(reason,'static_overlap_new_contact')

    def test_time_budget_rejects_slow_overlap(self):
        proof,reason=SR.validate_static_takeoff(self.scene,[phase(self.start,self.end,2.)])
        self.assertIsNone(proof)
        self.assertEqual(reason,'static_overlap_exit_budget')

    def test_distance_budget_rejects_parallel_overlap_escape(self):
        proof,reason=SR.validate_static_takeoff(self.scene,[phase(self.start,(.1,0.,1.),.3)])
        self.assertIsNone(proof)
        self.assertEqual(reason,'static_overlap_exit_budget')

    def test_dynamic_prop_not_granted_static_exemption(self):
        self.scene.surface.collider_ranges=[dict(start=2,count=2,id='prop-fixture')]
        _,reason=SR.validate_static_takeoff(self.scene,self.phases)
        self.assertEqual(reason,'static_overlap_dynamic_initial_contact')

    def test_frame_rechecks_geometry_and_position(self):
        proof,_=SR.validate_static_takeoff(self.scene,self.phases)
        plan=dict(kind='fly',start=self.start,goal=self.end,target=self.end,phases=self.phases,static_overlap_escape=proof)
        air=AM.AirMotion(plan,0.,clips=NS(pick=lambda key:None),terrain=self.scene)
        state,reason=SR.static_overlap_frame(self.scene,air,self.start,.1)
        self.assertIsNone(reason)
        _,reason=SR.static_overlap_frame(self.scene,air,(9.,0.,0.),.2,state)
        self.assertEqual(reason,'static_overlap_position_changed')
        self.scene.surface._tri[proof['initial_faces'][0],0,1]+=.1
        _,reason=SR.static_overlap_frame(self.scene,air,air.trajectory.pos(.1),.2,state)
        self.assertEqual(reason,'static_overlap_geometry_changed')

    def test_exact_capsule_view_and_ground_below(self):
        self.assertIsInstance(self.scene.capsule_probe(),CapsuleProbe)
        self.assertIsNone(AT.ground_below(self.scene,self.start[0],self.start[2],self.start[1]))
        self.assertEqual(AT.ground_below(self.scene,self.end[0],self.end[2],self.end[1]),0.)

    def test_two_wall_resolve_candidate_is_not_execution_permission(self):
        vertices=[(-20.,0.,-20.),(-20.,0.,20.),(20.,0.,20.),(20.,0.,-20.)]
        triangles=[(0,1,2),(0,2,3)]
        for x in (-.1,.1):
            i=len(vertices)
            vertices += [(x,-5.,-5.),(x,5.,-5.),(x,5.,5.),(x,-5.,5.)]
            triangles += [(i,i+1,i+2),(i,i+2,i+3)]
        scene=AT.TerrainView(GroundSurface(vertices,triangles))
        start=(0.,0.,0.)
        candidate=CapsuleProbe(scene).resolve(start)
        self.assertIsNotNone(candidate)
        proof,reason=SR.validate_static_takeoff(scene,[phase(start,tuple(candidate['point']))])
        self.assertIsNone(proof)
        self.assertEqual(reason,'static_overlap_deeper_penetration')
        with self.assertRaises(PL.PlanError):
            PL._try_capsule_fly(scene,start,(3.,2.,0.),(3.,0.,0.),6.)
        self.assertIsNone(AF.takeoff_plan(scene,start,observation((3.,2.,0.))))


class ContinuousMotionTests(unittest.TestCase):
    def setUp(self):
        self.scene=terrain()

    def test_real_takeoff_plan_ends_in_cruise(self):
        plan=AF.takeoff_plan(self.scene,(0.,0.,0.),observation())
        self.assertIsNotNone(plan)
        m=FM.AirFollowMotion(plan,0.,terrain=self.scene,clips=NS(pick=lambda key:None))
        for i in range(1,math.ceil(m.seconds/.05)+2):self.assertTrue(m.advance(i*.05)['ok'])
        self.assertEqual(m.follow_mode,'cruise')

    def test_supported_descent_plans_without_walk_failure(self):
        vertices=[(-20.,0.,-20.),(20.,0.,-20.),(20.,0.,20.),(-20.,0.,20.),
                  (-1.,4.,-2.),(1.,4.,-2.),(1.,4.,2.),(-1.,4.,2.)]
        scene=AT.TerrainView(GroundSurface(vertices,[(0,1,2),(0,2,3),(4,5,6),(4,6,7)]))
        # Exercise the no-edge branch; final path still uses real capsule geometry.
        with patch.object(PL,'_front_edge',return_value=None):
            result=PL.plan(scene,(0.,4.,0.),(8.,0.,0.),terrain_failed_seconds=0.)
        self.assertEqual(result['kind'],'fly',result)
        self.assertLess(result['landing'][1],4.)
        self.assertEqual(result['landing_validation'],'motor_capsule_0.25_1.5_skin_0.015')
        self.assertIsNone(SR.validate_static_takeoff(scene,result['phases'])[1])

    def test_same_object_cruises_with_speed_acceleration_bounds(self):
        m=motion(self.scene);identity=id(m);previous=m.current_velocity;now=.05
        for i in range(160):
            now+=.05
            target=(8.*math.sin(i/50.),4.+.5*math.sin(i/30.),8.*math.cos(i/50.))
            result=m.advance(now,observation=observation(target,now))
            self.assertTrue(result['ok'],result)
            self.assertEqual(id(m),identity)
            self.assertEqual(m.follow_mode,'cruise')
            v=m.current_velocity
            self.assertLessEqual(math.hypot(v[0],v[2]),6.5+1e-6)
            self.assertLessEqual(abs(v[1]),3.5+1e-6)
            self.assertLessEqual(math.dist(v,previous),6.*.05+1e-6)
            previous=v
        self.assertFalse(m.arrived(now+1000))

    def test_future_read_does_not_advance_or_land(self):
        m=motion(self.scene);position=m.pos(.05)
        self.assertEqual(m.pos(1000.),position)
        self.assertFalse(m.arrived(1000.))

    def test_cruise_history_and_long_lease_are_bounded(self):
        m=motion(self.scene)
        for i in range(540):
            now=.1+i*.05
            result=m.advance(now,observation=observation((0.,3.,3.),now))
            self.assertTrue(result['ok'])
        self.assertEqual(len(m.trajectory.rows),512)
        self.assertGreaterEqual(m.lease_count,2)
        self.assertEqual(m.follow_mode,'cruise')

    def test_cancel_brakes_then_installs_validated_landing(self):
        m=motion(self.scene)
        for i in range(20):m.advance(.1+i*.05,observation=observation((8.,3.,0.)))
        m.cancel(1.05,reason='target_lost')
        now=1.05
        for i in range(30):now+=.05;self.assertTrue(m.advance(now)['ok'])
        self.assertLess(math.dist(m.current_velocity,(0.,0.,0.)),1e-6)
        plan=AF.landing_plan(self.scene,m.pos(now),m.current_velocity)
        self.assertIsNotNone(plan)
        self.assertTrue(m.install_landing(plan,now,'target_lost'))
        end=now+m.seconds
        while now<end:now=min(end,now+.05);self.assertTrue(m.advance(now)['ok'])
        self.assertTrue(m.arrived(now))
        self.assertAlmostEqual(m.pos(now)[1],0.)

    def test_stale_landing_start_is_rejected(self):
        m=motion(self.scene)
        plan=AF.landing_plan(self.scene,m.pos(.05),m.current_velocity)
        plan['start']=(99.,3.,0.)
        self.assertFalse(m.install_landing(plan,.05))

    def test_clock_gap_holds_last_committed_position(self):
        m=motion(self.scene);position=m.pos(.05)
        result=m.advance(1.)
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'],'air_follow_clock_gap')
        self.assertEqual(m.pos(1.),position)

    def test_missing_path_coverage_holds(self):
        m=motion(self.scene);position=m.pos(.05)
        result=m.advance(.1,path_check=lambda points:False)
        self.assertEqual(result['reason'],'air_follow_path_coverage_missing')
        self.assertEqual(m.pos(.1),position)

    def test_guidance_rejects_invalid_dt_and_velocity(self):
        g=AF.Guidance()
        self.assertEqual(g.step(self.scene,(0.,3.,0.),(0.,0.,0.),observation(),dt=.2)['reason'],'air_follow_step_budget')
        self.assertEqual(g.step(self.scene,(0.,3.,0.),(7.,0.,0.),observation())['reason'],'air_follow_velocity_budget')

    def test_query_budget_is_enforced(self):
        g=AF.Guidance(AF.Policy(point_budget=1))
        self.assertFalse(g.step(self.scene,(0.,3.,0.),(0.,0.,0.),observation())['clear'])

    def test_ordinary_cruise_promotion_preserves_pose_and_velocity(self):
        p=(0.,3.,0.);end=(8.,3.,0.)
        plan=dict(kind='fly',reason='target_above',start=p,goal=end,target=end,phases=[phase(p,end,4.,'cruise')])
        ordinary=AM.AirMotion(plan,0.,terrain=self.scene,clips=NS(pick=lambda key:None))
        promoted=FM.AirFollowMotion.from_cruise(ordinary,1.,terrain=self.scene,observation=observation())
        self.assertIsNotNone(promoted)
        self.assertEqual(promoted.pos(1.),ordinary.pos(1.))
        self.assertEqual(promoted.current_velocity,ordinary.trajectory.velocity(1.))
        self.assertEqual(promoted.follow_mode,'cruise')
        self.assertEqual(promoted.trajectory.animation_offset,1.)

    def test_recovery_seat_and_overlap_flights_not_promoted(self):
        for key in ('recovery','seat_exit','prop_platform_transfer','static_overlap_escape','prop_overlap_escape'):
            p=(0.,3.,0.);q=(8.,3.,0.)
            plan=dict(kind='fly',start=p,goal=q,target=q,phases=[phase(p,q,4.,'cruise')],**{key:True})
            old=AM.AirMotion(plan,0.,terrain=self.scene,clips=NS(pick=lambda name:None))
            self.assertIsNone(FM.AirFollowMotion.from_cruise(old,1.,terrain=self.scene,observation=observation()))

    def test_render_reuses_actor_format_without_advancing(self):
        m=motion(self.scene)
        body=(ROOT/'tests/fixtures/actor_0168_candle.bin').read_bytes()
        position=m.pos(.05)
        result=m.render(body,1000.)
        self.assertEqual(len(result),len(body))
        self.assertEqual(m.pos(1000.),position)


class RuntimeOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.scene=terrain();self.c=controller(self.scene)

    def start(self):
        RT._observe(self.c,10.,self.scene)
        self.c.tracker.position_observed[7]=(0,10.2)
        with patch.object(RT,'_now',return_value=10.2):return RT.maybe_start(self.c,self.scene,'fixture',10.2)

    def test_start_uses_runtime_motion_factory(self):
        self.assertTrue(self.start())
        self.assertIsInstance(self.c.motion,FM.AirFollowMotion)
        self.assertEqual(self.c.motion._runtime_target,'synthetic-target')

    def test_social_owner_blocks_start(self):
        self.c.social=object()
        self.assertFalse(self.start());self.assertIsNone(self.c.motion)

    def test_seat_owner_blocks_start(self):
        self.c.sitting=object()
        self.assertFalse(self.start());self.assertIsNone(self.c.motion)

    def test_animation_owner_blocks_start(self):
        self.c.anim_lock.acquire()
        self.assertFalse(self.start());self.assertIsNone(self.c.motion)

    def test_candle_and_hop_owner_block_start(self):
        self.c.candle=object();self.assertFalse(self.start())
        self.c.candle=None;self.c._hop_running=lambda:True
        self.assertFalse(self.start())

    def test_continuous_emote_start_is_deferred_to_original_exit_gate(self):
        c=RP.PoseController.__new__(RP.PoseController)
        c.social=None;c._active_running=lambda:False
        c.event=AP.ContinuousEmoteEvent(100,101,{'gen':1,'key':0,'start':0},0)
        c._solo_gate=None;called=[]
        c._solo_defer=lambda function,args,kwargs:called.append(function.__name__) or True
        c._air_ground_start=lambda *args:self.fail('continuous emote must exit before takeoff')
        self.assertTrue(RP.PoseController._air_start(c,{},10.))
        self.assertEqual(called,['_air_start'])

    def test_priority_owner_retires_existing_air_session(self):
        m=motion(self.scene,9.95);snap=bind(self.c,m)
        for name,value in (('social',object()),('sitting',object()),('_solo_gate',object()),('holding',True)):
            setattr(self.c,name,value)
            self.assertEqual(RT._retire_reason(self.c,m,10.,snap,None),'priority_owner')
            setattr(self.c,name,False if name=='holding' else None)

    def test_target_stale_or_slot_replaced_retires(self):
        m=motion(self.scene,9.95);bind(self.c,m)
        snap,why=RT._snapshot(self.c,12.)
        self.assertEqual(RT._retire_reason(self.c,m,12.,snap,why),'target_stale')
        self.c.tracker._slot_versions[7]+=1
        snap,why=RT._snapshot(self.c,10.)
        self.assertEqual(RT._retire_reason(self.c,m,10.,snap,why),'target_binding_changed')

    def test_room_change_releases_without_old_coordinate_write(self):
        m=motion(self.scene,9.95);bind(self.c,m)
        self.c.tracker.room_generation+=1;self.c.position=(90.,0.,90.)
        self.assertFalse(RT.tick(self.c,10.1))
        self.assertIsNone(self.c.motion)
        self.assertEqual(self.c.position,(90.,0.,90.))

    def test_target_map_change_and_follow_change_retires(self):
        m=motion(self.scene,9.95);snap=bind(self.c,m)
        changed=dict(snap,level_change=10.)
        self.assertEqual(RT._retire_reason(self.c,m,10.,changed,None),'target_map_changed')
        self.assertEqual(RT._retire_reason(self.c,m,10.,dict(snap,target='new'),None),'target_changed')

    def test_lock_miss_uses_only_short_validated_lease(self):
        m=motion(self.scene,9.95);snap=bind(self.c,m)
        RT._state(self.c)['snapshot']=snap
        self.assertIsNone(RT._retire_reason(self.c,m,10.2,None,'tracker_busy'))
        self.assertEqual(RT._retire_reason(self.c,m,12.,None,'tracker_busy'),'tracker_busy')

    def test_logs_are_rate_limited_and_key_memory_bounded(self):
        for i in range(20):RT._log(self.c,'event',i*.1,'same')
        self.assertEqual(len(self.c.logs),1)
        RT._log(self.c,'event',5.,'same');self.assertEqual(len(self.c.logs),2)
        for i in range(30):RT._log(self.c,'event',10.,str(i))
        self.assertEqual(len(RT._state(self.c)['logs']),16)

    def test_hot_actor_frame_does_not_search_or_read_config(self):
        m=motion(self.scene,9.95);bind(self.c,m)
        RT._observe(self.c,10.,self.scene)
        body=(ROOT/'tests/fixtures/actor_0168_candle.bin').read_bytes()
        with patch.object(AF,'landing_plan',side_effect=AssertionError('cold search in hot frame')), \
                patch('builtins.open',side_effect=AssertionError('hot IO')):
            result=RT.frame(self.c,m,body,10.05)
        self.assertIsNotNone(result)
        self.assertIs(self.c.motion,m)


if __name__ == '__main__':
    unittest.main()
