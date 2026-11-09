"""Pure candidate coordinator. No game transport, movement writer or map-transfer implementation.
Adapters must recheck Effect.binding atomically before publishing; submission is never arrival.
"""
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
from math import hypot, isfinite
from typing import Protocol
from uuid import UUID

Vec3 = tuple[float, float, float]

def _uuid(value: str) -> bool:
    try: return type(value) is str and str(UUID(value)) == value
    except (ValueError, TypeError, AttributeError): return False

def _finite(value):
    return type(value) in (int, float) and isfinite(value)

def _point(value):
    return type(value) is tuple and len(value) == 3 and all(_finite(v) for v in value)

def _near(point, target, horizontal, vertical):
    return (_point(point) and _point(target)
            and hypot(point[0]-target[0], point[2]-target[2]) <= horizontal
            and abs(point[1]-target[1]) <= vertical)

class CarryKind(str, Enum):
    HANDHOLD = 'handhold'
    PIGGYBACK = 'piggyback'
    PRINCESS = 'princess_carry'

class Phase(str, Enum):
    WAITING_PEER = 'waiting_peer'
    WAITING_ACCEPT = 'waiting_accept'
    WAITING_ATTACHMENT = 'waiting_attachment'
    WAITING_SCENE = 'waiting_scene'
    NAVIGATING = 'navigating'
    PAUSED = 'paused_observation'
    SUCCEEDED = 'succeeded'
    CANCELLED = 'cancelled'
    REJECTED = 'rejected'
    EXPIRED = 'expired'
    SUPERSEDED = 'superseded'
    FAILED = 'failed'
    BLOCKED = 'adapter_unavailable'

TERMINAL = frozenset((Phase.SUCCEEDED, Phase.CANCELLED, Phase.REJECTED,
                      Phase.EXPIRED, Phase.SUPERSEDED, Phase.FAILED, Phase.BLOCKED))

# 2026-11-07 T21：命名落点（scene_spawn）不能只看 ctl.variant 就宣布到达。
# 同图传送可能被游戏/对端拒绝并把人拉回原处；必须再看一次真实坐标。
SCENE_SPAWN_HORIZONTAL = 3.0
SCENE_SPAWN_VERTICAL = 2.0
SAME_MAP_RETRY_SECONDS = 3.0
SAME_MAP_MAX_RETRIES = 1

@dataclass(frozen=True)
class SlashPermit:
    speaker_uuid: str
    revision: int
    # Construct only from the authenticated dispatcher, never an LLM's generated text.
    raw_command: str
    issued_at: float
    expires_at: float
    def valid(self, now):
        return (_uuid(self.speaker_uuid) and type(self.revision) is int and self.revision > 0
                and type(self.raw_command) is str and self.raw_command.lstrip().startswith('/')
                and bool(self.raw_command.lstrip()[1:].strip())
                and all(_finite(v) for v in (self.issued_at,self.expires_at,now))
                and self.issued_at <= now < self.expires_at)

@dataclass(frozen=True)
class Destination:
    destination_id: str
    map_id: int
    position: Vec3 | None
    horizontal_tolerance: float = 1.0
    vertical_tolerance: float = 0.75
    resource_cost: int = 0
    arrival: str = 'point'
    # 2026-11-07 T19：同一张图的命名落点（如 qianniaocheng）；None=原点。
    variant: str | None = None

@dataclass(frozen=True)
class Request:
    request_id: str
    bot_uuid: str
    permit: SlashPermit
    destination: Destination
    kind: CarryKind
    deadline: float

@dataclass(frozen=True)
class Scene:
    map_id: int
    room_id: str
    epoch: int
    generation: int
    # 观测到的当前命名落点；旧调用方不传时保持 None（原点）。
    variant: str | None = None

@dataclass(frozen=True)
class Presence:
    uuid: str
    scene: Scene
    incarnation: int
    observed_at: float
    position: Vec3

@dataclass(frozen=True)
class OfferEvidence:
    request_id: str
    peer_uuid: str
    bot_uuid: str
    scene: Scene
    bot_incarnation: int
    peer_incarnation: int
    observed_at: float
    # accepted / rejected are receiver-validated protocol evidence, not send results.
    state: str

