"""Local-only identity proxy. Website credentials never leave Python for the UI.

This module does not implement NetEase account login. Email verification grants
stay in bounded process memory; only the service bearer is stored through DPAPI.
"""
from __future__ import annotations

import json
import re
import threading
import time
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener

import xg_auth_client as auth

DEFAULT_SERVICE_URL = 'https://auth.dantarian.top'
_PUBLIC_ACTIONS = {'send-code', 'verify', 'register', 'login', 'reset-password'}
_MUTATING_SESSION = {'register', 'login', 'logout', 'reset-password'}
_lock = threading.RLock()
_pending = {}
_verified = {}
_ENTITLEMENT_FIELDS = ('plan_id', 'plan_name', 'plan_days', 'price_cents', 'status',
                       'expires_at', 'paused_remaining_seconds', 'remaining_seconds', 'active', 'unlimited', 'exempt')
_REMOTE_CODES = {'invalid_email', 'invalid_password', 'invalid_purpose', 'invalid_request',
                 'invalid_device_label', 'rate_limited', 'identity_busy', 'email_delivery_unavailable',
                 'verification_failed', 'registration_failed', 'invalid_credentials',
                 'identity_session_required', 'identity_session_invalid', 'password_reset_failed', 'generic_error'}
_LOCAL_CODES = _REMOTE_CODES | {'invalid_identity_action', 'unexpected_identity_authorization',
    'invalid_identity_response', 'identity_owner_mismatch', 'invalid_subscription_response',
    'invalid_verification_purpose', 'too_many_pending_challenges', 'email_verification_required',
    'email_challenge_expired', 'invalid_verification_code', 'legacy_service_token_required',
    'client_running_stop_first', 'bad_json', 'not_found', 'identity_service_error',
    'identity_operation_failed', 'invalid_service_url', 'https_required', 'invalid_service_token',
    'windows_secret_backend_required', 'local_secret_protection_failed', 'auth_service_not_configured',
    'service_redirect_rejected', 'invalid_api_path', 'service_response_too_large',
    'invalid_service_response', 'auth_service_unavailable', 'verification_service_changed', 'redeem_code_unavailable', 'redeem_code_required',
    'subscription_unavailable', 'subscription_required', 'subscription_expired', 'subscription_paused'}


class IdentityError(Exception):
    def __init__(self, code, status=400):
        self.code, self.status = code, status
        super().__init__(code)


def _email(value):
    if not isinstance(value, str):
        raise IdentityError('invalid_email')
    value = value.strip().lower()
    if len(value) > 254 or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', value):
        raise IdentityError('invalid_email')
    return value


def _password(value, *, new=False):
    # Login preserves passwords created under the previous 15..128 policy.
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        raise IdentityError('invalid_password')
    try:
        value.encode('utf-8')
    except UnicodeEncodeError:
        raise IdentityError('invalid_password') from None
    if new:
        categories = [unicodedata.category(c) for c in value]
        if (not 8 <= len(value) <= 30 or not re.search(r'[A-Z]', value)
                or not re.search(r'[a-z]', value)
                or not any(c[0] in ('P', 'S') for c in categories)
                or any(c.isspace() for c in value)
                or any(c in ('Cc', 'Cf', 'Cs') for c in categories)):
            raise IdentityError('invalid_password')
    return value


def _config():
    try:
        return auth.read_config()
    except auth.AuthError:
        return None


def _service_url(config):
    return ((config or {}).get('url') or DEFAULT_SERVICE_URL).rstrip('/')


def _remote_error(exc):
    code = 'generic_error'
    try:
        value = json.loads(exc.read(4096)).get('error')
        if isinstance(value, str) and value in _REMOTE_CODES:
            code = value
    except (ValueError, AttributeError, OSError):
        pass
    raise IdentityError(code, exc.code if 400 <= exc.code < 600 else 502) from None


def public_request(action, payload, *, token=None, config=None):
    if action not in _PUBLIC_ACTIONS:
        raise IdentityError('invalid_identity_action')
    cfg = config if config is not None else (_config() or {})
    url = cfg.get('url') or DEFAULT_SERVICE_URL
    auth.validate_service_url(url, cfg.get('allow_loopback', False))
    headers = {'Content-Type': 'application/json'}
    # A legacy owner is linked only following the user's explicit checkbox.
    if token is not None:
        if action != 'register' or payload.get('link_existing') is not True:
            raise IdentityError('unexpected_identity_authorization')
        auth.validate_config(url, token, cfg.get('allow_loopback', False))
        headers['Authorization'] = 'Bearer ' + token
    request = Request(url.rstrip('/') + '/v1/identity/' + action,
                      data=json.dumps(payload, allow_nan=False).encode('utf-8'),
                      method='POST', headers=headers)
    try:
        with build_opener(auth._NoRedirect()).open(request, timeout=30) as response:
            raw = response.read(256001)
        if len(raw) > 256000:
            raise IdentityError('service_response_too_large', 502)
        result = json.loads(raw)
        if not isinstance(result, dict) or result.get('ok') is False:
            raise IdentityError('invalid_identity_response', 502)
        return result
    except HTTPError as exc:
        _remote_error(exc)
    except (URLError, OSError, ValueError):
        raise IdentityError('auth_service_unavailable', 503) from None


