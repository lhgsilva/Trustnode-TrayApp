import platform
import re
import socket
import subprocess
import time
from collections import deque
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import parse_qs, urlparse

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.models import GatewayConfig, GatewayReading, GatewayStatus, GatewayType
from app.opcua_utils import resolve_requested_nodes, split_requested_identifiers
from app.state import app_store, plc_manager

router = APIRouter(prefix="/api/plc", tags=["plc"])


class DeviceConnectionTestRequest(BaseModel):
    # 2026-08-25: this used to repeat the gateway-type union by hand, so every
    # new device type made Test Connection answer 422 "[object Object]" while
    # the gateway itself worked. Use the ONE canonical union from models.
    gateway_type: GatewayType
    plc_ip: str
    opc_url: str = ""
    opc_node_id: str = ""
    opc_node_ids: list[str] = Field(default_factory=list)
    timeout_ms: int = 2000
    # ifm blocks: the IoT Core port is configurable, so the reachability test
    # must probe the port the operator actually set.
    ifm_http_port: int = 0
    ifm_use_https: bool = False


class DeviceConnectionTestResult(BaseModel):
    ok: bool
    ping_ok: bool
    port_ok: bool
    port: int
    message: str
    opc_session_ok: bool = False
    opc_nodes: list[dict] = Field(default_factory=list)


class GatewayRuntimeStartRequest(BaseModel):
    gateway_id: str
    config: GatewayConfig
    db_sink: dict | None = None
    db_sinks: list[dict] | None = None


class GatewayRuntimeStopRequest(BaseModel):
    gateway_id: str


class TagDiscoveryRequest(BaseModel):
    gateway_type: GatewayType
    plc_ip: str
    opc_url: str = ""
    timeout_ms: int = 4000
    max_tags: int = 500
    # 2026-08-24: IFM connection details, so "Search Available Tags" reaches a
    # block on a non-default IoT port or behind HTTPS exactly like "Scan ports"
    # does. Ignored by every other gateway type.
    ifm_http_port: int = 80
    ifm_use_https: bool = False
    ifm_verify_tls: bool = False
    ifm_username: str = ""
    ifm_password: str = ""
    ifm_port_count: int = 8


class TagDiscoveryResult(BaseModel):
    ok: bool
    tags: list[str] = Field(default_factory=list)
    message: str
    # Declared controller data type per tag, e.g. {"Batch_Status_String": "STRING",
    # "BT_PVA_Level": "REAL"}. Optional and additive: older callers ignore it,
    # and every discovery path that can't determine types simply omits it.
    # The UI uses this to stop a TEXT tag being dropped onto a numeric-only
    # widget (line/gauge/pie) BEFORE the tag has ever been collected.
    types: dict[str, str] = Field(default_factory=dict)


class OpcUaBrowseRequest(BaseModel):
    plc_ip: str
    opc_url: str = ""
    timeout_ms: int = 15000
    max_nodes: int = 2000
    max_depth: int = 8
    variables_only: bool = False


class OpcUaBrowseNode(BaseModel):
    node_id: str
    display_name: str
    browse_name: str
    node_class: str
    depth: int
    parent_node_id: str | None = None
    is_variable: bool = False


class OpcUaBrowseResult(BaseModel):
    ok: bool
    message: str
    nodes: list[OpcUaBrowseNode] = Field(default_factory=list)


def _opc_node_class_name(node_class: object) -> str:
    try:
        name = getattr(node_class, "name", None)
        if name:
            return str(name)
    except Exception:
        pass
    try:
        txt = str(node_class)
        if txt and not txt.isdigit():
            return txt.split(".")[-1]
        num = int(node_class)  # type: ignore[arg-type]
        mapping = {
            1: "Object",
            2: "Variable",
            4: "Method",
            8: "ObjectType",
            16: "VariableType",
            32: "ReferenceType",
            64: "DataType",
            128: "View",
        }
        return mapping.get(num, str(num))
    except Exception:
        return "Unknown"


def _gateway_port(gateway_type: str) -> int:
    # Common default PLC/protocol ports used for quick connectivity verification.
    ports = {
        "allen_bradley": 44818,  # EtherNet/IP
        "siemens_snap7": 102,    # ISO-on-TCP (S7Comm)
        "siemens_opcua": 4840,   # OPC-UA
        "boston": 502,           # Modbus TCP (fallback for custom connector)
        "ifm_iolink": 80,        # ifm IoT Core, HTTP/JSON
        "ethernet_ip": 44818,    # generic EtherNet/IP adapter
    }
    return ports.get(gateway_type, 0)


def _ping_host(host: str, timeout_ms: int) -> tuple[bool, str]:
    """Operator 2026-06-24: single-shot ping with a tight timeout, no
    retry. The previous implementation did 2 pings + a retry with +1.5s
    timeout, totaling 5-8 seconds on a healthy host. That made the
    Devices page sit on 'Checking…' for several seconds at boot before
    flipping to Online. For the UI check we only need a quick yes/no;
    the TCP port check is the authoritative signal anyway."""
    timeout_ms = max(500, min(timeout_ms, 10_000))
    is_windows = platform.system().lower().startswith("win")
    if is_windows:
        cmd = ["ping", "-n", "1", "-w", str(timeout_ms), host]
    else:
        timeout_s = max(1, int(timeout_ms / 1000))
        cmd = ["ping", "-c", "1", "-W", str(timeout_s), host]

    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=max(1.5, timeout_ms / 1000.0 + 0.5),
            check=False,
        )
        if proc.returncode == 0:
            return True, "IP reachable by ping"
        out = (proc.stdout or proc.stderr or "").strip()
        return False, f"Ping failed: {out[:220]}"
    except Exception as err:  # pragma: no cover - runtime/network dependent
        return False, f"Ping error: {err}"


def _check_tcp_port(host: str, port: int, timeout_ms: int) -> tuple[bool, str]:
    timeout_s = max(0.5, min(timeout_ms, 10_000) / 1000.0)
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True, f"Port {port} reachable"
    except Exception as err:  # pragma: no cover - runtime/network dependent
        return False, f"Port {port} not reachable: {err}"


def _resolve_host_port(payload: DeviceConnectionTestRequest) -> tuple[str, int]:
    default_port = _gateway_port(payload.gateway_type)
    ip = payload.plc_ip.strip()
    if payload.gateway_type == "ifm_iolink":
        # The IoT Core is usually on 80 but can be moved, and HTTPS builds use
        # 443. Honour whatever the dialog is configured with.
        port = int(payload.ifm_http_port or 0) or (443 if payload.ifm_use_https else 80)
        return ip, port
    if payload.gateway_type != "siemens_opcua":
        return ip, default_port
    opc_url = (payload.opc_url or "").strip()
    if not opc_url:
        return ip, default_port
    try:
        parsed = urlparse(opc_url)
        host = parsed.hostname or ip
        port = parsed.port or default_port
        return host, int(port)
    except Exception:
        return ip, default_port


def _normalize_opc_node_ids(payload: DeviceConnectionTestRequest) -> list[str]:
    nodes: list[str] = []
    for raw in split_requested_identifiers(list(payload.opc_node_ids or []) + [payload.opc_node_id]):
        if raw not in nodes:
            nodes.append(raw)

    opc_url = (payload.opc_url or "").strip()
    if opc_url:
        try:
            parsed = urlparse(opc_url)
            qs = parse_qs(parsed.query or "")
            for key in ("node", "nodeid", "nodes", "tags"):
                values = qs.get(key, [])
                for node in split_requested_identifiers(values):
                    if node not in nodes:
                        nodes.append(node)
        except Exception:
            pass

        for match in re.finditer(r'ns=\d+;(?:s="[^"]+"|s=[^,\n;|]+|i=\d+|g=[0-9a-fA-F-]+|b=[^,\n;|]+)', opc_url):
            node = match.group(0).strip()
            if node not in nodes:
                nodes.append(node)
    return nodes


def _check_opcua_handshake_and_read(payload: DeviceConnectionTestRequest, timeout_ms: int) -> tuple[bool, str, list[dict]]:
    try:
        from opcua import Client  # type: ignore
    except Exception:
        return False, "OPC-UA library not installed (python-opcua/opcua).", []

    endpoint = (payload.opc_url or "").strip()
    if not endpoint:
        endpoint = f"opc.tcp://{payload.plc_ip.strip()}:4840"
    node_ids = _normalize_opc_node_ids(payload)
    timeout_s = max(1.0, min(timeout_ms, 12_000) / 1000.0)
    client = Client(endpoint, timeout=timeout_s)
    try:
        client.connect()
        results: list[dict] = []
        if not node_ids:
            return True, f"OPC-UA session OK ({endpoint}). Node read skipped (no node id provided).", results
        resolved_targets, unresolved = resolve_requested_nodes(client, node_ids)
        ok_all = True
        for unresolved_item in unresolved:
            ok_all = False
            results.append(
                {
                    "node_id": unresolved_item,
                    "ok": False,
                    "value": None,
                    "message": "Node could not be resolved from browse name/path. Browse and reselect this tag.",
                }
            )
        for target in resolved_targets:
            try:
                node = client.get_node(target.resolved_node_id)
                value = node.get_value()
                results.append(
                    {
                        "node_id": target.requested,
                        "resolved_node_id": target.resolved_node_id,
                        "ok": True,
                        "value": value,
                        "message": f"Read OK ({target.matched_by})",
                    }
                )
            except Exception as node_err:  # pragma: no cover - runtime/device dependent
                ok_all = False
                results.append(
                    {
                        "node_id": target.requested,
                        "resolved_node_id": target.resolved_node_id,
                        "ok": False,
                        "value": None,
                        "message": str(node_err),
                    }
                )
        success_count = sum(1 for r in results if r.get("ok"))
        msg = f"OPC-UA session OK ({endpoint}). Node reads: {success_count}/{len(results)}"
        return ok_all, msg, results
    except Exception as err:  # pragma: no cover - runtime/device dependent
        return False, f"OPC-UA session/read failed ({endpoint}): {err}", []
    finally:
        try:
            client.disconnect()
        except Exception:
            pass


def _discover_opcua_tags(payload: TagDiscoveryRequest) -> TagDiscoveryResult:
    try:
        from opcua import Client, ua  # type: ignore
    except Exception as exc:
        return TagDiscoveryResult(ok=False, tags=[], message=f"OPC-UA library not installed: {exc}")

    endpoint = (payload.opc_url or "").strip() or f"opc.tcp://{payload.plc_ip.strip()}:4840"
    timeout_s = max(1.0, min(payload.timeout_ms, 45_000) / 1000.0)
    max_tags = max(10, min(int(payload.max_tags or 500), 2000))
    client = Client(endpoint, timeout=timeout_s)
    tags: list[str] = []
    visited: set[str] = set()
    queue: list = []
    try:
        client.connect()
        namespace_count = 0
        try:
            namespace_count = len(client.get_namespace_array() or [])
        except Exception:
            namespace_count = 0
        try:
            root = client.get_node(ua.ObjectIds.ObjectsFolder)
        except Exception:
            root = client.get_root_node()
        queue.append(root)
        while queue and len(tags) < max_tags:
            node = queue.pop(0)
            try:
                children = node.get_children()
            except Exception:
                continue
            for child in children:
                if len(tags) >= max_tags:
                    break
                try:
                    node_id = child.nodeid.to_string()
                except Exception:
                    continue
                if node_id in visited:
                    continue
                visited.add(node_id)
                try:
                    nclass = child.get_node_class()
                except Exception:
                    nclass = None
                class_name = _opc_node_class_name(nclass)
                if class_name.endswith("Variable"):
                    tags.append(node_id)
                elif class_name.endswith("Object"):
                    queue.append(child)
        if tags:
            return TagDiscoveryResult(
                ok=True,
                tags=tags,
                message=(
                    f"Discovered {len(tags)} OPC-UA tags from {endpoint}"
                    + (f" (namespaces: {namespace_count})" if namespace_count else "")
                )
            )
        return TagDiscoveryResult(
            ok=False,
            tags=[],
            message=f"No browseable OPC-UA tags found at {endpoint}. Check PLC OPC namespace visibility/security."
        )
    except Exception as exc:  # pragma: no cover - runtime/device dependent
        return TagDiscoveryResult(ok=False, tags=[], message=f"OPC-UA tag discovery failed: {exc}")
    finally:
        try:
            client.disconnect()
        except Exception:
            pass


