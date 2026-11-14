"""Typed Actor seat attachment codec; no network, files, clocks or controller ownership.

Native quantization bounds/zero-centered rounding are pinned to 0.16.8.
TableXmas steady pose formulas match captured side seats; optional anchor semantics
remain unknown. Call upright only while releasing an explicitly owned seat state.
"""
from bisect import bisect_left
import copy
from dataclasses import replace
import math
import struct
import anim_pose
from qfsky.long0167.actor_codec.actor_wire_0167 import inspect_actor_wire,MAX_ACTOR_WIRE_BYTES
from qfsky.long0167.actor_codec.bit_stream import BitWriter

PI32=struct.unpack('<f',bytes.fromhex('db0f4940'))[0]
TAU32=struct.unpack('<f',bytes.fromhex('db0fc940'))[0]
MAIN_GROUPS={'yaw1':('0x21dd1dc','bits',6,3),'yaw2':('0x21dd234','bits',6,3),
 'forward':('0x21dd350','bits',5,3),'attachment_kind':('0x21deffc','bits',2,1),
 'attachment_id':('0x21df014','raw',4,1),'local_origin':('0x21df04c','raw',4,3)}
POSE_GROUPS={'position_codes':('0x1716078',12,3),'yaw_code':('0x1716a64',10,1),
 'left_weight':('0x1715f48',3,1),'right_weight':('0x1715f70',3,1)}
SEAT_GROUPS={'seated':('0x21dd820',1,1),'sit_enter':('0x21dd830',1,1),
 'seat':('0x21dd858',13,6657),'avatar_state':('0x21de944',5,25)}


def _f32(value):
 if isinstance(value,bool) or not isinstance(value,(int,float)):
  raise ValueError('seat_nonfinite_number')
 try:
  if not math.isfinite(value):raise ValueError('seat_nonfinite_number')
  return struct.unpack('<f',struct.pack('<f',value))[0]
 except (OverflowError,struct.error):raise ValueError('seat_float_range') from None


def _vector(value,n,limit=10000.):
 if not isinstance(value,(list,tuple)) or len(value)!=n:raise ValueError('seat_vector_shape')
 out=tuple(_f32(x) for x in value)
 if any(abs(x)>limit for x in out):raise ValueError('seat_vector_range')
 return out


def quantize(value,bits,limit):
 """Native 0x32626b4, default=0, symmetric float32 bounds, +.5 truncate.
 Negative and positive halves use different level counts; zero is exact mid.
 """
 if type(bits) is not int or not 1<=bits<=16:raise ValueError('seat_quant_width')
 x,limit=_f32(value),_f32(limit)
 if limit<=0:raise ValueError('seat_quant_limit')
 mid=(1<<(bits-1))-1;top=(1<<bits)-1
 if x<0:
  factor=max(0.,min(1.,_f32(_f32(x+limit)/limit)))
  code=int(_f32(factor*mid+.5))
 else:
  factor=max(0.,min(1.,_f32(x/limit)))
  code=int(_f32(factor*(top-mid)+_f32(mid+.5)))
 return max(0,min(top,code))


def dequantize(code,bits,limit):
 if type(bits) is not int or not 1<=bits<=16:raise ValueError('seat_quant_width')
 if type(code) is not int or not 0<=code<(1<<bits):raise ValueError('seat_quant_code')
 mid=(1<<(bits-1))-1;top=(1<<bits)-1;limit=_f32(limit)
 if limit<=0:raise ValueError('seat_quant_limit')
 if code<=mid:return _f32(-limit+_f32(_f32(limit*code)/mid)) if mid else 0.
 return _f32(_f32(limit*(code-mid))/(top-mid))


def position_codes(pose_position,reference_position):
 p=_vector(pose_position,3);r=_vector(reference_position,3)
 d=[_f32(a-b) for a,b in zip(p,r)]
 squares=[_f32(v*v) for v in d]
 length=_f32(math.sqrt(_f32(_f32(squares[0]+squares[1])+squares[2])))
 if length>.001:
  scale=min(1.,_f32(20./length));d=[_f32(v*scale) for v in d]
 return [quantize(v,12,20.) for v in d]


