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