@dataclass(frozen=True)
class AttachmentEvidence:
    leader_uuid: str
    peer_uuid: str
    kind: CarryKind
    scene: Scene
    bot_incarnation: int
    peer_incarnation: int
    observed_at: float
    # detached must already pass the existing receiver's release/debounce checks.
    state: str

@dataclass(frozen=True)
class Observation:
    command_revision: int
    bot: Presence | None
    peer: Presence | None
    offer: OfferEvidence | None = None
    attachment: AttachmentEvidence | None = None
    connected: bool = True

@dataclass(frozen=True)
class Binding:
    request_id: str
    speaker_uuid: str
    bot_uuid: str
    command_revision: int
    deadline: float

class EffectKind(str, Enum):
    INVITE = 'invite'
    TRANSFER = 'transfer'
    NAVIGATE = 'navigate'
    STOP = 'stop_owned_task'

@dataclass(frozen=True)
class Effect:
    effect_id: str
    binding: Binding
    kind: EffectKind
    scene: Scene | None
    carry_kind: CarryKind
    destination: Destination
    allow_resource_spend: bool = False
    # An adapter must stop only this request/revision, never a successor's motion.
    reason: str | None = None
    # 2026-11-07 T19：同图换命名落点，不需要网络换图/重连。
    same_map: bool = False

class ReceiptState(str, Enum):
    SUBMITTED = 'submitted'
    UNAVAILABLE = 'unavailable'
    REJECTED = 'rejected'

@dataclass(frozen=True)
class Receipt:
    effect_id: str
    state: ReceiptState
    resource_spent: float = 0.0

class EffectsPort(Protocol):
    def available_without_resources(self, kind: EffectKind) -> bool: ...
    def is_current(self, binding: Binding) -> bool: ...
    def submit(self, effect: Effect) -> Receipt: ...

class DisconnectedPort:
    def available_without_resources(self, kind): return False
    def is_current(self, binding): return False
    def submit(self, effect): return Receipt(effect.effect_id, ReceiptState.UNAVAILABLE)

@dataclass(frozen=True)
class Status:
    phase: Phase
    reason: str
    revision: int
    # Deliberately no UUID or free-form chat in this public summary.

