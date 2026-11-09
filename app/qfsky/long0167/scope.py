"""Actual local account/peer lifetime binding; no synthetic WSS state."""
from dataclasses import dataclass,field
from typing import Callable

@dataclass(frozen=True,repr=False,eq=False)
class ConnectionScope:
    owner_uuid:bytes=field(repr=False)
    peer_identity:object=field(repr=False)
    room_generation:object=field(repr=False)
    auth_generation:object=field(repr=False)
    peer_epoch:int
    own_slot:int
    map_id:int
    authenticated_current_peer:bool
    live_check:Callable[[],bool]=field(repr=False)
    active:bool=True

    def validate(self):
        if type(self.owner_uuid)is not bytes or len(self.owner_uuid)!=16 or not any(self.owner_uuid):raise ValueError('invalid_owner_scope')
        if any(x is None for x in (self.peer_identity,self.room_generation,self.auth_generation)):raise ValueError('invalid_binding_scope')
        if any(type(x)is not int or not 1<=x<=255 for x in (self.peer_epoch,self.own_slot)):raise ValueError('invalid_peer_or_slot')
        if type(self.map_id)is not int or self.map_id!=3526133726:raise ValueError('home_only')
        if self.authenticated_current_peer is not True or self.active is not True or not callable(self.live_check) or self.live_check() is not True:raise ValueError('scope_not_active')
