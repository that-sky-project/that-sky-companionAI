"""Owned-account current connection lifecycle for explicit, bounded web actions.

No scheduler, automatic ticket retry, reward action or historical burst. A switch
closes the previous connection before requesting a new ticket (not seamless).
Public state deliberately excludes credentials, account UUIDs and server addresses.
"""
from __future__ import annotations
import xg_paths as _xg_paths
import copy, math, os, re, struct, threading, time
from dataclasses import dataclass,field
from pathlib import Path
import requests
from . import current_join,fp,netcfg,handshake0167_fields,register0167_codec
from .join0167_profile import build_from_cache,uuid_raw
from .receive0167 import validate_pending_enter_game
from .scene_control0167 import SceneConfirmation,frame_scene_reply
from .handshake0167_transport import ProbeTransport,ProbeTransportError
from .type2_dictionary0167 import load_type2_decoder
from .skyapi import SkyClient,SkyApiError

BUILD=421589
TICKET_URL='https://live-as-sky-merge.game.163.com/account/find_previous_or_empty'
PUBLIC_FIELDS={'phase','running','map_id','scene','duration','elapsed_seconds','reply_count',
 'scene_request_count','ignored_scene_requests','http_status','enter_game_valid','registration_transport_acked',
 'matching_generation_observed','reason','finished','generation','transport_closed','disconnect_sent',
 'error_code','application_packets','received_datagrams','sent_datagrams','crc_failures','ticket_requests','remote_disconnect_code','pending_reliable_commands','connection_kind','long_groups','long_actor_frames','long_controller_frames','long_scene_frames','long_feedback_frames','terminal_message_type','move_received','move_started','move_completed','move_busy_replies','move_ignored','move_graceful_closed','move_forced_closed','move_in_progress'}

class ConnectionCancelled(Exception):pass
class ConnectionFault(Exception):
    def __init__(self,code):self.code=code

@dataclass
class Prepared:
    account:dict=field(repr=False)
    profile:object=field(repr=False)
    decoder:object=field(repr=False)
    scene:str
    map_id:int
    resource_revision:str
    long_templates:tuple|None=field(default=None,repr=False)
    current_inputs:object=field(default=None,repr=False)

def _validate_account(account,build):
    if build!=BUILD or current_join.mode()!='current':raise ValueError('仅支持当前 0.16.7 / 421589 模式')
    if not isinstance(account,dict) or not account.get('id'):
        raise ValueError('缺少账号归属信息')
    # /session authorizes the tenant server-side. This public broker reference
    # binds the local record; it is not a tenant token or private SDK renew data.
    public_ref=account.get('renew')
    broker_owned=(isinstance(account['id'],str)
        and isinstance(public_ref,dict) and set(public_ref)=={'broker_account_id'}
        and isinstance(public_ref['broker_account_id'],str)
        and public_ref['broker_account_id']==account['id'])
    if not account.get('owner') and not broker_owned:
        raise ValueError('缺少账号归属信息')
    # Login platform is not the captured wire-format platform. Both official
    # account types use their unmodified broker session. The prepared capture
    # still has to pass account/build/revision/scene/netversion validation below.
    if account.get('channel')!='netease' or account.get('platform') not in ('android','ios'):
        raise ValueError('当前连接入口仅支持网易国服 Android / iOS 账号')
    try:
        uuid_raw(account['user_uuid'])
        if len(bytes.fromhex(account['session']))!=16 or not account['session2']:raise ValueError()
        account['session2'].encode('ascii')
    except (KeyError,TypeError,ValueError,AttributeError,UnicodeError):raise ValueError('当前账号缺少有效的本地会话') from None
    if os.environ.get('QFLY_LOCAL_NETWORK_MODE')=='offline':raise ValueError('当前配置未开启游戏网络连接')
    if account.get('proxy_id') or account.get('proxy_mode') in ('auto','manual') or netcfg.enabled():
        raise ValueError('当前受控连接入口需要直接连接，未启用代理轮换')

def _dictionary(doc):
    value=doc.get('dictionary_path') or os.environ.get('QFLY_0167_DICTIONARY','')
    if not value:raise ValueError('缺少当前 APK 的压缩字典路径配置')
    path=Path(value)
    if not path.is_absolute():path=Path(os.environ['QFLY_JOIN_CONFIG']).resolve().parent/path
    return load_type2_decoder(path.resolve(strict=True))

def _prepare(account,build,map_id):
    _validate_account(account,build)
    if type(map_id) is not int or not 0<=map_id<=0xffffffff:raise ValueError('地图编号格式错误')
    try:
        profile,entry,root,revision=current_join.load_profile(map_id,account['user_uuid'],build)
        doc,_,_=current_join.config();decoder=_dictionary(doc)
        fp.device(account['user_uuid'])
    except (ValueError,OSError,KeyError,TypeError):
        raise ValueError('该地图的账号、版本、资源缓存或字典检查未通过') from None
    return Prepared(copy.deepcopy(account),profile,decoder,str(entry['scene']),map_id,revision)