class TravelCoordinator:
    def __init__(self, request: Request, *, now: float, port: EffectsPort | None = None,
                 max_age: float = 2.0, accept_timeout: float = 20.0, reuse_existing_attachment: bool = False):
        d=request.destination
        if (not request.permit.valid(now) or not _uuid(request.bot_uuid)
                or request.bot_uuid == request.permit.speaker_uuid
                or type(request.request_id) is not str or not 8 <= len(request.request_id) <= 80
                or any(not (c.isascii() and (c.isalnum() or c in '_-')) for c in request.request_id)
                or not isinstance(request.kind, CarryKind) or not isinstance(d,Destination)
                or type(d.map_id) is not int or not 0 < d.map_id < 2**32
                or not d.destination_id
                or not ((d.arrival=='point' and _point(d.position))
                        or (d.arrival=='scene_spawn'
                            and (d.position is None or _point(d.position))))
                or not _finite(d.horizontal_tolerance) or not 0 < d.horizontal_tolerance <= 2
                or not _finite(d.vertical_tolerance) or not 0 < d.vertical_tolerance <= 1
                or not _finite(request.deadline) or not now < request.deadline <= request.permit.expires_at
                or not _finite(max_age) or not 0 < max_age <= 3
                or not _finite(accept_timeout) or not 0 < accept_timeout <= 30):
            raise ValueError('invalid_explicit_travel_request')
        self.request=request; self.port=port if port is not None else DisconnectedPort()
        self.binding=Binding(request.request_id,request.permit.speaker_uuid,request.bot_uuid,
                             request.permit.revision,request.deadline)
        self.max_age=max_age;self.accept_timeout=accept_timeout;self.last_now=now
        self.reuse_existing_attachment=reuse_existing_attachment is True
        self.phase=Phase.WAITING_PEER;self.reason='fresh_peer_required'
        self.initial_scene=None;self.current_scene=None;self.accepted=False;self.was_attached=False
        self.invited_at=None;self.accepted_at=None;self.transferred_at=None;self.initial_incarnations=None
        self.effect_seq=0;self.has_effect=False;self.submitted_effects=[]
        self.navigation_submitted=False
        # 2026-11-07 T21：scene_spawn 落点的实际坐标参考（由 adapter 在换种子/传送后
        # 记录）；不是目录里伪造的坐标。None 时保持 T19 的兼容行为。
        self.target_position=d.position
        self.transferred_same_map=False
        self.transfer_retries=0
        if type(d.resource_cost) is not int or d.resource_cost != 0:
            self.phase=Phase.FAILED;self.reason='resource_consumption_forbidden'

    @property
    def status(self): return Status(self.phase,self.reason,self.binding.command_revision)

    def set_scene_target(self, position):
        """adapter 实际换好种子后调用：登记目标落点的真实坐标。

        只接受有限三元组；已经结束的请求不再改。返回是否接受。
        """
        if self.phase in TERMINAL or not _point(position):
            return False
        self.target_position = tuple(float(v) for v in position)
        return True

    def _fresh(self, stamp, now): return _finite(stamp) and 0 <= now-stamp <= self.max_age

    def _pair(self, observation, now):
        a,b=observation.bot,observation.peer
        if not observation.connected or not isinstance(a,Presence) or not isinstance(b,Presence):return None
        if (a.uuid!=self.binding.bot_uuid or b.uuid!=self.binding.speaker_uuid
                or a.scene!=b.scene or not isinstance(a.scene,Scene)
                or type(a.scene.map_id) is not int or not 0 < a.scene.map_id < 2**32
                or type(a.scene.room_id) is not str or not a.scene.room_id
                or type(a.scene.epoch) is not int or type(a.scene.generation) is not int
                or min(a.scene.epoch,a.scene.generation)<0
                or type(a.incarnation) is not int or type(b.incarnation) is not int
                or min(a.incarnation,b.incarnation)<0 or not _point(a.position) or not _point(b.position)
                or not self._fresh(a.observed_at,now) or not self._fresh(b.observed_at,now)):
            return None
        return a,b

    def _evidence(self, evidence, pair, now):
        a,b=pair
        return (evidence is not None and evidence.scene==a.scene
                and evidence.bot_incarnation==a.incarnation and evidence.peer_incarnation==b.incarnation
                and evidence.peer_uuid==b.uuid and self._fresh(evidence.observed_at,now))

    def _submit(self, kind, scene, reason=None, same_map=False):
        self.effect_seq+=1
        effect=Effect(f'{self.request.request_id}_{self.effect_seq}',self.binding,kind,scene,
                      self.request.kind,self.request.destination,False,reason,bool(same_map))
        # This is a dispatch contract, not a substitute for the real writer's atomic owner check.
        try:
            if not self.port.available_without_resources(kind):return 'adapter_unavailable'
            if not self.port.is_current(self.binding):return 'ownership_changed'
            self.has_effect=True
            receipt=self.port.submit(effect)
        except Exception:return 'adapter_error'
        if (not isinstance(receipt,Receipt) or receipt.effect_id!=effect.effect_id
                or not _finite(receipt.resource_spent) or receipt.resource_spent!=0):
            return 'invalid_or_resource_spending_receipt'
        if receipt.state is ReceiptState.SUBMITTED:
            self.has_effect=True;self.submitted_effects.append(effect);return None
        return 'adapter_unavailable' if receipt.state is ReceiptState.UNAVAILABLE else 'adapter_rejected'

    def _stop(self, reason):
        if self.has_effect:self._submit(EffectKind.STOP,self.current_scene,reason)
        self.navigation_submitted=False

    def _finish(self, phase, reason):
        if self.phase in TERMINAL:return self.status
        self._stop(reason);self.phase=phase;self.reason=reason
        # 2026-11-07 T21：同图传送在游戏/对端被拒绝后，必须把本地 ctl 恢复到
        # 传送前的种子/落点；否则本地以为在千鸟城，服务器/哥哥还在神殿，
        # 下一次请求会 target_too_far 或直接“已到达”。
        if phase is Phase.FAILED and self.transferred_same_map:
            hook=getattr(self.port,'same_map_relocation_failed',None)
            if callable(hook):
                try:hook(self.binding,reason)
                except Exception:pass
        return self.status

    def cancel(self, *, now, speaker_uuid, command_revision):
        if speaker_uuid!=self.binding.speaker_uuid or type(command_revision) is not int or command_revision<=self.binding.command_revision:
            return self.status
        if not _finite(now) or now<self.last_now:return self._finish(Phase.FAILED,'clock_regressed')
        self.last_now=now;return self._finish(Phase.CANCELLED,'explicit_owner_cancel')

    def preempt(self, *, command_revision):
        # Only the existing authoritative command dispatcher calls this after accepting a new command.
        if type(command_revision) is int and command_revision>self.binding.command_revision:
            return self._finish(Phase.SUPERSEDED,'new_command')
        return self.status

    def _dispatch(self, kind, scene, next_phase, reason, same_map=False):
        failure=self._submit(kind,scene,reason,same_map=same_map)
        if failure:return self._finish(Phase.BLOCKED if failure=='adapter_unavailable' else Phase.FAILED,failure)
        self.phase=next_phase;self.reason=reason
        return self.status

    def tick(self, now: float, observation: Observation):
        if self.phase in TERMINAL:return self.status
        if not _finite(now) or now<self.last_now:return self._finish(Phase.FAILED,'clock_regressed')
        self.last_now=now
        if now>=self.binding.deadline:return self._finish(Phase.EXPIRED,'task_deadline')
        if type(observation) is not Observation:return self._finish(Phase.FAILED,'invalid_observation')
        if observation.command_revision!=self.binding.command_revision:
            return self._finish(Phase.SUPERSEDED,'command_revision_changed')
        if self.invited_at is not None and not self.accepted and now-self.invited_at>=self.accept_timeout:
            return self._finish(Phase.EXPIRED,'invitation_timeout')
        pair=self._pair(observation,now)
        if pair is None:
            if self.phase is Phase.NAVIGATING:
                self._stop('fresh_pair_lost');self.phase=Phase.PAUSED
            self.reason='waiting_for_fresh_same_room_pair';return self.status
        a,b=pair;scene=a.scene
        if self.initial_scene is None:
            self.initial_scene=self.current_scene=scene
            self.initial_incarnations=(a.incarnation,b.incarnation)
        if self.transferred_at is None and self.initial_incarnations!=(a.incarnation,b.incarnation):
            return self._finish(Phase.FAILED,'subject_incarnation_changed_before_transfer')
        if self.transferred_at is None and scene!=self.initial_scene:
            return self._finish(Phase.FAILED,'unrequested_scene_change')
        attachment=observation.attachment
        linked=(isinstance(attachment,AttachmentEvidence) and self._evidence(attachment,pair,now)
                and attachment.leader_uuid==a.uuid and attachment.kind is self.request.kind)
        if linked and attachment.state=='detached' and self.was_attached:
            return self._finish(Phase.CANCELLED,'confirmed_relationship_detached')
        if self.phase is Phase.WAITING_PEER:
            if self.reuse_existing_attachment and linked and attachment.state=='attached':
                self.accepted=True;self.accepted_at=attachment.observed_at;self.was_attached=True
            else:
                self.invited_at=now
                return self._dispatch(EffectKind.INVITE,scene,Phase.WAITING_ACCEPT,'awaiting_acceptance')
        offer=observation.offer
        if (isinstance(offer,OfferEvidence) and self._evidence(offer,pair,now)
                and offer.bot_uuid==a.uuid and offer.request_id==self.request.request_id
                and self.invited_at is not None and offer.observed_at>=self.invited_at):
            if offer.state=='rejected':return self._finish(Phase.REJECTED,'peer_declined')
            if offer.state=='accepted':
                self.accepted=True
                if self.accepted_at is None:self.accepted_at=offer.observed_at
        if not self.accepted:self.reason='awaiting_acceptance';return self.status
        if not linked or attachment.state!='attached' or attachment.observed_at<self.accepted_at:
            if self.phase is Phase.NAVIGATING:self._stop('fresh_attachment_lost');self.phase=Phase.PAUSED
            elif self.phase is not Phase.WAITING_SCENE:self.phase=Phase.WAITING_ATTACHMENT
            self.reason='awaiting_actual_attachment';return self.status
        self.was_attached=True
        d=self.request.destination
        same_map=scene.map_id==d.map_id
        at_variant=(scene.variant or None)==(d.variant or None)
        if self.transferred_at is not None:
            if same_map:
                # 同图换落点：等 ctl.variant 真的切过去；同一落点则直接算到达。
                if not at_variant:
                    self.phase=Phase.WAITING_SCENE;self.reason='same_map_relocation_not_observed';return self.status
            else:
                if scene==self.initial_scene:self.phase=Phase.WAITING_SCENE;self.reason='transfer_not_observed';return self.status
                if scene.map_id!=d.map_id:return self._finish(Phase.FAILED,'unexpected_destination_map')
            if a.observed_at<self.transferred_at or b.observed_at<self.transferred_at or attachment.observed_at<self.transferred_at:
                self.phase=Phase.WAITING_SCENE;self.reason='post_transfer_evidence_required';return self.status
        elif not same_map:
            self.transferred_at=now
            self.transferred_same_map=False
            return self._dispatch(EffectKind.TRANSFER,scene,Phase.WAITING_SCENE,'awaiting_actual_same_room_arrival')
        elif not at_variant:
            # 2026-11-07 T19：人已经在这张图、但要换到另一个命名落点。旧逻辑按
            # map_id 相同直接 SUCCEEDED，同图请求因此“不动”。这里只对落点不同
            # 派发一次同图 TRANSFER，别的图的“已经在这张图”判断完全不变。
            self.transferred_at=now
            self.transferred_same_map=True
            return self._dispatch(EffectKind.TRANSFER,scene,Phase.WAITING_SCENE,
                                  'same_map_variant_relocation',same_map=True)
        self.current_scene=scene
        if d.arrival=='scene_spawn':
            # 2026-11-07 T21：只有 adapter 没有给出目标坐标时才保留 T19 的兼容
            # 判定。给出坐标后必须 bot 和带路人都真的在落点附近；否则同图传送
            # 视为没有发生，重新派发，超过重试上限就失败并让 adapter 回滚本地状态。
            target=self.target_position
            if target is None:
                return self._finish(Phase.SUCCEEDED,'both_fresh_attached_in_destination_scene')
            htol=max(float(d.horizontal_tolerance),SCENE_SPAWN_HORIZONTAL)
            vtol=max(float(d.vertical_tolerance),SCENE_SPAWN_VERTICAL)
            bot_at=_near(a.position,target,htol,vtol)
            peer_at=(not linked) or _near(b.position,target,htol,vtol)
            if bot_at and peer_at:
                return self._finish(Phase.SUCCEEDED,'both_observed_at_destination_scene')
            if self.transferred_at is None:
                # 例如第二次发“去千鸟城”：ctl.variant 还是 qianniaocheng，
                # 但人和哥哥都已被游戏拉回神殿。这里必须重新执行，不能直接成功。
                self.transferred_at=now
                self.transferred_same_map=True
                return self._dispatch(EffectKind.TRANSFER,scene,Phase.WAITING_SCENE,
                                      'scene_spawn_not_observed',same_map=True)
            if (self.transferred_same_map
                    and now-self.transferred_at>=SAME_MAP_RETRY_SECONDS):
                if self.transfer_retries < SAME_MAP_MAX_RETRIES:
                    self.transfer_retries+=1
                    self.transferred_at=now
                    return self._dispatch(EffectKind.TRANSFER,scene,Phase.WAITING_SCENE,
                                          'scene_spawn_not_observed_retry',same_map=True)
                return self._finish(Phase.FAILED,'destination_not_reached')
            self.phase=Phase.WAITING_SCENE
            self.reason='scene_spawn_not_observed'
            return self.status
        def at_goal(p):return hypot(p[0]-d.position[0],p[2]-d.position[2])<=d.horizontal_tolerance and abs(p[1]-d.position[1])<=d.vertical_tolerance
        if at_goal(a.position) and at_goal(b.position):return self._finish(Phase.SUCCEEDED,'both_observed_at_destination')
        if not self.navigation_submitted:
            status=self._dispatch(EffectKind.NAVIGATE,scene,Phase.NAVIGATING,'navigation_submitted_not_arrived')
            self.navigation_submitted=status.phase is Phase.NAVIGATING
            return status
        self.phase=Phase.NAVIGATING;self.reason='awaiting_observed_arrival';return self.status
