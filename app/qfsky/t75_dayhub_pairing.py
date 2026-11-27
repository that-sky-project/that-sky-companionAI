"""仅中央神坛、票面明确带主人的 WS 配对；不持有网络连接或密钥状态。"""
import os
import time
from collections import deque

from . import ticket_route0167 as route
from .join0167_codec import decode

LEVEL = 2394719185
_BOUND = object()
_FIRST_ONLY = object()
_R3 = object()


def r3_enabled():
    return (enabled() and os.environ.get('SKY_T75_R2_DAYHUB_PAIRING','1')!='0'
            and os.environ.get('SKY_T75_R3_DAYHUB_PAIRING','1')!='0')


def enabled():
    return os.environ.get('SKY_T75_DAYHUB_PAIRING', '1') != '0'


def scoped(map_id, level, origin, policy, owner, ticket):
    return (map_id == level == ticket.get('level') == LEVEL and
            origin == 'ws_same_map' and policy == 'pairing' and enabled() and
            isinstance(owner, bytes) and len(owner) == 16 and
            owner in route.merge_others(ticket.get('other_players') or []))


def select_r2(pump, ticket):
    """None=原路径，False=所需签发字段无效，True=已选好这张票唯一的 Join 字段。"""
    if (os.environ.get('SKY_T75_R2_DAYHUB_PAIRING','1') == '0' or
            not scoped(pump._map_id, ticket.get('level'), 'ws_same_map', 'pairing',
                       pump._owner_uuid, ticket)):
        return None
    # watch_moves 可能已经按旧环境变量交换过字段；依据它留下的原始字段名还原。
    label = ticket.get('_t75_first_field','sig')
    preferred = 'sig' if r3_enabled() else 'signature'
    chosen = ticket.get('sig' if label==preferred else 'signature')
    if label not in ('sig','signature') or not isinstance(chosen,str) or len(chosen)!=64:
        return False
    try:
        if len(bytes.fromhex(chosen)) != 32:
            return False
    except ValueError:
        return False
    if label!=preferred:
        ticket['sig'],ticket['signature']=chosen,ticket.get('sig')
    ticket['_t75_first_field']=preferred
    ticket['_t75_r2_first_only']=_FIRST_ONLY
    if preferred=='sig':
        ticket['_t75_r3_scope']=_R3
    return True


def first_only(slot):
    core=getattr(getattr(slot,'plan',None),'ticket_core',None)
    return isinstance(core,dict) and core.get('_t75_r2_first_only') is _FIRST_ONLY


def r3_slot(slot):
    core=getattr(getattr(slot,'plan',None),'ticket_core',None)
    return isinstance(core,dict) and core.get('_t75_r3_scope') is _R3


def remember_control(pump, slot, *, move=None, reply=None):
    """本图活动槽的被动观察：只记已经解出的 Move、已经发完的 MoveReply，不产生帧。"""
    try:
        if pump._map_id!=LEVEL or slot is not pump.active or not r3_enabled():
            return
        reg=pump._registration
        memo=getattr(pump,'_t75_r3_controls',None)
        if memo is None or memo[0] is not reg or memo[1] is not slot:
            memo=(reg,slot,deque(maxlen=16))
            pump._t75_r3_controls=memo
        now=pump._clock()
        if move is not None:
            memo[2].append((now,'move9',tuple(move.endpoint),move.move_ts,
                            len(move.others),bool(move.split_requested)))
        else:
            memo[2].append((now,'reply10',int(reply)))
    except Exception:
        pass  # 观测失败不能影响原 Move 的处理


