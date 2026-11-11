"""Strict offline legacy->0.16.8 Actor typed-token migration.
Only the three statically verified additions are inserted. No field producer,
float requantizer, animation reset, entity mapping, network or product hook.
Input/output payloads remain in RAM and never appear in metadata/repr.
"""
from dataclasses import dataclass,field
from types import CodeType
from .legacy_actor import actor_wire as old
from .legacy_actor import actor_wire_schema as oschema
from . import actor_wire_0167 as new
from . import actor_wire_schema_0167 as nschema
from .bit_stream import BitWriter
OLD_SHA256='c5a7c9756f97b76562e7e28d86cbe4a58763c1295cd0c01f316f9e49d76fc9f8'
NEW_SHA256='0b18ecc44e873f6caa181e6bc49f14b7e07058888523e47db52a14b32077a7ed'
_PREFIX_OLD='0x2144620';_TIMED_OLD='0x2144bac';_BOOL_OLD='0x2143cf4'
_NEW_ADDITIONS={'0x21dd104','0x21df510','0x21de728'}
_DYNAMIC={'0x2142aa4':'pose','0x21dccb8':'pose','0x21dd4b4':'pose','0x2145220':'history','0x21df36c':'history','0x21dfb68':'history','0x1eb530c':'strings','0x1f5e92c':'strings','0x1ebb468':'bool256','0x1f64a88':'bool256','0x21473dc':'bool48','0x21e1534':'bool48','0x2144024':'multipass','0x21de25c':'multipass','0x21dea58':'multipass'}

def _profiles(old_sha,new_sha):
 if type(old_sha)is not str or old_sha!=OLD_SHA256 or type(new_sha)is not str or new_sha!=NEW_SHA256:raise ValueError('actor_migration_profile_unverified')
 if old.PUBLIC_ELF_SHA256!=OLD_SHA256 or new.PUBLIC_ELF_SHA256!=NEW_SHA256:raise ValueError('actor_migration_dependency_profile_changed')

def _flatten(plan,helpers,drop=(),stack=()):
 out=[]
 for row in plan:
  label,kind,*args=row
  if label in drop:continue
  if kind=='helper':
   target=args[0]
   if target in helpers:
    if target in stack:raise ValueError('actor_migration_schema_cycle')
    out.extend(_flatten(helpers[target],helpers,drop,stack+(target,)))
   elif target in _DYNAMIC:out.append(('dynamic',_DYNAMIC[target]))
   else:raise ValueError('actor_migration_unknown_helper')
  else:out.append(tuple(row[1:]))
 return tuple(out)

def _code_shape(code):
 # Nested comprehension code objects differ in file/line debug metadata only.
 # Retain opcodes, names, locals, closure shape, flags and recursively all data.
 def constant(value):
  if type(value)is CodeType:return ('code',_code_shape(value))
  if type(value)is tuple:return ('tuple',tuple(constant(x)for x in value))
  return (type(value).__name__,value)
 return (code.co_code,code.co_names,code.co_varnames,code.co_freevars,code.co_cellvars,code.co_argcount,code.co_posonlyargcount,code.co_kwonlyargcount,code.co_flags,tuple(constant(x)for x in code.co_consts))

def _verify_delta():
 # Refuse a fourth schema or dynamic-code difference instead of inventing data.
 om,nm=oschema.MAIN,nschema.MAIN
 if om[6]!=(_PREFIX_OLD,'raw',4):raise ValueError('actor_migration_anchor_changed')
 expected={'0x21dd104':('raw',8),'0x21df510':('raw',1),'0x21de728':('bool',)}
 rows=list(nm)+[row for plan in nschema.FIXED_HELPERS.values()for row in plan]
 for label,shape in expected.items():
  matches=[tuple(row[1:])for row in rows if row[0]==label]
  if matches!=[shape]:raise ValueError('actor_migration_three_additions_changed')
 if len(nm)!=len(om)+2 or len(nschema.FIXED_HELPERS)!=len(oschema.FIXED_HELPERS):raise ValueError('actor_migration_fourth_schema_difference')
 if _flatten(om,oschema.FIXED_HELPERS)!=_flatten(nm,nschema.FIXED_HELPERS,_NEW_ADDITIONS):raise ValueError('actor_migration_fourth_schema_difference')
 if sorted(repr(_flatten(p,oschema.FIXED_HELPERS))for p in oschema.FIXED_HELPERS.values())!=sorted(repr(_flatten(p,nschema.FIXED_HELPERS,_NEW_ADDITIONS))for p in nschema.FIXED_HELPERS.values()):raise ValueError('actor_migration_fourth_helper_difference')
 if tuple(tuple(x[1:])for x in oschema.POSE)!=tuple(tuple(x[1:])for x in nschema.POSE):raise ValueError('actor_migration_pose_difference')
 if not any(row==(_TIMED_OLD,'raw',8)for row in om):raise ValueError('actor_migration_anchor_changed')
 target=tuple(tuple(x[1:])for x in nschema.FIXED_HELPERS['0x21de5e0']if x[0]!='0x21de728')
 matches=[p for p in oschema.FIXED_HELPERS.values()if tuple(tuple(x[1:])for x in p)==target]
 if len(matches)!=1 or matches[0][-2]!=(_BOOL_OLD,'range',-4,4):raise ValueError('actor_migration_anchor_changed')
 for name in ('raw','bits','bounded','count','raw_float','history','strings','fixed_boolean_array','multipass_arrays'):
  a=getattr(old._Walk,name).__code__;b=getattr(new._Walk,name).__code__
  if _code_shape(a)!=_code_shape(b):raise ValueError('actor_migration_dynamic_method_difference')

