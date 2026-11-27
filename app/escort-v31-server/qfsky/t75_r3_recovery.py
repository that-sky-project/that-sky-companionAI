"""中央神坛合法好友票等待窗口：不取票、不收发包、也不自行判断准入。"""
import os

LEVEL=2394719185
WINDOW=50.0
RECENT=45.0
POLL_GAP=5.0


def enabled():
    return (all(os.environ.get(key,'1')!='0' for key in (
            'SKY_T75_DAYHUB_PAIRING','SKY_T75_R2_DAYHUB_PAIRING','SKY_T75_R3_DAYHUB_PAIRING'))
            and os.environ.get('SKY_T73_R2_OWNER_CHECK','1')!='0')


class Window:
    @classmethod
    def open(cls,pump,event,now,budget,inflight):
        if (not enabled() or budget<=0 or inflight or pump._map_id!=LEVEL
                or event.get('event')!='move_committed' or event.get('external') is not False
                or event.get('players')!=1 or event.get('owner_in_roster') is not False):
            return None
        a=getattr(pump,'_t75_r3_pair_anchor',None)
        if not isinstance(a,tuple) or len(a)!=3: return None
        reg,prior,at=a
        # 唯一依据是发过真实配对 Join 的旧槽；拒绝跨登记/跨提交套用历史快照。
        previous=next((s for s in reversed(pump._all) if s.stage=='retired'),None)
        current=pump.active
        if (reg is None or reg is not pump._registration or prior is not previous or prior is None
                or type(at) not in (int,float) or not 0<=now-at<=RECENT
                or current is prior or current.stage!='active' or not prior.ticket_origin
                or not current.ticket_origin or current.ticket_origin==prior.ticket_origin):
            return None
        end=min(now+WINDOW,pump._deadline-.05)
        if end<=now+.25: return None
        obj=cls()
        obj.reg=reg;obj.new=current;obj.old=prior.ticket_origin
        obj.start=now;obj.until=end;obj.limit=min(3,budget);obj.polls=0
        obj.refused=False;obj.staged_origin=None
        pump._t75_r3_pair_anchor=None
        return obj

    def invalid(self,pump,now,owner_seen):
        if not enabled(): return 'disabled'
        if now>=self.until: return 'deadline'
        if (pump._map_id!=LEVEL or self.reg is not pump._registration
                or self.new is not pump.active): return 'room_changed'
        try:
            if owner_seen is not None and owner_seen(self.start): return 'owner_in_later_roster'
        except Exception:
            pass  # 无新名单不能当成主人已经回来。
        return None

    def next_poll(self,now,cooldown,remaining,first=False):
        if remaining<=0 or self.polls>=self.limit: return None
        at=max(now+(.25 if first else POLL_GAP),cooldown)
        return at if at<self.until else None

    def classify(self,ticket,origin):
        if not isinstance(ticket,dict) or ticket.get('error'): return 'ticket_miss'
        if ticket.get('level')!=LEVEL or not origin: return 'invalid_target'
        if self.refused and origin==self.old: return 'same_rejected_origin'
        return 'normal_join'

    def on_36(self,origin,reason):
        if (origin=='owner_rejoin_after_solo_move' and reason=='server_disconnect_36'
                and self.staged_origin is not None and self.staged_origin==self.old):
            self.refused=True
            self.staged_origin=None
            return True
        return False
