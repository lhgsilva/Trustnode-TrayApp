"""Pydantic models for the Batch Management v2 (clean-rebuild) REST surface.

Spec-named shapes for Batch Definitions (+versions), Batch Groups, Batches, and
the lifecycle actions. DB access + lifecycle logic live in service_v2.py.

The legacy models.py is kept intact for the old (now-inert) endpoints; these are
the shapes the v2 router exposes. Guide: docs/BATCH_MANAGEMENT_REDESIGN_2026-07-14.md
"""
from __future__ import annotations

from typing import Any, Optional, Literal
from pydantic import BaseModel


# --------------------------------------------------------------------------- #
#  Enums (as Literals) — mirror the spec exactly
# --------------------------------------------------------------------------- #
BatchStatus = Literal[
    "planned", "ready", "running", "held", "completed", "aborted", "invalid"
]
QualityStatus = Literal[
    "not_evaluated", "within_specification", "with_warnings",
    "out_of_specification", "data_incomplete",
]
DataQualityStatus = Literal[
    "good", "good_with_warnings", "incomplete", "invalid", "not_evaluated"
]
GroupStatus = Literal["planned", "active", "completed", "aborted"]
DefinitionStatus = Literal["draft", "published", "retired"]
BatchMode = Literal["individual", "group", "both"]
TriggerScope = Literal[
    "BATCH_START", "BATCH_STOP", "BATCH_GROUP_START", "BATCH_GROUP_STOP",
    "HOLD", "RESUME", "ABORT",
]
LimitType = Literal[
    "operating_lower", "operating_upper", "warning_lower", "warning_upper",
    "spec_lower", "spec_upper",
]
KpiQualityStatus = Literal["valid", "incomplete", "invalid", "not_applicable"]


# --------------------------------------------------------------------------- #
#  Batch Definition
# --------------------------------------------------------------------------- #
class DefinitionTagIn(BaseModel):
    gateway_id: Optional[str] = None
    historian_tag_id: Optional[str] = None
    tag_name: str
    display_name: Optional[str] = None
    engineering_unit: Optional[str] = None
    data_type: Optional[str] = None
    tag_category: Optional[str] = None
    required: bool = False
    report_enabled: bool = True
    trend_enabled: bool = True
    chart_group: Optional[str] = None
    expected_sample_rate_s: Optional[float] = None
    sort_order: int = 0
    # per-tag limits (flattened into batch_limit_definition on save)
    limits: Optional[list[dict[str, Any]]] = None   # [{limit_type, limit_value, severity, persistence_seconds, enabled}]


class TriggerReferenceIn(BaseModel):
    trigger_scope: TriggerScope
    gateway_id: Optional[str] = None
    existing_trigger_id: Optional[str] = None
    condition: Optional[dict[str, Any]] = None      # {operator: AND|OR, rules:[...]} (reused evaluator shape)
    enabled: bool = True


class KpiDefinitionIn(BaseModel):
    code: str
    name: str
    scope: Literal["batch", "group"] = "batch"
    calculation_type: Optional[str] = None
    configuration: Optional[dict[str, Any]] = None
    engineering_unit: Optional[str] = None
    enabled: bool = True
    sort_order: int = 0


class DefinitionVersionConfig(BaseModel):
    """The editable body of a definition version (frozen on publish)."""
    batch_mode: BatchMode = "individual"
    group_config: Optional[dict[str, Any]] = None       # expected child count, naming, completion
    identification: Optional[dict[str, Any]] = None      # reference rules (batch + group)
    start_config: Optional[dict[str, Any]] = None        # manual|trigger ref|edge/threshold...
    stop_config: Optional[dict[str, Any]] = None
    report_config: Optional[dict[str, Any]] = None       # included tags/trends/kpis/events/excursions/pdf/csv
    batch_report_template_id: Optional[str] = None
    batch_group_report_template_id: Optional[str] = None
    auto_generate_batch_report: bool = False
    auto_generate_batch_group_report: bool = False
    auto_email_batch_report: bool = False
    auto_email_batch_group_report: bool = False
    email_config: Optional[dict[str, Any]] = None        # recipients/cc/subject/body/attach flags
    tags: Optional[list[DefinitionTagIn]] = None
    triggers: Optional[list[TriggerReferenceIn]] = None
    kpis: Optional[list[KpiDefinitionIn]] = None
    # 2026-07-16: charts + custom properties are stored ONLY inside the version's
    # configuration_json (they have no child table). They MUST be declared here —
    # Pydantic drops unknown fields, so without these the wizard's Charts step and
    # custom properties were silently stripped on save and always came back null.
    #   charts:     [{id, title, type: line|area|scatter|bar, tags: [tag_name]}]
    #   properties: [{key, label, source: manual|linked, capture_at: start|end,
    #                 gateway_id, tag_name}]
    charts: Optional[list[dict[str, Any]]] = None
    properties: Optional[list[dict[str, Any]]] = None


