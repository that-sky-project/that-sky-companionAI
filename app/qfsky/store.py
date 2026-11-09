"""Runtime-session cache only. The account vault is in the private service."""
from __future__ import annotations
from xg_auth_client import cache_get, cache_update, Client, AuthError


def list_accounts(owner=None):
    return cache_get()


def get_account(acc_id):
    return cache_get(acc_id)


def update_account(acc_id, **fields):
    cache_update(acc_id, fields)


def replace_session(acc_id, session):
    cache_update(acc_id, session)


def set_status(acc_id, status):
    update_account(acc_id, status=status)


def upsert_account_slot(session, alias="", owner=""):
    identity = session.get("id") or (session.get("renew") or {}).get("broker_account_id")
    if not identity:
        raise AuthError("private_account_vault_required")
    return Client().session(identity)


create_account = upsert_account_slot


def claim_ownerless(owner):
    return 0  # No private account migration or cross-owner transfer on clients.


def delete_account(acc_id):
    raise AuthError("private_account_management_required")


def delete_accounts_of(owner):
    raise AuthError("private_account_management_required")