def _browse_opcua_nodes(payload: OpcUaBrowseRequest) -> OpcUaBrowseResult:
    try:
        from opcua import Client, ua  # type: ignore
    except Exception as exc:
        return OpcUaBrowseResult(ok=False, message=f"OPC-UA library not installed: {exc}", nodes=[])

    endpoint = (payload.opc_url or "").strip() or f"opc.tcp://{payload.plc_ip.strip()}:4840"
    total_budget_s = max(8.0, min(payload.timeout_ms, 120_000) / 1000.0)
    max_nodes = max(10, min(int(payload.max_nodes or 2000), 12000))
    max_depth = max(1, min(int(payload.max_depth or 8), 20))
    variables_only = bool(payload.variables_only)
    # Keep browse responsive on large Siemens namespaces and multi-layer trees.
    hard_scan_cap = max(max_nodes * 20, 50000)
    deadline = time.monotonic() + total_budget_s
    # Per-node call timeout should stay small so one branch cannot block full browse.
    per_call_timeout_s = max(1.2, min(4.0, total_budget_s / 12.0))
    root_skip_names = {"server", "types", "views"}
    metadata_leaf_names = {
        "devicemanual",
        "devicerevision",
        "engineeringrevision",
        "hardwarerevision",
        "manufacturer",
        "model",
        "productinstanceuri",
        "producturi",
        "serialnumber",
        "softwarerevision",
    }
    priority_names = (
        "deviceset",
        "plc",
        "tags",
        "tagtable",
        "program",
        "programs",
        "datablock",
        "datablocks",
        "globaldb",
        "db",
    )

    client = Client(endpoint, timeout=per_call_timeout_s)
    out: list[OpcUaBrowseNode] = []
    visited: set[str] = set()

    try:
        client.connect()
        try:
            root = client.get_node(ua.ObjectIds.ObjectsFolder)
        except Exception:
            root = client.get_root_node()

        queue: deque[tuple[object, int, str | None, bool]] = deque([(root, 0, None, False)])
        # Stage 0: seed queue with direct children and prioritize Siemens process folders.
        try:
            root_children = root.get_children()
        except Exception:
            root_children = []
        prio: list[tuple[object, int, str | None, bool]] = []
        normal: list[tuple[object, int, str | None, bool]] = []
        for child in root_children:
            try:
                child_name = str(child.get_browse_name().Name or "").strip().lower()
            except Exception:
                child_name = ""
            entry = (child, 1, None, any(k in child_name for k in priority_names))
            if entry[3]:
                prio.append(entry)
            else:
                normal.append(entry)
        for entry in prio:
            queue.appendleft(entry)
        for entry in normal:
            queue.append(entry)

        scanned = 0
        timed_out_partial = False
        stage_relaxed = False
        while queue and len(out) < max_nodes:
            if scanned >= hard_scan_cap or time.monotonic() >= deadline:
                timed_out_partial = True
                # Stage 1 fallback: relax filters if we have very few process variables.
                if (not stage_relaxed) and sum(1 for n in out if n.is_variable) < 25:
                    stage_relaxed = True
                    hard_scan_cap = max(hard_scan_cap, int(max_nodes * 30))
                    continue
                break
            node, depth, parent_id, parent_priority = queue.popleft()
            scanned += 1
            if depth > max_depth:
                continue
            try:
                node_id = node.nodeid.to_string()
            except Exception:
                continue
            if node_id in visited:
                continue
            visited.add(node_id)

            try:
                node_class = _opc_node_class_name(node.get_node_class())
            except Exception:
                node_class = "Unknown"
            is_variable = node_class.endswith("Variable")

            try:
                display_name = str(node.get_display_name().Text or "")
            except Exception:
                display_name = ""
            try:
                browse_name = str(node.get_browse_name().Name or "")
            except Exception:
                browse_name = ""
            browse_name_norm = str(browse_name or "").strip().lower()

            if depth == 1 and browse_name_norm in root_skip_names:
                # Server/Types/Views are large and usually not user process tags.
                continue

            if (not variables_only) or is_variable:
                out.append(
                    OpcUaBrowseNode(
                        node_id=node_id,
                        display_name=display_name or browse_name or node_id,
                        browse_name=browse_name,
                        node_class=node_class,
                        depth=depth,
                        parent_node_id=parent_id,
                        is_variable=is_variable,
                    )
                )
                if len(out) >= max_nodes:
                    break

            if depth < max_depth:
                try:
                    children = node.get_children()
                except Exception:
                    children = []
                if (not stage_relaxed) and browse_name_norm in metadata_leaf_names:
                    # Keep traversal focused on process namespaces, not static device identity leaves.
                    children = []

                for child in children:
                    try:
                        child_browse_name = str(child.get_browse_name().Name or "")
                    except Exception:
                        child_browse_name = ""
                    child_norm = child_browse_name.strip().lower()
                    child_priority = parent_priority or any(k in child_norm for k in priority_names)
                    if child_norm in root_skip_names:
                        queue.append((child, depth + 1, node_id, child_priority))
                        continue
                    if child_priority:
                        queue.appendleft((child, depth + 1, node_id, child_priority))
                    else:
                        queue.append((child, depth + 1, node_id, child_priority))

        variable_count = sum(1 for n in out if n.is_variable)
        object_count = sum(1 for n in out if str(n.node_class).endswith("Object"))
        method_count = sum(1 for n in out if str(n.node_class).endswith("Method"))
        partial_note = (
            f" (partial; scanned {scanned} nodes in {int(total_budget_s)}s budget)"
            if timed_out_partial
            else ""
        )
        return OpcUaBrowseResult(
            ok=True,
            message=(
                f"Browsed {len(out)} nodes from {endpoint} "
                f"(objects: {object_count}, variables: {variable_count}, methods: {method_count})"
                f"{partial_note}"
            ),
            nodes=out,
        )
    except Exception as exc:  # pragma: no cover - runtime/device dependent
        return OpcUaBrowseResult(ok=False, message=f"OPC-UA browse failed: {exc}", nodes=[])
    finally:
        try:
            client.disconnect()
        except Exception:
            pass


