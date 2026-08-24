from typing import List, Literal

from pydantic import BaseModel, Field


# 2026-08-24: "ifm_iolink" is an IFM IO-Link master (AL13xx) read over its IoT
# Core HTTP/JSON port. Widening this union cannot affect the existing four —
# nothing dispatches on it until a gateway is actually saved with that type.
GatewayType = Literal["allen_bradley", "siemens_snap7", "siemens_opcua", "boston",
                      "ifm_iolink", "ethernet_ip"]


class GatewayConfig(BaseModel):
    gateway_type: GatewayType = "allen_bradley"
    # Display identity (2026-07-26): the operator-facing gateway name and the
    # resolved device name. The frontend sends them in the start payload so
    # historian rows carry real names instead of raw IDs / empty strings.
    name: str = ""
    device_name: str = ""
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
    # ---------------------------------------------------------------- IFM
    # 2026-08-24: only read when gateway_type == "ifm_iolink". Every field is
    # defaulted, so an existing gateway document that has never heard of them
    # constructs exactly as before.
    #
    # The block's address reuses `plc_ip`. `ifm_ports` carries the per-port tag
    # mapping — which slice of a port's process data becomes which named tag —
    # because `tags` is a flat List[str] and an IFM tag needs port + bit offset +
    # length + scale behind its name. Keeping the NAME in `tags` is what makes an
    # IFM tag indistinguishable from a PLC tag everywhere downstream.
    ifm_http_port: int = 80
    ifm_use_https: bool = False
    ifm_verify_tls: bool = False        # self-signed certificates are normal here
    ifm_username: str = ""
    ifm_password: str = ""
    ifm_port_count: int = 8
    ifm_ports: List[dict] = Field(default_factory=list)
    # ------------------------------------------------- generic EtherNet/IP
    # 2026-08-24: any EtherNet/IP adapter (IO-Link block, remote I/O, drive)
    # read by explicit CIP messaging against its input assembly. Defaulted, so
    # existing gateway documents are unaffected.
    eip_input_assembly: int = 0
    eip_output_assembly: int = 0
    eip_config_assembly: int = 0
    eip_slot: int = 0
    eip_signals: List[dict] = Field(default_factory=list)
    # What the imported EDS said, kept for display and for confirming the
    # device on the wire is the one the map was written against.
    eip_device_info: dict = Field(default_factory=dict)


class GatewayReading(BaseModel):
    ts_utc: str
    tag_name: str
    # For numeric tags this carries the float value. For string-typed tags
    # (PLC text registers, smart-meter strings, OPC-UA String/ByteString) we
    # store the original text in `value_text` and set `value` to NaN-equivalent
    # 0.0 so existing numeric consumers don't crash; downstream code should
    # branch on `value_text is not None` to render text-first.
    #
    # None means the read FAILED (quality BAD, `value_text` carries the driver
    # error). It is deliberately NOT 0.0: fabricating a zero made a broken tag
    # render as a legitimate flat-zero trend on charts and in reports, which
    # hid real faults (e.g. program-scoped tags that were never being read).
    value: float | None = None
    value_text: str | None = None
    # PLC-declared data type when the driver exposes it (pycomm3: "DINT",
    # "REAL", "STRING", "BOOL", UDT names...). "" = unknown. When empty but
    # value_text is set and value is None, readers infer "STRING" so every
    # consumer (historian Type column, dashboards, batches, reports) can
    # branch text-vs-numeric without guessing from the payload.
    data_type: str = ""
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
    # 2026-08-21 (footer truth): db_write_count/db_last_write_utc above are
    # stamped by the DISTRIBUTION path (extra sinks, cloud record, outbox).
    # When that path wedges they freeze while the local historian keeps
    # committing — the UI then reports "no DB writes" for hours on a perfectly
    # healthy gateway. historian_* is the durable local truth (what the UI
    # should show), sink_* is the same number db_* carries under a name that
    # says what it measures, and distribution_* exposes the wedge itself.
    historian_write_count: int = 0
    historian_last_write_utc: str | None = None
    sink_write_count: int = 0
    sink_last_write_utc: str | None = None
    distribution_stalled_s: float = 0.0
    distribution_stage: str | None = None
    distribution_restarts: int = 0
