from __future__ import annotations

import gzip
import hashlib
import json
import re
import uuid
from datetime import datetime
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Request

from app.auth import decode_access_token
from app.auth_device import create_device_access_token, decode_device_access_token
from app.models_telemetry_v1 import (
    DeviceTokenRequest,
    DeviceTokenResponse,
    HistoryQueryResponse,
    IngestBatchRequest,
    IngestBatchResponse,
    IngestRejectedRecord,
    LatestQueryResponse,
)
from app.state import ingest_store, telemetry_service
from app.tenant import normalize_tenant_id

router = APIRouter(prefix="/api/v1", tags=["telemetry-v1"])

_UUID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")
_HASH_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def _correlation_id() -> str:
    return str(uuid.uuid4())


def _parse_auth_token(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authentication required")
    return auth.replace("Bearer ", "", 1).strip()


def _validate_sample_shape(record: Dict[str, Any]) -> List[str]:
    reasons: List[str] = []
    rid = str(record.get("edge_record_id") or "")
    if not _UUID_RE.match(rid):
        reasons.append("invalid edge_record_id")
    h = str(record.get("payload_hash_sha256") or "")
    if not _HASH_RE.match(h):
        reasons.append("invalid payload_hash_sha256")
    if int(record.get("interval_ms") or 0) <= 0:
        reasons.append("interval_ms must be > 0")
    try:
        datetime.fromisoformat(str(record.get("sample_ts_utc") or "").replace("Z", "+00:00"))
    except Exception:
        reasons.append("invalid sample_ts_utc")
    if not isinstance(record.get("tags_json"), list):
        reasons.append("tags_json must be list")
    if int(record.get("edge_monotonic_seq") or 0) <= 0:
        reasons.append("edge_monotonic_seq must be > 0")
    return reasons


def _recompute_payload_hash(record: Dict[str, Any]) -> str:
    core = {
        "tenant_id": record.get("tenant_id"),
        "customer_id": record.get("customer_id"),
        "plant_id": record.get("plant_id"),
        "machine_id": record.get("machine_id"),
        "gateway_id": record.get("gateway_id"),
        "collector_instance_id": record.get("collector_instance_id"),
        "gateway_config_version": record.get("gateway_config_version"),
        "plc_driver_type": record.get("plc_driver_type"),
        "plc_endpoint_id": record.get("plc_endpoint_id"),
        "sample_ts_utc": record.get("sample_ts_utc"),
        "edge_monotonic_seq": int(record.get("edge_monotonic_seq") or 0),
        "interval_ms": int(record.get("interval_ms") or 0),
        "tags_json": record.get("tags_json") or [],
        "quality_code": int(record.get("quality_code") or 0),
        "collection_status": record.get("collection_status"),
        "collected_at_edge_ts_utc": record.get("collected_at_edge_ts_utc"),
        "time_status": record.get("time_status") or "ok",
    }
    canonical = json.dumps(core, separators=(",", ":"), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@router.get("/healthz")
def healthz() -> Dict[str, Any]:
    return {"ok": True, "status": "healthy"}


@router.get("/readyz")
def readyz() -> Dict[str, Any]:
    return {"ok": True, "status": "ready"}


@router.post("/devices/token", response_model=DeviceTokenResponse)
def issue_device_token(payload: DeviceTokenRequest, request: Request) -> DeviceTokenResponse:
    # Human admin auth path stays separate from device ingest auth.
    token = _parse_auth_token(request)
    try:
        claims = decode_access_token(token)
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"Invalid user token: {exc}") from exc
    role = str(claims.get("role") or "viewer")
    if role != "admin":
        raise HTTPException(status_code=403, detail="Admin role required")

    tenant_id = normalize_tenant_id(payload.tenant_id)
    gateway_id = str(payload.gateway_id or "").strip()
    if not gateway_id:
        raise HTTPException(status_code=400, detail="gateway_id is required")

    device_token = create_device_access_token(
        tenant_id=tenant_id,
        gateway_id=gateway_id,
        expires_seconds=int(payload.expires_seconds),
    )
    ingest_store.audit(
        actor_type="user",
        actor_id=str(claims.get("sub") or "admin"),
        tenant_id=tenant_id,
        action="device_token_issued",
        outcome="success",
        correlation_id=_correlation_id(),
        details={"gateway_id": gateway_id, "expires_seconds": int(payload.expires_seconds)},
    )
    return DeviceTokenResponse(
        ok=True,
        token=device_token,
        tenant_id=tenant_id,
        gateway_id=gateway_id,
        expires_seconds=int(payload.expires_seconds),
    )


@router.post("/ingest/batch", response_model=IngestBatchResponse)
async def ingest_batch(request: Request) -> IngestBatchResponse:
    corr = _correlation_id()

    # Device auth scope check.
    auth_token = _parse_auth_token(request)
    try:
        device_claims = decode_device_access_token(auth_token)
    except Exception as exc:
        ingest_store.audit(
            actor_type="device",
            actor_id="unknown",
            tenant_id="unknown",
            action="device_auth",
            outcome="failure",
            correlation_id=corr,
            details={"error": str(exc)},
        )
        raise HTTPException(status_code=401, detail=f"Invalid device token: {exc}") from exc

    body = await request.body()
    if str(request.headers.get("Content-Encoding") or "").lower() == "gzip":
        try:
            body = gzip.decompress(body)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid gzip payload: {exc}") from exc

    max_size = 5_000_000
    if len(body) > max_size:
        raise HTTPException(status_code=413, detail="Payload too large")

    try:
        payload = IngestBatchRequest.model_validate_json(body)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Invalid ingest payload: {exc}") from exc

    token_tenant = normalize_tenant_id(str(device_claims.get("tenant_id") or "default"))
    token_gateway = str(device_claims.get("gateway_id") or "").strip()
    if normalize_tenant_id(payload.tenant_id) != token_tenant:
        ingest_store.audit(
            actor_type="device",
            actor_id=token_gateway or "unknown",
            tenant_id=token_tenant,
            action="ingest_batch",
            outcome="failure",
            correlation_id=corr,
            details={"reason": "tenant_scope_mismatch", "payload_tenant": payload.tenant_id},
        )
        raise HTTPException(status_code=403, detail="Tenant scope mismatch")
    if str(payload.gateway_id or "").strip() != token_gateway:
        ingest_store.audit(
            actor_type="device",
            actor_id=token_gateway or "unknown",
            tenant_id=token_tenant,
            action="ingest_batch",
            outcome="failure",
            correlation_id=corr,
            details={"reason": "gateway_scope_mismatch", "payload_gateway": payload.gateway_id},
        )
        raise HTTPException(status_code=403, detail="Gateway scope mismatch")

    acknowledged_ids: List[str] = []
    duplicate_ids: List[str] = []
    rejected: List[IngestRejectedRecord] = []
    received_at = datetime.utcnow().isoformat() + "Z"

    for rec in payload.records:
        record = rec.model_dump(mode="json")
        rid = str(record.get("edge_record_id") or "")

        reasons = _validate_sample_shape(record)
        expected_hash = _recompute_payload_hash(record)
        supplied_hash = str(record.get("payload_hash_sha256") or "")
        if expected_hash != supplied_hash:
            reasons.append("payload_hash_sha256 mismatch")

        if reasons:
            rejected.append(IngestRejectedRecord(edge_record_id=rid or None, reason="; ".join(reasons)))
            continue

        upsert_result = ingest_store.upsert_record(record, received_at_vps_ts_utc=received_at)
        if upsert_result == "inserted":
            acknowledged_ids.append(rid)
        elif upsert_result == "duplicate":
            duplicate_ids.append(rid)
        else:
            rejected.append(IngestRejectedRecord(edge_record_id=rid or None, reason=upsert_result))

    ingest_store.audit(
        actor_type="device",
        actor_id=token_gateway,
        tenant_id=token_tenant,
        action="ingest_batch",
        outcome="success" if not rejected else "partial",
        correlation_id=corr,
        details={
            "received": len(payload.records),
            "ack": len(acknowledged_ids),
            "dupe": len(duplicate_ids),
            "rejected": len(rejected),
        },
    )

    return IngestBatchResponse(
        ok=True,
        acknowledged_ids=acknowledged_ids,
        duplicate_ids=duplicate_ids,
        rejected=rejected,
        correlation_id=corr,
    )


@router.get("/history", response_model=HistoryQueryResponse)
def history(request: Request, limit: int = 1000) -> HistoryQueryResponse:
    token = _parse_auth_token(request)
    try:
        claims = decode_access_token(token)
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"Invalid user token: {exc}") from exc
    tenant_id = normalize_tenant_id(str(claims.get("tenant_id") or "default"))
    return HistoryQueryResponse(ok=True, rows=ingest_store.query_history(tenant_id=tenant_id, limit=limit))


@router.get("/latest", response_model=LatestQueryResponse)
def latest(request: Request, limit: int = 500) -> LatestQueryResponse:
    token = _parse_auth_token(request)
    try:
        claims = decode_access_token(token)
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"Invalid user token: {exc}") from exc
    tenant_id = normalize_tenant_id(str(claims.get("tenant_id") or "default"))
    return LatestQueryResponse(ok=True, rows=ingest_store.query_latest(tenant_id=tenant_id, limit=limit))


@router.get("/edge/diagnostics")
def edge_diagnostics(request: Request) -> Dict[str, Any]:
    token = _parse_auth_token(request)
    try:
        decode_access_token(token)
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"Invalid user token: {exc}") from exc
    return {"ok": True, "diagnostics": telemetry_service.diagnostics()}
