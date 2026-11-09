# Home continuation — build 421589

This package contains the **idle Home connection subset** of the supplied
20261026 runtime. It has no network, file loading, HTTP, WSS, gift, payment,
joinability or account-store integration. The caller owns those lifecycle
boundaries and must not substitute a constant `True` for a real live check.

## Contract

```python
from qfsky.long0167 import Engine, validate_templates

validate_templates(scene_bytes, actor_seed)  # before requesting a ticket
engine = Engine(
    account_uuid=validated_account_uuid16,
    enter_game_body=authenticated_enter_game_body,
    generation=registration_generation,
    now=monotonic_after_registration_ack,
    scene=scene_bytes,
    actor=actor_seed,
    live_check=current_account_transport_and_cancel_check,
)
group = engine.handle(outer_type, authenticated_body, now)
# Or engine.tick(now). Dispatch only one of these at a time.
if group is not None:
    # Allocate shared skip-zero app counters, add peer/type/counter headers,
    # encrypt and queue every frame in order. No receive/service/wait between
    # frames. ENet handles its actual large-packet fragmentation rules.
    sent = queue_entire_group(group.frames)
    engine.commit(group, sent_count=sent)
```

- `Frame.outer_type`, `Frame.payload` (body only), `Frame.reliable`, `Frame.channel`.
- `FrameGroup.frames` is an immutable tuple; commit must receive the same group.
- Partial/unknown delivery, scope invalidation, pending-group interleaving,
  clock regression or own membership change permanently closes the Engine.
  Close the transport too. Never replay the group or reset it within this peer.
- `metadata()` emits fixed integer counters/booleans only. `close()` is idempotent.
- No automatic reconnection. Root controller supplies the wall-clock deadline;
  this package also rejects a lifetime over 1200 seconds, the clock helper bound.
- Disable any parallel scene-confirmation/NetTime reply implementation while
  this Engine handles them. Initial Join and registration remain caller-owned.

## Source-preserved idle order

1. Controller12 when no own Scene branch: minimum 113 ms cadence.
2. First Actor3 at registration +91 ms; the original planner uses `dt_ms/1000`.
3. In that same first pass: initial Actor, controller or Scene pair, then initial
   clock/revision extension. Subsequent Actor extensions have a 120 ms gap.
4. Scene output is ordered Aux23 → normal Scene11, or reliable Aux23 → forced
   Scene10 on the supported actual-cache own-Revoke path. The persistent Scene
   writer uses real received ACKs; receiver delta ACK14 has independent Actor
   and Scene saved tags.
5. Actor extensions always use the original held seed plus total elapsed time
   and committed extension count. They change only audited clock/revision bits
   before the typed old-to-current schema conversion. No action builder exists.

Delayed ticks do not emit a catch-up burst. One group commits as a unit after
local transport queueing; queue success is not server acceptance.

## Baselines and known limits

The caller provides two private templates. The package validates pinned hashes,
current Scene schema, canonical Home Actor map association, neutral mode,
no timed records, exact old-token preservation and current Actor roundtrip.
Templates are not bundled or written by this package.

The supplied Home Scene has a zero owner UUID. Comparison with the current
account-bound Home cache found the same zero owner, the same schema/counts,
and differences only in 16 rail FP64 values (not UUID serialization). Cold
record header values are retained from the supplied implementation.

The initial Scene path is not self-elected: it requires actual current-peer
own Elect. A bare own Elect can arm the supplied fixed Home baseline before an
actual Scene record has arrived. Public booleans distinguish
`supplied_home_baseline=True` and `actual_record_received=False`. A validated
current Snapshot adopts the complete actual record. These markers must not be
reported as fresh native Scene loading or full SceneBarn behavior.

New Actor raw8=zero/bool=false/timed-record-count=0 are the source's explicit
Home-only hypotheses, not universal native defaults. The package does not
reproduce full Actor physics, movement, animation generation, game actions,
room migration, level changes or native cache installation. It does not fake
the original runtime's WSS state; it binds to the real local connection scope.

Offline regression covers scheduling/order, exact ACKs, role branch changes,
no premature authority, complete-record adoption, partial failure, cancellation,
stale generations, roster changes, clock bounds, payload privacy and no I/O.
Real service acceptance is a separate parent-controlled test.
