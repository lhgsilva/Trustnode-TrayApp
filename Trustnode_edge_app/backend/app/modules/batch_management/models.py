"""Pydantic models for the Batch Management REST surface.

Only the request/response shapes the router exposes live here. DB
helpers and lifecycle logic live in service.py.
"""
from __future__ import annotations

from typing import Any, Optional, Literal
from pydantic import BaseModel, Field


# ---- batch types ---------------------------------------------------------

class BatchTypeIn(BaseModel):
    name: str
    description: Optional[str] = None
    parent_type_id: Optional[str] = None
    start_method: Literal["manual", "plc_trigger", "scheduled", "barcode"] = "manual"
    end_method: Literal["manual", "plc_trigger", "duration", "quantity", "scheduled"] = "manual"
    start_config: Optional[dict[str, Any]] = None
    end_config: Optional[dict[str, Any]] = None
    collection_profile: Literal["continuous", "trigger", "snapshot", "event", "pre_post"] = "continuous"
    report_template_id: Optional[str] = None
    identifier_method: Literal["auto", "manual", "plc", "barcode"] = "auto"
    identifier_prefix: Optional[str] = None
    summary_tags: Optional[list[str]] = None
    enabled: bool = True


class BatchTypeOut(BatchTypeIn):
    id: str
    created_utc: str
    updated_utc: str


# ---- batches -------------------------------------------------------------

class BatchIn(BaseModel):
    batch_type_id: Optional[str] = None
    parent_batch_id: Optional[str] = None
    identifier: Optional[str] = None
    identifier_method: Optional[str] = None
    product: Optional[str] = None
    recipe: Optional[str] = None
    operator: Optional[str] = None
    gateway_id: Optional[str] = None
    notes: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None


class BatchStart(BaseModel):
    operator: Optional[str] = None
    notes: Optional[str] = None
    gateway_id: Optional[str] = None


class BatchStop(BaseModel):
    result: Literal["completed", "failed", "cancelled"] = "completed"
    operator: Optional[str] = None
    notes: Optional[str] = None


class BatchValidationIn(BaseModel):
    decision: Literal["approved", "rejected"]
    notes: Optional[str] = None


class BatchEventIn(BaseModel):
    kind: str
    severity: Literal["info", "warning", "error"] = "info"
    message: Optional[str] = None
    meta: Optional[dict[str, Any]] = None


class BatchOut(BaseModel):
    id: str
    tenant_id: str
    batch_type_id: Optional[str]
    parent_batch_id: Optional[str]
    identifier: Optional[str]
    identifier_method: Optional[str]
    status: str
    started_utc: Optional[str]
    ended_utc: Optional[str]
    operator: Optional[str]
    source: Optional[str]
    gateway_id: Optional[str]
    product: Optional[str]
    recipe: Optional[str]
    notes: Optional[str]
    metadata: Optional[dict[str, Any]] = None
    created_utc: str
    updated_utc: str


class BatchListResponse(BaseModel):
    rows: list[BatchOut]
    total: int
