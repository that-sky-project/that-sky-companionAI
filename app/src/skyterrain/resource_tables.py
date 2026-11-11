"""Bounded CN Lua constant-resource declarations; never runs Lua or any global.

Custom opcode numbering is a format fact independently applied from:
https://github.com/that-sky-project/that-luac-decompiler/blob/main/src/constants.js
Declarations are NOT a runtime winner/override rule and do not select colliders.
"""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import math
from pathlib import Path
import struct

PROFILE='netease-android-0.16.8-421629'
HEADER=bytes.fromhex('1b4c7561520001040804080019930d0a1a0a')

class ResourceTableError(ValueError):pass

@dataclass(frozen=True)
class Limits:
    max_bytes:int=16*1024*1024
    max_string:int=1024*1024
    max_items:int=500_000
    max_functions:int=4096
    max_depth:int=32
    max_projected_values:int=2_000_000
    def __post_init__(self):
        ceilings={'max_bytes':64*1024*1024,'max_string':8*1024*1024,'max_items':2_000_000,'max_functions':16_384,'max_depth':128,'max_projected_values':8_000_000}
        for name,maximum in ceilings.items():
            value=getattr(self,name);minimum=0 if name=='max_depth' else 1
            if type(value) is not int or not minimum<=value<=maximum:
                raise ResourceTableError('invalid_limit:'+name)

@dataclass(frozen=True)
class _Symbol:
    kind:str
    value:object

class _Reader:
    def __init__(self,data,limits):
        if len(data)>limits.max_bytes:raise ResourceTableError('chunk_budget')
        if not data.startswith(HEADER):raise ResourceTableError('unsupported_CN_chunk_header')
        self.data=data;self.pos=len(HEADER);self.limits=limits;self.functions=0;self.items=0
    def take(self,n):
        if n<0 or n>len(self.data)-self.pos:raise ResourceTableError('chunk_bounds')
        begin=self.pos;self.pos+=n;return self.data[begin:self.pos]
    def number(self,fmt):return struct.unpack('<'+fmt,self.take(struct.calcsize('<'+fmt)))[0]
    def count(self):
        n=self.number('I');self.items+=n
        if self.items>self.limits.max_items:raise ResourceTableError('aggregate_item_budget')
        return n
    def string(self):
        n=self.number('I')
        if n>self.limits.max_string:raise ResourceTableError('string_budget')
        if n==0:return None
        b=self.take(n)
        if b[-1]!=0:raise ResourceTableError('string_terminator')
        try:return b[:-1].decode('utf8')
        except UnicodeError as e:raise ResourceTableError('string_utf8') from e
    def function(self,depth=0):
        self.functions+=1
        if depth>self.limits.max_depth or self.functions>self.limits.max_functions:raise ResourceTableError('function_budget')
        self.take(8);params=self.number('B');vararg=self.number('B');stack=self.number('B')
        if not 1<=stack<=255 or params>stack:raise ResourceTableError('register_header')
        count=self.count();code_offset=self.pos;code=[self.number('I') for _ in range(count)];constants=[]
        for _ in range(self.count()):
            tag=self.number('B')
            if tag==0:value=None
            elif tag==1:
                raw=self.number('B')
                if raw not in (0,1):raise ResourceTableError('boolean_tag_value')
                value=bool(raw)
            elif tag==3:
                value=self.number('d')
                if not math.isfinite(value):raise ResourceTableError('nonfinite_constant')
            elif tag==4:value=self.string()
            else:raise ResourceTableError('unsupported_constant_tag')
            constants.append(value)
        children=[self.function(depth+1) for _ in range(self.count())]
        upvalues=[(self.number('B'),self.number('B')) for _ in range(self.count())]
        source=self.string();self.take(4*self.count())
        for _ in range(self.count()):self.string();self.take(8)
        upnames=[self.string() for _ in range(self.count())]
        return {'code':code,'code_offset':code_offset,'constants':constants,'children':children,'stack':stack,'source':source,'upvalues':upvalues,'upnames':upnames,'params':params,'vararg':vararg}