def _discover_ab_tags(payload: TagDiscoveryRequest) -> TagDiscoveryResult:
    try:
        from pycomm3 import LogixDriver  # type: ignore
    except Exception as exc:
        return TagDiscoveryResult(
            ok=False,
            tags=[],
            message=f"AB discovery requires pycomm3 library (not installed): {exc}",
        )

    ip = (payload.plc_ip or "").strip()
    if not ip:
        return TagDiscoveryResult(ok=False, tags=[], message="PLC IP is required")

    max_tags = max(10, min(int(payload.max_tags or 2000), 10000))
    # Common path fallback for AB controllers if slot is not provided.
    paths = [ip]
    if "/" not in ip:
        paths.append(f"{ip}/1")

    last_err = ""
    for path in paths:
        try:
            with LogixDriver(path, init_tags=True, init_program_tags=True) as plc:
                tag_defs: list[dict] = []

                # Pull from preloaded tag dictionary (usually richest source).
                if isinstance(getattr(plc, "tags", None), dict):
                    for nm, meta in plc.tags.items():
                        row = dict(meta or {})
                        row.setdefault("tag_name", nm)
                        tag_defs.append(row)

                # Refresh from online browse (controller + program tags).
                try:
                    online = plc.get_tag_list(program="*", cache=False) or []
                    tag_defs.extend(online)
                except Exception:
                    pass
                try:
                    online_root = plc.get_tag_list(cache=False) or []
                    tag_defs.extend(online_root)
                except Exception:
                    pass

                # Cap how many indexed elements we enumerate per array
                # so a REAL[10000] data buffer doesn't drown the picker.
                # The user can always type a higher index by hand if they
                # really need element 1000 of a 1000-element array.
                ARRAY_ELEMENT_CAP = 256

                def _base_name(tag: dict) -> str:
                    name = str(tag.get("tag_name") or tag.get("name") or tag.get("symbol_name") or "").strip()
                    if not name:
                        return ""
                    program = str(tag.get("program_name") or tag.get("program") or "").strip()
                    if program and not name.startswith("Program:"):
                        if "." not in name:
                            name = f"Program:{program}.{name}"
                        else:
                            name = f"Program:{program}.{name.split('.')[-1]}"
                    return name

                def _array_dims(tag: dict) -> list[int]:
                    raw = tag.get("dimensions") or tag.get("dim") or []
                    if not isinstance(raw, (list, tuple)):
                        return []
                    return [int(d) for d in raw if isinstance(d, (int, float)) and int(d) > 0]

                def _enumerate_indexed_names(base: str, dims: list[int]) -> list[str]:
                    """SimREAL with dims=[10] -> SimREAL[0]..SimREAL[9].
                    MyTag with dims=[3,4] -> MyTag[0,0]..MyTag[2,3] (12 entries).
                    Capped at ARRAY_ELEMENT_CAP total elements per tag."""
                    if not dims:
                        return [base]
                    out: list[str] = []
                    # Walk the cartesian product of all dimensions.
                    import itertools
                    ranges = [range(min(d, ARRAY_ELEMENT_CAP)) for d in dims]
                    for combo in itertools.product(*ranges):
                        out.append(f"{base}[{','.join(str(i) for i in combo)}]")
                        if len(out) >= ARRAY_ELEMENT_CAP:
                            break
                    return out

                # Build the candidate list: scalars stay as-is, arrays are
                # expanded into [0]..[N-1] indices. For struct-typed arrays
                # (UDTs, module-defined data types like `PANEL_1:I`) we ALSO
                # emit the bare base name so the operator can spot it in the
                # picker — pycomm3 sometimes fails the per-element probe on
                # struct elements even though the array head reads fine, and
                # the operator can always type a specific element via Manual
                # Tag Entry on the UI.
                seen: set[str] = set()
                candidates: list[str] = []

                def _is_struct(td: dict) -> bool:
                    tt = str(td.get("tag_type") or "").lower()
                    if tt == "struct":
                        return True
                    dt = td.get("data_type")
                    if isinstance(dt, dict) and dt.get("internal_tags"):
                        return True
                    return False

                for td in tag_defs:
                    if not isinstance(td, dict):
                        continue
                    base = _base_name(td)
                    if not base:
                        continue
                    # If the base name already carries a subscript (rare —
                    # the discovery sources sometimes do this), keep it as-is.
                    if "[" in base:
                        if base not in seen:
                            seen.add(base); candidates.append(base)
                        continue
                    dims = _array_dims(td)
                    is_struct = _is_struct(td)
                    # Struct arrays: emit the bare name first so a per-
                    # element probe failure still leaves something visible.
                    if dims and is_struct and base not in seen:
                        seen.add(base); candidates.append(base)
                    for nm in _enumerate_indexed_names(base, dims):
                        if nm in seen:
                            continue
                        seen.add(nm); candidates.append(nm)
                        if len(candidates) >= max_tags:
                            break
                    if len(candidates) >= max_tags:
                        break

                if not candidates:
                    return TagDiscoveryResult(
                        ok=False,
                        tags=[],
                        message=f"No browseable AB tags found at {path}. Check External Access and controller browse permissions.",
                    )

                # Probe-read every candidate so only tags that the PLC
                # actually accepts are offered. Program-scoped names are
                # exempt because pycomm3 returns them in plc.tags only
                # when init_program_tags=True (which we did) — those are
                # already vetted. We probe in chunks because pycomm3
                # accepts a large batch but a single huge call can stall.
                # Operator 2026-06-12: "we should have a recovery logic
                # when the PLC has too many tags". Two safety nets:
                #   1. Soft time budget — bail out of the probe loop
                #      after PROBE_BUDGET_S and return what we have
                #      plus the unprobed remainder as candidate tags
                #      (better to offer 1000 unverified than to time
                #      out and offer none).
                #   2. Chunk-size de-escalation — pycomm3 chokes on
                #      huge multi-reads against some firmware; start
                #      at 64, drop to 16, then 4, then 1.
                import time as _time
                PROBE_BUDGET_S = 45.0
                CHUNK_LADDER = (64, 16, 4, 1)
                start_ts = _time.monotonic()
                good: list[str] = []
                bad_count = 0
                partial = False
                i = 0
                chunk_idx = 0
                while i < len(candidates):
                    if _time.monotonic() - start_ts > PROBE_BUDGET_S:
                        # Out of time: keep going with the unprobed
                        # remainder as raw candidates so the operator
                        # at least sees them in the picker.
                        partial = True
                        for nm in candidates[i:]:
                            good.append(nm)
                        break
                    CHUNK = CHUNK_LADDER[chunk_idx]
                    chunk = candidates[i:i + CHUNK]
                    try:
                        results = plc.read(*chunk)
                        if not isinstance(results, list):
                            results = [results]
                    except Exception:
                        # A whole-chunk failure → step the chunk size
                        # down. Eventually we'll be reading singles,
                        # which always works (or never works, in which
                        # case the bad_count grows and we move on).
                        if chunk_idx < len(CHUNK_LADDER) - 1:
                            chunk_idx += 1
                            continue
                        results = []
                        for nm in chunk:
                            try:
                                r = plc.read(nm)
                                results.append(r)
                            except Exception as inner:
                                class _Bad:
                                    error = str(inner) or "read failed"
                                    value = None
                                results.append(_Bad())
                    for nm, res in zip(chunk, results):
                        err = getattr(res, "error", None)
                        if err:
                            bad_count += 1
                            continue
                        good.append(nm)
                    i += CHUNK

                if good:
                    array_count = sum(1 for n in good if "[" in n and "]" in n)
                    msg = f"Discovered {len(good)} valid AB tags from {path} (arrays expanded: {array_count})"
                    if bad_count:
                        msg += f"; filtered {bad_count} unreadable tag(s)"
                    if partial:
                        msg += f"; probe-read budget exceeded — last {len(candidates) - i} tag(s) returned unverified"
                    # Declared data type per discovered tag. Indexed/member names
                    # (Arr[3], S.field) inherit their base tag's type. Best-effort:
                    # any failure just yields an empty map and the UI falls back
                    # to inferring from collected data.
                    types_map: dict[str, str] = {}
                    try:
                        base_types: dict[str, str] = {}
                        for td in tag_defs:
                            if not isinstance(td, dict):
                                continue
                            nm = _base_name(td)
                            if not nm:
                                continue
                            dt = td.get("data_type")
                            if isinstance(dt, dict):
                                dt = dt.get("name") or "STRUCT"
                            dt = str(dt or "").strip().upper()
                            if dt:
                                base_types[nm] = dt
                        for nm in good:
                            root = re.sub(r"\[[^\]]*\]", "", str(nm))
                            if root.startswith("Program:"):
                                parts = root.split(".")
                                root = ".".join(parts[:2]) if len(parts) >= 2 else root
                            else:
                                root = root.split(".", 1)[0]
                            dt = base_types.get(root) or base_types.get(str(nm))
                            if dt:
                                types_map[str(nm)] = dt
                    except Exception:
                        types_map = {}
                    return TagDiscoveryResult(ok=True, tags=good, message=msg, types=types_map)
                return TagDiscoveryResult(
                    ok=False,
                    tags=[],
                    message=f"Every tag at {path} failed probe-read; check External Access on the controller.",
                )
        except Exception as exc:  # pragma: no cover - runtime/device dependent
            last_err = str(exc)
            continue

    return TagDiscoveryResult(
        ok=False,
        tags=[],
        message=f"AB tag discovery failed for {ip}: {last_err or 'unknown error'}",
    )


@router.get("/config", response_model=GatewayConfig)
def get_config() -> GatewayConfig:
    return plc_manager.get_config()


@router.put("/config", response_model=GatewayConfig)
def update_config(payload: GatewayConfig) -> GatewayConfig:
    return plc_manager.set_config(payload)


@router.get("/status", response_model=GatewayStatus)
def get_status() -> GatewayStatus:
    return plc_manager.get_status()


@router.get("/snapshot", response_model=list[GatewayReading])
def get_snapshot() -> list[GatewayReading]:
    return plc_manager.get_snapshot()


@router.post("/start")
async def start() -> dict[str, bool]:
    await plc_manager.start()
    return {"started": True}


@router.post("/stop")
async def stop() -> dict[str, bool]:
    await plc_manager.stop()
    return {"stopped": True}


@router.post("/test-connection", response_model=DeviceConnectionTestResult)
def test_connection(payload: DeviceConnectionTestRequest) -> DeviceConnectionTestResult:
    ip = payload.plc_ip.strip()
    if not ip:
        return DeviceConnectionTestResult(
            ok=False,
            ping_ok=False,
            port_ok=False,
            port=0,
            message="PLC IP is required"
        )

    host, port = _resolve_host_port(payload)
    ping_ok, ping_msg = _ping_host(host, payload.timeout_ms)
    port_ok, port_msg = _check_tcp_port(host, port, payload.timeout_ms)
    ok = ping_ok and port_ok
    message = f"Target {host}:{port}. {ping_msg}; {port_msg}"
    opc_session_ok = False
    opc_nodes: list[dict] = []
    if payload.gateway_type == "siemens_opcua":
        if port_ok:
            opc_ok, opc_msg, opc_nodes = _check_opcua_handshake_and_read(payload, payload.timeout_ms)
            ok = ok and opc_ok
            opc_session_ok = opc_ok
            message = f"{message}; {opc_msg}"
        else:
            ok = False
            message = f"{message}; OPC-UA handshake skipped because TCP port check failed."
    return DeviceConnectionTestResult(
        ok=ok,
        ping_ok=ping_ok,
        port_ok=port_ok,
        port=port,
        message=message,
        opc_session_ok=opc_session_ok,
        opc_nodes=opc_nodes,
    )


@router.post("/discover-tags", response_model=TagDiscoveryResult)
def discover_tags(payload: TagDiscoveryRequest) -> TagDiscoveryResult:
    ip = (payload.plc_ip or "").strip()
    if not ip:
        return TagDiscoveryResult(ok=False, tags=[], message="PLC IP is required")

    if payload.gateway_type == "siemens_opcua":
        return _discover_opcua_tags(payload)

    if payload.gateway_type == "allen_bradley":
        return _discover_ab_tags(payload)

    if payload.gateway_type == "siemens_snap7":
        return TagDiscoveryResult(
            ok=False,
            tags=[],
            message=(
                "Siemens Snap7 cannot enumerate symbolic tags from IP-only. "
                "Use exported tag list from TIA/PLC project or OPC-UA browse."
            ),
        )

    if payload.gateway_type == "ifm_iolink":
        return _discover_ifm_tags(payload)

    return TagDiscoveryResult(
        ok=False,
        tags=[],
        message="Tag discovery is not supported for this gateway type in IP-only mode.",
    )


class EdsImportRequest(BaseModel):
    """An EDS file's TEXT, pasted or uploaded by the operator."""
    eds_text: str = ""


@router.post("/eip/parse-eds")
def eip_parse_eds(payload: EdsImportRequest) -> dict:
    """Read a device's EDS and report its identity and assemblies.

    This is the CODESYS step an operator recognises: install the device
    description, and the assemblies (with their sizes) come back so the input
    instance does not have to be found in a PDF.
    """
    from app.drivers.ethernet_ip import EdsParseError, guess_assemblies, parse_eds
    try:
        parsed = parse_eds(payload.eds_text or "")
    except EdsParseError as exc:
        return {"ok": False, "message": str(exc)}
    except Exception as exc:
        return {"ok": False, "message": f"Could not read the EDS: {exc}"}
    guess = guess_assemblies(parsed)
    # 2026-08-28: the EDS says WHICH assembly and HOW BIG, never what a bit
    # means. For a device family whose layout we know, combine the two so the
    # operator gets tags from one upload instead of hand-mapping 16 byte/bit
    # pairs. For anything else we say plainly that the EDS cannot supply them.
    from app.drivers.ethernet_ip import signals_from_eds
    layout = signals_from_eds(parsed)
    if layout.get("input_assembly"):
        guess = dict(guess or {})
        guess["input_assembly"] = layout["input_assembly"]
    message = (f"{parsed.get('product_name') or 'Device'} — "
               f"{len(parsed.get('assemblies') or [])} assembly(ies) found.")
    if layout.get("ok"):
        message += " " + layout["message"]
    return {"ok": True, "device": parsed, "suggested": guess,
            "layout": layout, "signals": layout.get("signals") or [],
            "message": message}


class IfmPinMapRequest(BaseModel):
    """Build (and optionally verify) the pin map of an ifm block on fieldbus."""
    plc_ip: str = ""
    instance: int = 0
    slot: int = 0
    port_count: int = 8
    pin4_byte: int = 0
    pin2_byte: int = 1
    verify: bool = True


class IfmFieldbusAutoRequest(BaseModel):
    """Configure an ifm block that is wired to its FIELDBUS port."""
    plc_ip: str = ""
    slot: int = 0
    port_count: int = 8


# Assembly instances an ifm master publishes. 100 is the documented input
# assembly on the DL/AL families; the rest are probed so an unfamiliar model
# still configures itself instead of asking the operator for a number they do
# not have.
IFM_ASSEMBLY_CANDIDATES = (100, 101, 102, 103, 104, 150, 151, 152, 199)


class CollectionTriggersApplyRequest(BaseModel):
    """The trigger set a client hydrated, re-applied to the running managers."""
    collection_triggers: list = []
    collection_trigger_mode: str = "any"


