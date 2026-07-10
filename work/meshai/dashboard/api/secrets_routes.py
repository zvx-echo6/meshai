"""Secrets management API routes.

Secret VALUES never appear in responses or logs — only set/delete/status.
"""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from meshai import secrets_store

router = APIRouter(tags=["secrets"])
log = logging.getLogger(__name__)


class SecretUpdate(BaseModel):
    value: str


@router.get("/secrets")
async def list_secrets():
    """List all managed secrets with set/unset status (no values)."""
    return secrets_store.list_secrets()


@router.put("/secrets/{env_var}")
async def set_secret(env_var: str, body: SecretUpdate):
    """Set a managed secret value."""
    if env_var not in secrets_store.SECRET_LABELS:
        raise HTTPException(status_code=400, detail="unknown secret var")
    secrets_store.set_secret(env_var, body.value)
    return {"ok": True, "restart_required": True}


@router.delete("/secrets/{env_var}")
async def delete_secret(env_var: str):
    """Delete a managed secret from the .env file."""
    if env_var not in secrets_store.SECRET_LABELS:
        raise HTTPException(status_code=400, detail="unknown secret var")
    secrets_store.delete_secret(env_var)
    return {"ok": True, "restart_required": True}


_ROOM_PK_HEX = set("0123456789abcdefABCDEF")


def _valid_room_pubkey(pk: str) -> bool:
    return len(pk) >= 12 and all(c in _ROOM_PK_HEX for c in pk)


@router.put("/meshcore/room-password/{pubkey}")
async def set_room_password_route(pubkey: str, body: SecretUpdate):
    """Set (or, if the value is empty, clear) a MeshCore room server's password.
    The value is never read back — only a boolean status is returned."""
    pk = pubkey.strip()
    if not _valid_room_pubkey(pk):
        raise HTTPException(status_code=400, detail="invalid room pubkey")
    value = (body.value or "").strip()
    if value:
        secrets_store.set_room_password(pk, value)
    else:
        secrets_store.delete_room_password(pk)
    return {"ok": True, "password_set": secrets_store.room_password_is_set(pk)}


@router.delete("/meshcore/room-password/{pubkey}")
async def clear_room_password_route(pubkey: str):
    """Clear a MeshCore room server's stored password."""
    pk = pubkey.strip()
    if not _valid_room_pubkey(pk):
        raise HTTPException(status_code=400, detail="invalid room pubkey")
    secrets_store.delete_room_password(pk)
    return {"ok": True, "password_set": False}