def _subscription(client, owner=None):
    result = client.request('GET', '/v1/subscription')
    identity = result.get('owner_id')
    if not isinstance(identity, str) or not identity or (owner and identity != owner):
        raise IdentityError('identity_owner_mismatch', 502)
    if not isinstance(result.get('enforcement'), bool):
        raise IdentityError('invalid_subscription_response', 502)
    entitlement = result.get('entitlement')
    if entitlement is not None and not isinstance(entitlement, dict):
        raise IdentityError('invalid_subscription_response', 502)
    if entitlement is not None and any(
            key in entitlement and (not isinstance(entitlement[key], (str, int, float, bool, type(None)))
                                    or isinstance(entitlement[key], str) and len(entitlement[key]) > 512)
            for key in _ENTITLEMENT_FIELDS):
        raise IdentityError('invalid_subscription_response', 502)
    if type(result.get('exempt', False)) is not bool:
        raise IdentityError('invalid_subscription_response', 502)
    if entitlement is not None and any(key in entitlement and type(entitlement[key]) is not bool
                                       for key in ('unlimited', 'exempt')):
        raise IdentityError('invalid_subscription_response', 502)
    return {'owner_id': identity, 'enforcement': result['enforcement'], 'exempt': result.get('exempt', False),
            'entitlement': None if entitlement is None else {
                key: entitlement[key] for key in _ENTITLEMENT_FIELDS if key in entitlement}}


def _public_user(result):
    owner = result.get('owner_id')
    user = result.get('user') or {}
    if (not isinstance(owner, str) or not owner or len(owner) > 128 or not isinstance(user, dict)
            or user.get('owner_id') != owner or user.get('email_verified') is not True):
        raise IdentityError('invalid_identity_response', 502)
    if type(result.get('expires_at')) is not int or result['expires_at'] < 0:
        raise IdentityError('invalid_identity_response', 502)
    email = user.get('email_masked')
    if not isinstance(email, str) or len(email) > 256:
        email = ''
    return {'owner_id': owner, 'email': email, 'email_verified': True,
            'expires_at': result.get('expires_at')}


def status():
    cfg = _config()
    view = {'ok': True, 'configured': bool(cfg), 'authenticated': False, 'legacy': False,
            'owner_id': None, 'email_verified': False, 'subscription': None,
            'service_url': (cfg or {}).get('url') or DEFAULT_SERVICE_URL}
    if not cfg:
        return view
    client = auth.Client(cfg)
    try:
        response = client.request('GET', '/v1/identity/session')
        view.update(_public_user(response), authenticated=True)
    except auth.AuthError as exc:
        if exc.status == 401 and exc.code == 'identity_session_required':
            view['legacy'] = True
        else:
            view['error'] = 'identity_session_invalid' if exc.status == 401 else 'auth_service_unavailable'
            return view
    try:
        view['subscription'] = _subscription(client, view.get('owner_id'))
        view['owner_id'] = view['subscription']['owner_id']
    except (auth.AuthError, IdentityError):
        view['subscription_error'] = 'subscription_unavailable'
    return view


def _current_owner(cfg):
    if not cfg:
        return None
    owner = cfg.get('owner_id')
    if isinstance(owner, str) and owner:
        return owner
    try:
        return _subscription(auth.Client(cfg))['owner_id']
    except (auth.AuthError, IdentityError):
        return None


def _store_session(response, previous):
    view = _public_user(response)
    token = response.get('token')
    url = (previous or {}).get('url') or DEFAULT_SERVICE_URL
    auth.configure(url, token, (previous or {}).get('allow_loopback', False),
                   owner_id=view['owner_id'], previous_owner_id=_current_owner(previous))
    # Explicit projection: token/client_id/raw response never reaches the browser.
    return dict(view, ok=True, configured=True, authenticated=True, legacy=False, service_url=url)


def _expire_pending():
    now = time.monotonic()
    for store in (_pending, _verified):
        for key in list(store):
            if store[key]['deadline'] <= now:
                del store[key]


def _grant(purpose, email=None, config=None):
    _expire_pending()
    grant = _verified.get(purpose)
    if not grant or (email is not None and grant['email'] != email):
        raise IdentityError('email_verification_required')
    if grant.get('service_url') != _service_url(config):
        _verified.pop(purpose, None)
        raise IdentityError('verification_service_changed')
    return grant