@router.post("/collection-triggers/apply")
def apply_collection_triggers(payload: CollectionTriggersApplyRequest) -> dict:
    """Make the saved trigger rules effective without editing them.

    2026-09-02: a restart left the manager with no trigger set until somebody
    edited a trigger, so the rules sat on disk gating nothing - and on a site
    with a power meter and no PLC gateway they gated nothing ever, because the
    set was rebuilt purely from running workers. The client hydrates the rules
    with its own scope already resolved, so it is the right thing to hand them
    back; the server must not guess which scope's document to read.
    """
    from app.state import plc_manager as _pm
    rows = [t for t in (payload.collection_triggers or []) if isinstance(t, dict)]
    n = _pm.apply_collection_triggers(rows, str(payload.collection_trigger_mode or "any"))
    return {"ok": True, "rules": len(rows), "gateways_updated": n}


@router.post("/eip/ifm-fieldbus-autoconfig")
def eip_ifm_fieldbus_autoconfig(payload: IfmFieldbusAutoRequest) -> dict:
    """One call: identify the block, find its input assembly, build the tags.

    An ifm block wired to its fieldbus port has no IoT Core to scan, so the
    "Scan block" flow cannot help and the operator is left to find an assembly
    instance and 16 byte/bit pairs by hand. This does the whole thing: CIP
    identity, assembly probe, pin map, and a live verification - and hands back
    a gateway configuration ready to save.
    """
    from app.drivers.ethernet_ip import (
        EipDeviceClient, EipSignal, decode_signal, ifm_master_signals)

    host = (payload.plc_ip or "").strip()
    if not host:
        return {"ok": False, "message": "The block address is required."}

    client = EipDeviceClient(host=host, slot=int(payload.slot or 0), timeout_s=4.0)
    try:
        info = client.identify() or {}
    except Exception as exc:
        return {"ok": False, "message":
                f"No EtherNet/IP device answered at {host}:44818 ({exc}). "
                f"Check the block is on its fieldbus port and reachable."}

    vendor = str(info.get("vendor") or "")
    if vendor and "ifm" not in vendor.lower():
        # Not fatal - the pin map may still be wanted - but say so plainly.
        note = (f"This device reports as '{vendor}', not ifm. The ifm pin map "
                f"below may not match it; check the byte offsets against its "
                f"manual before saving.")
    else:
        note = ""

    # Probe for a readable input assembly. The biggest one is the full input
    # image; the tiny ones are status/diagnostic assemblies.
    found: list[dict] = []
    for inst in IFM_ASSEMBLY_CANDIDATES:
        try:
            data = client.read_assembly(inst)
        except Exception:
            continue
        if data:
            found.append({"instance": inst, "size_bytes": len(data),
                          "first_bytes": data[:8].hex()})
    if not found:
        return {"ok": False, "device": info, "assemblies": [],
                "message": f"{info.get('product_name') or 'The device'} answered, "
                           f"but none of the usual input assemblies could be read. "
                           f"Import its EDS file to find the right instance."}

    # 100 when it is there (documented), else the largest readable one.
    chosen = next((f for f in found if f["instance"] == 100), None) or max(
        found, key=lambda f: f["size_bytes"])
    instance = int(chosen["instance"])

    # 2026-09-02: was ifm_pin_signals, which mapped bytes 0-1 only - the
    # digital inputs and nothing else. The block also publishes each port's
    # IO-Link identity and its process data further into the same assembly,
    # so a fieldbus gateway offered neither until now.
    signals = ifm_master_signals(port_count=int(payload.port_count or 8),
                                 assembly_size=int(chosen["size_bytes"] or 446))
    values = []
    try:
        data = client.read_assembly(instance)
        for spec in signals:
            sig = EipSignal.from_dict(spec)
            try:
                values.append({"name": sig.name,
                               "value": decode_signal(data, sig),
                               "channel": spec.get("channel", "")})
            except Exception as exc:
                values.append({"name": sig.name, "value": None,
                               "error": str(exc)})
    except Exception:
        pass

    high = [v["name"] for v in values if v.get("value")]
    message = (f"{info.get('product_name') or 'ifm block'} found on fieldbus. "
               f"Input assembly {instance} ({chosen['size_bytes']} bytes), "
               f"{len(signals)} pin tag(s). "
               + (f"Currently high: {', '.join(high)}." if high
                  else "No input is currently high."))
    if note:
        message = note + " " + message

    return {
        "ok": True,
        "device": info,
        "assemblies": found,
        "config": {
            "gateway_type": "ethernet_ip",
            "plc_ip": host,
            "eip_slot": int(payload.slot or 0),
            "eip_input_assembly": instance,
            "eip_signals": signals,
            "eip_device_info": info,
            "tags": [s["name"] for s in signals],
        },
        "values": values,
        "message": message,
    }


@router.post("/eip/ifm-pin-map")
def eip_ifm_pin_map(payload: IfmPinMapRequest) -> dict:
    """The standard ifm digital-input map, ready to save.

    An EDS says which assembly to read and how big it is; it does NOT say what
    any single bit means. For an ifm IO-Link master the digital input image is
    the first two bytes of the input assembly - byte 0 is pin 4 and byte 1 is
    pin 2, bit N-1 being port N - and hand-typing 16 byte/bit pairs is exactly
    where an operator makes a mistake that yields a plausible wrong value
    rather than an error.

    The names produced here are the SAME ones the IoT Core path discovers, so a
    trend built against a block on one transport keeps working on the other.

    With `verify` the assembly is read live and the decoded values come back
    beside the raw bytes, which is what makes the map checkable before saving.
    """
    from app.drivers.ethernet_ip import (
        EipDeviceClient, EipSignal, decode_signal, ifm_pin_signals)

    signals = ifm_pin_signals(port_count=int(payload.port_count or 8),
                              pin4_byte=int(payload.pin4_byte or 0),
                              pin2_byte=int(payload.pin2_byte if payload.pin2_byte is not None else 1))
    result = {"ok": True, "signals": signals, "values": [], "raw": "",
              "message": f"{len(signals)} pin signal(s) for a "
                         f"{int(payload.port_count or 8)}-port ifm block."}
    if not payload.verify or not (payload.plc_ip or "").strip():
        return result
    if int(payload.instance or 0) <= 0:
        result["message"] += " Give an input assembly instance to verify it live."
        return result

    client = EipDeviceClient(host=payload.plc_ip.strip(), slot=int(payload.slot or 0),
                             timeout_s=4.0)
    try:
        data = client.read_assembly(int(payload.instance))
    except Exception as exc:
        # The map is still valid and still worth returning - only the live
        # check failed, and saying so beats returning nothing.
        result["message"] += f" Could not verify against the device: {exc}"
        return result

    values = []
    for spec in signals:
        sig = EipSignal.from_dict(spec)
        try:
            values.append({"name": sig.name, "value": decode_signal(data, sig),
                           "channel": spec.get("channel", ""), "error": ""})
        except Exception as exc:
            values.append({"name": sig.name, "value": None,
                           "channel": spec.get("channel", ""), "error": str(exc)})
    high = [v["name"] for v in values if v.get("value")]
    result["raw"] = data.hex()
    result["size_bytes"] = len(data)
    result["values"] = values
    result["message"] = (
        f"{len(signals)} pin signal(s) verified against assembly "
        f"{payload.instance} ({len(data)} bytes). "
        + (f"Currently high: {', '.join(high)}." if high
           else "No input is currently high."))
    return result


class EipProbeRequest(BaseModel):
    plc_ip: str
    instance: int = 0
    slot: int = 0
    timeout_ms: int = 4000
    signals: list[dict] = Field(default_factory=list)


class ModbusPreviewRequest(BaseModel):
    plc_ip: str
    port: int = 502
    unit_id: int = 1
    registers: list[dict] = Field(default_factory=list)
    timeout_ms: int = 4000


@router.get("/device-profiles")
def list_device_profiles(protocol: str = "") -> dict:
    """The device catalogue, optionally narrowed to one protocol.

    The same idea as an Ignition driver list or a Studio 5000 AOP: pick the
    device you have, get its tags, then confirm them against the hardware.
    """
    from app.services import device_profiles
    return {"ok": True, "profiles": device_profiles.list_profiles(protocol)}


class CipParamScanRequest(BaseModel):
    plc_ip: str
    slot: int = 0
    first: int = 1
    last: int = 40
    kind: str = "INT"
    timeout_ms: int = 20000


@router.post("/eip/scan-parameters")
def scan_cip_parameters(payload: CipParamScanRequest) -> dict:
    """Read a RANGE of CIP parameters from a drive and report what answers.

    Parameter numbering differs between drive families and firmware revisions,
    and a wrong number returns a plausible value rather than an error. So the
    operator reads the range, puts it beside the drive's own display, and keeps
    what matches - instead of trusting a table typed from a manual.
    """
    from app.drivers.ethernet_ip import EipDeviceClient

    host = (payload.plc_ip or "").strip()
    if not host:
        return {"ok": False, "message": "The device address is empty.", "values": []}
    first = max(1, int(payload.first or 1))
    last = max(first, min(int(payload.last or 40), first + 199))   # bounded
    try:
        client = EipDeviceClient(host=host, slot=int(payload.slot or 0), timeout_s=3.0)
        rows = client.scan_parameters(
            first=first, last=last, kind=str(payload.kind or "INT"),
            deadline_s=max(2.0, float(payload.timeout_ms or 20000) / 1000.0))
    except Exception as exc:
        return {"ok": False, "message": f"Parameter scan failed: {exc}", "values": []}
    answered = [r for r in rows if r.get("ok")]
    return {
        "ok": True,
        "message": (f"{len(answered)} of {len(rows)} parameter(s) in {first}-{last} "
                    f"answered. Compare them with the drive's own display before "
                    f"trusting any of them."),
        "values": rows,
    }


class CipParamPreviewRequest(BaseModel):
    plc_ip: str
    slot: int = 0
    parameters: list[dict] = Field(default_factory=list)
    timeout_ms: int = 8000


@router.post("/eip/preview-parameters")
def preview_cip_parameters(payload: CipParamPreviewRequest) -> dict:
    """Read the mapped parameters once, with their scaling applied."""
    from app.drivers.ethernet_ip import EipDeviceClient, parameters_from_config

    host = (payload.plc_ip or "").strip()
    if not host:
        return {"ok": False, "message": "The device address is empty.", "values": []}
    rows = [dict(r, enabled=True) for r in (payload.parameters or []) if isinstance(r, dict)]
    params = parameters_from_config(rows)
    if not params:
        return {"ok": False, "message": "No parameters are mapped yet.", "values": []}
    try:
        client = EipDeviceClient(host=host, slot=int(payload.slot or 0), timeout_s=3.0)
        values = client.read_parameters(
            params, deadline_s=max(2.0, float(payload.timeout_ms or 8000) / 1000.0))
    except Exception as exc:
        return {"ok": False, "message": f"Parameter read failed: {exc}", "values": []}
    ok_count = sum(1 for v in values if v.get("quality"))
    return {"ok": True,
            "message": f"{ok_count} of {len(values)} parameter(s) read.",
            "values": values}


