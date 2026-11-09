"""Build-421589 minimal initial-connection parameters; pure local, no transport.
Static derivation is not a successful custom-service JoinGame. Existing live
connection counters/tickets must NEVER be copied into a fresh connection.
"""
import hashlib
import ipaddress
import json
import struct
from dataclasses import dataclass
from .join0167_profile import BUILD, NATIVE_SHA256, account_binding, uuid_raw
from .join0167_codec import uint, octets

TICKET_DOMAIN=b"sky-cn-421589-fresh-initial-ticket-v1\0"


def peer_key(uid):
    """Native0x1f79614..6e8: unsigned-byte FNV1a32 over canonical UID16."""
    h=0x811c9dc5
    for b in uuid_raw(uid): h=((h^b)*0x01000193)&0xffffffff
    return h


def advance_registration_sequence(value):
    value=uint(value,8,'registration sequence')
    return value+1 if value<255 else 1


def advance_message_sequence(value,msg_type):
    uint(value,8,'message sequence');uint(msg_type,8,'message type')
    return value if (msg_type&0xfd)==1 else advance_registration_sequence(value)


@dataclass
class ConnectionSequences:
    """A newly allocated conn starts at zero; reconnect only resets reg sequence."""
    message:int=0
    registration:int=0

    def enet_connected(self):
        self.registration=0 # Native0x1f6769c deliberately does NOT clear +1.

    def send(self,msg_type):
        self.message=advance_message_sequence(self.message,msg_type)
        if msg_type==4:self.registration=advance_registration_sequence(self.registration)
        return self.message,self.registration


def address(raw):
    raw=octets(raw,20,'native address')
    port,tag=struct.unpack_from('<HH',raw,16)
    if tag not in (0,1,2):raise ValueError('unsupported native address tag')
    data={0:b'',1:raw[12:16],2:raw[:16]}[tag]
    return {'tag':tag,'address_hex':data.hex(),'port':port,'valid':tag!=0 and port!=0 and any(data)}


def tail_fields(active_raw,previous_raw,previous_level_id,current_level_id,flag_b):
    active=address(active_raw);previous=address(previous_raw)
    uint(previous_level_id,32,'previous level');uint(current_level_id,32,'current level')
    uint(flag_b,1,'tail bool b')
    #0x1f7211c..148: equal addresses OR invalid active address, AND level match.
    same=(active_raw==previous_raw or not active['valid']) and previous_level_id==current_level_id
    previous=previous if previous['valid'] else address(bytes(20))
    return {'tail_bool_a':int(same),'tail_bool_b':flag_b,
            'previous_address_tag':previous['tag'],'previous_address':previous['address_hex'],
            'previous_port':previous['port'],'previous_level_id':previous_level_id}


def ticket_unsigned(value, bits, name, *, allow_bool=False):
    """Canonical numeric forms only; reject overflow rather than native truncation."""
    if allow_bool and type(value) is bool:
        value = int(value)
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        value = int(value)
    return uint(value, bits, name)


def initial_ticket_fields(ticket):
    """Only the initial HTTP200 parser0x1f89510 path, not invite/event/move tickets.

    Native reads uri/move_ts/sig/platform_filter; always empty others and explicit
    sentinel extras. Missing fields are rejected rather than silently defaulted.
    No DNS: the native wire ticket serializer requires IPv4.
    """
    if not isinstance(ticket,dict):raise ValueError('ticket must be an object')
    for k in ('uri','move_ts','sig','platform_filter'):
        if k not in ticket:raise ValueError('fresh initial ticket missing '+k)
    uri=ticket['uri']
    if not isinstance(uri,str) or uri.count(':')!=1:raise ValueError('ticket uri must be explicit IPv4:port')
    host,port=uri.rsplit(':',1)
    try:ip=ipaddress.IPv4Address(host)
    except ipaddress.AddressValueError:raise ValueError('ticket must contain literal IPv4, not DNS/IPv6') from None
    if not port.isdecimal() or not 0<int(port)<65536:raise ValueError('ticket port out of range')
    stamp=ticket_unsigned(ticket['move_ts'],64,'move_ts')
    if stamp==0:raise ValueError('fresh ticket move_ts must be nonzero')
    sig=ticket['sig']
    if not isinstance(sig,str) or len(sig)!=64:raise ValueError('signature must be 32 bytes of hex')
    try:sig=octets(bytes.fromhex(sig),32,'signature')
    except ValueError:raise ValueError('signature must be 32 bytes of hex') from None
    # Native 0x3355dac uses %u and stores the low byte, not a JSON-bool getter.
    # Keep valid u8 values; do not silently wrap malformed/out-of-range numbers.
    pf=ticket_unsigned(ticket['platform_filter'],8,'platform_filter',allow_bool=True)
    fields={'signature':sig,'ticket_ipv4':ip.packed,'ticket_port':int(port),'move_ts':stamp,
            'ticket_flag':int(pf),'others':[], 'ticket_extra_u32_a':0xffffffff,
            'ticket_extra_u8':0xff,'ticket_extra_u32_b':0xffffffff}
    return fields