def angle_delta_code(angle,reference):
 d=_f32(_f32(angle)-_f32(reference));turns=math.floor(_f32(_f32(d+PI32)/TAU32))
 d=_f32(d-turns*TAU32);d=max(-PI32,min(PI32,d))
 return quantize(d,10,PI32)


def _layout(body):
 o=inspect_actor_wire(body,verified_profile_sha256=anim_pose.SHA,check_zero_padding=False)
 starts=anim_pose._starts(o);bounds={}
 for label,pos in o._boundaries:bounds.setdefault(label,[]).append(pos)
 def group(label,kind,width,count=1,scope=None):
  poses=bounds.get(label,[])
  if scope:
   a,b=(bounds[anim_pose.CURRENT][0],bounds[anim_pose.ANIM_BLOCK][0]) if scope=='current' else (bounds[anim_pose.ANIM_BLOCK][0],bounds['0x21deffc'][0])
   poses=[v for v in poses if a<=v<b]
  result=[]
  for pos in poses:
   i=bisect_left(starts,pos);ids=list(range(i,i+count))
   if len(o._tokens)<i+count or any(o._tokens[k].kind!=kind or o._tokens[k].extent!=width for k in ids):
    raise ValueError('seat_token_shape_'+label)
   result.append(ids)
  if scope is None and len(result)!=1:raise ValueError('seat_label_not_unique_'+label)
  return result
 loc={name:group(*info)[0] for name,info in MAIN_GROUPS.items()}
 for scope in ('current','history'):
  pose={name:group(label,'bits',width,count,scope) for name,(label,width,count) in POSE_GROUPS.items()}
  count=dict(o._counts)['pose_records' if scope=='current' else 'history_pose_records']
  if any(len(v)!=count for v in pose.values()):raise ValueError('seat_pose_count')
  loc['pose_'+scope]=[{name:indices[i] for name,indices in pose.items()} for i in range(count)]
 loc['seat']={name:group(label,'bits',bits)[0][0] for name,(label,bits,_) in SEAT_GROUPS.items()}
 flag=group('0x21df190','bits',1)[0][0];loc['optional_flag']=flag
 enabled=o._tokens[flag].value
 if enabled:
  loc['optional_entity']=group('0x21df1a8','raw',1)[0][0]
  loc['optional_position']=group('0x21df1e0','raw',4,3)[0]
  if loc['optional_entity']!=flag+1 or loc['optional_position']!=list(range(flag+2,flag+5)):
   raise ValueError('seat_optional_shape')
 return o,starts,loc


def observe(body):
 o,starts,loc=_layout(body);result={}
 for name,ids in loc.items():
  if name.startswith('optional') or name in ('pose_current','pose_history','seat'):continue
  values=[]
  for i in ids:
   t=o._tokens[i]
   values.append(struct.unpack('<f',t.value)[0] if name=='local_origin' else int.from_bytes(t.value,'little') if t.kind=='raw' else t.value)
  result[name]=values[0] if len(values)==1 else values
 for scope in ('pose_current','pose_history'):
  result[scope]=[]
  for groups in loc[scope]:
   row={name:[o._tokens[i].value for i in ids] for name,ids in groups.items()}
   result[scope].append({k:v[0] if len(v)==1 else v for k,v in row.items()})
 result['seat']={name:o._tokens[i].value for name,i in loc['seat'].items()}
 result['optional_anchor']=None
 if o._tokens[loc['optional_flag']].value:
  result['optional_anchor']=dict(entity=int.from_bytes(o._tokens[loc['optional_entity']].value,'little'),
   position=[struct.unpack('<f',o._tokens[i].value)[0] for i in loc['optional_position']])
 f=anim_pose.read(body)
 result.update(origin=[f[k] for k in ('ox','oy','oz')],history_origin=[f[k] for k in ('ax','ay','az')],
  source='observed_actor_fields',computed=False)
 return result