@router.post("/modbus/preview")
def preview_modbus_registers(payload: ModbusPreviewRequest) -> dict:
    """Read a Modbus device once, decoded values AND raw registers.

    A wrong address, format or word order returns a plausible number rather
    than an error, so the operator must be able to compare the two before
    saving. Same reasoning as /eip/preview.
    """
    from app.drivers.modbus_tcp import points_from_config, read_once, ModbusReadError

    host = (payload.plc_ip or "").strip()
    if not host:
        return {"ok": False, "message": "The device address is empty.", "values": []}
    # Preview everything that is mapped, ticked or not - the operator is
    # deciding WHICH to tick, so showing only the ticked ones is backwards.
    rows = [dict(r, enabled=True) for r in (payload.registers or []) if isinstance(r, dict)]
    points = points_from_config(rows)
    if not points:
        return {"ok": False, "message": "No registers are mapped yet.", "values": []}
    try:
        values = read_once(host, int(payload.port or 502), int(payload.unit_id or 1),
                           points, timeout_s=max(1.0, float(payload.timeout_ms or 4000) / 1000.0))
    except ModbusReadError as exc:
        return {"ok": False, "message": str(exc), "values": []}
    except Exception as exc:
        return {"ok": False, "message": f"Modbus read failed: {exc}", "values": []}
    ok_count = sum(1 for v in values if v.get("quality"))
    return {
        "ok": True,
        "message": f"{ok_count} of {len(values)} register(s) read.",
        "values": values,
    }


@router.post("/eip/preview")
def eip_preview(payload: EipProbeRequest) -> dict:
    """Read the live assembly and decode it with a candidate signal map.

    Byte offsets are the part an operator gets wrong, and a wrong offset yields
    a plausible number rather than an error. Showing the raw bytes beside the
    decoded values is what makes a map verifiable before it is saved.
    """
    from app.drivers.ethernet_ip import EipDeviceClient, EipSignal, decode_signal
    if not (payload.plc_ip or "").strip():
        return {"ok": False, "message": "The device address is required.", "values": []}
    if int(payload.instance or 0) <= 0:
        return {"ok": False, "message": "An input assembly instance is required.", "values": []}
    client = EipDeviceClient(host=payload.plc_ip.strip(), slot=int(payload.slot or 0),
                             timeout_s=max(1.0, float(payload.timeout_ms or 4000) / 1000.0))
    try:
        data = client.read_assembly(int(payload.instance))
    except Exception as exc:
        return {"ok": False, "values": [], "raw": "",
                "message": f"Could not read assembly {payload.instance}: {exc}"}
    values = []
    for spec in payload.signals or []:
        sig = EipSignal.from_dict(spec)
        try:
            values.append({"name": sig.name, "value": decode_signal(data, sig),
                           "unit": sig.unit, "error": ""})
        except Exception as exc:
            values.append({"name": sig.name, "value": None, "unit": sig.unit,
                           "error": str(exc)})
    return {"ok": True, "raw": data.hex(), "size_bytes": len(data), "values": values,
            "message": f"Assembly {payload.instance} returned {len(data)} bytes."}


@router.post("/eip/identify")
def eip_identify(payload: EipProbeRequest) -> dict:
    """Who is actually at this address (CIP Identity object)."""
    from app.drivers.ethernet_ip import EipDeviceClient
    if not (payload.plc_ip or "").strip():
        return {"ok": False, "message": "The device address is required."}
    try:
        info = EipDeviceClient(host=payload.plc_ip.strip(),
                               slot=int(payload.slot or 0)).identify()
    except Exception as exc:
        return {"ok": False, "message": f"Could not reach the device: {exc}"}
    return {"ok": bool(info), "device": info,
            "message": (f"{info.get('product_name') or 'Device'} answered."
                        if info else "The device did not answer a ListIdentity.")}


@router.get("/eip/discover")
def eip_discover() -> dict:
    """EtherNet/IP adapters answering on this subnet."""
    from app.drivers.ethernet_ip import discover_devices
    devices = discover_devices()
    return {"ok": True, "devices": devices,
            "message": f"{len(devices)} EtherNet/IP device(s) answered."}


class IfmScanRequest(BaseModel):
    """Scan an IFM IO-Link master's ports (2026-08-24).

    The dialog calls this so the operator sees the hardware actually plugged in
    instead of typing port numbers and hoping.
    """
    plc_ip: str
    http_port: int = 80
    use_https: bool = False
    verify_tls: bool = False
    username: str = ""
    password: str = ""
    port_count: int = 8
    timeout_ms: int = 4000
    # auto | iolink_master | io_module
    variant: str = "auto"


def _ifm_client(payload: "IfmScanRequest"):
    from app.drivers.ifm_iolink import IfmMasterClient
    return IfmMasterClient(
        host=(payload.plc_ip or "").strip(),
        port=int(payload.http_port or 80),
        timeout_s=max(1.0, float(payload.timeout_ms or 4000) / 1000.0),
        use_https=bool(payload.use_https),
        username=str(payload.username or ""),
        password=str(payload.password or ""),
        verify_tls=bool(payload.verify_tls),
    )


@router.post("/ifm/scan-ports")
def ifm_scan_ports(payload: IfmScanRequest) -> dict:
    """Ask the block what it is and what it offers to collect.

    Works for every ifm device kind: an IO-Link master reports its ports and the
    sensors on them, an I/O module (AL4022) reports each digital input and
    counter. Both come back as the same `datapoints` list, so the dialog renders
    one table and the operator just ticks values.
    """
    if not (payload.plc_ip or "").strip():
        return {"ok": False, "message": "The block address is required.", "ports": [],
                "datapoints": []}
    try:
        from app.drivers.ifm_iolink import (
            BUILTIN_PROFILES, VARIANTS, IfmTransportError)
        client = _ifm_client(payload)
        # BEFORE identify(): pointed at a fieldbus port, identify's own requests
        # hang, so the preflight has to run first or it never runs at all.
        client.preflight()
        info = client.identify()
        found = client.discover_datapoints(variant=str(payload.variant or "auto"),
                                           port_count=int(payload.port_count or 8))
    except IfmTransportError as exc:
        # Already an operator-facing sentence naming the fix - do not bury it
        # under "Could not reach", which describes the wrong problem.
        return {"ok": False, "ports": [], "datapoints": [], "message": str(exc)}
    except Exception as exc:
        return {"ok": False, "ports": [], "datapoints": [],
                "message": f"Could not reach the IFM block: {type(exc).__name__}: {exc}"}
    return {
        "ok": True,
        "device": info,
        "variant": found.get("variant"),
        "variants": VARIANTS,
        "datapoints": found.get("datapoints") or [],
        "ports": found.get("ports") or [],
        "profiles": BUILTIN_PROFILES,
        "message": found.get("message") or "",
    }


class IfmReadRequest(IfmScanRequest):
    """Read a candidate datapoint list live, before it is saved."""
    datapoints: list[dict] = Field(default_factory=list)


@router.post("/ifm/read")
def ifm_read(payload: IfmReadRequest) -> dict:
    """Live values for the datapoints the operator has ticked.

    This is the proof step: see the real numbers change before saving a gateway.
    """
    from app.drivers.ifm_iolink import datapoints_from_config
    if not (payload.plc_ip or "").strip():
        return {"ok": False, "message": "The block address is required.", "values": []}
    points = datapoints_from_config(list(payload.datapoints or []))
    if not points:
        return {"ok": False, "message": "No datapoints selected.", "values": []}
    try:
        rows = _ifm_client(payload).read_datapoints(points)
    except Exception as exc:
        return {"ok": False, "values": [], "message": f"Read failed: {exc}"}
    return {"ok": True, "values": rows, "message": f"{len(rows)} value(s) read."}


class IfmPreviewRequest(IfmScanRequest):
    """Decode one port's live process data with a candidate mapping, so the
    operator can confirm a bit offset produces a sensible number BEFORE saving."""
    port: int = 1
    fields: list[dict] = Field(default_factory=list)


@router.post("/ifm/preview")
def ifm_preview(payload: IfmPreviewRequest) -> dict:
    from app.drivers.ifm_iolink import IfmField, decode_field, port_pdin_adr
    try:
        client = _ifm_client(payload)
        pdin = client.get_value(port_pdin_adr(int(payload.port or 1)))
    except Exception as exc:
        return {"ok": False, "raw": "", "values": [],
                "message": f"Could not read port {payload.port}: {exc}"}
    if pdin is None:
        return {"ok": False, "raw": "", "values": [],
                "message": f"Port {payload.port} returned no process data."}
    values = []
    for spec in payload.fields or []:
        item = dict(spec); item["port"] = int(payload.port or 1)
        fld = IfmField.from_dict(item)
        try:
            values.append({"name": fld.name, "value": decode_field(str(pdin), fld),
                           "unit": fld.unit, "error": ""})
        except Exception as exc:
            values.append({"name": fld.name, "value": None, "unit": fld.unit,
                           "error": str(exc)})
    return {"ok": True, "raw": str(pdin), "values": values, "message": ""}


def _discover_ifm_tags(payload: TagDiscoveryRequest) -> TagDiscoveryResult:
    """Suggested tag names from whatever is plugged into the block."""
    try:
        client = _ifm_client(IfmScanRequest(
            plc_ip=payload.plc_ip, timeout_ms=payload.timeout_ms,
            http_port=int(payload.ifm_http_port or 80),
            use_https=bool(payload.ifm_use_https),
            verify_tls=bool(payload.ifm_verify_tls),
            username=str(payload.ifm_username or ""),
            password=str(payload.ifm_password or ""),
            port_count=int(payload.ifm_port_count or 8)))
        found = client.discover_datapoints(port_count=int(payload.ifm_port_count or 8))
    except Exception as exc:
        # "Search Available Tags" is the other way into the same driver, and it
        # is where a wrong port showed as a "Searching..." button that never
        # came back. discover_datapoints preflights and is deadline-bounded, so
        # this now returns promptly; pass the driver's own sentence through
        # when it has one rather than prefixing it with the wrong diagnosis.
        from app.drivers.ifm_iolink import IfmTransportError
        if isinstance(exc, IfmTransportError):
            return TagDiscoveryResult(ok=False, tags=[], message=str(exc))
        return TagDiscoveryResult(ok=False, tags=[],
                                  message=f"Could not reach the IFM block: {exc}")
    tags = [str(p.get("name") or "") for p in (found.get("datapoints") or []) if p.get("name")]
    return TagDiscoveryResult(
        ok=bool(tags), tags=tags,
        message=(f"Found {len(tags)} value(s) on this block. Open the gateway to "
                 f"choose which to collect."
                 if tags else "The block reported no collectable values."))


class NetworkScanRequest(BaseModel):
    # Empty scan_range → broadcast EtherNet/IP discovery (pylogix
    # PLC.Discover) and/or a fast TCP sweep of the host's local /24.
    # When `scan_range` is a CIDR ("192.168.10.0/24") or comma-list of
    # IPs ("192.168.1.10,192.168.1.50") we restrict the probe to those
    # hosts only.
    scan_range: str = ""
    gateway_type: GatewayType = "allen_bradley"
    timeout_ms: int = 4000
    include_tcp_probe: bool = True
    # Operator 2026-06-12: "we should scan for any TCP/IP devices,
    # any computer, PLC, Siemens or Allen-Bradley, anything at all
    # in the network". When True, the TCP probe widens beyond the
    # protocol-specific port and tries a broad set of well-known
    # TCP ports (44818, 102, 4840, 502, 80, 443, 22, 8080, 5900,
    # 7000). First responding port wins and is reported on the
    # device row so the operator can tell what KIND of host
    # answered.
    scan_any_tcp: bool = False