def _r3_wire(pump,slot):
    plan,c=slot.plan,slot.plan.ticket_core
    pump._t75_r3_pair_anchor=(pump._registration,pump.active,pump._clock())
    ws=c.get('_t75_r3_ws_mono_ns')
    watch=c.get('_t75_r3_watch_mono_ns')
    # 只比较同机单调时钟。异常顺序、缺失、超出票龄窗口一律 null，不用秒级墙钟补零。
    staged=int(slot.started_at*1e9)
    sent=int(slot.join_sent_at*1e9)
    def span(end,start):
        return (round((end-start)/1e6,1) if type(start) is int and type(end) is int
                and 0<start<=end and end-start<30_000_000_000 else None)
    memo=getattr(pump,'_t75_r3_controls',None)
    old=[]
    if memo is not None and memo[0] is pump._registration and memo[1] is pump.active:
        for row in memo[2]:
            age=round((slot.join_sent_at-row[0])*1000)
            if not 0<=age<=8000:continue
            e=dict(before_join_ms=age,kind=row[1])
            if row[1]=='move9':
                e.update(same_target=row[2]==plan.origin_endpoint,exact_ts=row[3]==plan.move_ts,
                         others=row[4],split=row[5])
            else:e['result']=row[2]
            old.append(e)
    return dict(ws_to_watch_ms=span(watch,ws),watch_to_stage_ms=span(staged,watch),
                stage_to_join_ms=span(sent,staged),ws_to_join_ms=span(sent,ws),
                observed_old_controls_8s=old)


def bind(pump, core, frame):
    # 经成功迁移退役的最近一个逻辑槽，不是眼下的活动槽，也不是失败的 pending。
    # 只继承应用层的一个公开顺序字节；传输、ACK、注册代数仍全新。
    prev = next((s for s in reversed(pump._all) if s.stage == 'retired'), None)
    value = prev.counter if prev is not None else 0
    if (not isinstance(frame, bytes) or len(frame) < 6 or frame[4:6] != b'\x03\x00'
            or type(value) is not int or not 0 <= value <= 255):
        return frame
    core['_t75_bound'] = _BOUND
    core['_t75_reused'] = prev is not None
    return frame[:5] + bytes((value,)) + frame[6:]


def bound(slot):
    plan = getattr(slot, 'plan', None)
    core = getattr(plan, 'ticket_core', None)
    return isinstance(core, dict) and core.get('_t75_bound') is _BOUND


def _mask(data):
    return {'bytes': len(data), 'first2': data[:2].hex()}


def _ep(text):
    return route.endpoint_key(route.split_uri(text) or ())


def facts(slot):
    p = slot.plan
    selected = p.ticket_core.get('_t75_first_field', '')
    if selected not in ('sig', 'signature'):
        selected = 'unknown'
    elif p.sig_swapped:
        selected = 'sig' if selected == 'signature' else 'signature'
    return dict(move_ts=p.move_ts, source_field=selected, alternate=bool(p.sig_swapped))


def sent(pump, slot):
    # 解真正交给 queue_join 的那一帧。严格白名单：禁止 repr(plan)/原始 frame。
    try:
        if first_only(slot):
            # 换场景会替换登记模板；据对象身份限制本段 ValueError 定位的作用域。
            pump._t75_r2_pair_registration = pump._registration
        r3=_r3_wire(pump,slot) if r3_slot(slot) else {}
        p, c = slot.plan, slot.plan.ticket_core
        f = decode(p.plaintext, frame=True)
        prev = route.endpoint_key(p.previous_endpoint)
        active = route.endpoint_key(pump.active.ticket_origin)
        src = _ep(c.get('src_uri'))
        def ipv4_endpoint(ip, port):
            return route.endpoint_key(('.'.join(str(b) for b in ip), port)) if len(ip)==4 else ''
        pump._event('t75_pair_wire', **facts(slot),
            ticket_age_ms=round(time.time() * 1000 - (p.move_ts if p.move_ts > 10**11 else p.move_ts*1000)),
            ts_unit='ms' if p.move_ts > 10**11 else 's',
            src=src, active_origin=active, previous=prev,
            src_matches_active=(src == active if src and active else None),
            wire_previous=ipv4_endpoint(f['previous_address'],f['previous_port']),
            wire_target=ipv4_endpoint(f['ticket_ipv4'],f['ticket_port']),
            previous_tag=f['previous_address_tag'], previous_level=f['previous_level_id'],
            ticket_origin=route.endpoint_key(p.origin_endpoint), dial=pump._dial_text(slot),
            dial_source=pump._dial_source(slot), ts_on_wire=f['move_ts'],
            level=f['level_id'], flag=f['ticket_flag'], others=len(f['others']),
            owner_in_others=p.expected_owner in f['others'],
            counter=f['counter'], counter_source='retired' if c['_t75_reused'] else 'fresh',
            active_counter=pump.active.counter, tail=[f['tail_bool_a'],f['tail_bool_b'],f['tail_u8']],
            extras=[f['ticket_extra_u32_a'], f['ticket_extra_u8'], f['ticket_extra_u32_b']],
            uid=_mask(f['uid']), session=_mask(f['session']), peer_key=_mask(p.plaintext[:4]),
            join_signature=_mask(f['signature']), other_signature=_mask(p.signature_alt or b''),
            rc4_key_matches_join=(slot.transport.app_rc4_key==f['signature']
                                  if hasattr(slot.transport,'app_rc4_key') else None),
            versions=[f['build'], f['server_version'], f['client_version']],
            nv=f['netversion'], level_flag=f['level_flag'], state=[f['state_extra_u32_a'],f['state_extra_u32_b']],
            blob_bytes=len(f['blob']), join_bytes=len(p.plaintext), b264=(bytes(f['blob'][264:267]).hex() if len(f['blob'])>267 else None), **r3)
    except Exception as exc:
        pump._event('t75_pair_diag_error', where='sent', error=type(exc).__name__)