def _project(function,limits):
    if not function['code'] or (function['code'][-1]&63)!=31:
        raise ResourceTableError('missing_terminal_RETURN')
    registers={};calls={};declarations=[];references=[];values=0;constants=function['constants'];stack=function['stack']
    def snapshot(v,active=None,depth=0):
        nonlocal values
        values+=1
        if values>limits.max_projected_values or depth>limits.max_depth:raise ResourceTableError('projection_budget')
        if not isinstance(v,dict):return v
        active=set() if active is None else active
        if id(v) in active:raise ResourceTableError('cyclic_table')
        active.add(id(v))
        result={k:snapshot(x,active,depth+1) for k,x in v.items()};active.remove(id(v));return result
    def reg(i):
        if not 0<=i<stack:raise ResourceTableError('register_range')
        return registers.get(i,_Symbol('unassigned_register',i))
    def put(i,v):reg(i);registers[i]=v
    def const(i):
        if not 0<=i<len(constants):raise ResourceTableError('constant_range')
        return constants[i]
    def rk(i):return const(i&255) if i&256 else reg(i)
    def key(v):
        if isinstance(v,bool) or not isinstance(v,(str,int,float)):raise ResourceTableError('nonliteral_table_key')
        return v
    for pc,word in enumerate(function['code']):
        op=word&63;a=(word>>6)&255;c=(word>>14)&511;b=(word>>23)&511;bx=word>>14
        if op==6:put(a,reg(b))
        elif op==7:put(a,const(bx))
        elif op==9:
            if c or b not in (0,1):raise ResourceTableError('branching_or_invalid_LOADBOOL')
            put(a,bool(b))
        elif op==10:
            for i in range(a,a+b+1):put(i,None)
        elif op==12:
            name=rk(c)
            if b!=0 or not isinstance(name,str):raise ResourceTableError('nonliteral_environment_access')
            put(a,_Symbol('global',name))
        elif op==13:
            table=reg(b)
            if not isinstance(table,dict):raise ResourceTableError('GETTABLE_nonconstant_table')
            put(a,table.get(key(rk(c))))
        elif op==16:
            table=reg(a)
            if not isinstance(table,dict):raise ResourceTableError('SETTABLE_nonconstant_table')
            k=key(rk(b));v=rk(c)
            if v is None:table.pop(k,None)
            else:table[k]=v
        elif op==17:put(a,{})
        elif op==29:
            if b==0 or c==0:raise ResourceTableError('dynamic_CALL_arity')
            function_value=reg(a);args=[snapshot(reg(i)) for i in range(a+1,a+b)]
            if not isinstance(function_value,_Symbol) or function_value.kind not in ('global','call_result'):
                raise ResourceTableError('CALL_nonfunction_or_unassigned')
            calls[pc]={'function':function_value,'args':args}
            declaration_call=False
            # Only a literal resource(family)(name)(table) chain is extracted.
            if isinstance(function_value,_Symbol) and function_value.kind=='call_result':
                name_call=calls.get(function_value.value)
                family_ref=name_call and name_call['function']
                family_call=calls.get(family_ref.value) if isinstance(family_ref,_Symbol) and family_ref.kind=='call_result' else None
                if family_call and family_call['function'] in (_Symbol('global','resource'),_Symbol('global','resourcedef'),_Symbol('global','remote_resource')):
                    fargs=family_call['args'];nargs=name_call['args']
                    if len(fargs)==len(nargs)==len(args)==1 and isinstance(fargs[0],str) and isinstance(nargs[0],str) and isinstance(args[0],dict):
                        # Current ELF's embedded GatherCons writes these two
                        # fields to the original argument (including aliases).
                        original=reg(a+1);original['class']=fargs[0];original['name']=nargs[0]
                        properties=snapshot(original)
                        declarations.append({'directive':family_call['function'].value,'family':fargs[0],'name':nargs[0],'properties':properties,'implicit_constructor_fields':['class','name'],'pc':pc,'instruction_offset':function['code_offset']+4*pc,'call_chain':[family_ref.value,function_value.value,pc]})
                        declaration_call=True
                if name_call and name_call['function']==_Symbol('global','resourceref') and len(name_call['args'])==len(args)==1 and isinstance(name_call['args'][0],str) and isinstance(args[0],str):
                    references.append({'family':name_call['args'][0],'name':args[0],'pc':pc,'definition_resolved':False})
            # The sole table-accepting primitive modeled here is the declarative
            # resource(family)(name)(table) form. Unknown Lua code could mutate
            # any passed table/alias/nested table: stop the WHOLE projection.
            if not declaration_call and any(isinstance(value,dict) for value in args):
                raise ResourceTableError('unknown_CALL_with_mutable_table')
            for i in range(c-1):put(a+i,_Symbol('call_result',pc) if i==0 else _Symbol('extra_call_result',(pc,i)))
        elif op==31:
            if b not in (1,2) or (b==2 and isinstance(reg(a),_Symbol)):raise ResourceTableError('unsupported_RETURN')
            if pc!=len(function['code'])-1:raise ResourceTableError('early_RETURN')
        elif op==36:
            if b==0 or c==0:raise ResourceTableError('dynamic_SETLIST')
            table=reg(a)
            if not isinstance(table,dict):raise ResourceTableError('SETLIST_nonconstant_table')
            for i in range(1,b+1):
                k=(c-1)*50+i;v=reg(a+i)
                if v is None:table.pop(k,None)
                else:table[k]=v
        else:raise ResourceTableError(f'unsupported_CN_opcode:{op}:pc:{pc}')
    return declarations,references,len(calls),values

