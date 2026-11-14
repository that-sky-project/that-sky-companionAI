"""Opt-in chat router. No destination guessing and no game transport implementation.
Chat ingress revision is local provenance. The receiver assigns the actual controller
revision after accepting the exact request; chat never fabricates that revision.
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path

import xg_paths
from typing import Callable
from uuid import UUID, uuid4
import json, math, os, re, threading, time

SLASH=re.compile(r'^\s*[/／]\s*(.*)$',re.S)
# 2026-11-06：自然说法。称呼/客气话开头 + “带/牵/背/抱(着)我” + 去/回/到/前往 + 地点 + 语气词结尾。
LEAD=re.compile(r'^(?:小光|光光)?[\s～~，,、：:!！。…]*'
 r'(?:(?:那|那你|那就|你|就|请|请你|麻烦|麻烦你|能不能|能|可不可以|可以|快|快点|现在|再|来|要不|要不你|帮我|给我)[\s，,]*)*')
START=re.compile(r'^(?P<kind>公主抱着我|公主抱我|抱抱我|抱着我|抱我|背背我|背着我|背我|牵着我|牵我|拉着我|带着我|带上我|带我)'
 r'\s*(?:一起\s*)?(?:带我\s*)?(?P<motion>去|回|到|前往)\s*(?P<destination>.*)$')
# 2026-11-07 T18：显式斜杠下的“去/到/前往+地点”“我想去/我要去+地点”也算带路。
BARE=re.compile(r'^(?:我)?(?:想|要)?(?:去|到|前往)\s*(?P<destination>.*)$')
PLACE_Q=re.compile(r'^(?P<name>[^，,。！!？?]{1,32}?)(?:在哪|在哪里|在哪儿|是哪里|是什么地方|在哪个图)')
TAIL=re.compile(r'(?:[\s～~!！。.…?？,，]|吧|呀|啦|嘛|呗|哦|噢|喔|呢|哈|好不好|好吗|好嘛|可以吗|可以不|行吗|行不行|行不|可不可以|成吗|好不|咯|了)+$')
CANCEL=re.compile(r'^(?:取消带路|停止带路|取消同行|取消这次带路|不去啦|不去了|别带我去了)[!！。\s]*$')
QUERY_BLOCK=re.compile(r'[“”「」『』"\n\r]|(?:上次|刚才|他说|她说|别人说|我朋友说|如果|假如|例如|比如)|(?:不要|别|不想|不用)(?:带|牵|背|抱)')
RESOURCE=re.compile(r'解锁|升级|赠送|送礼|花钱|氪|消耗|(?:花|扣|用).*(?:蜡烛|爱心|红蜡|季蜡|钱)')
DEST_UNCLEAR=frozenset(('','那里','那边','这儿','那里吧','刚才那里','随便','随便哪里','某地点'))

def canonical_uuid(value):
    try:return type(value) is str and str(UUID(value))==value
    except (ValueError,TypeError,AttributeError):return False

def finite(value):return type(value) in (int,float) and math.isfinite(value)

@dataclass(frozen=True)
class Intent:
    operation: str
    destination_query: str | None = None
    carry_kind: str | None = None
    reason: str | None = None

def parse(raw: str) -> Intent | None:
    if type(raw) is not str or len(raw)>320:return None
    m=SLASH.match(raw)
    if not m:return None
    text=m.group(1).strip()
    if CANCEL.fullmatch(text):return Intent('cancel')
    if QUERY_BLOCK.search(text):return None
    body=LEAD.sub('',text,count=1)
    m=START.fullmatch(body)
    if not m:
        want=_parse_want(text)
        if want is not None:return want
        m=BARE.fullmatch(body)
        if not m:return None
        if RESOURCE.search(text):return Intent('blocked',reason='resource_consumption_forbidden')
        destination=TAIL.sub('',m.group('destination')).strip()
        if len(destination)>64 or re.search(r'[，,；;]|然后|再去|接着',destination):
            return Intent('clarify',reason='multiple_or_ambiguous_destination')
        if destination in DEST_UNCLEAR:return Intent('clarify',carry_kind='handhold',reason='destination_required')
        return Intent('start',destination,'handhold')
    if RESOURCE.search(text):return Intent('blocked',reason='resource_consumption_forbidden')
    # 只去掉结尾的语气词/标点，不动地图名本身。
    destination=TAIL.sub('',m.group('destination')).strip()
    if len(destination)>64 or re.search(r'[，,；;]|然后|再去|接着',destination):
        return Intent('clarify',reason='multiple_or_ambiguous_destination')
    kind=m.group('kind')
    carry='piggyback' if kind.startswith('背') else 'princess_carry' if '抱' in kind else 'handhold'
    if destination in DEST_UNCLEAR:return Intent('clarify',carry_kind=carry,reason='destination_required')
    return Intent('start',destination,carry)

@dataclass(frozen=True)
class Origin:
    speaker_uuid: str
    raw_message: str
    line_id: str | int
    channel: str
    chat_session: str
    ingress_revision: int
    observed_mono: float

@dataclass(frozen=True)
class DestinationRef:
    destination_id: str
    scene: str
    map_id: int
    position: tuple[float,float,float] | None = None
    resource_cost: int = 0
    variant: str | None = None
    def valid(self):
        return (type(self.destination_id) is str and 0<len(self.destination_id)<=96
                and type(self.scene) is str and re.fullmatch(r'[A-Za-z0-9_]{1,96}',self.scene) is not None
                and type(self.map_id) is int and 0<self.map_id<2**32
                and self.position is None and type(self.resource_cost) is int and self.resource_cost==0
                and (self.variant is None or (type(self.variant) is str
                     and re.fullmatch(r'[a-z0-9_]{1,32}',self.variant) is not None)))

@dataclass(frozen=True)
class SubmitResult:
    request_id: str | None
    submitted: bool
    reason: str
    issued_at: float | None = None
    expires_at: float | None = None

@dataclass(frozen=True)
class AcceptanceResult:
    request_id: str
    accepted: bool
    reason: str

@dataclass(frozen=True)
class Decision:
    handled: bool
    reply: str | None = None
    remaining: tuple | None = None
    reason: str | None = None
    reply_guard: Callable[[], bool] | None = None
    receiver_accepted: bool = False

def _acceptance_message(reason):
    # Receiver errors are codes, never arbitrary exceptions, paths or log text.
    return {
        'destination_capture_unavailable': '本账号缺少目的地图目录，请到资源页重新构建。',
        'ack_timeout': '带路尚未确认，暂时不承诺出发。',
        'execution_disabled': '带路还没接好，暂时不会出发。',
        'stale_command': '这次带路请求过期了，请再说一次。',
        'command_expired': '这次带路请求过期了，请再说一次。',
        'destination_mismatch': '目的地还没核对好，这次没有接下带路。',
        'destination_variant_mismatch': '目的落点还没核对好，这次没有接下带路。',
        'runtime_failed': '这次带路没接上，稍后再试试。',
    }.get(reason, '这次带路没有被接下，稍后再试试。')


def guard_model_departure(text, payloads=()):
    """Only definite first-person departure promises in a current travel turn.

    The model branch never submits escort commands. Keep candidate names and
    normal/conditional/quoted descriptions; do not treat generated text as input.
    """
    if not isinstance(text,str):return text
    protected=set()
    for payload in payloads or ():
        if not isinstance(payload,dict):continue
        for row in payload.get('maps',[]) if isinstance(payload.get('maps'),list) else ():
            if not isinstance(row,dict):continue
            if isinstance(row.get('名称'),str):protected.add(row['名称'].strip())
            aliases=row.get('其它叫法',[])
            if isinstance(aliases,list):protected.update(x.strip() for x in aliases if isinstance(x,str))
    promise=re.compile(r'^(?:好(?:呀|啊|的|哒)?[，,\s]*)?(?:'
        r'(?:牵好|背好|抱好)(?:了)?(?:就回遇境|再出发|就出发|出发)'
        r'|(?:我|我们)(?:现在|马上|这就|就|已经|准备){0,2}(?:出发|动身)(?:去.{1,32})?'
        r'|(?:现在|马上|这就)出发(?:去.{1,32})?'
        r'|我(?:现在|马上|这就|就|已经){0,2}(?:带|牵|背|抱)你(?:去|回|到).{1,32}'
        r')[啦了咯呀吧～~\s]*$')
    parts=re.split(r'([。！？!?\n])',text)
    for i in range(0,len(parts),2):
        body=parts[i].strip()
        if body not in protected and promise.fullmatch(body):
            parts[i]='还没确认带路，先选好目的地吧'
    return ''.join(parts)


def _read_json(path,limit):
    with Path(path).open('rb') as stream:raw=stream.read(limit+1)
    if len(raw)>limit:raise ValueError('file_size')
    value=json.loads(raw)
    if type(value) is not dict:raise ValueError('json_shape')
    return value

class RegistryResolver:
    """Only root-installed verified IDs/names. Ambiguous aliases never pick an arbitrary map."""
    def __init__(self,path):self.path=Path(path)
    def __call__(self,query):
        doc=_read_json(self.path,262144)
        if doc.get('version')!=1 or type(doc.get('version')) is not int or type(doc.get('destinations')) is not list:return None
        matches=[]
        for row in doc['destinations']:
            if type(row) is not dict:continue
            aliases=row.get('aliases',[])
            if type(aliases) is not list:continue
            names=[n for n in [row.get('id'),row.get('scene'),*aliases] if type(n) is str]
            if query not in names and _loose(query) not in {_loose(n) for n in names}:continue
            ref=DestinationRef(row.get('id'),row.get('scene'),row.get('map_id'),row.get('position'),row.get('resource_cost',0),row.get('variant'))
            if ref.valid():matches.append(ref)
        return matches[0] if len(matches)==1 else None

class PoseControlPort:
    """One atomic escort envelope only; default flag absent/disabled means no write.
    Receiver must recheck sender/scene/deadline and allocate its real revision before effects.
    """
    def __init__(self,root,*,clock=time.time,monotonic=time.monotonic,sleep=time.sleep):
        self.root=Path(root);self.directory=self.root/'control';self.path=self.directory/'pose.json'
        self.state_path=self.directory/'pose.state.json';self.flag=self.directory/'escort_travel.enabled.json'
        self.feedback_path=self.directory/'escort_travel.feedback.json'
        self.clock=clock;self.monotonic=monotonic;self.sleep=sleep;self.lock=threading.Lock()
    def _config(self):
        try:cfg=_read_json(self.flag,4096)
        except (OSError,ValueError,TypeError):return None
        now=self.clock();deadline=cfg.get('session_deadline')
        if cfg.get('version')!=1 or type(cfg.get('version')) is not int or cfg.get('enabled') is not True or not finite(deadline) or deadline<=now:return None
        return cfg
    @property
    def available(self):return self._config() is not None
    def _same_room(self,origin,now):
        state=_read_json(self.state_path,2097152);room=state.get('room')
        if type(room) is not dict or room.get('connected') is not True:return False
        if type(room.get('map_id')) is not int or room['map_id']<=0 or room.get('pending_map_id') not in (None,room['map_id']):return False
        for stamp in (state.get('generated_at'),room.get('generated_at')):
            if not finite(stamp) or not 0<=now-stamp<=3:return False
        slots=room.get('slots');players=room.get('players')
        if type(slots) is not dict or type(players) is not dict:return False
        found=[slot for slot,uid in slots.items() if uid==origin.speaker_uuid]
        if len(found)!=1:return False
        peer=players.get(found[0]);position=peer.get('position') if type(peer) is dict else None
        if type(peer) is not dict or peer.get('uuid')!=origin.speaker_uuid or type(position) is not dict:return False
        age=position.get('age_seconds');xyz=position.get('value')
        return (position.get('freshness')=='fresh' and finite(age) and 0<=age+now-room['generated_at']<=3
                and type(xyz) in (tuple,list) and len(xyz)>=3 and all(finite(x) for x in xyz[:3]))
    def submit(self,origin,intent,destination,*,still_current):
        with self.lock:
            cfg=self._config();now=self.clock()
            if cfg is None:return SubmitResult(None,False,'execution_disabled')
            if not still_current():return SubmitResult(None,False,'superseded')
            if intent.operation not in ('start','cancel') or parse(origin.raw_message)!=intent or not canonical_uuid(origin.speaker_uuid):return SubmitResult(None,False,'invalid_origin')
            if intent.operation=='start' and (not isinstance(destination,DestinationRef) or not destination.valid()):return SubmitResult(None,False,'invalid_destination')
            try:
                if not self._same_room(origin,now):return SubmitResult(None,False,'fresh_peer_required')
            except (OSError,ValueError,TypeError):return SubmitResult(None,False,'fresh_peer_required')
            request_id=uuid4().hex
            command={'pose':'escort_travel' if intent.operation=='start' else 'escort_cancel',
                     'request_id':request_id,'requester':origin.speaker_uuid,'target':origin.speaker_uuid,
                     'raw_command':origin.raw_message,'line_id':origin.line_id,'issued_at':now,
                     'expires_at':min(now+180,cfg['session_deadline']),'src':'chat',
                     'feedback_version':1,'chat_session':origin.chat_session,
                     'ingress_revision':origin.ingress_revision,'channel':origin.channel}
            if intent.operation=='start':command.update(scene=destination.scene,map_id=destination.map_id,arrival='scene_spawn',carry_kind=intent.carry_kind)
            if intent.operation=='start' and getattr(destination,'variant',None) is not None:
                command['variant']=destination.variant
            if self.path.is_symlink() or self.directory.is_symlink():return SubmitResult(None,False,'control_path')
            raw=json.dumps(command,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode('utf-8')
            if len(raw)>65536:return SubmitResult(None,False,'control_size')
            pending=self.directory/(self.path.name+'.'+request_id+'.pending')
            try:
                with pending.open('xb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
                fresh_cfg=self._config()
                if not still_current() or fresh_cfg!=cfg or self.clock()>=command['expires_at']:
                    return SubmitResult(None,False,'superseded_or_disabled_before_publish')
                os.replace(pending,self.path)
            finally:
                if pending.exists():pending.unlink()
            return SubmitResult(request_id,True,'queued_not_arrived',now,command['expires_at'])

    def _acceptance(self,origin,receipt):
        now=self.clock()
        if not finite(receipt.issued_at) or not finite(receipt.expires_at):return None
        try:doc=_read_json(self.feedback_path,65536)
        except (OSError,ValueError,TypeError,UnicodeError):return None
        if (doc.get('version')!=1 or type(doc.get('version')) is not int
                or not finite(doc.get('generated_at')) or not 0<=now-doc['generated_at']<=15
                or not re.fullmatch('[0-9a-f]{32}',str(doc.get('runtime_id','')))
                or type(doc.get('results')) is not list or len(doc['results'])>16):return None
        binding={'request_id':receipt.request_id,'requester':origin.speaker_uuid,
                 'issued_at':receipt.issued_at,'chat_session':origin.chat_session,
                 'ingress_revision':origin.ingress_revision,'channel':origin.channel,
                 'operation':'escort_travel'}
        for row in reversed(doc['results']):
            if type(row) is not dict or any(type(row.get(k)) is not type(v) or row.get(k)!=v for k,v in binding.items()):continue
            if (not finite(row.get('observed_at')) or not receipt.issued_at<=row['observed_at']<=now
                    or not finite(row.get('expires_at')) or not now<row['expires_at']<=row['observed_at']+15):continue
            status=row.get('status')
            if status=='accepted':
                revision=row.get('controller_revision')
                if (type(revision) is not int or revision<=0 or doc.get('active_request_id')!=receipt.request_id
                        or type(doc.get('active_revision')) is not int or doc['active_revision']!=revision):
                    return AcceptanceResult(receipt.request_id,False,'superseded')
                try:command=_read_json(self.path,65536)
                except (OSError,ValueError,TypeError,UnicodeError):return None
                # A newer action already queued must invalidate the old optimistic
                # reply even before the receiver reaches its next one-second poll.
                if any(command.get(k)!=v for k,v in binding.items() if k!='operation') or command.get('pose')!='escort_travel':
                    return AcceptanceResult(receipt.request_id,False,'superseded')
                if self._config() is None or now>=receipt.expires_at:
                    return AcceptanceResult(receipt.request_id,False,'execution_disabled')
                return AcceptanceResult(receipt.request_id,True,'runtime_accepted')
            if status in ('rejected','superseded'):
                reason=row.get('reason') if type(row.get('reason')) is str else 'runtime_rejected'
                return AcceptanceResult(receipt.request_id,False,'superseded' if status=='superseded' else reason)
        return None

    def await_acceptance(self,origin,receipt,*,still_current,timeout=4.0):
        # Submission's file lock is released before waiting; no runtime/controller
        # lock is shared or held here. Never retry a command after timeout.
        end=self.monotonic()+max(0.0,min(float(timeout),4.0))
        while True:
            if not still_current():return AcceptanceResult(receipt.request_id,False,'superseded')
            result=self._acceptance(origin,receipt)
            if result is not None:
                return result if still_current() else AcceptanceResult(receipt.request_id,False,'superseded')
            if self.monotonic()>=end:return AcceptanceResult(receipt.request_id,False,'ack_timeout')
            self.sleep(0.05)

    def acceptance_current(self,origin,receipt):
        result=self._acceptance(origin,receipt)
        return isinstance(result,AcceptanceResult) and result.accepted is True

class Router:
    def __init__(self,*,resolver=None,port=None,clock=time.monotonic):
        self.resolver=resolver;self.port=port;self.clock=clock;self._consumed=set();self.last_submitted=None
        self.choice_proposals={}  # speaker/channel-scoped, 120-second destination choices
        self.proposal=None   # 2026-11-06：地名纠错后等确认的提议
    def route(self,origin,intent,*,still_current):
        if not isinstance(origin,Origin) or not canonical_uuid(origin.speaker_uuid) or not canonical_uuid(origin.chat_session):return Decision(True,None,reason='invalid_origin')
        now=self.clock()
        if (type(origin.ingress_revision) is not int or origin.ingress_revision<=0
                or type(origin.line_id) not in (str,int) or not origin.line_id
                or type(origin.channel) is not str or not finite(origin.observed_mono)
                or not 0<=now-origin.observed_mono<=30 or parse(origin.raw_message)!=intent):return Decision(True,None,reason='unbound_or_expired_origin')
        key=(origin.chat_session,origin.ingress_revision)
        if key in self._consumed or not still_current():return Decision(True,None,reason='superseded_or_consumed')
        self._consumed.add(key)
        if len(self._consumed)>512:self._consumed={key}
        if intent.operation=='clarify':return Decision(True,'你想去哪个具体地点呀？',reason=intent.reason)
        if intent.operation=='blocked':return Decision(True,'先用已经解锁的互动吧，这次不花蜡烛或爱心。',reason=intent.reason)
        destination=None
        if intent.operation=='start':
            if self.resolver is None:return Decision(True,'带路还没接好，暂时不会出发。',reason='resolver_unavailable')
            try:destination=self.resolver(intent.destination_query)
            except Exception:return Decision(True,'这个地点还没确认好，你再说具体一点？',reason='resolver_error')
            if destination is None:
                choices=_find_choices(self,intent.destination_query)
                if len(choices)>1:
                    _remember_choices(self,origin,choices,intent.carry_kind)
                    return Decision(True,_choice_listing(choices),reason='destination_choices')
                guess=choices[0]['query'] if choices else _closest(intent.destination_query)
                if guess:
                    self.proposal={'speaker':origin.speaker_uuid,'channel':origin.channel,'kind':intent.carry_kind,
                                   'alias':guess,'expires':now+60}
                    verb={'handhold':'带','piggyback':'背','princess_carry':'抱'}.get(intent.carry_kind,'带')
                    go='回' if _loose(guess) in HOME_KEYS else '去'
                    return Decision(True,f'你说的是不是{guess}呀？那我{verb}你{go}？',reason='suggested_correction')
                return Decision(True,_suggest(intent.destination_query),reason='unknown_destination')
            if not isinstance(destination,DestinationRef) or not destination.valid():return Decision(True,'这个地点还没确认好，你再说具体一点？',reason='invalid_destination')
        if self.port is None or not self.port.available:return Decision(True,'带路还没接好，暂时不会出发。',reason='execution_disabled')
        if not still_current():return Decision(True,None,reason='superseded_before_submit')
        try:receipt=self.port.submit(origin,intent,destination,still_current=still_current)
        except Exception:return Decision(True,'这次没接上，先等等我。',reason='submit_failed')
        if not isinstance(receipt,SubmitResult) or receipt.submitted is not True or not re.fullmatch('[0-9a-f]{32}',receipt.request_id or ''):
            return Decision(True,'这次没接上，先等等我。',reason=receipt.reason if isinstance(receipt,SubmitResult) else 'invalid_receipt')
        self.last_submitted=(origin,receipt.request_id)
        guard=None
        if intent.operation=='start':
            waiter=getattr(self.port,'await_acceptance',None)
            try:accepted=waiter(origin,receipt,still_current=still_current) if callable(waiter) else None
            except Exception:accepted=None
            if not still_current():return Decision(True,None,reason='superseded_after_submit')
            if not (isinstance(accepted,AcceptanceResult) and accepted.request_id==receipt.request_id and accepted.accepted is True):
                reason=accepted.reason if isinstance(accepted,AcceptanceResult) else 'ack_timeout'
                return Decision(True,None if reason=='superseded' else _acceptance_message(reason),reason=reason,
                                reply_guard=still_current)
            current=getattr(self.port,'acceptance_current',None)
            def guard():
                try:return still_current() and callable(current) and current(origin,receipt) is True
                except Exception:return False
            if not guard():return Decision(True,None,reason='superseded_after_accept')
        home=getattr(destination,'map_id',None)==3526133726
        go='就回遇境' if home else '再出发'
        reply='好，先停下。' if intent.operation=='cancel' else ('好呀，牵好'+go+'。' if intent.carry_kind=='handhold' else '好呀，背好'+go+'。' if intent.carry_kind=='piggyback' else '好呀，抱好'+go+'。')
        return Decision(True,reply,reason='submitted_not_completed',reply_guard=guard,
                        receiver_accepted=intent.operation=='start')

def _suggest(query):
    """可执行候选已在路由中处理；这里仅解释未绑定地图的地标或未知名称。"""
    try:
        from destination_resolver_v25 import load_default, _key
        r=load_default();res=r.resolve(query)
        dest=res.get('destination') or {}
        if dest.get('kind')=='landmark' and not dest.get('sceneName'):
            # 2026-11-07 T18：只有“已经绑定场景+变体”的地标才可执行；其余地标仍只是资料。
            return f'{dest.get("displayName") or query}是图里的一个地方，我还不知道在哪张图～你说一下地图名，我带你过去？'
    except Exception:
        pass
    return '你说的是哪个具体地点呀？'

HOME_KEYS={'遇境','家','回城','主城','城','老遇境'}
CARRY_ANY=re.compile(r'(公主抱着我|公主抱我|抱抱我|抱着我|抱我|背背我|背着我|背我|牵着我|牵我|拉着我|带着我|带上我|带我)')
WANT=re.compile(r'(?:我)?(?:想要|想|要|好想)(?:去|回|到)\s*(?P<destination>[^，,。！!？?～~、\s]+)')
CONFIRM=re.compile(r'(?:(?:对|对的|对呀|对啊|是|是的|是呀|是啊|嗯|嗯嗯|恩|好|好的|好呀|好啊|好嘞|行|可以|没错|就是|就它|就这个|就那个|走|走吧|走呀|出发|去吧|去呀|快走|yes|ok|okk|en)[\s，,!！。～~…]*)+',re.I)
DENY=re.compile(r'^(?:不是|不对|不要|不去|算了|错了|不是这个|不用了)')

def _loose(s):
    try:
        from destination_resolver_v25 import _key
        return _key(s or '')
    except Exception:
        return (s or '').strip()

def _parse_want(text):
    """“我想去预言峡谷，你带我去呗”：要同时有“想去/要去+地点”和“带/背/抱/牵我”。"""
    w=WANT.search(text);c=CARRY_ANY.search(text)
    if not w or not c:return None
    if RESOURCE.search(text):return Intent('blocked',reason='resource_consumption_forbidden')
    destination=TAIL.sub('',w.group('destination')).strip()
    if not destination or destination in DEST_UNCLEAR or len(destination)>64:return None
    kind=c.group(1)
    carry='piggyback' if kind.startswith('背') else 'princess_carry' if '抱' in kind else 'handhold'
    return Intent('start',destination,carry)

def _reachable_scenes():
    """小光现在能去的图（地图目录 + 录制）；取不到时返回空集合，表示不过滤。"""
    try:
        import xg_runtime
        return {m['scene'] for m in xg_runtime.startable_maps()}
    except Exception:
        return set()

def _alias_rows():
    try:
        from destination_resolver_v25 import load_default
        r=load_default();rows=getattr(r,'_records',{})
        rows=rows.values() if isinstance(rows,dict) else rows
    except Exception:
        return []
    out=[]
    reach=_reachable_scenes()
    for row in rows:
        kind=row.get('kind')
        scene=row.get('sceneName')
        if kind=='landmark':
            # 2026-11-07 T18：千鸟城这种“图里的命名落点”可以纠错，但必须已经绑定场景+变体。
            if not row.get('spawnVariant') or not scene or (reach and scene not in reach):
                continue
        elif kind!='scene' or (reach and scene not in reach):
            continue   # 只提议小光真能去的地点（去不了的图/地标不提议，否则会反复问同一个名字）
        dn=row.get('displayName') or ''
        keys={_loose(x) for x in row.get('aliases',[]) if isinstance(x,str)}
        show=dn if dn and _loose(dn) in keys else None   # 提示时说短主名（须能被认出）
        for a in row.get('aliases',[]):
            if isinstance(a,str) and not a.isascii():
                clean=not re.search(r'[()（）:：A-Za-z]',a)       # 匹配到的名字本身干净就照它说，否则说短主名
                out.append((a,scene,a if clean else (show or a)))
    return out

def _closest(query):
    """近似地名：唯一明显的最佳候选才返回（≥0.5 且比第二名的别的图高 0.08），返回可直接用的别名。"""
    import difflib
    q=_loose(query)
    if len(q)<2:return None
    rows=_alias_rows()
    # 2026-11-07 T19：能叫出完整名字（包括千鸟城这种命名落点）就直接用，
    # 绝不再纠错成它所在的图（霞谷神殿），否则会把变体请求降级成原点。
    if any(_loose(alias)==q for alias,_scene,_show in rows):return None
    best={}
    for alias,scene,show in rows:
        k=_loose(alias)
        if not k:continue
        s=difflib.SequenceMatcher(None,q,k).ratio()-0.05*abs(len(q)-len(k))   # 2026-11-07：长度相近优先（姆明屋夜空→姆明屋天空）
        if s>best.get(scene,(0,None))[0]:best[scene]=(s,show)
    ranked=sorted(best.items(),key=lambda kv:-kv[1][0])
    if ranked and _loose(ranked[0][1][1])==q:return None   # 提议和原话一样：说明这个名字认不出来，别再反问同一个
    if not ranked or ranked[0][1][0]<0.5:return None
    if len(ranked)>1 and ranked[0][1][0]-ranked[1][1][0]<0.08:return None
    return ranked[0][1][1]

def place_answer(text):
    """“/千鸟城在哪”这类问路：地名已绑定时直接答在哪个图，不再走纠错/大模型。"""
    if type(text) is not str:return None
    m=PLACE_Q.fullmatch(text.strip())
    if not m:return None
    name=TAIL.sub('',m.group('name')).strip()
    if not name or len(name)>32:return None
    try:
        from destination_resolver_v25 import load_default
        r=load_default();res=r.resolve(name);dest=res.get('destination') or {}
        rows=getattr(r,'_records',{})
        rows=rows.values() if isinstance(rows,dict) else rows
        if dest.get('kind')=='landmark' and dest.get('sceneName'):
            scene=dest['sceneName'];label=dest.get('displayName') or name;mapname=scene
            for row in rows:
                if row.get('kind')=='scene' and row.get('sceneName')==scene:
                    mapname=row.get('displayName') or scene;break
            if not dest.get('spawnVariant'):
                return f'{label}在{mapname}附近，但我还不能直接带你落到那里～'
            return f'{label}在{mapname}里～发“/带我去{label}”我就带你过去。'
        if dest.get('kind')=='scene':
            return f'{dest.get("displayName") or name}是一张能去的图～发“/带我去{name}”就行。'
    except Exception:
        pass
    return None

def _confirm_proposal(bot,sender,ch,rows):
    """对上一次“你说的是不是X呀？”的确认：同一人、同频道、60 秒内，回“对/走吧”等就当作完整带路指令。"""
    router=getattr(bot,'escort_travel_router',None)
    p=getattr(router,'proposal',None) if isinstance(router,Router) else None
    last=rows[-1] if rows else None
    origin=last.get('travel_origin') if isinstance(last,dict) else None
    if not p or not isinstance(origin,Origin):return None
    if p['speaker']!=sender or p['channel']!=ch or origin.speaker_uuid!=sender:return None
    if router.clock()>p['expires']:
        router.proposal=None;return None
    m=SLASH.match(origin.raw_message);text=LEAD.sub('',(m.group(1) if m else '').strip(),count=1)
    if DENY.match(text):
        router.proposal=None
        return Decision(True,'好～那你想去哪儿？',reason='suggestion_declined')
    if not CONFIRM.fullmatch(text):return None
    router.proposal=None
    cmd={'handhold':'带我去','piggyback':'背我去','princess_carry':'抱我去'}.get(p['kind'],'带我去')
    if _loose(p['alias']) in HOME_KEYS:cmd=cmd[:-1]+'回'
    raw='/'+cmd+p['alias']
    synth=Origin(origin.speaker_uuid,raw,origin.line_id,origin.channel,origin.chat_session,origin.ingress_revision,origin.observed_mono)
    intent=parse(raw)
    if intent is None or intent.operation!='start':return Decision(True,'这次没接上，你再说一次地点？',reason='confirm_parse_failed')
    def current():return getattr(bot,'_travel_chat_session',None)==synth.chat_session and getattr(bot,'_travel_ingress_revision',None)==synth.ingress_revision
    return router.route(synth,intent,still_current=current)

# Choices are identity-only proposals. Movement still goes through Router.route,
# the exact same ingress checks and the configured control port as explicit commands.
CHOICE_TTL = 120
CHOICE_LIMIT = 8
# The catalogue uses the official English event family, not the colloquial Chinese
# season name. Keep this translation tied to its scene family, never to a guessed ID.
CHOICE_SERIES = {'万圣节': 'Event_DaysOfMischief', '万圣': 'Event_DaysOfMischief'}


def _choice_records():
    try:
        from destination_resolver_v25 import load_default
        return list(load_default()._records.values())
    except Exception:
        return []


def _choice_catalogue(router):
    if router.resolver is None:return []
    out=[];seen=set()
    for row in _choice_records():
        name=row.get('displayName');scene=row.get('sceneName')
        if not isinstance(name,str) or not name or not scene:continue
        if row.get('kind')!='scene' and not (row.get('kind')=='landmark' and row.get('spawnVariant')):continue
        aliases=tuple(dict.fromkeys(x for x in [name,*row.get('aliases',[]),row.get('id')]
                                   if isinstance(x,str) and x))
        for query in aliases:
            try:ref=router.resolver(query)
            except Exception:continue
            if (isinstance(ref,DestinationRef) and ref.valid() and ref.scene==scene
                    and ref.variant==row.get('spawnVariant')):break
        else:continue
        if ref in seen:continue
        seen.add(ref)
        out.append({'name':name,'query':query,'aliases':aliases,'ref':ref})
    return out


def _choice_query(query):
    q=_loose(TAIL.sub('',query or ''))
    # Strip request classifiers, not arbitrary characters from destination names.
    return re.sub(r'(?:的)?(?:地图|系列|相关地图|相关的图|的图|图)$','',q)


def _find_choices(router,query):
    q=_choice_query(query)
    if not q:return []
    catalogue=_choice_catalogue(router)
    prefix=CHOICE_SERIES.get(q)
    if prefix:
        return [c for c in catalogue if c['ref'].scene==prefix or c['ref'].scene.startswith(prefix+'_')][:CHOICE_LIMIT]
    # Ambiguous aliases and series aliases are included here too. Preserve catalogue
    # order so the numbers in the response and the pending proposal always agree.
    return [c for c in catalogue if any(q in _loose(a) or _loose(a) in q for a in c['aliases']
                                        if len(_loose(a))>=2)][:CHOICE_LIMIT]


def _choice_key(origin):return (origin.speaker_uuid,origin.channel)


def _remember_choices(router,origin,candidates,kind='handhold'):
    now=router.clock()
    router.choice_proposals={key:p for key,p in router.choice_proposals.items() if p['expires']>now}
    router.choice_proposals[_choice_key(origin)]={
        'speaker':origin.speaker_uuid,'channel':origin.channel,'session':origin.chat_session,
        'candidates':tuple(candidates),'kind':kind or 'handhold','expires':now+CHOICE_TTL,'asked':False}
    # A newer list supersedes an older correction for this conversation only.
    if router.proposal and router.proposal['speaker']==origin.speaker_uuid and router.proposal['channel']==origin.channel:
        router.proposal=None


def _choice_listing(candidates):
    return '可以去：'+'；'.join(f'{i}、{c["name"]}' for i,c in enumerate(candidates,1))+'。你想去哪个？'


def _choice_origin_current(bot,router,origin,sender,ch):
    return (isinstance(origin,Origin) and canonical_uuid(origin.speaker_uuid)
            and canonical_uuid(origin.chat_session) and origin.speaker_uuid==sender and origin.channel==ch
            and type(origin.channel) is str and type(origin.raw_message) is str
            and len(origin.raw_message)<=320 and SLASH.match(origin.raw_message) is not None
            and type(origin.ingress_revision) is int and origin.ingress_revision>0
            and type(origin.line_id) in (str,int) and bool(origin.line_id)
            and finite(origin.observed_mono) and 0<=router.clock()-origin.observed_mono<=30
            and getattr(bot,'_travel_chat_session',None)==origin.chat_session
            and getattr(bot,'_travel_ingress_revision',None)==origin.ingress_revision
            and (origin.chat_session,origin.ingress_revision) not in router._consumed)


def _choice_explicit(raw):
    m=SLASH.match(raw)
    if not m:return False
    text=m.group(1).strip();body=LEAD.sub('',text,count=1)
    return bool(CANCEL.fullmatch(text) or START.fullmatch(body) or _parse_want(text) is not None)


def _choice_declines(text):
    bare=re.sub(r'[\s，,!！。～~…]+$','',LEAD.sub('',text,count=1))
    return re.fullmatch(r'(?:不去(?:了|啦)?|算了|都不要|都不去|不要了|不用了|取消|不是|不对|不要|哪也不去)(?:吧|呀|呗|哦)?',bare) is not None


def _choice_matches(text,candidates):
    # Questions, reported speech and negated mentions are chat, not consent to go.
    if (QUERY_BLOCK.search(text) or RESOURCE.search(text)
            or re.search(r'[?？]|(?:不|没|别|莫)(?:想|要|用)?(?:去|选|带|背|抱|牵)|不选|不要|'
                         r'在哪|哪里|怎么|什么|好玩|好看|喜欢|听说|介绍|聊聊|记得|昨天|能去吗|能不能',text)):
        return []
    cleaned=TAIL.sub('',text).strip()
    hits=set()
    ordinal={'一':0,'二':1,'两':1,'三':2,'四':3,'五':4,'六':5,'七':6,'八':7}
    for match in re.finditer(r'第([一二两三四五六七八1-8])(?:个|张|处)',cleaned):
        n=match.group(1);index=int(n)-1 if n.isdigit() else ordinal[n]
        if index<len(candidates):hits.add(index)
    for token,index in [('前面那个',0),('前一个',0),('最后那个',len(candidates)-1),
                        ('最后一个',len(candidates)-1),('后面那个',len(candidates)-1),('后一个',len(candidates)-1)]:
        if token in cleaned:hits.add(index)
    key=_loose(cleaned)
    for i,c in enumerate(candidates):
        if any(len(_loose(a))>=2 and _loose(a) in key for a in c['aliases']):hits.add(i)
    if hits:
        frame=_loose(cleaned)
        for alias in sorted({a for i in hits for a in candidates[i]['aliases']},key=len,reverse=True):
            frame=frame.replace(_loose(alias),'')
        frame=re.sub(r'第[一二两三四五六七八1-8](?:个|张|处)|前面那个|前一个|最后那个|最后一个|后面那个|后一个','',frame)
        frame=re.sub(r'我想要|我想|我要|想要|那就|就|还是|选择|选|我|去|到|前往|那个|这个|那一个|这一个|那张|这张|'
                     r'好了|或者|或|和|与|都|吧|呀|呗|嘛|啦|呢|哦|好|[\s，,、。.!！～~]','',frame)
        return sorted(hits) if not frame else []
    # Short unique names such as “就密室那个” and “螃蟹那个吧”. Only remove
    # selection framing: unrelated prose that happens to mention a place stays chat.
    core=LEAD.sub('',cleaned,count=1)
    core=re.sub(r'^(?:(?:我)?(?:想要|想|要)|就|还是|选择|选|去|到|前往)+','',core)
    core=re.sub(r'(?:那个|这个|那一个|这一个|那张|这张|地图|的图|好了)+$','',core)
    q=_loose(core)
    if len(q)<2:return []
    return [i for i,c in enumerate(candidates) if any(q in _loose(a) for a in c['aliases'])]


def _select_choice(bot,sender,ch,rows):
    router=getattr(bot,'escort_travel_router',None)
    if not isinstance(router,Router):return None
    last=rows[-1] if rows else None
    origin=last.get('travel_origin') if isinstance(last,dict) else None
    if not isinstance(origin,Origin):return None
    key=(sender,ch);p=router.choice_proposals.get(key)
    if not p:return None
    if router.clock()>=p['expires']:
        router.choice_proposals.pop(key,None);return None
    if not _choice_origin_current(bot,router,origin,sender,ch):
        return Decision(True,None,reason='unbound_or_expired_choice_origin')
    if p['session']!=origin.chat_session:
        router.choice_proposals.pop(key,None);return None
    # A newer full command/decline in the same merged batch also invalidates
    # the old list. Never execute that older ingress; retain route_pending's
    # existing newest-message precedence instead of resurrecting a stale choice.
    for row in rows[:-1]:
        previous=row.get('travel_origin') if isinstance(row,dict) else None
        if not (isinstance(previous,Origin) and previous.speaker_uuid==sender and previous.channel==ch
                and previous.chat_session==origin.chat_session
                and type(previous.ingress_revision) is int and 0<previous.ingress_revision<origin.ingress_revision
                and finite(previous.observed_mono) and 0<=router.clock()-previous.observed_mono<=30
                and type(previous.raw_message) is str and len(previous.raw_message)<=320):continue
        match=SLASH.match(previous.raw_message)
        if match and (_choice_declines(match.group(1).strip())
                      or (parse(previous.raw_message) is not None and _choice_explicit(previous.raw_message))):
            router.choice_proposals.pop(key,None);return None
    text=SLASH.match(origin.raw_message).group(1).strip()
    if _choice_declines(text):
        router.choice_proposals.pop(key,None)
        return Decision(True,'好～那就先不去了。',reason='choice_declined')
    if _choice_explicit(origin.raw_message):
        router.choice_proposals.pop(key,None);return None
    hits=_choice_matches(text,p['candidates'])
    if not hits:return None
    if len(hits)!=1:
        reply=None if p['asked'] else '这几个都对得上，'+_choice_listing(p['candidates'])
        p['asked']=True
        return Decision(True,reply,reason='choice_ambiguous')
    chosen=p['candidates'][hits[0]]
    # Ref identity must still be executable now, not merely when the list was sent.
    try:current_ref=router.resolver(chosen['query'])
    except Exception:current_ref=None
    if current_ref!=chosen['ref']:
        router.choice_proposals.pop(key,None)
        return Decision(True,'这个地点现在还没确认好，你再说一次地点？',reason='choice_destination_changed')
    router.choice_proposals.pop(key,None)
    cmd={'handhold':'带我去','piggyback':'背我去','princess_carry':'抱我去'}.get(p['kind'],'带我去')
    raw='/'+cmd+chosen['query']
    synth=Origin(origin.speaker_uuid,raw,origin.line_id,origin.channel,origin.chat_session,
                 origin.ingress_revision,origin.observed_mono)
    intent=parse(raw)
    if intent is None or intent.operation!='start':return Decision(True,None,reason='choice_parse_failed')
    def current():return getattr(bot,'_travel_chat_session',None)==synth.chat_session and getattr(bot,'_travel_ingress_revision',None)==synth.ingress_revision
    return router.route(synth,intent,still_current=current)


def record_list_maps_choices(bot,sender,ch,rows,reply,payloads):
    """Remember only executable names actually sent after this turn's list_maps.

    The caller supplies sent (not merely generated) reply text and tool payloads
    scoped to this turn. No model text is ever interpreted as a movement command.
    """
    if not isinstance(reply,str) or not reply or not isinstance(payloads,(list,tuple)) or not payloads:return False
    if not isinstance(rows,(list,tuple)):return False
    rows=[r for r in rows if isinstance(r,dict)]
    origin=rows[-1].get('travel_origin') if rows else None
    router=getattr(bot,'escort_travel_router',None)
    if not isinstance(router,Router):
        router=default_router(bot);bot.escort_travel_router=router
    if not _choice_origin_current(bot,router,origin,sender,ch):return False
    catalogue=_choice_catalogue(router);eligible=[]
    for payload in payloads:
        if not isinstance(payload,dict) or payload.get('ok') is not True:continue
        maps=payload.get('maps')
        if not isinstance(maps,list):continue
        for row in maps:
            if not isinstance(row,dict):continue
            aliases=row.get('其它叫法',[])
            if not isinstance(aliases,list):continue
            names=[row.get('落点') or row.get('名称'),*aliases]
            for c in catalogue:
                if c in eligible:continue
                if any(isinstance(n,str) and _loose(n) in {_loose(a) for a in c['aliases']} for n in names):
                    eligible.append(c)
    mentions=[]
    for i,c in enumerate(eligible):
        for alias in c['aliases']:
            if len(alias)<2:continue
            for m in re.finditer(re.escape(alias),reply):mentions.append((m.start(),m.end(),i))
    # “霞谷神殿” should not also count as a separate mention of “霞谷”.
    visible=[m for m in mentions if not any(other[2]!=m[2] and other[0]<=m[0] and other[1]>=m[1]
                                           and other[1]-other[0]>m[1]-m[0] for other in mentions)]
    order=[]
    for start,end,index in sorted(visible):
        if index not in order:order.append(index)
    if len(order)>CHOICE_LIMIT:
        router.choice_proposals.pop(_choice_key(origin),None)
        return False
    if not order:return False
    intent=parse(origin.raw_message)
    kind=intent.carry_kind if intent is not None else None
    prior=router.choice_proposals.get(_choice_key(origin))
    if kind is None and prior and prior['session']==origin.chat_session and router.clock()<prior['expires']:
        kind=prior['kind']
    _remember_choices(router,origin,[eligible[i] for i in order],kind or 'handhold')
    return True


def capture_ingress(bot,sender,raw,ch,line_id):
    raw=raw.strip()  # 客户端消息可能带尾随换行，先归一化
    if not canonical_uuid(sender) or not SLASH.match(raw):return None
    with bot.lock:
        if not getattr(bot,'_travel_chat_session',None):bot._travel_chat_session=str(uuid4())
        bot._travel_ingress_revision=getattr(bot,'_travel_ingress_revision',0)+1
        return Origin(sender,raw,line_id,ch,bot._travel_chat_session,bot._travel_ingress_revision,time.monotonic())

def default_router(bot):
    # Real Companion.control shares this exact ROOT; no filesystem/process guessing.
    method=getattr(bot,'control',None);fn=getattr(method,'__func__',None)
    root=getattr(fn,'__globals__',{}).get('ROOT')
    if root is None:return Router()
    sp = Path(root)/'control/escort_travel.destinations.json'
    if not sp.is_file():
        sp = xg_paths.spawn_points()   # 随包的哥哥标注出生点（程序目录只读）
    return Router(resolver=RegistryResolver(sp),port=PoseControlPort(root))

def route_pending(bot,sender,ch,msgs):
    rows=[m for m in msgs if isinstance(m,dict)]
    selected=_select_choice(bot,sender,ch,rows)
    if selected is not None:return selected
    candidates=[m for m in rows if isinstance(m.get('travel_origin'),Origin) and parse(m['travel_origin'].raw_message) is not None]
    if not candidates:
        confirmed=_confirm_proposal(bot,sender,ch,rows)
        if confirmed is not None:return confirmed
        answer=place_answer(rows[-1].get('text') if rows and isinstance(rows[-1],dict) else None)
        if answer:return Decision(True,answer,reason='place_question')
        return Decision(False)
    last=rows[-1] if rows else None
    if last not in candidates:return Decision(False,remaining=(last,) if last is not None else None,reason='newer_nontravel_message')
    origin=last['travel_origin'];intent=parse(origin.raw_message)
    if origin.speaker_uuid!=sender or origin.channel!=ch:return Decision(True,None,reason='sender_or_channel_changed')
    def current():return getattr(bot,'_travel_chat_session',None)==origin.chat_session and getattr(bot,'_travel_ingress_revision',None)==origin.ingress_revision
    router=getattr(bot,'escort_travel_router',None)
    if not isinstance(router,Router):router=default_router(bot);bot.escort_travel_router=router
    return router.route(origin,intent,still_current=current)