def prepare_home_long(account,build=BUILD,map_id=3526133726):
    if map_id!=3526133726:raise ValueError('当前长连接仅适配遇境')
    prepared=_prepare(account,build,map_id)
    from .long0167 import validate_templates
    doc,_,_=current_join.config();items=doc.get('home_long_templates',{})
    root=Path(os.environ['QFLY_JOIN_CONFIG']).resolve().parent
    values=[]
    for name,size in (('scene',4292),('actor',814)):
        item=Path(items[name]);item=item if item.is_absolute() else root/item
        if item.stat().st_size!=size:raise ValueError('长连接模板长度不匹配')
        values.append(item.read_bytes())
    validate_templates(*values)
    prepared.long_templates=tuple(values)
    return prepared

TICKET_OVERRIDE = {}  # claudework：{'ticket': {uri,move_ts,sig,platform_filter}}，下一次长连接入服用它（WS move 推来的票），用一次就清


def _cw_join_players():
    """CLAUDEWORK_JOIN_PLAYERS=uuid[,uuid]：拉票时请服务器优先分到这些玩家所在的实例（默认空 = 原行为）。"""
    import os
    raw = os.environ.get("CLAUDEWORK_JOIN_PLAYERS", "").strip()
    return [u.strip() for u in raw.split(",") if u.strip()]


def _catalog_preferred():
    import json as _json
    try:
        import os as _os
        f=_os.environ.get('QFSKY_CATALOG_PREFERRED_FILE',str(_xg_paths.CONTROL/'catalog_preferred.json'))
        v=_json.loads(open(f,encoding='utf-8').read()).get('maps',[])
        return {int(x) for x in v}
    except (OSError,ValueError,TypeError,AttributeError):
        return set()

def prepare_current_long(account,build=BUILD,map_id=None,*,variant=None):
    """One explicit complete capture; never combine an old Join and new Actor.

    2026-11-07 T18：variant 只由带路命令（命名落点）显式传入；None 时与
    T17 的选路完全一致（后台点图/好友传送/录制图都不带 variant）。
    """
    _validate_account(account,build)
    if type(map_id)is not int or not 0<map_id<=0xffffffff:raise ValueError('map_capture_invalid_map')
    if variant is not None and(type(variant)is not str or re.fullmatch(r'[a-z0-9_]{1,32}',variant)is None):
        raise ValueError('spawn_variant_invalid')
    doc,root,revision=current_join.config()
    entry=doc['maps'].get(str(map_id))
    capture=entry.get('current_capture') if isinstance(entry,dict) else None
    if variant is not None:
        # 显式变体：只走目录（变体是目录的一部分），不使用录制数据。
        from .asset_catalog_runtime_v32c import prepare_catalog_connection
        trial=prepare_catalog_connection(account,build,map_id,decoder=_dictionary(doc),variant=variant)
        if trial is None:raise ValueError('map_spawn_variant_not_configured')
        fp.device(account['user_uuid'])
        return trial
    if isinstance(capture,dict) and map_id in _catalog_preferred():
        # 2026-11-06：名单里的图优先用地图目录出生点；目录里没有/校验不过就照旧用录制。
        from .asset_catalog_runtime_v32c import prepare_catalog_connection
        try:
            trial=prepare_catalog_connection(account,build,map_id,decoder=_dictionary(doc))
        except Exception:
            trial=None
        if trial is not None:
            fp.device(account['user_uuid'])
            return trial
    if not isinstance(capture,dict):
        from .asset_bootstrap_runtime_v32 import prepare_asset_trial_connection
        trial=prepare_asset_trial_connection(account,build,map_id,decoder=_dictionary(doc))
        if trial is None:
            from .asset_catalog_runtime_v32c import prepare_catalog_connection
            trial=prepare_catalog_connection(account,build,map_id,decoder=_dictionary(doc))
        if trial is not None:
            fp.device(account['user_uuid'])
            return trial
        raise ValueError('map_capture_not_configured')
    own=(capture.get('by_account')or{}).get(account['user_uuid'])  # claudework：每个号用自己录的
    if isinstance(own,dict):capture={**capture,**own}
    elif capture.get('account') and capture['account']!=account['user_uuid']:
        raise ValueError('map_capture_not_recorded_for_account')
    directory=capture.get('record_dir');nv=capture.get('netversion_u16');scene=entry.get('scene')
    if type(directory)is not str or not directory or len(directory)>240:
        raise ValueError('map_capture_config_invalid')
    if (type(scene)is not str or not scene or type(nv)not in(list,tuple)or len(nv)!=6
        or any(type(x)is not int or not 0<=x<=65535 for x in nv)):
        raise ValueError('map_capture_config_invalid')
    from .map_long0167 import prepare_current_capture
    capture=prepare_current_capture(root,directory,expected_account_uuid=account['user_uuid'],
        expected_scene=scene,expected_resource_revision=revision,expected_level_id=map_id,
        expected_netversion=nv,apk_build=build)
    decoder=_dictionary(doc);fp.device(account['user_uuid'])
    return Prepared(copy.deepcopy(account),capture.profile,decoder,scene,map_id,revision,
        current_inputs=capture.inputs)