class NetworkScanDevice(BaseModel):
    ip: str
    product_name: str = ""
    vendor: str = ""
    vendor_id: int | None = None
    device_type: str = ""
    revision: str = ""
    serial: str = ""
    source: str = ""  # "pylogix_discover" | "tcp_probe" | "identified"
    # 2026-08-27: a port number is not an identity. An ifm IO-Link master on
    # 44818 was reported as "EtherNet/IP (Allen-Bradley)" purely because that
    # is Rockwell's port, which is how a block ends up configured against the
    # wrong driver. These carry what the DEVICE says about itself.
    transports: list[str] = Field(default_factory=list)   # e.g. ["ethernet_ip", "ifm_iot"]
    suggested_protocol: str = ""                          # gateway_type to preselect
    identity_note: str = ""                               # one line for the operator


class NetworkScanResult(BaseModel):
    ok: bool
    devices: list[NetworkScanDevice] = Field(default_factory=list)
    message: str = ""


# ifm electronic's CIP vendor id. A block that answers ListIdentity with this
# is an ifm device whatever port it was found on.
IFM_CIP_VENDOR_ID = 310


def _probe_ifm_iot(host: str, timeout: float = 2.0) -> dict:
    """Does an ifm IoT Core live here, and what does it call itself?

    One GET. A device that answers with a productcode is an ifm block serving
    JSON, which is the `ifm_iolink` driver's transport.
    """
    import json as _json
    import urllib.request as _rq
    out: dict = {}
    try:
        req = _rq.Request("http://%s/deviceinfo/productcode/getdata" % host, method="GET")
        with _rq.urlopen(req, timeout=timeout) as resp:
            payload = _json.loads(resp.read().decode("utf-8", "replace") or "{}")
        value = ((payload.get("data") or {}) or {}).get("value")
        if value:
            out["product_code"] = str(value)
    except Exception:
        return {}
    try:
        req = _rq.Request("http://%s/deviceinfo/serialnumber/getdata" % host, method="GET")
        with _rq.urlopen(req, timeout=timeout) as resp:
            payload = _json.loads(resp.read().decode("utf-8", "replace") or "{}")
        serial = ((payload.get("data") or {}) or {}).get("value")
        if serial:
            out["serial"] = str(serial)
    except Exception:
        pass
    return out


def _probe_cip_identity(host: str, timeout: float = 2.5) -> dict:
    """CIP Identity object — vendor, product name, revision, serial.

    Every EtherNet/IP device answers this, and it is the difference between
    "something is listening on 44818" and "ifm electronic gmbh, IO-Link
    Master DL EIP 8P IP67".
    """
    try:
        from app.drivers.ethernet_ip import EipDeviceClient
        info = EipDeviceClient(host=host, slot=0, timeout_s=timeout).identify()
        return info if isinstance(info, dict) else {}
    except Exception:
        return {}


def _identify_scanned_device(host: str, port: int) -> dict:
    """Ask the device what it is, instead of guessing from its port number.

    Returns the fields to put on a NetworkScanDevice. Both probes are cheap and
    read-only; whichever answer arrives is used, and a device that answers BOTH
    (ifm blocks commonly do) reports both transports so the operator can see
    the choice rather than discover it by trial.
    """
    result: dict = {"transports": [], "suggested_protocol": "", "identity_note": ""}

    cip = _probe_cip_identity(host) if int(port) == 44818 else {}
    if not cip and int(port) in (80, 443, 8080):
        # An ifm block found on its web port may still speak EtherNet/IP.
        cip = _probe_cip_identity(host, timeout=1.5)

    iot = {}
    if int(port) in (80, 8080) or cip.get("vendor_id") == IFM_CIP_VENDOR_ID \
            or "ifm" in str(cip.get("vendor") or "").lower():
        iot = _probe_ifm_iot(host)

    if cip:
        result["vendor"] = str(cip.get("vendor") or "")
        result["product_name"] = str(cip.get("product_name") or "")
        rev = cip.get("revision") or {}
        if isinstance(rev, dict) and rev:
            result["revision"] = "%s.%s" % (rev.get("major"), rev.get("minor"))
        result["serial"] = str(cip.get("serial") or "")
        result["device_type"] = str(cip.get("product_type") or "")
        result["transports"].append("ethernet_ip")

    if iot:
        if not result.get("product_name"):
            result["product_name"] = str(iot.get("product_code") or "")
        if not result.get("vendor"):
            result["vendor"] = "ifm electronic"
        if not result.get("serial"):
            result["serial"] = str(iot.get("serial") or "")
        result["transports"].append("ifm_iot")

    vendor_l = str(result.get("vendor") or "").lower()
    is_ifm = "ifm" in vendor_l or bool(iot)

    if is_ifm:
        # An ifm block that speaks EtherNet/IP should be collected over it.
        # Measured on an "IO-Link Master DL EIP 8P IP67" (2026-08-27): the
        # fieldbus read is one 3 ms request for every pin at 100% success,
        # while its IoT Core refused ~75% of a 1 Hz poll with HTTP 503. The
        # IoT path stays available and is the right choice on a block with no
        # fieldbus, but it must not be the silent default on one that has it.
        if "ethernet_ip" in result["transports"]:
            result["suggested_protocol"] = "ethernet_ip"
            if "ifm_iot" in result["transports"]:
                result["identity_note"] = (
                    "ifm block — speaks EtherNet/IP and IoT Core. EtherNet/IP "
                    "reads every pin in one request; prefer it unless you need "
                    "IO-Link process data only the IoT Core exposes.")
            else:
                result["identity_note"] = (
                    "ifm block on EtherNet/IP. No IoT Core answered on port 80.")
        else:
            result["suggested_protocol"] = "ifm_iolink"
            result["identity_note"] = "ifm block serving IoT Core over HTTP."
    elif "allen" in vendor_l or "rockwell" in vendor_l:
        result["suggested_protocol"] = "allen_bradley"
    return result


def _pylogix_discover() -> list[NetworkScanDevice]:
    """Broadcast EtherNet/IP discovery via pylogix. Inspired by
    pylogix/examples/20_discover_devices.py and 81_simple_gui.py —
    we don't need every field, just enough for the operator to pick
    the right PLC from a list."""
    try:
        from pylogix import PLC  # type: ignore
    except Exception:
        return []
    out: list[NetworkScanDevice] = []
    try:
        with PLC() as comm:
            try:
                devices = comm.Discover()
            except Exception:
                devices = None
            if devices is None or not getattr(devices, "Value", None):
                return []
            for d in devices.Value:
                ip = str(getattr(d, "IPAddress", "") or "").strip()
                if not ip:
                    continue
                out.append(
                    NetworkScanDevice(
                        ip=ip,
                        product_name=str(getattr(d, "ProductName", "") or ""),
                        vendor=str(getattr(d, "Vendor", "") or ""),
                        vendor_id=int(getattr(d, "VendorID", 0) or 0) or None,
                        device_type=str(getattr(d, "DeviceType", "") or ""),
                        revision=str(getattr(d, "Revision", "") or ""),
                        serial=str(getattr(d, "SerialNumber", "") or ""),
                        source="pylogix_discover",
                    )
                )
    except Exception:
        return out
    return out


def _expand_scan_range(rng: str) -> list[str]:
    """Expand a comma-list / CIDR to a flat list of IPv4 strings.
    Returns an empty list when rng is empty / invalid. Capped at
    1024 hosts so a /20 typo doesn't pin the worker."""
    import ipaddress
    txt = (rng or "").strip()
    if not txt:
        return []
    hosts: list[str] = []
    for chunk in [c.strip() for c in txt.split(",") if c.strip()]:
        try:
            if "/" in chunk:
                net = ipaddress.ip_network(chunk, strict=False)
                for h in net.hosts():
                    hosts.append(str(h))
            else:
                addr = ipaddress.ip_address(chunk)
                hosts.append(str(addr))
        except Exception:
            continue
        if len(hosts) >= 1024:
            break
    return hosts[:1024]


def _tcp_probe_ports(host: str, ports: list[int], timeout_s: float) -> int | None:
    """Return the first port that responded with a SYN-ACK, or None.

    Note: kept for the narrow per-type probe. The broader ANY-TCP
    sweep submits (host, port) pairs to a parallel pool via
    _tcp_probe_single below so probe latency is per-port not per-
    host."""
    import socket
    for p in ports:
        try:
            with socket.create_connection((host, p), timeout=timeout_s):
                return p
        except Exception:
            continue
    return None


def _tcp_probe_single(host: str, port: int, timeout_s: float) -> int | None:
    """Probe ONE (host, port). Returns the port on success, None
    otherwise. Used by the ANY-TCP fan-out so 18 ports × 254 hosts
    finishes quickly even when many hosts are dead."""
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return port
    except Exception:
        return None


def _list_local_ipv4_subnets() -> list[str]:
    """Enumerate every IPv4 /24 attached to the host. Operator
    2026-06-12: the auto-derived range used the default-route trick
    which on multi-NIC edge boxes picked the WAN interface and
    missed the industrial VLAN where the PLCs live. Now we union
    every active IPv4 address that's RFC1918 or otherwise private,
    expand each to its /24, and probe them all. Returns a
    deduplicated, capped list of CIDR strings."""
    import socket as _socket
    subnets: list[str] = []
    seen: set[str] = set()

    def _add_subnet(addr: str) -> None:
        try:
            parts = str(addr).split(".")
            if len(parts) != 4:
                return
            o1, o2, o3 = int(parts[0]), int(parts[1]), int(parts[2])
            # Skip loopback / link-local / multicast / non-private
            # by default — we don't want to scan public WAN.
            if o1 == 127 or o1 >= 224:
                return
            if o1 == 169 and o2 == 254:
                return
            # Accept anything RFC1918 OR plant-floor 192.168.* /
                # 10.* etc. — basically all private space.
            cidr = f"{o1}.{o2}.{o3}.0/24"
            if cidr in seen:
                return
            seen.add(cidr)
            subnets.append(cidr)
        except Exception:
            return

    # Strategy A — Windows / Linux uses getaddrinfo on hostname.
    try:
        for info in _socket.getaddrinfo(_socket.gethostname(), None):
            if info[0] == _socket.AF_INET:
                ip = info[4][0]
                _add_subnet(ip)
    except Exception:
        pass
    # Strategy B — psutil if available (richer interface list).
    try:
        import psutil  # type: ignore
        for nic, addrs in psutil.net_if_addrs().items():
            for a in addrs:
                fam = getattr(a, "family", None)
                if fam is not None and int(fam) == int(_socket.AF_INET):
                    _add_subnet(a.address)
    except Exception:
        pass
    # Strategy C — default-route trick as a last resort.
    if not subnets:
        try:
            s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
            s.settimeout(0.5)
            try:
                s.connect(("8.8.8.8", 80))
                _add_subnet(s.getsockname()[0])
            finally:
                s.close()
        except Exception:
            pass
    return subnets[:8]  # cap so a host with 50 interfaces doesn't DoS itself


