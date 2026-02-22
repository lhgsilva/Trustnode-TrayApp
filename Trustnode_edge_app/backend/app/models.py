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


class GatewayReading(BaseModel):
    ts_utc: str
    tag_name: str
    value: float
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