def _current_ready_error(exc):
    # Never echo loader paths, account bindings, IDs or exception bodies.
    codes={'map_capture_join_fields_missing':'同次采集缺少入服字段，请补齐完整当前记录',
        'map_capture_not_configured':'未登记当前地图的完整 Scene 与 Actor 记录',
        'map_capture_config_invalid':'当前记录目录或版本参数配置不完整'}
    code=str(exc) if isinstance(exc,ValueError) else ''
    if code not in codes:code='map_capture_not_ready'
    return code,codes.get(code,'同账号、版本、资源和场景的完整输入校验尚未通过')

def readiness(account,build=BUILD):
    result={'enabled':False,'mode':'current','apk_build':build,'maps':[],'reason':'',
            'max_duration':180,'default_duration':15,'home_long_available':False,
            'home_long_reason':'遇境长连接模板或缓存未就绪','current_long_available':False}
    try:
        _validate_account(account,build);doc,_,revision=current_join.config();_dictionary(doc)
    except (ValueError,OSError,KeyError,TypeError):
        result['reason']='当前账号会话、渠道或本地缓存/字典配置尚未就绪'
        return result
    result['resource_revision']=revision
    for key,entry in doc['maps'].items():
        try:
            map_id=int(key);_prepare(account,build,map_id)
            row={'map_id':map_id,'scene':str(entry.get('scene','')),'ready':True,'reason':'账号与当前版本缓存检查通过'}
        except (ValueError,OSError,KeyError,TypeError):
            try:map_id=int(key)
            except (ValueError,TypeError):continue
            row={'map_id':map_id,'scene':str(entry.get('scene','')),'ready':False,'reason':'缓存绑定或场景检查未通过'}
        try:
            prepare_current_long(account,build,map_id)
            row.update(current_long_ready=True,current_long_error_code='',
                current_long_reason='同次完整 Scene、Actor 与入服字段检查通过；点击后才连接')
        except (ValueError,OSError,KeyError,TypeError,ImportError)as exc:
            code,reason=_current_ready_error(exc)
            row.update(current_long_ready=False,current_long_error_code=code,current_long_reason=reason)
        result['maps'].append(row)
    if any(row['map_id']==3526133726 and row['ready'] for row in result['maps']):
        try:
            prepare_home_long(account,build,3526133726)
            result.update(home_long_available=True,home_long_reason='遇境专用模板入口已配置')
        except (ValueError,OSError,KeyError,TypeError,ImportError):pass
    result['current_long_available']=any(r['current_long_ready'] for r in result['maps'])
    result['enabled']=any(r['ready'] or r['current_long_ready'] for r in result['maps'])
    result['reason']='按需连接；切图将先关闭旧连接再重连。其他游戏功能保持原验证状态。' if result['enabled'] else '没有匹配当前账号的可用地图缓存'
    return result

class CurrentTransport(ProbeTransport):
    def __init__(self,*args,cancel,**kwargs):
        self.cancel=cancel
        super().__init__(*args,**kwargs)
    def _check(self):
        if self.cancel.is_set():raise ConnectionCancelled()
        return super()._check()
    def connect(self,*args,**kwargs):
        self._check()
        return super().connect(*args,**kwargs)

def _transport_bounds(duration,remaining):
    """返回 (timeout, max_application_packets, max_datagrams)。CLAUDEWORK_LONG=1 时按时长放大。"""
    if os.environ.get('CLAUDEWORK_LONG')=='1':
        return max(3.0,min(86400.0,remaining)),max(8192,duration*48),max(16384,duration*32)
    return min(180.0,remaining),min(8192,max(512,duration*24)),min(16384,max(4096,duration*48))

