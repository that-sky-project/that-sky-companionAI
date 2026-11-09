"""Same-map Move for a validated current map; never a cross-map transition.

The caller supplies that map's complete current capture. The independent pure
Join preparer revalidates its account, resource, native build and frozen endpoint.
No map ID rewriting or inherited Home template is performed here.
"""
from .home_handoff_runtime0167 import _RoomHandoffPump

class CurrentMapHandoffPump(_RoomHandoffPump):
    def __init__(self, *, map_id, prepare_join=None, **kwargs):
        if prepare_join is None:
            from .asset_pending_join_v32 import prepare_pending_join
            prepare_join = prepare_pending_join
        super().__init__(map_id=map_id, prepare_join=prepare_join, **kwargs)

    def change_level_same_peer(self, target, **callbacks):
        from .same_peer_level_update_v31 import migrate
        return migrate(self, target, **callbacks)