def _code(value,maximum):
 if type(value) is not int or not 0<=value<=maximum:raise ValueError('seat_code_range')
 return value


def apply(body,profile):
 """Change only explicit seat groups; legal optional group insertion/removal.
 Existing record counts, animation clocks, social fields and opaque tokens survive.
 """
 if type(profile) is not dict:raise ValueError('seat_profile_shape')
 required=set(MAIN_GROUPS)|{'pose_current','pose_history','seat','optional_anchor'}
 if not required<=profile.keys() or type(profile['seat']) is not dict or not set(SEAT_GROUPS)<=profile['seat'].keys():
  raise ValueError('seat_profile_missing_fields')
 o,starts,loc=_layout(body);tokens=list(o._tokens);changed=set()
 def put(i,value,kind=None):
  t=tokens[i]
  if t.kind=='raw':
   raw=struct.pack('<f',_vector([value],1)[0]) if kind=='float' else _code(value,(1<<(8*t.extent))-1).to_bytes(t.extent,'little')
   tokens[i]=replace(t,value=raw)
  else:tokens[i]=replace(t,value=_code(value,(1<<t.extent)-1))
  changed.add(i)
 for name,info in MAIN_GROUPS.items():
  value=profile[name];values=value if len(loc[name])>1 else [value]
  if not isinstance(values,(list,tuple)) or len(values)!=len(loc[name]):raise ValueError('seat_group_shape_'+name)
  for i,v in zip(loc[name],values):put(i,v,'float' if name=='local_origin' else None)
 for scope in ('pose_current','pose_history'):
  values=profile[scope]
  if not isinstance(values,list):raise ValueError('seat_pose_shape')
  if profile.get('computed') is not True and len(values)!=len(loc[scope]):raise ValueError('seat_pose_count_mismatch')
  for n,groups in enumerate(loc[scope]):
   if not values:raise ValueError('seat_pose_missing')
   row=values[min(n,len(values)-1)]
   for name,ids in groups.items():
    vals=row[name] if len(ids)>1 else [row[name]]
    if not isinstance(vals,(list,tuple)) or len(vals)!=len(ids):raise ValueError('seat_pose_group_shape')
    for i,v in zip(ids,vals):put(i,v)
 for name,i in loc['seat'].items():
  value=_code(profile['seat'][name],SEAT_GROUPS[name][2]);put(i,value)
 anchor=profile['optional_anchor'];flag=loc['optional_flag'];old_count=4 if 'optional_entity' in loc else 0
 put(flag,0 if anchor is None else 1)
 pairs=[(t,i) for i,t in enumerate(tokens)]
 if anchor is None:new=[]
 else:
  if type(anchor) is not dict:raise ValueError('seat_optional_profile')
  entity=_code(anchor['entity'],255);point=_vector(anchor['position'],3)
  raw=next(t for t in tokens if t.kind=='raw')
  new=[(replace(raw,kind='raw',extent=1,value=bytes([entity])),loc.get('optional_entity'))]
  new += [(replace(raw,kind='raw',extent=4,value=struct.pack('<f',v)),loc.get('optional_position',[None]*3)[j]) for j,v in enumerate(point)]
 pairs[flag+1:flag+1+old_count]=new
 writer=BitWriter(max_bytes=MAX_ACTOR_WIRE_BYTES)
 def bits_at(pos,width):
  return sum(((body[(pos+k)//8]>>((pos+k)%8))&1)<<k for k in range(width))
 for token,old in pairs:
  if token.kind=='raw':
   pad=(-writer.bit_position)%8
   if pad:
    before=0 if old is None or old==0 else starts[old-1]+(o._tokens[old-1].extent*8 if o._tokens[old-1].kind=='raw' else o._tokens[old-1].extent)
    oldpad=0 if old is None else starts[old]-before
    writer.write_bits(bits_at(before,min(pad,oldpad)) if oldpad else 0,pad)
   writer.write_raw(token.value)
  else:writer.write_bits(token.value,token.extent)
 tail=(-writer.bit_position)%8
 if tail:writer.write_bits(bits_at(o.bit_length,min(tail,len(body)*8-o.bit_length)),tail)
 result=writer.to_bytes()
 chk=inspect_actor_wire(result,verified_profile_sha256=anim_pose.SHA,check_zero_padding=False)
 if chk._tokens!=tuple(t for t,_ in pairs):raise ValueError('seat_reencode_mismatch')
 # Explicitly audit untouched typed fields, including every opaque token.
 for token,old in pairs:
  if old is not None and old not in changed and old not in range(flag+1,flag+1+old_count) and token!=o._tokens[old]:
   raise ValueError('seat_unrelated_token_changed')
 return result


def upright_profile(profile):
 """Explicit seat-owner release, not a global social/movement sanitizer."""
 result=copy.deepcopy(profile);result['computed']=True
 result['yaw1'][1:]=[31,31];result['yaw2'][1:]=[31,31]
 yaw=_code(result['yaw1'][0],63)
 heading=dequantize(yaw,6,PI32)
 calculation=profile.get('calculation')
 if isinstance(calculation,dict):
  candidate=calculation.get('world_heading')
  if isinstance(candidate,(int,float)) and not isinstance(candidate,bool) and math.isfinite(candidate) and quantize(candidate,6,PI32)==yaw:
   heading=candidate
 result['forward']=[quantize(math.sin(heading),5,1.),15,quantize(math.cos(heading),5,1.)]
 for key in ('pose_current','pose_history'):
  result[key]=[dict(position_codes=[2047]*3,yaw_code=511,left_weight=0,right_weight=0)]
 result.update(attachment_kind=0,attachment_id=0xffffffff,local_origin=[0.,0.,0.],optional_anchor=None)
 result['seat']=dict(seated=0,sit_enter=0,seat=0,avatar_state=2)
 result['source']='explicit_seat_owner_upright'
 return result


def upright(body):
 return apply(body,upright_profile(observe(body)))


def _rotation(q):
 x,y,z,w=_vector(q,4,2.);norm=math.sqrt(x*x+y*y+z*z+w*w)
 if not .8<=norm<=1.2:raise ValueError('seat_quaternion_norm')
 x,y,z,w=(v/norm for v in (x,y,z,w))
 return ((1-2*(y*y+z*z),2*(x*y-z*w),2*(x*z+y*w)),
  (2*(x*y+z*w),1-2*(x*x+z*z),2*(y*z-x*w)),
  (2*(x*z-y*w),2*(y*z+x*w),1-2*(x*x+y*y)))


def _dot(a,b):return sum(x*y for x,y in zip(a,b))


def derive(prop,local_origin,actor_origin,local_heading,*,evidence,definition_scale=.075,inferred_rig=False):
 """Computed steady TableXmas side-seat pose, evidence-bound optional metadata.
 Native encoding is proven; inverse-horizontal-yaw and R decomposition are
 empirically verified for captured marker0/3, not arbitrary unknown seat rigs.
 """
 if prop.get('name') not in (('TableXmas','MischiefTable') if inferred_rig else ('TableXmas',)):
  raise ValueError('seat_rig_unverified')
 if (type(evidence) is not dict or evidence.get('attachment_kind')!=2
     or (not inferred_rig and evidence.get('optional_anchor') is None)):
  raise ValueError('seat_live_attachment_evidence_required')
 if prop.get('tag') is not None:
  try:tag=int.from_bytes(bytes.fromhex(prop['tag']),'little')
  except (ValueError,TypeError):raise ValueError('seat_prop_tag') from None
  if tag!=evidence['attachment_id']:raise ValueError('seat_attachment_mismatch')
 local=_vector(local_origin,3,1000.);origin=_vector(actor_origin,3);heading=float(local_heading)
 if not math.isfinite(heading):raise ValueError('seat_heading')
 heading=(heading+math.pi)%(2*math.pi)-math.pi
 if not inferred_rig and abs(abs(heading)-math.pi/2)>1e-4:raise ValueError('seat_marker_heading_unverified')
 scale=_f32(definition_scale);rpc_scale=_f32(prop.get('scale',1.))
 if not 0<scale<=10 or not .05<=rpc_scale<=20:raise ValueError('seat_scale')
 R=_rotation(prop['rotation']);local_f=(math.sin(heading),0.,math.cos(heading))
 f=[_dot(row,local_f) for row in R];up=[row[1] for row in R]
 h=math.atan2(f[0],f[2]);pitch=math.asin(max(-1.,min(1.,f[1])))
 right=(math.cos(h),0.,-math.sin(h));flat_up=(-math.sin(pitch)*math.sin(h),math.cos(pitch),-math.sin(pitch)*math.cos(h))
 roll=-math.atan2(_dot(up,right),_dot(up,flat_up))
 horizontal=(math.sin(h),0.,math.cos(h));inverse=[sum(R[j][i]*horizontal[j] for j in range(3)) for i in range(3)]
 pose_yaw=math.atan2(inverse[0],inverse[2])
 pos=[_f32(_f32(v*scale)*rpc_scale) for v in local]
 result=copy.deepcopy(evidence);result.update(computed=True,source='computed_TableXmas_side_seat',
  yaw1=[quantize(v,6,PI32) for v in (h,pitch,roll)],yaw2=[quantize(h,6,PI32),quantize(pitch,6,PI32),31],
  forward=[quantize(v,5,1.) for v in f],local_origin=list(local),origin=list(origin))
 row=dict(position_codes=position_codes(pos,origin),yaw_code=angle_delta_code(pose_yaw,h),left_weight=7,right_weight=7)
 result['pose_current']=[row]
 result['pose_history']=[copy.deepcopy(row)]
 result['calculation']=dict(world_heading=h,pitch=pitch,roll=roll,pose_local_heading=pose_yaw,
  pose_local_scaled=pos,optional_anchor_semantics='unknown_dynamic_observation_not_stand_target')
 if inferred_rig:
  result['source']='inferred_GoSit2_marker_rig'
  result['optional_anchor']=None
  result['calculation'].update(rig_inferred=True,native_marker_pose_verified=False,
   optional_anchor_semantics='unknown_not_synthesized')
 return result


# Exact captured second-chair GetUp; not transferable world/anchor geometry.
GETUP_SECONDS = 1.143
_CAPTURE_START = {'attachment_id': 78,
 'attachment_kind': 2,
 'computed': False,
 'forward': [17, 27, 25],
 'history_origin': [-12.87668228149414, 0.6682469844818115, -18.098020553588867],
 'local_origin': [-13.3330078125, 9.397469520568848, 1.52587890625e-05],
 'optional_anchor': {'entity': 0, 'position': [-12.678356170654297, 1.525481939315796, -17.496431350708008]},
 'origin': [-12.87668228149414, 0.6682469844818115, -18.098020553588867],
 'pose_current': [{'left_weight': 7, 'position_codes': [3171, 2050, 3759], 'right_weight': 7, 'yaw_code': 864}],
 'pose_history': [{'left_weight': 7, 'position_codes': [3171, 2050, 3759], 'right_weight': 7, 'yaw_code': 864}],
 'seat': {'avatar_state': 15, 'seat': 5545, 'seated': 1, 'sit_enter': 0},
 'source': 'observed_actor_fields',
 'yaw1': [33, 40, 22],
 'yaw2': [33, 40, 31]}
_CAPTURE_GETUP = ({'elapsed': 0.0,
  'local_origin_delta': [0.27264404296875, 0.4029560089111328, 0.1778564453125],
  'origin_delta': [0.014265060424804688, 0.03444981575012207, -0.010915756225585938],
  'profile': {'attachment_id': 78,
              'attachment_kind': 2,
              'computed': False,
              'forward': [17, 26, 27],
              'history_origin': [-12.87668228149414, 0.6682469844818115, -18.098020553588867],
              'local_origin': [-13.06036376953125, 9.80042552947998, 0.1778717041015625],
              'optional_anchor': {'entity': 0,
                                  'position': [-12.76655387878418, 1.5294157266616821, -17.451711654663086]},
              'origin': [-12.862417221069336, 0.7026968002319336, -18.108936309814453],
              'pose_current': [{'left_weight': 0,
                                'position_codes': [3170, 2050, 3760],
                                'right_weight': 0,
                                'yaw_code': 864}],
              'pose_history': [{'left_weight': 7,
                                'position_codes': [3171, 2050, 3759],
                                'right_weight': 7,
                                'yaw_code': 864}],
              'seat': {'avatar_state': 2, 'seat': 0, 'seated': 0, 'sit_enter': 0},
              'source': 'observed_actor_fields',
              'yaw1': [33, 38, 31],
              'yaw2': [33, 38, 31]}},
 {'elapsed': 0.266,
  'local_origin_delta': [0.93609619140625, 0.7431125640869141, 0.628204345703125],
  'origin_delta': [0.013791084289550781, 0.09977400302886963, -0.010496139526367188],
  'profile': {'attachment_id': 78,
              'attachment_kind': 2,
              'computed': False,
              'forward': [17, 19, 30],
              'history_origin': [-12.862668991088867, 0.7452791929244995, -18.10872459411621],
              'local_origin': [-12.39691162109375, 10.140582084655762, 0.6282196044921875],
              'optional_anchor': {'entity': 0,
                                  'position': [-12.700884819030762, 1.2004344463348389, -17.01578712463379]},
              'origin': [-12.86289119720459, 0.7680209875106812, -18.108516693115234],
              'pose_current': [{'left_weight': 0,
                                'position_codes': [3172, 2046, 3758],
                                'right_weight': 0,
                                'yaw_code': 864}],
              'pose_history': [{'left_weight': 0,
                                'position_codes': [3171, 2048, 3759],
                                'right_weight': 0,
                                'yaw_code': 864}],
              'seat': {'avatar_state': 2, 'seat': 0, 'seated': 0, 'sit_enter': 0},
              'source': 'observed_actor_fields',
              'yaw1': [33, 33, 31],
              'yaw2': [33, 33, 31]}},
 {'elapsed': 0.376,
  'local_origin_delta': [1.0487518310546875, 0.7987117767333984, 0.704376220703125],
  'origin_delta': [0.01361083984375, 0.11078983545303345, -0.010320663452148438],
  'profile': {'attachment_id': 78,
              'attachment_kind': 2,
              'computed': False,
              'forward': [17, 17, 31],
              'history_origin': [-12.86289119720459, 0.7680209875106812, -18.108516693115234],
              'local_origin': [-12.284255981445312, 10.196181297302246, 0.7043914794921875],
              'optional_anchor': {'entity': 0,
                                  'position': [-12.700817108154297, 1.0698498487472534, -16.982563018798828]},
              'origin': [-12.86307144165039, 0.779036819934845, -18.108341217041016],
              'pose_current': [{'left_weight': 0,
                                'position_codes': [3172, 2046, 3758],
                                'right_weight': 0,
                                'yaw_code': 864}],
              'pose_history': [{'left_weight': 0,
                                'position_codes': [3172, 2046, 3758],
                                'right_weight': 0,
                                'yaw_code': 864}],
              'seat': {'avatar_state': 2, 'seat': 0, 'seated': 0, 'sit_enter': 0},
              'source': 'observed_actor_fields',
              'yaw1': [33, 32, 31],
              'yaw2': [33, 32, 31]}},
 {'elapsed': 0.597,
  'local_origin_delta': [1.0751190185546875, 0.8490676879882812, 0.782928466796875],
  'origin_delta': [0.012406349182128906, 0.11683386564254761, -0.014181137084960938],
  'profile': {'attachment_id': 78,
              'attachment_kind': 2,
              'computed': False,
              'forward': [17, 16, 31],
              'history_origin': [-12.863228797912598, 0.7855642437934875, -18.108186721801758],
              'local_origin': [-12.257888793945312, 10.246537208557129, 0.7829437255859375],
              'optional_anchor': {'entity': 0,
                                  'position': [-12.706334114074707, 0.9981909990310669, -16.99249267578125]},
              'origin': [-12.864275932312012, 0.7850808501243591, -18.112201690673828],
              'pose_current': [{'left_weight': 0,
                                'position_codes': [3172, 2045, 3758],
                                'right_weight': 0,
                                'yaw_code': 864}],
              'pose_history': [{'left_weight': 0,
                                'position_codes': [3172, 2045, 3758],
                                'right_weight': 0,
                                'yaw_code': 864}],
              'seat': {'avatar_state': 2, 'seat': 0, 'seated': 0, 'sit_enter': 0},
              'source': 'observed_actor_fields',
              'yaw1': [33, 31, 31],
              'yaw2': [33, 31, 31]}},
 {'elapsed': 0.727,
  'local_origin_delta': [0.7752227783203125, 0.7833099365234375, 0.71722412109375],
  'origin_delta': [0.010153770446777344, 0.09526968002319336, -0.02336883544921875],
  'profile': {'attachment_id': 78,
              'attachment_kind': 2,
              'computed': False,
              'forward': [17, 15, 31],
              'history_origin': [-12.864275932312012, 0.7850808501243591, -18.112201690673828],
              'local_origin': [-12.557785034179688, 10.180779457092285, 0.7172393798828125],
              'optional_anchor': {'entity': 0,
                                  'position': [-12.702930450439453, 0.9972550272941589, -16.986583709716797]},
              'origin': [-12.866528511047363, 0.7635166645050049, -18.121389389038086],
              'pose_current': [{'left_weight': 0,
                                'position_codes': [3170, 2047, 3759],
                                'right_weight': 0,
                                'yaw_code': 864}],
              'pose_history': [{'left_weight': 0,
                                'position_codes': [3172, 2045, 3758],
                                'right_weight': 0,
                                'yaw_code': 864}],
              'seat': {'avatar_state': 2, 'seat': 0, 'seated': 0, 'sit_enter': 0},
              'source': 'observed_actor_fields',
              'yaw1': [33, 31, 31],
              'yaw2': [33, 31, 31]}},
 {'elapsed': 1.015,
  'local_origin_delta': [13.3330078125, -9.397469520568848, -1.52587890625e-05],
  'origin_delta': [0.18072891235351562, -0.1468549370765686, 0.039886474609375],
  'profile': {'attachment_id': 4294967295,
              'attachment_kind': 0,
              'computed': False,
              'forward': [17, 15, 31],
              'history_origin': [-12.802459716796875, 0.5761299133300781, -18.13587188720703],
              'local_origin': [0.0, 0.0, 0.0],
              'optional_anchor': None,
              'origin': [-12.695953369140625, 0.5213920474052429, -18.058134078979492],
              'pose_current': [{'left_weight': 0,
                                'position_codes': [2047, 2047, 2047],
                                'right_weight': 0,
                                'yaw_code': 511}],
              'pose_history': [{'left_weight': 0,
                                'position_codes': [3162, 2063, 3765],
                                'right_weight': 0,
                                'yaw_code': 864}],
              'seat': {'avatar_state': 2, 'seat': 0, 'seated': 0, 'sit_enter': 0},
              'source': 'observed_actor_fields',
              'yaw1': [33, 31, 31],
              'yaw2': [33, 31, 31]}},
 {'elapsed': 1.143,
  'local_origin_delta': [13.3330078125, -9.397469520568848, -1.52587890625e-05],
  'origin_delta': [0.2864398956298828, -0.2759135365486145, 0.12924766540527344],
  'profile': {'attachment_id': 4294967295,
              'attachment_kind': 0,
              'computed': False,
              'forward': [21, 15, 30],
              'history_origin': [-12.695953369140625, 0.5213920474052429, -18.058134078979492],
              'local_origin': [0.0, 0.0, 0.0],
              'optional_anchor': None,
              'origin': [-12.590242385864258, 0.392333447933197, -17.968772888183594],
              'pose_current': [{'left_weight': 0,
                                'position_codes': [2047, 2047, 2047],
                                'right_weight': 0,
                                'yaw_code': 511}],
              'pose_history': [{'left_weight': 0,
                                'position_codes': [2047, 2047, 2047],
                                'right_weight': 0,
                                'yaw_code': 511}],
              'seat': {'avatar_state': 2, 'seat': 0, 'seated': 0, 'sit_enter': 0},
              'source': 'observed_actor_fields',
              'yaw1': [35, 31, 31],
              'yaw2': [35, 31, 31]}})



def getup_capture_sample(elapsed):
 """Exact step-sampled 04:10:31.255..32.398 capture, specific recorded binding.
 It carries recorded world/camera values for replay only, not arbitrary props.
 """
 age=float(elapsed)
 if not math.isfinite(age) or age<0:raise ValueError('seat_getup_age')
 row=next(row for row in reversed(_CAPTURE_GETUP) if row['elapsed']<=age)
 return dict(profile=copy.deepcopy(row['profile']),complete=age>=GETUP_SECONDS,
  sample_age=row['elapsed'],source='exact_capture_0408_second_chair',
  origin_delta=list(row['origin_delta']),local_origin_delta=list(row['local_origin_delta']))


def _capture_matches(profile):
 keys=('yaw1','yaw2','forward','pose_current','pose_history','attachment_kind','attachment_id',
       'local_origin','optional_anchor','seat','origin','history_origin')
 return all(profile.get(key)==_CAPTURE_START[key] for key in keys)


def getup_sample(profile,elapsed,*,allow_capture=True):
 """Owner-scoped engineering curve; not claimed native for arbitrary rotations.
 Attachment clears at 1.015s, historical pose at 1.143s. Generic curve suggests
 no world position: controller must pick/check the standing point independently.
 """
 age=float(elapsed)
 if not math.isfinite(age) or age<0:raise ValueError('seat_getup_age')
 if type(profile) is not dict:raise ValueError('seat_profile_shape')
 if allow_capture is True and _capture_matches(profile):return getup_capture_sample(age)
 if age>=GETUP_SECONDS:
  return dict(profile=upright_profile(profile),complete=True,sample_age=GETUP_SECONDS,
   source='engineering_curve_observed_timing',origin_delta=None,local_origin_delta=None)
 # Ratios are from observed codes, not a native interpolation function.
 curve=((0.,7/9,11/12),(.266,2/9,4/12),(.376,1/9,2/12),(.597,0.,1/12),
        (.727,0.,0.),(1.015,0.,0.))
 stamp,pitch_ratio,forward_ratio=next(row for row in reversed(curve) if row[0]<=age)
 result=copy.deepcopy(profile);result['computed']=True
 pitch=dequantize(profile['yaw1'][1],6,PI32)*pitch_ratio
 result['yaw1']=[profile['yaw1'][0],quantize(pitch,6,PI32),31]
 result['yaw2']=[profile['yaw2'][0],quantize(pitch,6,PI32),31]
 heading=profile.get('calculation',{}).get('world_heading',dequantize(profile['yaw1'][0],6,PI32))
 fy=dequantize(profile['forward'][1],5,1.)*forward_ratio
 horizontal=math.sqrt(max(0.,1.-fy*fy))
 result['forward']=[quantize(math.sin(heading)*horizontal,5,1.),quantize(fy,5,1.),
                    quantize(math.cos(heading)*horizontal,5,1.)]
 result['seat']=dict(seated=0,sit_enter=0,seat=0,avatar_state=2)
 for row in result['pose_current']:row.update(left_weight=0,right_weight=0)
 if age>=.266:
  for row in result['pose_history']:row.update(left_weight=0,right_weight=0)
 if age>=1.015:
  result.update(attachment_kind=0,attachment_id=0xffffffff,local_origin=[0.,0.,0.],optional_anchor=None)
  result['pose_current']=[dict(position_codes=[2047]*3,yaw_code=511,left_weight=0,right_weight=0)]
 result['source']='engineering_curve_observed_timing'
 return dict(profile=result,complete=False,sample_age=stamp,source=result['source'],
             origin_delta=None,local_origin_delta=None)