def run_connection(prepared,duration,cancel,emit,*,same_peer_moves=None):
    """One HTTP ticket; bounded server-directed Home Move. Owner closes all peers."""
    start=time.monotonic();deadline=start+duration;client=None;transport=None;engine=None;pump=None;same_peer_attempt_started=False
    is_current=prepared.current_inputs is not None
    is_long=is_current or prepared.long_templates is not None
    result={'phase':'preflight','running':True,'finished':False,'ticket_requests':0,
       'enter_game_valid':False,'registration_transport_acked':False,'matching_generation_observed':False,
       'reply_count':0,'scene_request_count':0,'ignored_scene_requests':0,'http_status':None,'error_code':'',
       'transport_closed':None,'disconnect_sent':False,'reason':'正在检查连接参数',
       'connection_kind':'current_long' if is_current else 'home_long' if is_long else 'short','long_groups':0,'long_actor_frames':0,
       'long_controller_frames':0,'long_scene_frames':0,'long_feedback_frames':0}
    def update(**values):
        result.update(values);result['elapsed_seconds']=round(time.monotonic()-start,3);emit(dict(result))
    def check():
        if cancel.is_set():raise ConnectionCancelled()
        if time.monotonic()>=deadline:raise ConnectionFault('deadline')
    class SingleSession(requests.Session):
        def request(self,method,url,**kwargs):
            check()
            if method.upper()!='POST' or url!=TICKET_URL:raise ConnectionFault('unexpected_http_endpoint')
            kwargs['allow_redirects']=False
            response=super().request(method,url,**kwargs)
            update(http_status=response.status_code)
            check()
            return response
    try:
        check();_validate_account(prepared.account,BUILD)
        a=prepared.account
        client=SkyClient(a['user_uuid'],a['session'],a['session2'],a.get('data_version') or '')
        client._local.close();client._local=SingleSession();client._local.trust_env=False
        update(phase='ticket',reason='正在获取一次入服票据',ticket_requests=1)
        ticket_timeout=min(8.0,max(0.1,deadline-time.monotonic()))
        pushed=TICKET_OVERRIDE.pop('ticket',None) if is_long else None
        if pushed:
            response={'data':pushed}  # claudework：服务器 WS move 推的票（被牵着过图/回遇境）
        elif is_long:
            # The supplied build-421589 Home runtime explicitly requests flags=0.
            response=client._post_once('/account/find_previous_or_empty',{
                'server_version':442,'client_version':5127,'force_empty':False,'net_flags':0,
                'net_exclusive_flags':0,'level_hash':prepared.profile.record['netversion_u16'][0],
                'quick_rejoin_players':_cw_join_players() if os.environ.get("CLAUDEWORK_JOIN_QUICK")=="1" else [],'recent_players':_cw_join_players()},True,str(prepared.map_id),None,ticket_timeout)
        else:
            response=client.find_previous_or_empty(server_version=442,client_version=5127,
                level_hash=prepared.profile.record['netversion_u16'][0],level_id=prepared.map_id,
                single_attempt=True,timeout=ticket_timeout)
        check();data=response.get('data') if isinstance(response.get('data'),dict) else response
        ticket={key:data[key] for key in ('uri','move_ts','sig','platform_filter')}
        initial=handshake0167_fields.fresh_initial_record(prepared.profile,ticket,
            account_uuid=a['user_uuid'],newly_allocated_connection=True)
        handshake0167_fields.validate_fresh_ticket(initial,ticket)
        fields=dict(initial['fields']);fields['previous_address']=bytes.fromhex(fields['previous_address'])
        fields.update(handshake0167_fields.initial_ticket_fields(ticket))
        extra=TICKET_OVERRIDE.pop('fields',None) if pushed else None
        if extra:fields.update(extra)  # claudework：好友票带上要找的玩家（others）
        peer=handshake0167_fields.peer_key(a['user_uuid'])
        fields.update(uid=uuid_raw(a['user_uuid']),session=bytes.fromhex(a['session']),peer_key=peer)
        payload=build_from_cache(prepared.profile,fields,frame=True)
        host,port=ticket['uri'].rsplit(':',1)
        remaining=deadline-time.monotonic()
        if remaining<3:raise ConnectionFault('deadline')
        TransportClass=CurrentTransport
        if is_long:
            from .long_transport0167 import LongTransport,OutboundFrame
            if is_current:from .map_long0167 import Engine
            else:from .long0167 import Engine
            class CurrentLongTransport(LongTransport):
                def __init__(self,*args,cancel,**kwargs):
                    self.cancel=cancel;super().__init__(*args,**kwargs)
                def _check(self):
                    if self.cancel.is_set():raise ConnectionCancelled()
                    guard=getattr(self,'_same_peer_level_guard',None)
                    if guard is not None:guard()
                    return super()._check()
            TransportClass=CurrentLongTransport
        transport=TransportClass(host,int(port),peer,sig_hex=ticket['sig'],decoder=prepared.decoder,
            archive=lambda *args:None,cancel=cancel,timeout=_transport_bounds(duration,remaining)[0],max_transmissions=4,
            max_application_packets=_transport_bounds(duration,remaining)[1],max_datagrams=_transport_bounds(duration,remaining)[2])
        update(phase='connecting',reason='正在建立游戏连接');check();transport.connect()
        if not transport.wait_until_connected(timeout_sec=min(5.0,deadline-time.monotonic())):
            raise ConnectionFault('connect_timeout')
        update(phase='join',reason='等待账号和场景匹配的入服消息');transport.send_join(payload)
        entered=None;entered_body=None;pending=[];enter_deadline=min(deadline,time.monotonic()+8)
        def inspect(message):
            if message.message_type in (1,2,9,0x53):
                result['terminal_message_type']=message.message_type
                if is_long and os.environ.get('QFLY_DATA_DIR'):
                    import json
                    path=Path(os.environ['QFLY_DATA_DIR'])/('home-long-terminal-'+str(time.time_ns())+'.local.json')
                    record={'message_type':message.message_type,'body_hex':message.body.hex(),
                            'body_length':len(message.body),'elapsed':round(time.monotonic()-start,3)}
                    tmp=path.with_suffix('.tmp')
                    with tmp.open('x',encoding='utf-8') as f:json.dump(record,f)
                    os.replace(tmp,path)
                raise ConnectionFault('server_ended_or_changed_connection')
        while time.monotonic()<enter_deadline and entered is None:
            check();transport.service()
            while transport.messages:
                message=transport.messages.popleft();inspect(message)
                if message.message_type==0x11:
                    entered=validate_pending_enter_game(message,account_uuid=a['user_uuid'],expected_level_id=prepared.map_id,pending_state=3)
                    entered_body=message.body
                elif is_long or message.message_type==0x0e:pending.append(message)
            cancel.wait(.005)
        if entered is None:raise ConnectionFault('enter_timeout')
        check();update(phase='registration',enter_game_valid=True,reason='正在发送当前版本注册信息')
        player=prepared.profile.player_fields
        body=register0167_codec.encode(build=BUILD,field00=1,netversion=prepared.profile.record['netversion_u16'],
            level_flag=player['level_flag'],level_id=prepared.map_id,matrix=player['matrix'],tail_flag=False)
        transport.send_registration(struct.pack('<IBB',peer,4,1)+body)
        responder=SceneConfirmation(connection_generation=body[0],level_id=prepared.map_id,
            max_replies=min(128,max(8,math.ceil(duration/3))))
        counter=1
        if is_long:
            from . import enet
            if not is_current:scene,actor=prepared.long_templates
            def make_long_engine(next_transport,enter_body,now,local_idle=None):
                common=dict(account_uuid=uuid_raw(a['user_uuid']),enter_game_body=enter_body,
                    generation=body[0],now=now,
                    live_check=lambda:not cancel.is_set() and not next_transport.closed and next_transport.state==enet.ST_CONNECTED)
                if local_idle is not None:common['local_idle']=local_idle
                if is_current:
                    return Engine(**common,inputs=prepared.current_inputs,scene=prepared.scene,
                        resource_revision=prepared.resource_revision,level_id=prepared.map_id,
                        netversion=prepared.profile.record['netversion_u16'])
                return Engine(**common,scene=scene,actor=actor)
            engine=make_long_engine(transport,entered_body,time.monotonic())
        if is_long:
            from .home_handoff_runtime0167 import HomeHandoffPump,HandoffRuntimeError
            Pump=HomeHandoffPump;pump_options={}
            if os.environ.get('CLAUDEWORK_LONG')=='1':pump_options['max_moves']=100000  # CLAUDEWORK_LONG
            if is_current:
                from .current_map_handoff_runtime0167 import CurrentMapHandoffPump
                Pump=CurrentMapHandoffPump;pump_options['map_id']=prepared.map_id
            def make_transport(move,remaining):
                host,port=move.endpoint
                return CurrentLongTransport(host,port,peer,sig_hex=move.signature.hex(),
                    decoder=prepared.decoder,archive=lambda *args:None,cancel=cancel,
                    timeout=_transport_bounds(duration,remaining)[0],max_transmissions=4,
                    max_application_packets=_transport_bounds(duration,remaining)[1],max_datagrams=_transport_bounds(duration,remaining)[2])
            def make_engine(next_transport,enter_body,now,local_idle):
                return make_long_engine(next_transport,enter_body,now,local_idle)
            pump=Pump(transport=transport,engine=engine,peer_key=peer,
                profile=prepared.profile,account_uuid=a['user_uuid'],resource_revision=prepared.resource_revision,
                current_join_fields=fields,registration_body=body,make_transport=make_transport,
                make_engine=make_engine,check=check,deadline=deadline,on_terminal=inspect,**pump_options)
        update(phase='observing',registration_transport_acked=True,reason='连接已建立，等待本次连接的场景确认')
        def handle(body):
            nonlocal counter
            check();reply=responder.reply(body)
            if reply is not None:
                wire,counter=frame_scene_reply(peer,counter,reply);transport.send_scene_confirmation(wire)
                update(reply_count=result['reply_count']+1,matching_generation_observed=True,reason='当前场景确认已回复，正在保持连接')
            update(scene_request_count=responder.request_count,ignored_scene_requests=responder.ignored_count)
        if pump is not None:
            pump.accept_initial(pending)
        else:
            for message in pending:
                if message.message_type==0x0e:handle(message.body)
        last=0.0
        while time.monotonic()<deadline-.05:
            check()
            if pump is not None:
                pump.step()
                if same_peer_moves is not None and is_current:
                    try:request=same_peer_moves.get_nowait()
                    except __import__('queue').Empty:request=None
                    if request is not None:
                        same_peer_attempt_started=True
                        if request['guard']() is not True:raise ConnectionFault('same_peer_stale_request')
                        target=prepare_current_long(prepared.account,BUILD,request['level'],
                            variant=request.get('variant'))
                        def target_engine(target,next_transport,roster,epoch,now,binding):
                            return Engine(account_uuid=uuid_raw(a['user_uuid']),enter_game_body=roster,
                                generation=epoch,now=now,inputs=target.current_inputs,
                                scene=target.scene,resource_revision=target.resource_revision,
                                level_id=target.map_id,netversion=target.profile.record['netversion_u16'],
                                live_check=lambda:not cancel.is_set() and not next_transport.closed and next_transport.state==enet.ST_CONNECTED,
                                local_registration_binding=binding)
                        def after_target(target,new_registration):
                            nonlocal prepared,body,fields
                            prepared,body=target,new_registration
                            fields=dict(pump._fields)
                            request['after_install'](target)
                        detail=pump.change_level_same_peer(target,guard=request['guard'],commit_guard=request['commit_guard'],
                            make_engine=target_engine,before_install=request['before_install'],after_install=after_target)
                        update(map_id=target.map_id,scene=target.scene,phase='observing',
                            same_peer_scene_revision=result.get('same_peer_scene_revision',0)+1,
                            enter_game_valid=False,local_registration_acked=True,server_roster_received=False,
                            membership_provenance='self_announced_local_registration_acked',
                            reason='同连接已提交新场景登记；等待真实同图及互动状态')
                        same_peer_attempt_started=False
                        try:request['report'](detail)
                        except Exception:pass  # telemetry must not tear down an ACKed migration
                result.update(pump.metrics())
            else:
                transport.service()
                while transport.messages:
                    message=transport.messages.popleft();inspect(message)
                    if message.message_type==0x0e:handle(message.body)
            if time.monotonic()-last>.25:
                if pump is not None:update(**pump.metrics())
                else:update(application_packets=transport.raw_packets,received_datagrams=transport.pkt_recv,
                           sent_datagrams=transport.pkt_sent,crc_failures=transport.crc_fail_count)
                last=time.monotonic()
            cancel.wait(.02)
        if pump is not None and pump.metrics()['move_in_progress']:
            raise ConnectionFault('move_incomplete_deadline')
        update(phase='completed',reason='已到设定时长，连接已结束' if result['matching_generation_observed'] else '已到设定时长；未取得本次场景确认')
    except ConnectionCancelled:
        update(phase='stopped',reason='已停止本次连接')
    except Exception as exc:
        if is_long and os.environ.get('QFLY_DATA_DIR'):
            # Sensitive diagnostic text stays in excluded local runtime storage.
            try:
                import json,traceback
                path=Path(os.environ['QFLY_DATA_DIR'])/('home-long-fault-'+str(time.time_ns())+'.local.json')
                diagnostic={'exception_type':type(exc).__name__,'message':str(exc)[:2000],
                    'traceback':traceback.format_exc(limit=10)[-12000:],'phase':result['phase']}
                tmp=path.with_suffix('.tmp')
                with tmp.open('x',encoding='utf-8') as f:json.dump(diagnostic,f,ensure_ascii=False)
                os.replace(tmp,path)
            except Exception:pass
        # No exception text, traceback, account ID or server address crosses this boundary.
        status=getattr(exc,'status',None)
        if type(status) is int:update(http_status=status)
        expired=(isinstance(exc,ConnectionFault) and exc.code=='deadline') or (
            isinstance(exc,ProbeTransportError) and str(exc)=='overall transport deadline reached')
        if expired and result['registration_transport_acked'] and not (pump is not None and pump.metrics()['move_in_progress']):
            update(phase='completed',reason='已到设定时长，连接已结束')
        else:
            codes={'transport disconnected':'transport_disconnected','reliable transport failed':'reliable_ack_timeout',
                'current server envelope decode failed':'server_envelope_decode_failed',
                'malformed ENet datagram':'malformed_enet_datagram','invalid ENet command':'invalid_enet_command',
                'application receive bounds reached':'receive_bound_reached','datagram count bound reached':'datagram_bound_reached',
                'application message count bound reached':'message_queue_bound_reached'}
            code=exc.code if isinstance(exc,ConnectionFault) else ('http_rejected' if isinstance(exc,SkyApiError) else 'connection_failed')
            if is_long and pump is not None and isinstance(exc,HandoffRuntimeError):code=exc.code
            if isinstance(exc,ProbeTransportError):
                code=codes.get(str(exc),'transport_protocol_error')
                if getattr(pump.active.transport if pump is not None else transport,'remote_disconnect_code',None) is not None:code='server_transport_disconnect'
            if same_peer_attempt_started:code='same_peer_level_update_failed'
            update(phase='failed',error_code=code,reason='连接未通过；请检查会话、缓存或服务响应，不会自动重试')
    finally:
        if pump is not None:
            try:result.update(pump.close())
            except Exception:result['transport_closed']=False
            if not result.get('transport_closed'):
                result.update(phase='failed',error_code='close_failed',reason='旧连接关闭未确认，已阻止后续连接')
        elif engine is not None:
            try:engine.close()
            except Exception:pass
        if transport is not None and pump is None:
            try:transport.close()
            except Exception:pass
            try:confirmed=transport.socket.fileno()==-1
            except Exception:confirmed=False
            result.update(transport_closed=confirmed,disconnect_sent=bool(getattr(transport,'disconnect_sent',False)),
                remote_disconnect_code=getattr(transport,'remote_disconnect_code',None),
                pending_reliable_commands=len(getattr(transport,'pending_cmds',{})),
                application_packets=transport.raw_packets,received_datagrams=transport.pkt_recv,sent_datagrams=transport.pkt_sent,crc_failures=transport.crc_fail_count)
            if not confirmed:result.update(phase='failed',error_code='close_failed',reason='旧连接关闭未确认，已阻止后续连接')
        if client is not None:
            try:client._local.close()
            except Exception:pass
        if same_peer_attempt_started:result['same_peer_level_update_failed']=True
        update(running=False,finished=True)
    return result

