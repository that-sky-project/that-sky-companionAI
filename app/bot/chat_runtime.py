"""Account-scoped process ownership and privacy-safe chat failure metadata."""
import errno
import hashlib
import json
import os
import re
from pathlib import Path


class ChatInstanceLock:
    """Hold an OS lock for this project/account; never unlink its stable inode.

    Locks are released by the OS on exit, including abnormal termination. The
    PID written inside is diagnostic only; it never decides ownership.
    """
    def __init__(self, root, account_id):
        account = str(account_id or '').strip().casefold()
        if not account:
            raise ValueError('chat_account_required')
        root = Path(root).resolve()
        key = hashlib.sha256(account.encode('utf-8')).hexdigest()
        self.path = root / 'control' / 'chat_instances' / (key + '.lock')
        if not self.path.resolve().is_relative_to(root):
            raise ValueError('chat_lock_outside_project')
        self._file = None

    def acquire(self):
        if self._file is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        os.set_inheritable(fd, False)
        handle = os.fdopen(fd, 'r+b', buffering=0)
        try:
            if os.name == 'nt':
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                return False
            raise
        try:
            handle.seek(0)
            handle.write((str(os.getpid()) + '\n').encode('ascii'))
            handle.truncate()
        except BaseException:
            handle.close()  # Closing also releases the acquired lock.
            raise
        self._file = handle
        return True

    def release(self):
        handle, self._file = self._file, None
        if handle is not None:
            # Do not remove the file: deleting/recreating it permits two owners.
            handle.close()


_CHANNELS = frozenset(('local', 'global', 'world', 'friend', 'friends', 'private', 'group'))
_CODE_CATEGORIES = {
    'too_many_requests': 'rate_limit', 'rate_limited': 'rate_limit',
    'rate_limit_exceeded': 'rate_limit', '429': 'rate_limit',
    'invalid_message': 'message_validation', 'message_too_long': 'message_validation',
    'empty_message': 'message_validation', 'content_rejected': 'content_rejection',
    'prohibited_content': 'content_rejection', 'chat_muted': 'chat_restricted',
    'muted': 'chat_restricted', 'invalid_level': 'room_state',
    'not_in_room': 'room_state', 'not_joined': 'room_state',
    'invalid_channel': 'channel_validation', 'invalid_parameter': 'parameter_validation',
    'invalid_params': 'parameter_validation', 'validation_error': 'parameter_validation',
    'session_expired': 'authentication', 'invalid_session': 'authentication',
    'unauthorized': 'authentication', '401': 'authentication',
    'forbidden': 'permission', '403': 'permission',
    'duplicate_request': 'duplicate_request', 'timeout': 'timeout',
    'internal_error': 'server_error', '500': 'server_error',
    '502': 'server_error', '503': 'server_error', '504': 'server_error',
}
_CATEGORY_PATTERNS = (
    ('rate_limit', r'too many requests|rate[ _-]?limit|发送.{0,6}频繁|频率.{0,6}(过高|限制)'),
    ('room_state', r'not in (?:the )?(?:room|level)|not joined|invalid[ _-]?(?:room|level)|不在.{0,6}(房间|地图)'),
    ('channel_validation', r'invalid[ _-]?channel|频道.{0,6}(无效|错误)'),
    ('message_validation', r'(?:message|msg).{0,12}(?:too long|empty|invalid)|(?:empty|invalid).{0,8}(?:message|msg)|消息.{0,6}(为空|过长)'),
    ('content_rejection', r'(?:content|text).{0,10}(?:reject|prohibit)|敏感词|违禁词|内容.{0,6}(违规|禁止)'),
    ('chat_restricted', r'chat.{0,10}(?:muted|disabled)|禁言'),
    ('authentication', r'(?:invalid|expired).{0,8}(?:session|token)|(?:session|token).{0,8}(?:invalid|expired)|登录.{0,6}过期'),
)


def safe_api_error(exc, *, map_id, channel):
    """Return fixed categories/allowlisted codes, never raw bodies or messages.

    HTTP 422 alone does not imply moderation, rate limiting, or map mismatch.
    Unknown error codes stay unknown instead of becoming unbounded log data.
    """
    status = getattr(exc, 'status', None)
    if type(status) is not int or not 100 <= status <= 599:
        status = None
    fields = {'status': status, 'pid': os.getpid(),
              'map_id': map_id if type(map_id) is int and 0 <= map_id <= 0xffffffff else None,
              'channel': channel if isinstance(channel, str) and channel in _CHANNELS else 'other',
              'review_stage': 'nonempty_review_output',
              'error_category': 'http_422_unspecified' if status == 422 else 'http_error',
              'api_error_code': None}
    raw = getattr(exc, 'body', None)
    if isinstance(raw, (bytes, bytearray)):
        raw = bytes(raw[:65536]).decode('utf-8', errors='replace')
    if isinstance(raw, str):
        try:
            body = json.loads(raw[:65536])
        except (ValueError, TypeError):
            body = None
    else:
        body = raw if isinstance(raw, dict) else None
    codes, texts = [], []
    def visit(value, depth=0):
        if depth > 3 or not isinstance(value, dict):
            return
        for name in ('code', 'error_code', 'errorCode', 'type'):
            code = value.get(name)
            if isinstance(code, str) and len(code) <= 64:
                codes.append(code.strip().lower())
            elif type(code) is int and code in (401, 403, 429, 500, 502, 503, 504):
                codes.append(str(code))
        for name in ('error', 'message', 'reason', 'detail', 'msg'):
            text = value.get(name)
            if isinstance(text, str):
                candidate = text[:512].strip().lower()
                texts.append(candidate)
                if candidate in _CODE_CATEGORIES:
                    codes.append(candidate)
            elif isinstance(text, dict):
                visit(text, depth + 1)
        # Only known error containers, not arbitrary nested user/account data.
        for name in ('errors', 'data'):
            child = value.get(name)
            if isinstance(child, dict):
                visit(child, depth + 1)
            elif name == 'errors' and isinstance(child, list):
                for entry in child[:4]:
                    visit(entry, depth + 1)
    visit(body)
    for code in codes:
        if code in _CODE_CATEGORIES:
            fields.update(api_error_code=code, error_category=_CODE_CATEGORIES[code])
            return fields
    for category, pattern in _CATEGORY_PATTERNS:
        if any(re.search(pattern, text, re.I) for text in texts):
            fields['error_category'] = category
            break
    return fields
