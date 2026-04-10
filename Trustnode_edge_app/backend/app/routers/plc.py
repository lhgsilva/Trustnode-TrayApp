import platform
import re
import socket
import subprocess
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import parse_qs, urlparse

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.models import GatewayConfig, GatewayReading, GatewayStatus
from app.state import app_store, plc_manager

router = APIRouter(prefix="/api/plc", tags=["plc"])


class DeviceConnectionTestRequest(BaseModel):
    gateway_type: Literal["allen_bradley", "siemens_snap7", "siemens_opcua", "boston"]
    plc_ip: str
    opc_url: str = ""
    opc_node_id: str = ""
    opc_node_ids: list[str] = Field(default_factory=list)
    timeout_ms: int = 2000


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


class GatewayRuntimeStopRequest(BaseModel):
    gateway_id: str


class TagDiscoveryRequest(BaseModel):
    gateway_type: Literal["allen_bradley", "siemens_snap7", "siemens_opcua", "boston"]
    plc_ip: str
    opc_url: str = ""
    timeout_ms: int = 4000
    max_tags: int = 500


class TagDiscoveryResult(BaseModel):
    ok: bool
    tags: list[str] = Field(default_factory=list)
    message: str


class OpcUaBrowseRequest(BaseModel):
    plc_ip: str
    opc_url: str = ""
    timeout_ms: int = 7000
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


def _gateway_port(gateway_type: str) -> int:
    # Common default PLC/protocol ports used for quick connectivity verification.
    ports = {
        "allen_bradley": 44818,  # EtherNet/IP
        "siemens_snap7": 102,    # ISO-on-TCP (S7Comm)
        "siemens_opcua": 4840,   # OPC-UA
        "boston": 502            # Modbus TCP (fallback for custom connector)
    }
    return ports.get(gateway_type, 0)