@router.post("/discover-network", response_model=NetworkScanResult)
def discover_network(payload: NetworkScanRequest) -> NetworkScanResult:
    """Discover PLCs on the local network.

    Strategy borrowed from pylogix/examples/81_simple_gui.py:
      1. Broadcast EtherNet/IP discovery via pylogix.PLC().Discover()
         when no explicit scan_range is set. This catches every
         AB/Logix on the local L2 segment without needing the
         operator to type any IPs.
      2. When scan_range is supplied (CIDR or comma-list), or as a
         fallback when discover() returned nothing, TCP-probe each
         host on the protocol's well-known port:
            allen_bradley → 44818
            siemens_snap7 → 102
            siemens_opcua → 4840
            boston         → 502
         A 1 s probe timeout keeps the scan responsive even on a
         dense /24.

    The merged result is de-duplicated by IP, broadcast wins on
    metadata when both sources find the same host."""
    timeout_s = max(0.2, min(5.0, (payload.timeout_ms or 4000) / 1000.0))
    found: dict[str, NetworkScanDevice] = {}

    if not payload.scan_range:
        for d in _pylogix_discover():
            found[d.ip] = d

    # TCP probe rules — operator request 2026-06-12: "still not showing
    # all devices connected that can be seen in network, I have at
    # least other computer, one siemens plc and one energy meter".
    # Previously the probe was skipped whenever pylogix broadcast
    # already found something, so non-AB hosts (Siemens, PCs, meters)
    # were ignored. Now the probe runs whenever the operator asked
    # for it (include_tcp_probe) AND either scan_any_tcp is on, a
    # scan_range was supplied, OR pylogix turned up nothing.
    should_probe = (
        payload.include_tcp_probe and
        (payload.scan_any_tcp or payload.scan_range or not found)
    )
    if should_probe:
        port_by_type = {
            "allen_bradley": [44818],
            "siemens_snap7": [102],
            "siemens_opcua": [4840],
            "boston": [502],
        }
        if payload.scan_any_tcp:
            # Broad TCP sweep — first responding port wins. The order
            # is roughly: industrial protocol ports first (so a real
            # PLC gets labelled correctly when both 44818 AND 80 are
            # open), then general-purpose ports for printers / PCs /
            # cameras / SSH boxes / VNC.
            # 18 ports cover:
            #  - PLC protocols (44818, 102, 4840, 502, 47808 BACnet, 9600 Omron)
            #  - Web admin UIs typical on energy meters / drives (80, 443, 8080, 8443)
            #  - PC / server boxes (22 SSH, 3389 RDP, 445 SMB, 135 RPC)
            #  - Discovery extras (5900 VNC, 161 SNMP, 23 Telnet)
            ports = [
                44818, 102, 4840, 502, 47808, 9600,
                80, 443, 8080, 8443,
                22, 3389, 445, 135,
                5900, 161, 23, 21,
            ]
        else:
            ports = port_by_type.get(payload.gateway_type or "allen_bradley", [44818])
        hosts = _expand_scan_range(payload.scan_range)
        if not hosts:
            # Auto-scan every IPv4 /24 attached to this host instead
            # of only the default-route NIC. Operator 2026-06-12:
            # "still not showing all devices connected that can be
            # seen in network". On multi-NIC edge boxes (one VLAN
            # for WAN, another for plant floor) the old default-
            # route trick picked the wrong interface and the PLCs
            # went undiscovered.
            for cidr in _list_local_ipv4_subnets():
                hosts.extend(_expand_scan_range(cidr))
            # Dedup preserving order (so we don't probe the same /24 twice).
            seen_hosts: set[str] = set()
            uniq: list[str] = []
            for h in hosts:
                if h in seen_hosts:
                    continue
                seen_hosts.add(h)
                uniq.append(h)
            hosts = uniq[:1024]
        # Probe in parallel. Fan out (host, port) pairs to the pool
        # so the worst-case wall-clock is per-port not per-host.
        # Operator 2026-06-12: per-host serial probe meant 1 dead
        # host = 9 s of serial timeouts × 18 ports, choking the
        # /24 sweep.
        from concurrent.futures import ThreadPoolExecutor, as_completed
        worker_count = 256 if payload.scan_any_tcp else 32
        probe_timeout_s = 0.4 if payload.scan_any_tcp else timeout_s
        host_best_port: dict[str, int] = {}
        if hosts:
            with ThreadPoolExecutor(max_workers=worker_count) as pool:
                if payload.scan_any_tcp:
                    # True (host, port) fan-out. Walking each (host, port)
                    # as its own task means the worst-case wall-clock is
                    # per-port not per-host — a /24 of dead addresses
                    # finishes in ~probe_timeout × ceil(hosts*ports/workers)
                    # ≈ 0.4 × 18 ≈ 7 s instead of 30+ s.
                    pair_futures = {
                        pool.submit(_tcp_probe_single, h, p, probe_timeout_s): (h, p)
                        for h in hosts for p in ports
                    }
                    for fut in as_completed(pair_futures):
                        h, p = pair_futures[fut]
                        try:
                            port = fut.result()
                        except Exception:
                            port = None
                        if not port:
                            continue
                        # Keep the PRIORITY (lowest index in `ports`)
                        # responder so industrial labels win over web
                        # admin UIs on hosts that expose both.
                        if h not in host_best_port:
                            host_best_port[h] = port
                        else:
                            if ports.index(port) < ports.index(host_best_port[h]):
                                host_best_port[h] = port
                else:
                    futures = {pool.submit(_tcp_probe_ports, h, ports, probe_timeout_s): h for h in hosts}
                    for fut in as_completed(futures):
                        h = futures[fut]
                        try:
                            port = fut.result()
                        except Exception:
                            port = None
                        if not port:
                            continue
                        host_best_port[h] = port
            # Promote into the `found` map; pylogix-discovered hosts
            # already have richer metadata so we don't overwrite.
            port_hints = {
                44818: "EtherNet/IP (Allen-Bradley)",
                102: "S7 (Siemens)",
                4840: "OPC-UA",
                502: "Modbus TCP",
                47808: "BACnet/IP",
                9600: "Omron FINS",
                80: "HTTP",
                443: "HTTPS",
                8080: "HTTP-alt",
                8443: "HTTPS-alt",
                22: "SSH",
                3389: "RDP (Windows)",
                445: "SMB (Windows)",
                135: "RPC (Windows)",
                5900: "VNC",
                161: "SNMP",
                23: "Telnet",
                21: "FTP",
            }
            # Ask each responder what it IS before falling back to the port
            # name. Concurrent and bounded: identification must not turn a
            # 30-host scan into a minute of waiting.
            identities: dict[str, dict] = {}
            to_identify = [(h, p) for h, p in host_best_port.items() if h not in found]
            if to_identify:
                with ThreadPoolExecutor(max_workers=min(12, len(to_identify))) as pool:
                    jobs = {pool.submit(_identify_scanned_device, h, p): h
                            for h, p in to_identify}
                    for fut in as_completed(jobs):
                        h = jobs[fut]
                        try:
                            identities[h] = fut.result() or {}
                        except Exception:
                            identities[h] = {}

            for h, port in host_best_port.items():
                if h in found:
                    continue
                hint = port_hints.get(int(port), f"port {port}")
                ident = identities.get(h) or {}
                # A real product name always beats a port label.
                label = str(ident.get("product_name") or "").strip() or hint
                found[h] = NetworkScanDevice(
                    ip=h,
                    product_name=label,
                    vendor=str(ident.get("vendor") or ""),
                    revision=str(ident.get("revision") or ""),
                    serial=str(ident.get("serial") or ""),
                    device_type=str(ident.get("device_type") or "") or f"tcp/{port}",
                    transports=list(ident.get("transports") or []),
                    suggested_protocol=str(ident.get("suggested_protocol") or ""),
                    identity_note=str(ident.get("identity_note") or ""),
                    source="identified" if ident.get("product_name") else "tcp_probe",
                )

    devices = sorted(found.values(), key=lambda d: tuple(int(p) for p in d.ip.split(".") if p.isdigit()))
    if not devices:
        return NetworkScanResult(ok=False, devices=[], message="No PLCs discovered. Check that the edge host is on the same VLAN as the PLC.")
    return NetworkScanResult(ok=True, devices=devices, message=f"Discovered {len(devices)} device(s)")


@router.post("/opcua/browse", response_model=OpcUaBrowseResult)
def browse_opcua_nodes(payload: OpcUaBrowseRequest) -> OpcUaBrowseResult:
    ip = (payload.plc_ip or "").strip()
    if not ip:
        return OpcUaBrowseResult(ok=False, message="PLC IP is required", nodes=[])
    return _browse_opcua_nodes(payload)


@router.post("/pointio/scan")
def scan_point_io(payload: dict) -> dict:
    """Ask a 1734-AENTR what is on its backplane, and what we can read.

    Returns the modules AND the datapoints in the shape the gateway config
    stores, so the dialog can offer tags without a second round trip.
    """
    from app.drivers.point_io import PointIoClient, PointIoError, datapoints_from_scan

    host = str((payload or {}).get("ip") or (payload or {}).get("host") or "").strip()
    if not host:
        return {"ok": False, "message": "adapter IP is required", "modules": [], "datapoints": []}
    slots = int((payload or {}).get("max_slots") or 8)
    cli = PointIoClient(host, timeout_s=float((payload or {}).get("timeout_s") or 3.0))
    try:
        modules = cli.scan(max_slots=slots)
    except PointIoError as exc:
        return {"ok": False, "message": str(exc), "modules": [], "datapoints": []}
    except Exception as exc:  # noqa: BLE001 - the dialog shows this verbatim
        return {"ok": False, "message": "scan failed: %s" % exc, "modules": [], "datapoints": []}
    finally:
        cli.close()                          # scan_error survives close()
    if not modules:
        return {"ok": False, "modules": [], "datapoints": [],
                "message": ("The adapter answered but reported no modules. Check the "
                            "rack is powered and, after changing modules, power-cycle "
                            "the adapter - it only rescans its backplane at boot.")}
    points = datapoints_from_scan(modules)
    msg = "%d module(s), %d point(s)" % (len(modules), len(points))
    # A walk that stopped early lists SOME of the rack. Presenting that as the
    # whole rack invites an operator to tick a partial configuration.
    if getattr(cli, "scan_error", ""):
        msg += (" - the walk stopped early (%s), so this list may be "
                "incomplete. Re-scan before saving." % cli.scan_error)
    return {"ok": True, "modules": modules, "datapoints": points, "message": msg,
            "partial": bool(getattr(cli, "scan_error", ""))}