def ticket_binding(ticket):
    f=initial_ticket_fields(ticket)
    payload=f['signature']+f['ticket_ipv4']+struct.pack('<HQB',f['ticket_port'],f['move_ts'],f['ticket_flag'])
    return hashlib.sha256(TICKET_DOMAIN+payload).hexdigest()


def fresh_initial_record(profile,ticket,*,account_uuid,newly_allocated_connection):
    if newly_allocated_connection is not True:
        raise ValueError('fresh initial rules require a newly allocated connection, not reconnect/switch')
    if account_binding(account_uuid)!=profile.binding:raise ValueError('profile account binding mismatch')
    t=initial_ticket_fields(ticket)
    fields={k:v for k,v in t.items() if k not in ('signature','ticket_ipv4','ticket_port','move_ts')}
    fields.update(counter=0,server_version=442,client_version=5127)
    #Native0x1f89af8..b24 passes empty active address + current localLevel into
    #the ticket setter. Previous level is NOT zero on an initial scene join.
    fields.update(tail_fields(bytes(20),bytes(20),profile.record['level_id'],profile.record['level_id'],0))
    return {'schema':1,'kind':'sky-static-fresh-initial-fields','apk_build':BUILD,
            'native_sha256':NATIVE_SHA256,'resource_revision':profile.record['resource_revision'],
            'scene':profile.record['scene'],'level_id':profile.record['level_id'],
            'account_binding':profile.binding,'cache_sha256':profile.record['cache']['sha256'],
            'fresh_ticket_binding_sha256':ticket_binding(ticket),
            'connection_mode':'newly-allocated-initial-http-ticket-only','fields':fields,
            'initial_registration':{'body_sequence':1,'app_frame_counter':1,'tail_flag_without_event_0x53':False,
                                    'send_only_after_valid_join_reply_type':17},
            'validation':'static-derived-and-offline-tested-not-wire-observed',
            'custom_service_join_verified':False,'registration_chain_verified':False}


def validate_fresh_ticket(record,ticket):
    if record.get('kind')!='sky-static-fresh-initial-fields' or record.get('fresh_ticket_binding_sha256')!=ticket_binding(ticket):
        raise ValueError('ticket differs from prepared fresh initial record')


def main():
    import argparse
    from pathlib import Path
    from .join0167_profile import load_cache_profile,inside
    from .join0167_codec import atomic_bytes
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--private-root',type=Path,required=True);p.add_argument('--cache-record',required=True)
    p.add_argument('--session-record',required=True,help='Private JSON with account_uuid only read; no value printed')
    p.add_argument('--ticket-record',required=True,help='Private fresh native-shape initial HTTP ticket JSON')
    p.add_argument('--scene',required=True);p.add_argument('--resource-revision',required=True)
    p.add_argument('--out-relative',required=True);p.add_argument('--new-connection',action='store_true',required=True)
    a=p.parse_args();root=a.private_root.resolve(strict=True)
    def read(rel):
        f=inside(root,rel)
        if f.stat().st_size>65536:raise ValueError('private JSON exceeds bound')
        return json.loads(f.read_text(encoding='utf-8-sig'))
    uid=read(a.session_record)['account_uuid'];ticket=read(a.ticket_record)
    profile=load_cache_profile(root,a.cache_record,scene=a.scene,resource_revision=a.resource_revision,account_uuid=uid)
    record=fresh_initial_record(profile,ticket,account_uuid=uid,newly_allocated_connection=a.new_connection)
    out=(root/a.out_relative).resolve()
    if not out.is_relative_to(root) or out.exists():raise ValueError('output must be a new file inside private root')
    out.parent.mkdir(parents=True,exist_ok=True)
    atomic_bytes(out,(json.dumps(record,indent=2)+'\n').encode())
    print(json.dumps({'written':True,'account_or_ticket_values_printed':False,'network_calls':False,
                      'custom_service_join_verified':False}))


if __name__=='__main__':main()