class BatchDefinitionIn(BaseModel):
    code: Optional[str] = None
    name: str
    description: Optional[str] = None
    plant: Optional[str] = None
    area: Optional[str] = None
    equipment_id: Optional[str] = None
    product: Optional[str] = None
    owner: Optional[str] = None
    # the working draft config (applied to the draft version)
    config: Optional[DefinitionVersionConfig] = None


class BatchDefinitionOut(BaseModel):
    id: str
    tenant_id: str
    code: Optional[str] = None
    name: str
    description: Optional[str] = None
    plant: Optional[str] = None
    area: Optional[str] = None
    equipment_id: Optional[str] = None
    product: Optional[str] = None
    owner: Optional[str] = None
    status: DefinitionStatus
    current_version_id: Optional[str] = None
    version_number: Optional[int] = None
    created_utc: str
    updated_utc: str
    config: Optional[dict[str, Any]] = None      # decoded version config for the returned version


# --------------------------------------------------------------------------- #
#  Batch Group
# --------------------------------------------------------------------------- #
class BatchGroupIn(BaseModel):
    reference: Optional[str] = None
    external_reference: Optional[str] = None
    definition_id: Optional[str] = None
    equipment_id: Optional[str] = None
    expected_child_count: Optional[int] = None


class BatchGroupOut(BaseModel):
    id: str
    tenant_id: str
    reference: Optional[str] = None
    external_reference: Optional[str] = None
    definition_id: Optional[str] = None
    definition_version_id: Optional[str] = None
    equipment_id: Optional[str] = None
    status: GroupStatus
    expected_child_count: Optional[int] = None
    actual_child_count: int = 0
    started_utc: Optional[str] = None
    completed_utc: Optional[str] = None
    created_utc: str
    updated_utc: str


# --------------------------------------------------------------------------- #
#  Batch
# --------------------------------------------------------------------------- #
class BatchIn(BaseModel):
    reference: Optional[str] = None
    batch_group_id: Optional[str] = None
    definition_id: Optional[str] = None
    definition_version_id: Optional[str] = None
    equipment_id: Optional[str] = None
    product: Optional[str] = None
    notes: Optional[str] = None
    trigger_mode: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None
    idempotency_key: Optional[str] = None
    # Manual custom-property values typed per batch at create time, keyed by the
    # definition property key: {"order_no": "ORD-1001", "barcode": "..."}.
    # Linked/snapshot properties are captured later at start/end (not here).
    properties: Optional[dict[str, Any]] = None


class BatchActionIn(BaseModel):
    """Body for start/stop/hold/resume/abort — all optional context."""
    reason: Optional[str] = None
    actor: Optional[str] = None
    equipment_id: Optional[str] = None       # used by start to scope the window
    quality_status: Optional[QualityStatus] = None   # optional manual override on stop


class BatchCommentIn(BaseModel):
    message: str
    actor: Optional[str] = None


class BatchOut(BaseModel):
    id: str
    tenant_id: str
    reference: Optional[str] = None
    batch_group_id: Optional[str] = None
    definition_id: Optional[str] = None
    definition_version_id: Optional[str] = None
    equipment_id: Optional[str] = None
    status: BatchStatus
    quality_status: QualityStatus
    data_quality_status: DataQualityStatus
    sequence_number: Optional[int] = None
    trigger_mode: Optional[str] = None
    started_utc: Optional[str] = None
    ended_utc: Optional[str] = None
    start_reason: Optional[str] = None
    stop_reason: Optional[str] = None
    product: Optional[str] = None
    notes: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None
    created_utc: str
    updated_utc: str


class BatchListResponse(BaseModel):
    rows: list[BatchOut]
    total: int


# --------------------------------------------------------------------------- #
#  Excursion acknowledgement
# --------------------------------------------------------------------------- #
class ExcursionAckIn(BaseModel):
    acknowledged: bool = True
    actor: Optional[str] = None
    comment: Optional[str] = None


# Resolve forward references eagerly so the models are fully defined regardless
# of import order/context (Pydantic v2 otherwise builds lazily and can raise
# "not fully defined" when a parent is instantiated first). Rebuild every model
# with this module's namespace so `Any`/`Optional`/`Literal` resolve even when the
# module is loaded under a synthetic name (e.g. importlib in tests).
_ns = dict(globals())
for _m in (
    DefinitionTagIn, TriggerReferenceIn, KpiDefinitionIn, DefinitionVersionConfig,
    BatchDefinitionIn, BatchDefinitionOut, BatchGroupIn, BatchGroupOut,
    BatchIn, BatchActionIn, BatchCommentIn, BatchOut, BatchListResponse, ExcursionAckIn,
):
    _m.model_rebuild(_types_namespace=_ns)
