"""In-memory real-payload offline exercise; synthetic Enter, no transport.

The caller supplies a currently verified UUID and validated CurrentInputs.
Nothing reads files/devices, stores the UUID, logs payloads or contacts servers.
This is not proof of a new server admission or 180 seconds of real online time.
"""
import struct
from .current_actor_update import _checked
from .map_long_engine import MapLongEngine
from qfsky.join0167_profile import NATIVE_SHA256
from qfsky.long0167.delta_sync import _fnv8


def exercise_current_inputs(inputs,account_uuid):
    context=inputs._context
    synthetic_enter=bytes((1,5))+bytes(account_uuid)+bytes(16)+struct.pack('<I',context.level_id)
    engine=MapLongEngine(account_uuid=bytes(account_uuid),enter_game_body=synthetic_enter,
        generation=1,now=0,inputs=inputs,scene=context.scene,resource_revision=context.resource_revision,
        level_id=context.level_id,netversion=inputs._netversion,live_check=lambda:True)
    # This TRUE is an explicit offline test lifetime, not a production WSS or
    # transport claim. No transport is constructed and no frame leaves memory.
    actor_frames=controller_frames=groups=0
    try:
        for now in(0,.090,.091,.212,.333,1.0,180.0):
            group=engine.tick(now)
            if group is None:continue
            for frame in group.frames:
                if frame.outer_type!=14 or frame.channel!=0:raise AssertionError('unexpected offline frame')
                body=frame.payload
                if body[0]==3:
                    if body[1:3]!=b'\x01\x00'or body[4]!=0 or body[5]!=_fnv8(body[6:]):
                        raise AssertionError('offline Actor header')
                    _checked(body[6:],context.level_id,NATIVE_SHA256)
                    actor_frames+=1
                elif body[0]==12:
                    if struct.unpack_from('<I',body,5)[0]!=context.level_id:
                        raise AssertionError('offline controller map')
                    controller_frames+=1
                else:raise AssertionError('unexpected role-free offline subtype')
            engine.commit(group,len(group.frames));groups+=1
        result=engine.metadata()
        if result['actor_frames']!=actor_frames or result['controller_frames']!=controller_frames:
            raise AssertionError('offline commit counts')
        return {'passed':True,'caller_supplied_current_payloads_exercised':True,
            'synthetic_enter_used':True,'synthetic_role_used':False,'network_used':False,
            'transport_created':False,'server_admission_proven':False,'real_180_seconds_elapsed':False,
            'actor_frames_roundtrip_checked':actor_frames,'controller_frames_checked':controller_frames,
            'groups_committed':groups,'actor_extensions':result['actor_extensions'],
            'scene_bytes':len(inputs.scene_bytes()),'actor_bytes':len(inputs.actor_bytes()),
            'scene_role_branch_exercised':False,'full_native_actor_producer':False}
    finally:
        engine.close()
