"""Request exactly one 20s passive capture; does not start/reconnect the game."""
import json
from pathlib import Path
import time
import uuid

from hop_observer_bootstrap_v20 import RELATIVE_DIRECTORY, managed_layout, _read_json, _atomic, _need


def main():
    case, _ = managed_layout(__file__)
    directory = case / RELATIVE_DIRECTORY
    state, _ = _read_json(directory / 'status.json', 4096)
    _need(state.get('schema') == 'passive-hop-observer-status-v20' and
          state.get('state') in ('ready', 'complete') and state.get('scope_token'), 'observer_not_ready')
    arm = directory / 'arm.json'
    _need(not arm.exists() and not arm.is_symlink(), 'request_already_pending')
    now, nonce = time.time(), uuid.uuid4().hex
    _atomic(arm, {'schema': 'passive-hop-arm-v20', 'request_id': nonce,
                  'scope_token': state['scope_token'], 'issued_unix': now, 'expires_unix': now + 8})
    print(json.dumps({'status': 'requested_not_started', 'request_id': nonce,
                      'duration_seconds': 20, 'instruction': 'Wait for status=recording and sample_count>=1 before moving.'}))


if __name__ == '__main__':
    main()