@dataclass
class _Entry:
    owner:object=field(repr=False)
    uid:object=field(repr=False)
    state:dict
    cancel:threading.Event=field(default_factory=threading.Event,repr=False)
    thread:threading.Thread|None=field(default=None,repr=False)
    started:float=field(default_factory=time.monotonic)

def _claudework_log(account,state):
    """claudework 测试副本：把每次连接的最终公开状态追加到日志，便于服务器端观察。"""
    path=os.environ.get('CLAUDEWORK_CONN_LOG')
    if not path:return
    try:
        import json
        row={'ts':time.strftime('%Y-%m-%d %H:%M:%S'),'account_id':account.get('id'),**state}
        with open(path,'a',encoding='utf-8') as f:f.write(json.dumps(row,ensure_ascii=False)+chr(10))
    except Exception:pass

class ConnectionManager:
    def __init__(self,*,prepare_fn=None,runner=None,stop_timeout=10.0):
        self.prepare_fn=prepare_fn or _prepare;self.runner=runner or run_connection
        self.stop_timeout=stop_timeout;self._entries={};self._operation=threading.RLock();self._state=threading.RLock();self._serial=0;self._closing=False
    def _owned(self,account_id,owner_id):
        entry=self._entries.get(account_id)
        if entry is not None and entry.owner!=owner_id:raise ValueError('该连接不属于当前用户')
        return entry
    def _public(self,entry):
        result={key:copy.deepcopy(value) for key,value in entry.state.items() if key in PUBLIC_FIELDS}
        if result.get('running'):result['elapsed_seconds']=round(time.monotonic()-entry.started,3)
        return result
    def status(self,account_id,owner_id):
        with self._state:
            entry=self._owned(account_id,owner_id)
            return self._public(entry) if entry else {'phase':'idle','running':False,'finished':True,'map_id':None,'scene':'','duration':15,'elapsed_seconds':0,'reply_count':0,'scene_request_count':0,'ignored_scene_requests':0,'http_status':None,'enter_game_valid':False,'registration_transport_acked':False,'matching_generation_observed':False,'reason':'尚未启动连接','generation':0,'transport_closed':None}
    def _check_request(self,account,duration):
        _cap=int(os.environ.get('CLAUDEWORK_MAX_DURATION','180')) if os.environ.get('CLAUDEWORK_LONG')=='1' else 180
        if type(duration) is not int or not 15<=duration<=_cap:raise ValueError('连接时长必须为 15 到 '+str(_cap)+' 秒的整数')
        if not isinstance(account,dict) or not account.get('id') or not account.get('owner') or not account.get('user_uuid'):
            raise ValueError('缺少账号归属信息')
    def _start(self,prepared,duration):
        account=prepared.account;identity=uuid_raw(account['user_uuid'])
        with self._state:
            old=self._owned(account['id'],account['owner'])
            if old and old.state.get('transport_closed') is False:raise RuntimeError('旧连接关闭未确认，尚未创建新连接')
            if old and old.thread and old.thread.is_alive():raise RuntimeError('该账号已有连接，请先停止或使用重连切图')
            if any(e.uid==identity and e.state.get('transport_closed') is False for e in self._entries.values()):
                raise RuntimeError('旧连接关闭未确认，尚未创建新连接')
            active=[e for e in self._entries.values() if e.thread and e.thread.is_alive()]
            if any(e.uid==identity for e in active):raise RuntimeError('同一游戏账号已有连接')
            if len(active)>=4:raise RuntimeError('当前连接数量已达上限')
            if len(self._entries)>=128:
                for key,e in list(self._entries.items()):
                    if not(e.thread and e.thread.is_alive()) and e.state.get('transport_closed') is not False:
                        self._entries.pop(key);break
                else:raise RuntimeError('没有可安全回收的连接记录，请先处理关闭失败')
            self._serial+=1
            state={'phase':'preflight','running':True,'finished':False,'map_id':prepared.map_id,'scene':prepared.scene,
                'duration':duration,'elapsed_seconds':0,'reply_count':0,'scene_request_count':0,'ignored_scene_requests':0,
                'http_status':None,'enter_game_valid':False,'registration_transport_acked':False,'matching_generation_observed':False,
                'reason':'正在启动当前版本连接','generation':self._serial,'transport_closed':None,
                'connection_kind':'current_long' if prepared.current_inputs is not None else 'home_long' if prepared.long_templates is not None else 'short'}
            entry=_Entry(account['owner'],identity,state);self._entries[account['id']]=entry
            def emit(patch):
                with self._state:
                    # A worker updates only its own entry, never a later generation.
                    entry.state.update({k:v for k,v in patch.items() if k in PUBLIC_FIELDS and k not in ('generation','map_id','scene','duration')})
            def work():
                try:emit(self.runner(prepared,duration,entry.cancel,emit))
                except Exception:emit({'phase':'failed','reason':'连接任务异常结束，未自动重试','error_code':'worker_failed'})
                finally:
                    emit({'running':False,'finished':True})
                    _claudework_log(prepared.account,self._public(entry))
            entry.thread=threading.Thread(target=work,name='connection0167-'+str(self._serial),daemon=True)
            entry.thread.start()
            return self._public(entry)
    def start(self,account,*,build=BUILD,map_id,duration=15,kind='short'):
        if kind not in ('short','home_long','current_long'):raise ValueError('未知连接模式')
        self._check_request(account,duration)
        with self._operation:
            if self._closing:raise RuntimeError('服务正在关闭，不再创建连接')
            with self._state:self._owned(account['id'],account['owner'])
            prepare=prepare_current_long if kind=='current_long' else prepare_home_long if kind=='home_long' else self.prepare_fn
            prepared=prepare(account,build,map_id)
            return self._start(prepared,duration)
    def stop(self,account_id,owner_id):
        with self._operation,self._state:
            entry=self._owned(account_id,owner_id)
            if entry and entry.thread and entry.thread.is_alive():
                entry.cancel.set();entry.state.update(phase='stopping',reason='正在停止；旧连接关闭前不会启动新连接')
            return self.status(account_id,owner_id)
    def switch(self,account,*,build=BUILD,map_id,duration=15,kind='short'):
        if kind not in ('short','current_long'):raise ValueError('未知重连模式')
        self._check_request(account,duration)
        with self._operation:
            if self._closing:raise RuntimeError('服务正在关闭，不再创建连接')
            with self._state:entry=self._owned(account['id'],account['owner'])
            prepare=prepare_current_long if kind=='current_long' else self.prepare_fn
            prepared=prepare(account,build,map_id)  # invalid target must not kill the old connection
            self.stop(account['id'],account['owner'])
            if entry and entry.thread:
                entry.thread.join(timeout=self.stop_timeout)
                if entry.thread.is_alive():raise RuntimeError('旧连接仍在停止，尚未创建新连接')
                if entry.state.get('transport_closed') is False:raise RuntimeError('旧连接关闭未确认，尚未创建新连接')
            return self._start(prepared,duration)
    def close_all(self):
        with self._operation:
            self._closing=True
            with self._state:entries=list(self._entries.values())
            for entry in entries:entry.cancel.set()
            for entry in entries:
                if entry.thread:entry.thread.join(timeout=self.stop_timeout)

MANAGER=ConnectionManager()
