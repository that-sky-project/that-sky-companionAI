"""Prepare Join profile and Engine input from one current private capture."""
from dataclasses import dataclass,field
import hashlib
import re
from qfsky.join0167_profile import account_binding,validate_record,NATIVE_SHA256,BINDING_ALGORITHM
from .load_current_capture import load_current_capture,StoredCaptureScope,stored_capture_metadata
from .map_long_engine import validate_inputs

@dataclass(frozen=True,repr=False)
class PreparedCapture:
    profile:object=field(repr=False)
    inputs:object=field(repr=False)
    def __repr__(self):return '<map_long0167.PreparedCapture private same-capture inputs>'
    def metadata(self):
        return {**stored_capture_metadata(self.inputs),'join_profile_and_engine_same_capture':True,
            'join_player_fields_current_capture':True,'template_fallback_used':False,
            'preparation_ready':True,'server_application_verified':False}

def _prepare_current_capture(capture_root,record_dir,*,expected_account_uuid,expected_scene,
        expected_resource_revision,expected_level_id,expected_netversion,apk_build=421589):
    if type(apk_build)is not int or apk_build!=421589:raise ValueError('map_capture_build_mismatch')
    inputs=load_current_capture(capture_root,record_dir,
        expected_account_binding=account_binding(expected_account_uuid),expected_scene=expected_scene,
        expected_resource_revision=expected_resource_revision,expected_level_id=expected_level_id,
        expected_netversion=expected_netversion)
    scope=inputs._context.world_generation
    if type(scope)is not StoredCaptureScope or scope.join_fields is None:
        raise ValueError('map_capture_join_fields_missing')
    spawn,matrix,flag=scope.join_fields
    binding,scene,level,nv=scope.scene_observation
    record={'schema':1,'kind':'sky-native-level-cache','classification':'private-current-account-session-state',
        'apk_build':421589,'native_sha256':NATIVE_SHA256,'scene':scene,'resource_revision':expected_resource_revision,
        'level_id':level,'netversion_u16':list(nv),'cache':{'sha256':hashlib.sha256(inputs.cache_bytes()).hexdigest()},
        'account_binding':{'algorithm':BINDING_ALGORITHM,'source':'native-join-builder-uid16',
                           'value':inputs._context.account_binding_hash},
        'player_fields':{'spawn_hex':spawn.hex(),'matrix_hex':matrix.hex(),'level_flag':flag},
        'scene_observation':{'binding':binding,'scene':scene,'level_id':level,'netversion_u16':list(nv)},
        'derived_from_current_capture_record_sha256':scope.record_sha256}
    profile=validate_record(record,inputs.cache_bytes(),scene=expected_scene,
        resource_revision=expected_resource_revision,account_uuid=expected_account_uuid,apk_build=apk_build)
    validate_inputs(inputs,account_uuid=expected_account_uuid,scene=expected_scene,
        resource_revision=expected_resource_revision,level_id=expected_level_id,netversion=expected_netversion)
    if profile.blob!=inputs.scene_bytes()or profile.cache!=inputs.cache_bytes():
        raise ValueError('map_capture_join_input_disagreement')
    return PreparedCapture(profile,inputs)

class PreparationError(ValueError):
    """Stable public error code; never a private filesystem or payload repr."""
    def __init__(self,code):self.code=code;super().__init__(code)

def prepare_current_capture(capture_root,record_dir,*,expected_account_uuid,expected_scene,
        expected_resource_revision,expected_level_id,expected_netversion,apk_build=421589):
    try:
        return _prepare_current_capture(capture_root,record_dir,expected_account_uuid=expected_account_uuid,
            expected_scene=expected_scene,expected_resource_revision=expected_resource_revision,
            expected_level_id=expected_level_id,expected_netversion=expected_netversion,apk_build=apk_build)
    except OSError:
        raise PreparationError('map_capture_io_error')from None
    except (ValueError,TypeError,KeyError,IndexError,UnicodeError)as error:
        message=str(error)
        code=message if re.fullmatch(r'(?:stored_capture|map_capture|map_long|current_actor|map_scene)_[a-z0-9_]{1,90}',message)else'map_capture_validation_failed'
        raise PreparationError(code)from None