def _plain(value):
    if isinstance(value,_Symbol):return {'unresolved_symbol':value.kind,'value':value.value}
    if isinstance(value,dict):return {k:_plain(v) for k,v in value.items()}
    return value

def _symbolic(value):
    return isinstance(value,_Symbol) or (isinstance(value,dict) and any(_symbolic(v) for v in value.values()))

def read_resource_tables(path_or_bytes,*,profile,limits=Limits()):
    if profile!=PROFILE:raise ResourceTableError('unsupported_native_profile')
    if not isinstance(limits,Limits):raise ResourceTableError('Limits_required')
    if isinstance(path_or_bytes,(bytes,bytearray,memoryview)):
        if len(path_or_bytes)>limits.max_bytes:raise ResourceTableError('chunk_budget')
        data=bytes(path_or_bytes);source='<bytes>'
    else:
        path=Path(path_or_bytes)
        with path.open('rb') as stream:data=stream.read(limits.max_bytes+1)
        source=str(path)
    reader=_Reader(data,limits);function=reader.function()
    if reader.pos!=len(data):raise ResourceTableError('unparsed_chunk_tail')
    declarations,references,calls,values=_project(function,limits)
    for row in declarations:
        row['has_symbolic_properties']=_symbolic(row['properties']);row['properties']=_plain(row['properties'])
    groups={}
    for row in declarations:groups.setdefault((row['family'],row['name']),[]).append(row['pc'])
    return {'schema':'sky-cn-static-resource-declarations-v1','profile':profile,'source':source,'source_sha256':hashlib.sha256(data).hexdigest(),'read_bytes':reader.pos,'instruction_count':len(function['code']),'function_count':reader.functions,'recorded_calls':calls,'projected_values':values,'declarations':declarations,'resource_references':references,'duplicate_declarations':[{'family':key[0],'name':key[1],'pcs':pcs} for key,pcs in groups.items() if len(pcs)>1],'declaration_snapshots_not_runtime_queue':True,'lua_executed':False,'runtime_precedence_verified':False,'resource_path_selected':False,'collision_verified':False}