@router.post("/gateways/start")
async def start_gateway_runtime(payload: GatewayRuntimeStartRequest) -> dict[str, str | bool]:
    gateway_id = payload.gateway_id.strip()
    if not gateway_id:
        return {"started": False, "message": "gateway_id is required"}

    # A gateway must be able to ADDRESS its values, not just name them.
    _gt = str(getattr(payload.config, "gateway_type", "") or "").strip().lower()
    if _gt == "ifm_iolink" and not list(getattr(payload.config, "ifm_datapoints", None) or []):
        return {
            "started": False,
            "message": (
                "This ifm gateway has no datapoints. Tag names alone do not tell "
                "the driver where to read them - open the gateway and use "
                "\"Search Available Tags\" so the block can list what it offers, "
                "then save and start. (Nothing was started; a gateway that cannot "
                "read must not report RUNNING.)"
            ),
        }
    if _gt == "point_io" and not list(getattr(payload.config, "point_io_modules", None) or []):
        return {
            "started": False,
            "message": (
                "This POINT I/O gateway has no modules. Open the gateway and use "
                "\"Scan rack\" so the adapter can report which slots are populated, "
                "then save and start."
            ),
        }
    if _gt == "ethernet_ip" and not list(getattr(payload.config, "eip_signals", None) or [])             and not int(getattr(payload.config, "eip_input_assembly", 0) or 0):
        return {
            "started": False,
            "message": (
                "This EtherNet/IP gateway has no input assembly and no signals, so "
                "there is nothing for it to read. Open the gateway and map the "
                "assembly (the block's pins are discovered for you), then save "
                "and start."
            ),
        }
    # Operator 2026-06-23: enforce license limits on the canonical
    # operation that activates collection. Failing OPEN: if the
    # license helper can't be reached, we let the start through.
    # Operator 2026-06-24: short-circuit — only call the heavy
    # count_configured_* helpers when a limit is ACTUALLY set. On
    # legacy/unlicensed installs both limits are 0, so we skip the
    # bootstrap walk entirely and the start endpoint stays instant.
    try:
        from app.services import license_inspect
        max_tags = license_inspect.get_limit("max_tags")
        max_gw = license_inspect.get_limit("max_gateways_per_edge")
        if max_tags or max_gw:
            if max_tags:
                other_tags = max(0, license_inspect.count_configured_tags() - len(list(payload.config.tags or [])))
                new_total = other_tags + len(list(payload.config.tags or []))
                if new_total > max_tags:
                    return {
                        "started": False,
                        "message": (
                            f"License limit reached: {new_total} tags requested, "
                            f"max is {max_tags}. Upgrade the license or reduce tags."
                        ),
                    }
            if max_gw:
                existing = license_inspect.count_configured_gateways()
                if existing > max_gw:
                    return {
                        "started": False,
                        "message": (
                            f"License limit reached: {existing} gateways configured, "
                            f"max is {max_gw}."
                        ),
                    }
    except Exception:
        pass
    try:
        await plc_manager.start_gateway(
            gateway_id=gateway_id,
            config=payload.config,
            db_sink=payload.db_sink,
            db_sinks=payload.db_sinks,
        )
        # Operator 2026-06-25: clear any prior user-stopped flag so
        # auto-recover can supervise this gateway again.
        try:
            plc_manager._user_stopped.discard(gateway_id)
        except Exception:
            pass
        # Operator 2026-06-19: persist running state so the gateway
        # auto-resumes after a backend restart (auto-update, crash,
        # Windows reboot). The customer reported only 1 historian
        # cycle after re-activation — the backend silently restarted
        # and the in-memory gateway runtime was lost. Now the worker
        # comes back on its own.
        try:
            from app.state import telemetry_service
            telemetry_service.mark_gateway_running(gateway_id, True)
        except Exception:
            pass
        return {"started": True, "message": f"Gateway '{gateway_id}' started"}
    except ValueError as err:
        return {"started": False, "message": str(err)}


@router.post("/gateways/stop")
async def stop_gateway_runtime(payload: GatewayRuntimeStopRequest) -> dict[str, str | bool]:
    gateway_id = payload.gateway_id.strip()
    if not gateway_id:
        return {"stopped": False, "message": "gateway_id is required"}
    # Operator 2026-06-25: set the user-stopped flag BEFORE the actual
    # stop so the supervisor scan can NEVER observe a window where the
    # gateway is stopped but the flag isn't set yet — which would let
    # auto-recover resurrect it ~10s after the operator's click.
    try:
        plc_manager._user_stopped.add(gateway_id)
    except Exception:
        pass
    try:
        from app.state import telemetry_service
        telemetry_service.mark_gateway_running(gateway_id, False)
    except Exception:
        pass
    await plc_manager.stop_gateway(gateway_id)
    return {"stopped": True, "message": f"Gateway '{gateway_id}' stopped"}


@router.post("/gateways/stop-all")
async def stop_all_gateway_runtime() -> dict[str, str | bool]:
    # Capture which gateways were running before we clear, so we can
    # clear last_running for all of them (not just the ones we know
    # were active in-memory — the DB record may reference stale ones).
    try:
        from app.state import telemetry_service
        running_ids = telemetry_service.list_running_gateways()
    except Exception:
        running_ids = []
    # Operator 2026-06-25: mark every targeted gateway as user-stopped
    # BEFORE the actual stop call, so the supervisor scan can never
    # observe a stopped-but-not-flagged window and resurrect them.
    for gid in running_ids:
        try:
            plc_manager._user_stopped.add(gid)
        except Exception:
            pass
        try:
            telemetry_service.mark_gateway_running(gid, False)
        except Exception:
            pass
    await plc_manager.stop_all_gateways()
    return {"stopped": True, "message": "All gateways stopped"}


@router.get("/gateways/status")
def list_gateway_runtime_status(request: Request) -> list[dict]:
    statuses = plc_manager.list_gateway_statuses()
    try:
        # Operator 2026-06-18: the filter list of "allowed" gateway ids
        # must read from the SAME scope the UI sees. The customer's real
        # gateways are saved under the per-edge scoped doc (because
        # gateway_configurations is in _SHARED_EDGE_DOMAINS); the
        # unscoped get_bootstrap only contains the legacy "gw-primary"
        # seed. That caused the running worker for gw-1779098315351 to
        # be filtered OUT of /api/plc/gateways/status — the UI then
        # painted "Stopped" even though the backend was actively
        # collecting and writing to the historian.
        #
        # Try the user's resolved scope first; fall back to unscoped if
        # scope resolution yields nothing.
        cfg_rows: list = []
        try:
            from app.routers.app_store import _build_scope_key, _SHARED_EDGE_DOMAINS  # type: ignore
            scope_key = _build_scope_key(request, domain="gateway_configurations")
            if scope_key:
                scoped = app_store.get_bootstrap_scoped_or_shout(scope_key, prefer_cloud_reads=False) or {}
                cand = scoped.get("gateway_configurations") if isinstance(scoped, dict) else None
                if isinstance(cand, list):
                    cfg_rows = cand
        except Exception:
            cfg_rows = []
        if not cfg_rows:
            bootstrap = app_store.get_bootstrap(prefer_cloud_reads=False) or {}
            cand = bootstrap.get("gateway_configurations") if isinstance(bootstrap, dict) else []
            cfg_rows = cand if isinstance(cand, list) else []
        allowed_ids = {
            str(row.get("id") or "").strip()
            for row in (cfg_rows or [])
            if isinstance(row, dict) and str(row.get("id") or "").strip()
        }
        if allowed_ids:
            raw_statuses = list(statuses or [])
            filtered = [
                row
                for row in raw_statuses
                if str((row or {}).get("gateway_id") or "").strip() in allowed_ids
            ]
            # If the filter dropped a RUNNING worker (legacy/active path whose
            # synthetic id doesn't match the scoped config id) and there's exactly
            # ONE configured gateway, re-attach that running status under the
            # configured id so the multi-gateway view agrees with /api/plc/status
            # (which already shows running:true). Prevents the "Running but shows
            # empty/stopped in the gateways table" desync. Only for the
            # single-gateway case — real multi-gateway setups keep strict scoping.
            if not filtered and len(allowed_ids) == 1:
                running = next((r for r in raw_statuses if (r or {}).get("running")), None)
                if running:
                    running = dict(running)
                    running["gateway_id"] = next(iter(allowed_ids))
                    filtered = [running]
            statuses = filtered
        # If local bootstrap rows are temporarily empty/unavailable, keep raw runtime
        # statuses to avoid false STOPPED flicker in the local footer.
    except Exception:
        # Never fail status endpoint because of bootstrap filtering.
        pass
    if statuses:
        return statuses

    host = str(request.headers.get("host") or "").strip().lower().split(":")[0]
    prefer_cloud = bool(host and host not in {"localhost", "127.0.0.1"})
    if not prefer_cloud:
        return statuses

    return _synthesize_gateway_status_from_cloud()


def _parse_utc(value: str) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        text = raw.replace("Z", "+00:00")
        if " " in text and "T" not in text:
            text = text.replace(" ", "T")
        return datetime.fromisoformat(text)
    except Exception:
        return None


def _synthesize_gateway_status_from_cloud() -> list[dict]:
    rows = app_store.get_live_rows(limit=5000, prefer_cloud_reads=True)
    if not rows:
        return []

    bootstrap = app_store.get_bootstrap(prefer_cloud_reads=True) or {}
    gateway_cfgs = bootstrap.get("gateway_configurations")
    interval_by_gateway: dict[str, int] = {}
    if isinstance(gateway_cfgs, list):
        for item in gateway_cfgs:
            if not isinstance(item, dict):
                continue
            gid = str(item.get("id") or "").strip()
            if not gid:
                continue
            try:
                interval_by_gateway[gid] = max(100, int(item.get("interval_ms") or 1000))
            except Exception:
                interval_by_gateway[gid] = 1000

    now_utc = datetime.now(timezone.utc)
    grouped: dict[str, dict] = {}
    for row in rows:
        gid = str(row.get("gateway_id") or "").strip()
        if not gid:
            continue
        group = grouped.get(gid)
        if not group:
            source = str(row.get("source") or "").strip().lower()
            group = {
                "running": False,
                "gateway_type": "siemens_opcua" if "siemens" in source else "allen_bradley",
                "plc_ip": str(row.get("plc_ip") or ""),
                "interval_ms": int(interval_by_gateway.get(gid, 1000)),
                "tags": [],
                "last_error": None,
                "db_sink_engine": "cloud_mirror",
                "db_write_count": 0,
                "db_last_write_utc": "",
                "db_last_error": None,
                "db_pending_count": 0,
                "collection_blocked": True,
                "collection_block_reason": "No fresh cloud rows",
                "gateway_id": gid,
                "_latest_dt": None,
            }
            grouped[gid] = group

        tag = str(row.get("tag") or row.get("tag_name") or "").strip()
        if tag and tag not in group["tags"]:
            group["tags"].append(tag)
        group["db_write_count"] = int(group["db_write_count"]) + 1

        ts_text = str(row.get("ts") or row.get("ts_utc") or "")
        ts_dt = _parse_utc(ts_text)
        latest = group.get("_latest_dt")
        if ts_dt and (latest is None or ts_dt > latest):
            group["_latest_dt"] = ts_dt
            group["db_last_write_utc"] = ts_text

    out: list[dict] = []
    for _, group in grouped.items():
        latest = group.pop("_latest_dt", None)
        interval_ms = int(group.get("interval_ms") or 1000)
        # Cloud mirrors can have short transport jitter; use a wider window to
        # avoid false OFFLINE flips in web view.
        freshness_window_s = max(15.0, (interval_ms / 1000.0) * 10.0)
        if latest is not None:
            age_s = (now_utc - latest.astimezone(timezone.utc)).total_seconds()
            is_running = age_s <= freshness_window_s
            group["running"] = bool(is_running)
            group["collection_blocked"] = not bool(is_running)
            group["collection_block_reason"] = None if is_running else f"Stale cloud feed ({int(age_s)}s old)"
        out.append(group)

    out.sort(
        key=lambda s: (
            0 if s.get("running") else 1,
            str(s.get("db_last_write_utc") or ""),
        ),
        reverse=False,
    )
    return out
