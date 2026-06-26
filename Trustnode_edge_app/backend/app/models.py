from typing import List, Literal

from pydantic import BaseModel, Field


GatewayType = Literal["allen_bradley", "siemens_snap7", "siemens_opcua", "boston"]


class GatewayConfig(BaseModel):
    gateway_type: GatewayType = "allen_bradley"
    plc_ip: str = ""
    opc_url: str = ""
    tags: List[str] = Field(default_factory=list)
    interval_ms: int = 1000
    equipment: str = "MACHINE-01"
    site: str = "Limerick"
    area: str = "LineA"
    collection_triggers: List[dict] = Field(default_factory=list)
    collection_trigger_mode: Literal["any", "all"] = "any"
    # Operator 2026-06-25: daily-window scheduler. When enabled, the
    # supervisor starts the gateway at schedule_start and stops it at
    # schedule_stop every day (interpreted in the edge's local
    # timezone). Disabled by default — existing gateways keep their
    # manual start/stop behavior.
    schedule_enabled: bool = False
    schedule_start: str = "08:00"  # HH:MM, 24h, local time
    schedule_stop: str = "18:00"   # HH:MM, 24h, local time
    # Operator 2026-06-25: auto-recover defaults to ON. If the
    # gateway was running and stopped unexpectedly (PLC drop, DB
    # write failure, watchdog give-up, backend restart), the
    # supervisor restarts it within ~30s. Operator can flip this OFF
    # per-gateway to suppress baseline recovery. Explicit Stop button
    # clicks are honored — they keep the gateway down regardless of
    # this flag, until the next Start click.
    auto_recover_enabled: bool = True


class GatewayReading(BaseModel):
    ts_utc: str
    tag_name: str
    # For numeric tags this carries the float value. For string-typed tags
    # (PLC text registers, smart-meter strings, OPC-UA String/ByteString) we
    # store the original text in `value_text` and set `value` to NaN-equivalent
    # 0.0 so existing numeric consumers don't crash; downstream code should
    # branch on `value_text is not None` to render text-first.
    value: float
    value_text: str | None = None
    quality: int = 192
    quality_label: str = "GOOD"
    source: str
    site: str
    area: str
    equipment: str


class GatewayStatus(BaseModel):
    running: bool
    gateway_type: GatewayType
    plc_ip: str
    interval_ms: int
    tags: List[str]
    last_error: str | None = None
    db_sink_engine: str | None = None
    db_write_count: int = 0
    db_last_write_utc: str | None = None
    db_last_error: str | None = None
    db_pending_count: int = 0
    collection_blocked: bool = False
    collection_block_reason: str | None = None