def _triples(tokens):return tuple((t.kind,t.extent,t.value)for t in tokens)

@dataclass(frozen=True,repr=False)
class MigratedActor0167:
 _body:bytes=field(repr=False)
 old_bytes:int
 new_bytes:int
 old_token_count:int
 new_token_count:int
 timed_records:int
 new_bit_length:int
 def wire_bytes(self):return self._body
 def metadata(self):
  return {'scope':'offline_typed_Actor_migration_only','old_bytes':self.old_bytes,'new_bytes':self.new_bytes,'old_tokens_preserved':self.old_token_count,'new_token_count':self.new_token_count,'inserted_prefix_raw8':1,'inserted_timed_raw1':self.timed_records,'inserted_helper_bool':1,'new_bits':self.new_bit_length,'quantized_codes_rebuilt':False,'strings_decoded_or_rebuilt':False,'animation_or_timing_fields_reset':False,'padding_regenerated_zero':True,'new_fields_explicitly_supplied':True,'field_origins_verified':False,'entity_dictionary_resolved':False,'actor_authority_verified':False,'network_ready':False,'server_accepted':False}

def migrate_actor_body_0167(old_body,*,prefix_raw8,timed_extra_raw1,helper_extra_bool,verified_old_profile_sha256,verified_new_profile_sha256):
 """All new data mandatory. Preserves old typed values, not old wire offsets.
 Strict structural/zero-padding readers are used; native-reader noncanonical
 strings/ignored bools remain exact bytes/codes rather than being normalized.
 No real-account ownership, dictionary or gameplay authorization is established.
 """
 _profiles(verified_old_profile_sha256,verified_new_profile_sha256);_verify_delta()
 if type(prefix_raw8)is not bytes or len(prefix_raw8)!=8:raise ValueError('actor_migration_explicit_prefix8_required')
 if type(timed_extra_raw1)is not tuple or any(type(x)is not bytes or len(x)!=1 for x in timed_extra_raw1):raise ValueError('actor_migration_explicit_timed_bytes_required')
 if type(helper_extra_bool)is not bool:raise ValueError('actor_migration_explicit_bool_required')
 base=old.inspect_actor_wire(old_body,verified_profile_sha256=OLD_SHA256,check_zero_padding=True,require_canonical_writer=False)
 if old.reencode_actor_wire(base)!=old_body:raise ValueError('actor_migration_old_roundtrip_mismatch')
 counts=[v for k,v in base._counts if k=='raw8_records']
 if len(counts)!=1 or len(timed_extra_raw1)!=counts[0]:raise ValueError('actor_migration_timed_count_mismatch')
 class Inject(old._Walk):
  def __init__(self):
   super().__init__(old_body,True,False);self.inserted=[];self.prefix_count=0;self.flag_count=0;self.timed_index=0
  def add(self,kind,width,value):
   self.inserted.append(len(self.tokens));self.tokens.append(old._Token(kind,width,value))
  def operation(self,item):
   value=super().operation(item);label=item[0]
   if label==_PREFIX_OLD:self.add('raw',8,prefix_raw8);self.prefix_count+=1
   elif label==_TIMED_OLD:
    if self.timed_index>=len(timed_extra_raw1):raise ValueError('actor_migration_timed_count_mismatch')
    self.add('raw',1,timed_extra_raw1[self.timed_index]);self.timed_index+=1
   elif label==_BOOL_OLD:self.add('bits',1,int(helper_extra_bool));self.flag_count+=1
   return value
 w=Inject();w.main();w.reader.require_end(allow_zero_padding=True)
 if (w.prefix_count,w.flag_count,w.timed_index)!=(1,1,counts[0]):raise ValueError('actor_migration_insertion_count_mismatch')
 indices=set(w.inserted)
 if tuple(t for i,t in enumerate(w.tokens)if i not in indices)!=base._tokens:raise ValueError('actor_migration_old_token_changed')
 writer=BitWriter(max_bytes=new.MAX_ACTOR_WIRE_BYTES)
 for t in w.tokens:
  if t.kind=='raw':writer.write_raw(t.value)
  elif t.kind=='bits':writer.write_bits(t.value,t.extent)
  else:raise ValueError('actor_migration_unknown_token')
 body=writer.to_bytes()
 checked=new.inspect_actor_wire(body,verified_profile_sha256=NEW_SHA256,check_zero_padding=True,require_canonical_writer=False)
 if _triples(checked._tokens)!=_triples(w.tokens) or new.reencode_actor_wire(checked,verified_profile_sha256=NEW_SHA256)!=body:raise ValueError('actor_migration_new_roundtrip_mismatch')
 if tuple((t.kind,t.extent,t.value)for i,t in enumerate(checked._tokens)if i not in indices)!=_triples(base._tokens):raise ValueError('actor_migration_preservation_mismatch')
 return MigratedActor0167(body,len(old_body),len(body),len(base._tokens),len(checked._tokens),counts[0],checked.bit_length)