def terminal(pump, slot, layer, code=None, types=()):
    try:
        extra={}
        if first_only(slot) and layer=='application' and 2 in types:
            import hashlib
            from .t75_reject_text import describe
            details=[]
            for m in slot.buffered:
                if m.message_type!=2:
                    continue
                row=describe(m.body)
                row['compressed']=bool(getattr(m,'compressed',False))
                if not row.get('format_matched') and len(m.body)<=32:
                    row['hex']=m.body.hex()  # Claude 11-21：非文本格式时记原始短正文以便解码
                # Claude 11-21：拒因原文（纯字母句）与非文本短正文照记，这正是要找的原因。
                details.append(row)
                if len(details)==1:
                    # 只输出本段跨票正文是否完全相同；不输出正文或可离线枚举的摘要。
                    stamp=slot.plan.move_ts
                    last=getattr(pump,'_t75_r2_previous_rejection',None)
                    fingerprint=hashlib.sha256(m.body).digest()
                    extra['same_body_as_previous_ticket']=(last[1]==fingerprint
                        if last and last[0]!=stamp else None)
                    pump._t75_r2_previous_rejection=(stamp,fingerprint)
                if len(details)==4:
                    break
            extra['outer2_detail']=details
        try:
            ack = slot.transport.reliable_status(slot.token) if slot.token is not None else 'not_sent'
        except Exception:
            ack = 'unknown'
        pump._event('t75_pair_terminal', **facts(slot), layer=layer, code=code,
            stage=slot.stage, dial=pump._dial_text(slot), dial_source=pump._dial_source(slot),
            after_join_ms=(round((pump._clock()-slot.join_sent_at)*1000) if slot.stage=='enter' else None),
            join_ack=ack if ack in ('pending','acked','failed','not_sent') else 'unknown',
            outer=list(types)[:8], messages=[(m.message_type,len(m.body)) for m in slot.buffered[-6:]],
            envelope_decode_failed=(getattr(slot.transport,'receive_error','')=='current server envelope decode failed'),
            received_apps=getattr(slot.transport,'raw_packets',0), **extra)
    except Exception as exc:
        pump._event('t75_pair_diag_error', where='terminal', error=type(exc).__name__)


def observe_move(pump, slot, move):
    # 如果服务端恰好在旧连接补发 Move，这是同一接收方的比较，强于跨账号比前缀。
    try:
        p = slot.plan
        def seconds(ts):
            return ts//1000 if ts>10**11 else ts
        pump._event('t75_pair_move', **facts(slot),
            same_origin=(tuple(move.endpoint)==tuple(p.origin_endpoint)),
            same_second=(seconds(move.move_ts)==seconds(p.move_ts)), same_ts=(move.move_ts==p.move_ts),
            matches_selected=(move.signature==p.signature), matches_other=(move.signature==p.signature_alt),
            inband_split=getattr(move,'split_requested',None),
            inband_others=len(move.others), inband_owner=p.expected_owner in move.others)
    except Exception as exc:
        pump._event('t75_pair_diag_error', where='move', error=type(exc).__name__)