def dispatch(action, payload):
    if not isinstance(payload, dict):
        raise IdentityError('bad_json')
    with _lock:
        if action == 'redeem':
            if set(payload) != {'code'}:
                raise IdentityError('invalid_request')
            code = payload.get('code')
            if not isinstance(code, str) or not 1 <= len(code) <= 256:
                raise IdentityError('redeem_code_unavailable')
            cfg = _config()
            if not cfg:
                raise IdentityError('identity_session_invalid', 401)
            client = auth.Client(cfg)
            result = client.request('POST', '/v1/subscription/redeem', {'code': code})
            if result.get('ok') is not True or type(result.get('replayed')) is not bool:
                raise IdentityError('invalid_subscription_response', 502)
            return {'ok': True, 'subscription': _subscription(client, _current_owner(cfg)),
                    'replayed': result['replayed']}
        if action == 'send-code':
            email = _email(payload.get('email'))
            purpose = payload.get('purpose')
            if purpose not in ('register', 'reset'):
                raise IdentityError('invalid_verification_purpose')
            _expire_pending()
            if len(_pending) >= 128:
                raise IdentityError('too_many_pending_challenges', 429)
            config = _config() or {}
            response = public_request(action, {'email': email, 'purpose': purpose}, config=config)
            challenge = response.get('challenge_id')
            expires = response.get('expires_in')
            retry = response.get('retry_after')
            if (not isinstance(challenge, str) or not 1 <= len(challenge) <= 256
                    or type(expires) is not int or not 1 <= expires <= 3600
                    or type(retry) is not int or not 0 <= retry <= 3600):
                raise IdentityError('invalid_identity_response', 502)
            _pending[challenge] = {'email': email, 'purpose': purpose,
                                   'deadline': time.monotonic() + expires, 'service_url': _service_url(config)}
            _verified.pop(purpose, None)
            return {'ok': True, 'challenge_id': challenge, 'expires_in': expires, 'retry_after': retry}
        if action == 'verify':
            _expire_pending()
            challenge, code = payload.get('challenge_id'), payload.get('code')
            if not isinstance(challenge, str) or challenge not in _pending:
                raise IdentityError('email_challenge_expired')
            if not isinstance(code, str) or not re.fullmatch(r'[0-9]{6}', code):
                raise IdentityError('invalid_verification_code')
            config = _config() or {}
            if _pending[challenge].get('service_url') != _service_url(config):
                context = _pending.pop(challenge)
                _verified.pop(context['purpose'], None)
                raise IdentityError('verification_service_changed')
            response = public_request(action, {'challenge_id': challenge, 'code': code}, config=config)
            token = response.get('verification_token')
            expires = response.get('expires_in')
            if (not isinstance(token, str) or not 16 <= len(token) <= 2048
                    or type(expires) is not int or not 1 <= expires <= 600):
                raise IdentityError('invalid_identity_response', 502)
            context = _pending.pop(challenge)
            _verified[context['purpose']] = dict(context, token=token, deadline=time.monotonic() + expires)
            return {'ok': True, 'verified': True, 'purpose': context['purpose']}
        if action == 'register':
            email, password = _email(payload.get('email')), _password(payload.get('password'), new=True)
            previous = _config()
            grant = _grant('register', email, previous)
            request = {'email': email, 'password': password, 'verification_token': grant['token']}
            token = None
            if payload.get('link_existing') is True:
                if not previous:
                    raise IdentityError('legacy_service_token_required')
                request['link_existing'] = True
                token = previous['token']
            response = public_request(action, request, token=token, config=previous)
            result = _store_session(response, previous)
            _verified.pop('register', None)
            return result
        if action == 'login':
            request = {'email': _email(payload.get('email')), 'password': _password(payload.get('password'))}
            label = payload.get('device_label')
            if isinstance(label, str) and label.strip():
                request['device_label'] = label.strip()[:80]
            previous = _config()
            return _store_session(public_request(action, request, config=previous), previous)
        if action == 'reset-password':
            config = _config() or {}
            grant = _grant('reset', config=config)
            response = public_request(action, {'verification_token': grant['token'],
                                              'new_password': _password(payload.get('new_password'), new=True)}, config=config)
            if response.get('ok') is not True or response.get('requires_login') is not True:
                raise IdentityError('invalid_identity_response', 502)
            auth.clear_local_service_session()
            _pending.clear()
            _verified.clear()
            return {'ok': True, 'requires_login': True, 'authenticated': False}
        if action == 'logout':
            cfg = _config()
            revoked = False
            if cfg:
                try:
                    revoked = auth.Client(cfg).request('POST', '/v1/identity/logout', {}).get('ok') is True
                except auth.AuthError:
                    pass
            auth.clear_local_service_session()
            _pending.clear()
            _verified.clear()
            return {'ok': True, 'authenticated': False, 'configured': False, 'remote_revoked': revoked}
        raise IdentityError('not_found', 404)


def handle(action, *, method='POST', payload=None, busy=False):
    try:
        if method == 'GET' and action == 'status':
            return 200, dict(status(), busy=bool(busy))
        if method != 'POST':
            raise IdentityError('not_found', 404)
        if busy and action in _MUTATING_SESSION:
            raise IdentityError('client_running_stop_first', 409)
        return 200, dispatch(action, payload if payload is not None else {})
    except (IdentityError, auth.AuthError) as exc:
        code = exc.code if isinstance(exc.code, str) and exc.code in _LOCAL_CODES else 'generic_error'
        status_code = exc.status if 400 <= exc.status < 600 else 400
        return status_code, {'ok': False, 'error': code}
    except (OSError, ValueError, TypeError, KeyError):
        return 503, {'ok': False, 'error': 'identity_operation_failed'}
