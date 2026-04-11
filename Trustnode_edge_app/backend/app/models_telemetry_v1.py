from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, ConfigDict


class TelemetryTagValue(BaseModel):
    tag_name: str
    value: Optional[float] = None
    quality_code: int = 192
    quality_label: str = "GOOD"


class TelemetrySample(BaseModel):
    model_config = ConfigDict(extra="forbid")

    edge_record_id: str
    tenant_id: str
    customer_id: str
    plant_id: str
    machine_id: str
    gateway_id: str
    collector_instance_id: str
    gateway_config_version: str
    plc_driver_type: str
    plc_endpoint_id: str
    sample_ts_utc: str
    edge_monotonic_seq: int
    interval_ms: int
    tags_json: List[TelemetryTagValue] = Field(default_factory=list)
    quality_code: int = 192
    collection_status: str = "ok"
    collected_at_edge_ts_utc: str
    received_at_vps_ts_utc: Optional[str] = None
    ingested_at_cloud_ts_utc: Optional[str] = None
    payload_hash_sha256: str
    time_status: str = "ok"


class IngestBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    gateway_id: str
    records: List[TelemetrySample] = Field(default_factory=list)


class IngestRejectedRecord(BaseModel):
    edge_record_id: Optional[str] = None
    reason: str


class IngestBatchResponse(BaseModel):
    ok: bool
    acknowledged_ids: List[str] = Field(default_factory=list)
    duplicate_ids: List[str] = Field(default_factory=list)
    rejected: List[IngestRejectedRecord] = Field(default_factory=list)
    correlation_id: str


class DeviceTokenRequest(BaseModel):
    tenant_id: str
    gateway_id: str
    expires_seconds: int = 3600


class DeviceTokenResponse(BaseModel):
    ok: bool
    token: str
    tenant_id: str
    gateway_id: str
    expires_seconds: int


class LatestQueryResponse(BaseModel):
    ok: bool
    rows: List[Dict[str, Any]] = Field(default_factory=list)


class HistoryQueryResponse(BaseModel):
    ok: bool
    rows: List[Dict[str, Any]] = Field(default_factory=list)


ActorType = Literal["device", "user", "system"]
