# -*- coding: utf-8 -*-
"""Admin API routes for multi-tenant management."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Security
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from loguru import logger
from pydantic import BaseModel, Field

from kiro.config import ADMIN_API_TOKEN

# --- Security ---
admin_key_header = APIKeyHeader(name="Authorization", auto_error=False)


async def verify_admin_token(auth_header: str = Security(admin_key_header)) -> bool:
    if not auth_header or auth_header != f"Bearer {ADMIN_API_TOKEN}":
        raise HTTPException(status_code=403, detail="Invalid admin token")
    return True


# --- Request / Response models ---

class CreateKeyRequest(BaseModel):
    name: str
    owner: str = ""
    budget_usd: Optional[float] = Field(None, ge=0)
    rate_limit_rpm: Optional[int] = Field(None, ge=0)
    rate_limit_tpm: Optional[int] = Field(None, ge=0)


class UpdateKeyRequest(BaseModel):
    name: Optional[str] = None
    owner: Optional[str] = None
    budget_usd: Optional[float] = Field(None, ge=0)
    rate_limit_rpm: Optional[int] = Field(None, ge=0)
    rate_limit_tpm: Optional[int] = Field(None, ge=0)
    enabled: Optional[int] = Field(None, ge=0, le=1)


# --- Router ---
router = APIRouter(prefix="/admin", tags=["Admin API"], dependencies=[Depends(verify_admin_token)])


@router.post("/keys")
async def create_key(request: Request, body: CreateKeyRequest):
    db = request.app.state.tenant_db
    key_info = await db.create_key(
        name=body.name,
        owner=body.owner,
        budget_usd=body.budget_usd,
        rate_limit_rpm=body.rate_limit_rpm,
        rate_limit_tpm=body.rate_limit_tpm,
    )
    logger.info(f"Created API key: {key_info['key'][:16]}... for {body.name}")
    return JSONResponse(status_code=201, content=key_info)


@router.get("/keys")
async def list_keys(request: Request):
    db = request.app.state.tenant_db
    keys = await db.list_keys()
    return {"keys": keys, "total": len(keys)}


@router.get("/keys/{key}")
async def get_key(request: Request, key: str):
    db = request.app.state.tenant_db
    key_info = await db.get_key(key)
    if not key_info:
        raise HTTPException(status_code=404, detail="API key not found")
    return key_info


@router.put("/keys/{key}")
async def update_key(request: Request, key: str, body: UpdateKeyRequest):
    db = request.app.state.tenant_db
    existing = await db.get_key(key)
    if not existing:
        raise HTTPException(status_code=404, detail="API key not found")
    updates = body.model_dump(exclude_none=True)
    if not updates:
        return existing
    updated = await db.update_key(key, **updates)
    logger.info(f"Updated API key: {key[:16]}... fields={list(updates.keys())}")
    return updated


@router.delete("/keys/{key}")
async def delete_key(request: Request, key: str):
    db = request.app.state.tenant_db
    deleted = await db.delete_key(key)
    if not deleted:
        raise HTTPException(status_code=404, detail="API key not found")
    rate_limiter = request.app.state.rate_limiter
    rate_limiter.evict(key)
    logger.info(f"Deleted API key: {key[:16]}...")
    return {"deleted": True}


@router.get("/keys/{key}/usage")
async def get_key_usage(
    request: Request,
    key: str,
    start: Optional[str] = Query(None, description="Start datetime (ISO 8601)"),
    end: Optional[str] = Query(None, description="End datetime (ISO 8601)"),
):
    db = request.app.state.tenant_db
    key_info = await db.get_key(key)
    if not key_info:
        raise HTTPException(status_code=404, detail="API key not found")
    usage = await db.get_usage(key, start=start, end=end)
    return {
        "key": key,
        "name": key_info["name"],
        "budget_usd": key_info["budget_usd"],
        "used_usd": key_info["used_usd"],
        **usage,
    }


@router.post("/keys/reset-usage")
async def reset_monthly_usage(request: Request):
    db = request.app.state.tenant_db
    count = await db.reset_monthly_usage()
    logger.info(f"Monthly usage reset for {count} keys")
    return {"reset_count": count}


# ===========================================================================
# Upstream Credential Pool Management
# ===========================================================================

class AddCredentialRequest(BaseModel):
    name: str
    cred_type: str = "refresh_token"
    refresh_token: str = ""
    creds_file: str = ""
    sqlite_db: str = ""
    profile_arn: str = ""
    region: str = "us-east-1"


class UpdateCredentialRequest(BaseModel):
    name: Optional[str] = None
    refresh_token: Optional[str] = None
    creds_file: Optional[str] = None
    sqlite_db: Optional[str] = None
    profile_arn: Optional[str] = None
    region: Optional[str] = None
    enabled: Optional[int] = Field(None, ge=0, le=1)


@router.post("/pool/credentials")
async def add_pool_credential(request: Request, body: AddCredentialRequest):
    pool = getattr(request.app.state, "credential_pool", None)
    if pool is None:
        raise HTTPException(status_code=400, detail="Credential pool is not enabled")
    cred = await pool.add_credential(
        name=body.name,
        cred_type=body.cred_type,
        refresh_token=body.refresh_token,
        creds_file=body.creds_file,
        sqlite_db=body.sqlite_db,
        profile_arn=body.profile_arn,
        region=body.region,
    )
    logger.info(f"Added pool credential #{cred['id']} ({body.name})")
    return JSONResponse(status_code=201, content=cred)


@router.get("/pool/credentials")
async def list_pool_credentials(request: Request):
    pool = getattr(request.app.state, "credential_pool", None)
    if pool is None:
        raise HTTPException(status_code=400, detail="Credential pool is not enabled")
    creds = await pool.list_credentials()
    return {"credentials": creds, "total": len(creds), "active": pool.active_count, "strategy": pool.strategy.value}


@router.get("/pool/credentials/{cred_id}")
async def get_pool_credential(request: Request, cred_id: int):
    pool = getattr(request.app.state, "credential_pool", None)
    if pool is None:
        raise HTTPException(status_code=400, detail="Credential pool is not enabled")
    cred = await pool.get_credential(cred_id)
    if not cred:
        raise HTTPException(status_code=404, detail="Credential not found")
    return cred


@router.put("/pool/credentials/{cred_id}")
async def update_pool_credential(request: Request, cred_id: int, body: UpdateCredentialRequest):
    pool = getattr(request.app.state, "credential_pool", None)
    if pool is None:
        raise HTTPException(status_code=400, detail="Credential pool is not enabled")
    updates = body.model_dump(exclude_none=True)
    if not updates:
        cred = await pool.get_credential(cred_id)
        if not cred:
            raise HTTPException(status_code=404, detail="Credential not found")
        return cred
    updated = await pool.update_credential(cred_id, **updates)
    if not updated:
        raise HTTPException(status_code=404, detail="Credential not found")
    logger.info(f"Updated pool credential #{cred_id} fields={list(updates.keys())}")
    return updated


@router.delete("/pool/credentials/{cred_id}")
async def delete_pool_credential(request: Request, cred_id: int):
    pool = getattr(request.app.state, "credential_pool", None)
    if pool is None:
        raise HTTPException(status_code=400, detail="Credential pool is not enabled")
    deleted = await pool.delete_credential(cred_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Credential not found")
    logger.info(f"Deleted pool credential #{cred_id}")
    return {"deleted": True}