def _ping_host(host: str, timeout_ms: int) -> tuple[bool, str]:
    timeout_ms = max(500, min(timeout_ms, 10_000))
    is_windows = platform.system().lower().startswith("win")
    if is_windows:
        cmd = ["ping", "-n", "2", "-w", str(timeout_ms), host]
    else:
        timeout_s = max(1, int(timeout_ms / 1000))
        cmd = ["ping", "-c", "2", "-W", str(timeout_s), host]

    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=max(2.0, timeout_ms / 1000.0 + 1.0),
            check=False
        )
        if proc.returncode == 0:
            return True, "IP reachable by ping"
        # Some environments have flaky first ICMP reply; retry once with a higher timeout.
        retry_timeout = str(min(timeout_ms + 1500, 10_000))
        retry_cmd = cmd.copy()
        if is_windows:
            retry_cmd[4] = retry_timeout
        else:
            retry_cmd[4] = str(max(1, int(int(retry_timeout) / 1000)))
        retry = subprocess.run(
            retry_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=max(3.0, int(retry_timeout) / 1000.0 + 1.0),
            check=False
        )
        if retry.returncode == 0:
            return True, "IP reachable by ping (retry)"
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
    def _extract_node_ids(text: str) -> list[str]:
        raw = str(text or "").strip()
        if not raw:
            return []
        matches = [
            m.group(0).strip()
            for m in re.finditer(
                r'ns=\d+;(?:s="[^"]+"|s=[^,\n;|]+|i=\d+|g=[0-9a-fA-F-]+|b=[^,\n;|]+)',
                raw,
            )
        ]
        if matches:
            return matches
        # Fallback for plain names list.
        out: list[str] = []
        for part in re.split(r"[,;\n|]+", raw):
            node = part.strip()
            if not node:
                continue
            if not node.startswith("ns="):
                node = f'ns=3;s="{node}"'
            out.append(node)
        return out

    nodes: list[str] = []
    for raw in list(payload.opc_node_ids or []) + [payload.opc_node_id]:
        for node in _extract_node_ids(str(raw or "")):
            if node not in nodes:
                nodes.append(node)

    opc_url = (payload.opc_url or "").strip()
    if opc_url:
        try:
            parsed = urlparse(opc_url)
            qs = parse_qs(parsed.query or "")
            for key in ("node", "nodeid", "nodes", "tags"):
                values = qs.get(key, [])
                for value in values:
                    for node in _extract_node_ids(str(value or "")):
                        if node not in nodes:
                            nodes.append(node)
        except Exception:
            pass

        for match in re.finditer(r'ns=\d+;(?:s="[^"]+"|s=[^,\n;|]+|i=\d+|g=[0-9a-fA-F-]+|b=[^,\n;|]+)', opc_url):
            node = match.group(0).strip()
            if node and node not in nodes:
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
        ok_all = True
        for node_id in node_ids:
            try:
                node = client.get_node(node_id)
                value = node.get_value()
                results.append({"node_id": node_id, "ok": True, "value": value, "message": "Read OK"})
            except Exception as node_err:  # pragma: no cover - runtime/device dependent
                ok_all = False
                results.append({"node_id": node_id, "ok": False, "value": None, "message": str(node_err)})
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
    timeout_s = max(1.0, min(payload.timeout_ms, 20_000) / 1000.0)
    max_tags = max(10, min(int(payload.max_tags or 500), 2000))
    client = Client(endpoint, timeout=timeout_s)
    tags: list[str] = []
    visited: set[str] = set()
    queue: list = []
    try:
        client.connect()
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
                if str(nclass).endswith("Variable"):
                    tags.append(node_id)
                elif str(nclass).endswith("Object"):
                    queue.append(child)
        if tags:
            return TagDiscoveryResult(
                ok=True,
                tags=tags,
                message=f"Discovered {len(tags)} OPC-UA tags from {endpoint}"
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
    timeout_s = max(1.0, min(payload.timeout_ms, 20_000) / 1000.0)
    max_nodes = max(10, min(int(payload.max_nodes or 2000), 10000))
    max_depth = max(1, min(int(payload.max_depth or 8), 20))
    variables_only = bool(payload.variables_only)

    client = Client(endpoint, timeout=timeout_s)
    out: list[OpcUaBrowseNode] = []
    visited: set[str] = set()

    try:
        client.connect()
        try:
            root = client.get_node(ua.ObjectIds.ObjectsFolder)
        except Exception:
            root = client.get_root_node()

        queue: list[tuple[object, int, str | None]] = [(root, 0, None)]
        while queue and len(out) < max_nodes:
            node, depth, parent_id = queue.pop(0)
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
                node_class = str(node.get_node_class()).split(".")[-1]
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
                for child in children:
                    queue.append((child, depth + 1, node_id))

        variable_count = sum(1 for n in out if n.is_variable)
        return OpcUaBrowseResult(
            ok=True,
            message=f"Browsed {len(out)} nodes from {endpoint} (variables: {variable_count})",
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

                def _fmt_ab_tag_name(tag: dict) -> str:
                    name = str(tag.get("tag_name") or tag.get("name") or tag.get("symbol_name") or "").strip()
                    if not name:
                        return ""
                    program = str(tag.get("program_name") or tag.get("program") or "").strip()
                    # Keep program-scoped tags explicit if metadata gives program name.
                    if program and not name.startswith("Program:"):
                        if "." not in name:
                            name = f"Program:{program}.{name}"
                        else:
                            name = f"Program:{program}.{name.split('.')[-1]}"

                    dims = tag.get("dimensions") or tag.get("dim") or []
                    if isinstance(dims, (list, tuple)):
                        arr_dims = [int(d) for d in dims if isinstance(d, (int, float)) and int(d) > 0]
                        if arr_dims and "[" not in name:
                            name = f"{name}{''.join([f'[{d}]' for d in arr_dims])}"
                    return name

                seen: set[str] = set()
                names: list[str] = []
                for td in tag_defs:
                    if not isinstance(td, dict):
                        continue
                    formatted = _fmt_ab_tag_name(td)
                    if not formatted or formatted in seen:
                        continue
                    seen.add(formatted)
                    names.append(formatted)
                    if len(names) >= max_tags:
                        break
                if names:
                    array_count = sum(1 for n in names if "[" in n and "]" in n)
                    return TagDiscoveryResult(
                        ok=True,
                        tags=names,
                        message=f"Discovered {len(names)} AB tags from {path} (arrays detected: {array_count})",
                    )
                return TagDiscoveryResult(
                    ok=False,
                    tags=[],
                    message=f"No browseable AB tags found at {path}. Check External Access and controller browse permissions.",
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

    return TagDiscoveryResult(
        ok=False,
        tags=[],
        message="Tag discovery is not supported for this gateway type in IP-only mode.",
    )


@router.post("/opcua/browse", response_model=OpcUaBrowseResult)
def browse_opcua_nodes(payload: OpcUaBrowseRequest) -> OpcUaBrowseResult:
    ip = (payload.plc_ip or "").strip()
    if not ip:
        return OpcUaBrowseResult(ok=False, message="PLC IP is required", nodes=[])
    return _browse_opcua_nodes(payload)


@router.post("/gateways/start")
async def start_gateway_runtime(payload: GatewayRuntimeStartRequest) -> dict[str, str | bool]:
    gateway_id = payload.gateway_id.strip()
    if not gateway_id:
        return {"started": False, "message": "gateway_id is required"}
    try:
        await plc_manager.start_gateway(gateway_id=gateway_id, config=payload.config, db_sink=payload.db_sink)
        return {"started": True, "message": f"Gateway '{gateway_id}' started"}
    except ValueError as err:
        return {"started": False, "message": str(err)}


@router.post("/gateways/stop")
async def stop_gateway_runtime(payload: GatewayRuntimeStopRequest) -> dict[str, str | bool]:
    gateway_id = payload.gateway_id.strip()
    if not gateway_id:
        return {"stopped": False, "message": "gateway_id is required"}
    await plc_manager.stop_gateway(gateway_id)
    return {"stopped": True, "message": f"Gateway '{gateway_id}' stopped"}


@router.post("/gateways/stop-all")
async def stop_all_gateway_runtime() -> dict[str, str | bool]:
    await plc_manager.stop_all_gateways()
    return {"stopped": True, "message": "All gateways stopped"}


@router.get("/gateways/status")
def list_gateway_runtime_status(request: Request) -> list[dict]:
    statuses = plc_manager.list_gateway_statuses()
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
        freshness_window_s = max(3.0, (interval_ms / 1000.0) * 3.0)
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
