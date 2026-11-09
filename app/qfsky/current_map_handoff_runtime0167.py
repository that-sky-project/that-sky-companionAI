"""Same-map Move for a validated current map; never a cross-map transition.

The caller supplies that map's complete current capture. The independent pure
Join preparer revalidates its account, resource, native build and frozen endpoint.
No map ID rewriting or inherited Home template is performed here.
"""
from .home_handoff_runtime0167 import _RoomHandoffPump

class CurrentMapHandoffPump(_RoomHandoffPump):
    def __init__(self, *, map_id, prepare_join=None, **kwargs):
        if prepare_join is None:
            from .current_map_handoff0167 import prepare_current_pending_join
            prepare_join = prepare_current_pending_join
        super().__init__(map_id=map_id, prepare_join=prepare_join, **kwargs)
