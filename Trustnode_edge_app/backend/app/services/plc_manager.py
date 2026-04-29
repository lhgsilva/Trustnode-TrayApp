import asyncio
import json
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Set
from urllib.parse import quote_plus

from app.models import GatewayConfig, GatewayReading, GatewayStatus
from app.opcua_utils import resolve_requested_nodes, split_requested_identifiers
from app.tenant import normalize_tenant_id


class GatewayWorker:
    def __init__(
        self,
        gateway_id: str,
        config: GatewayConfig,
        db_sink: Dict[str, Any] | None,
        db_sinks: List[Dict[str, Any]] | None = None,
        collection_gate_cb=None,
    ) -> None:
        self.gateway_id = gateway_id
        self.config = config
        self.db_sink = db_sink or None
        self.db_sinks = self._normalize_db_sinks(db_sink, db_sinks)
        self._collection_gate_cb = collection_gate_cb
        self.running = False
        self.last_error: str | None = None
        self.latest_readings: List[GatewayReading] = []
        self._task: asyncio.Task | None = None

        self._db_engine = None
        self._db_engine_key = ""
        self._db_schema_ready_key = ""
        self._buffer_engine = None
        self._buffer_engine_key = ""

        self.db_write_count = 0
        self.db_last_write_utc: str | None = None
        self.db_last_error: str | None = None
        self.db_pending_count = 0
        self.collection_blocked = False
        self.collection_block_reason: str | None = None
        self._remote_flush_inflight = False
        self._remote_flush_lock = threading.Lock()
        self._remote_last_flush_started_monotonic = 0.0
        self._remote_last_pending_probe_monotonic = 0.0
        self._remote_flush_min_interval_seconds = max(
            0.1, float(os.environ.get("TRUSTNODE_REMOTE_FLUSH_MIN_SECONDS", "0.5") or "0.5")
        )
        self._remote_pending_probe_seconds = max(
            0.25, float(os.environ.get("TRUSTNODE_REMOTE_PENDING_PROBE_SECONDS", "2.0") or "2.0")
        )
        self._ab_preferred_path: str | None = None
        self._ab_pycomm3_client = None
        self._ab_pycomm3_path: str | None = None
        self._ab_pylogix_client = None
        self._ab_pylogix_ip: str | None = None
        self._ab_pylogix_slot: int | None = None
        self._opc_resolve_cache_key = ""
        self._opc_resolved_targets: list[tuple[str, str]] = []
        self._telemetry_runtime_refresh_monotonic = 0.0

    def set_config(self, config: GatewayConfig) -> None:
        if (
            str(getattr(self.config, "plc_ip", "") or "").strip() != str(getattr(config, "plc_ip", "") or "").strip()
            or str(getattr(self.config, "gateway_type", "") or "").strip().lower()
            != str(getattr(config, "gateway_type", "") or "").strip().lower()
        ):
            self._dispose_gateway_clients()
        if (
            str(getattr(self.config, "opc_url", "") or "").strip() != str(getattr(config, "opc_url", "") or "").strip()
            or list(getattr(self.config, "tags", []) or []) != list(getattr(config, "tags", []) or [])
        ):
            self._opc_resolve_cache_key = ""
            self._opc_resolved_targets = []
        self.config = config

    def _normalize_db_sinks(
        self, db_sink: Dict[str, Any] | None, db_sinks: List[Dict[str, Any]] | None
    ) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        seen: Set[str] = set()
        for sink in [db_sink, *((db_sinks or []))]:
            if not isinstance(sink, dict):
                continue
            engine = str(sink.get("engine") or "").strip().lower()
            if not engine:
                continue
            key = (
                f"{engine}|{str(sink.get('id') or '').strip()}|"
                f"{str(sink.get('host') or '').strip().lower()}|{int(sink.get('port') or 0)}|"
                f"{str(sink.get('database') or '').strip().lower()}|"
                f"{str(sink.get('file_path') or '').strip().lower()}|"
                f"{str(sink.get('sqlite_path') or '').strip().lower()}"
            )
            if key in seen:
                continue
            seen.add(key)
            out.append(dict(sink))
        return out

    def set_db_sink(
        self, db_sink: Dict[str, Any] | None, db_sinks: List[Dict[str, Any]] | None = None
    ) -> None:
        self.db_sink = db_sink or None
        self.db_sinks = self._normalize_db_sinks(db_sink, db_sinks)
        self.db_write_count = 0
        self.db_last_write_utc = None
        self.db_last_error = None
        self.db_pending_count = 0
        with self._remote_flush_lock:
            self._remote_flush_inflight = False
            self._remote_last_flush_started_monotonic = 0.0
            self._remote_last_pending_probe_monotonic = 0.0
        self._dispose_db_engine()

    def set_collection_gate_cb(self, cb) -> None:
        self._collection_gate_cb = cb

    async def start(self, emit_event) -> None:
        if self.running:
            return
        self.running = True
        self.last_error = None
        self._task = asyncio.create_task(self._run_loop(emit_event))

    async def stop(self) -> None:
        if not self.running:
            return
        self.running = False
        with self._remote_flush_lock:
            self._remote_flush_inflight = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._dispose_gateway_clients()

    def get_status(self) -> GatewayStatus:
        return GatewayStatus(
            running=self.running,
            gateway_type=self.config.gateway_type,
            plc_ip=self.config.plc_ip,
            interval_ms=self.config.interval_ms,
            tags=self.config.tags,
            last_error=self.last_error,
            db_sink_engine=(self.db_sink or {}).get("engine"),
            db_write_count=self.db_write_count,
            db_last_write_utc=self.db_last_write_utc,
            db_last_error=self.db_last_error,
            db_pending_count=self.db_pending_count,
            collection_blocked=self.collection_blocked,
            collection_block_reason=self.collection_block_reason,
        )

    async def _run_loop(self, emit_event) -> None:
        while self.running:
            cycle_started = time.monotonic()
            try:
                # Gateway drivers are synchronous/blocking (socket I/O). Keep the
                # asyncio loop responsive by executing reads off-loop.
                readings = await asyncio.to_thread(self._read_from_gateway)
                self.latest_readings = readings
                persisted_local = False
                persisted_edge_record_id: str | None = None
                if self._collection_gate_cb:
                    collection_allowed, block_reason = self._collection_gate_cb(self.gateway_id, readings)
                else:
                    collection_allowed, block_reason = self._is_collection_allowed(readings)
                self.collection_blocked = not collection_allowed
                self.collection_block_reason = block_reason
                if collection_allowed:
                    # Durable local commit is the definition of successful collection.
                    # If this fails, we keep runtime alive but we do not mark this cycle as collected.
                    try:
                        from app.state import telemetry_service, app_store  # local import avoids circular import timing

                        now_mono = time.monotonic()
                        if now_mono - self._telemetry_runtime_refresh_monotonic >= 10.0:
                            try:
                                bootstrap = app_store.get_bootstrap(prefer_cloud_reads=False) or {}
                                telemetry_service.configure_from_bootstrap(bootstrap)
                            except Exception:
                                pass
                            self._telemetry_runtime_refresh_monotonic = now_mono

                        ok, err, edge_record_id = await asyncio.to_thread(
                            lambda: telemetry_service.record_collection_cycle(
                                gateway_id=self.gateway_id,
                                config=self.config,
                                readings=readings,
                                collection_status="ok",
                            )
                        )
                        persisted_local = bool(ok)
                        persisted_edge_record_id = edge_record_id
                        if not ok:
                            collection_allowed = False
                            self.collection_blocked = True
                            self.collection_block_reason = f"Local persistence failed: {err}"
                            self.last_error = self.collection_block_reason
                    except Exception as exc:
                        collection_allowed = False
                        self.collection_blocked = True
                        self.collection_block_reason = f"Local persistence failed: {exc}"
                        self.last_error = self.collection_block_reason
                await emit_event(
                    {
                        "type": "reading",
                        "gateway_id": self.gateway_id,
                        "collection_allowed": collection_allowed,
                        "persisted_local": persisted_local,
                        "edge_record_id": persisted_edge_record_id,
                        "collection_block_reason": self.collection_block_reason,
                        "status": self.get_status().model_dump(),
                        "readings": [r.model_dump() for r in readings],
                    }
                )
                if collection_allowed:
                    # DB sink writes can be blocking (sqlite/file/remote enqueue).
                    # Execute off-loop to avoid delaying other gateways/API calls.
                    await asyncio.to_thread(self._persist_readings, readings)
            except Exception as exc:
                self.last_error = str(exc)
                # Do not keep stale values visible when a read cycle fails.
                self.latest_readings = []
                await emit_event({"type": "error", "gateway_id": self.gateway_id, "message": self.last_error})
            target_s = max(self.config.interval_ms / 1000.0, 0.1)
            elapsed_s = max(0.0, time.monotonic() - cycle_started)
            await asyncio.sleep(max(0.01, target_s - elapsed_s))

    def _is_collection_allowed(self, readings: List[GatewayReading]) -> tuple[bool, str | None]:
        triggers = [t for t in (self.config.collection_triggers or []) if bool(t.get("enabled", True))]
        if not triggers:
            return True, None

        latest_by_tag = {str(r.tag_name or "").strip().lower(): r for r in readings}

        def _cmp(value: float, operator: str, threshold: float) -> bool:
            if operator == "<":
                return value < threshold
            if operator == "<=":
                return value <= threshold
            if operator == ">":
                return value > threshold
            if operator == ">=":
                return value >= threshold
            return False

        hit = False
        for tr in triggers:
            tag = str(tr.get("tag_name") or "").strip().lower()
            if not tag:
                continue
            reading = latest_by_tag.get(tag)
            if not reading:
                continue
            try:
                threshold = float(tr.get("value"))
                op = str(tr.get("operator") or ">=").strip()
                if _cmp(float(reading.value), op, threshold):
                    hit = True
                    break
            except Exception:
                continue

        if hit:
            return True, None
        return False, "Trigger condition is FALSE (collection/write paused)."

    def _get_read_tags(self) -> List[str]:
        tags: List[str] = []
        seen: Set[str] = set()

        def _add(tag_raw: str) -> None:
            tag = str(tag_raw or "").strip()
            if not tag:
                return
            key = tag.lower()
            if key in seen:
                return
            seen.add(key)
            tags.append(tag)

        for t in (self.config.tags or []):
            _add(t)

        # Always monitor local trigger source tags in real-time, even if user
        # did not include them in the main gateway tag list.
        for tr in (self.config.collection_triggers or []):
            if not bool(tr.get("enabled", True)):
                continue
            trig_gid = str(tr.get("gateway_id") or "").strip()
            if trig_gid and trig_gid != self.gateway_id:
                continue
            _add(str(tr.get("tag_name") or ""))

        return tags

    def _read_from_gateway(self) -> List[GatewayReading]:
        gateway_type = (self.config.gateway_type or "").strip().lower()
        if gateway_type == "siemens_opcua":
            return self._read_from_opcua()
        if gateway_type == "allen_bradley":
            return self._read_from_allen_bradley()
        if gateway_type == "siemens_snap7":
            return self._read_from_snap7()
        raise RuntimeError(f"Gateway type '{self.config.gateway_type}' is not implemented for real-time reads.")

    def _coerce_value_to_float(self, raw: Any, tag_name: str) -> float:
        if raw is None:
            raise RuntimeError(f"Tag '{tag_name}' returned null value.")
        if isinstance(raw, bool):
            return 1.0 if raw else 0.0
        if isinstance(raw, (int, float)):
            return float(raw)
        text = str(raw).strip()
        try:
            return float(text)
        except Exception as exc:
            raise RuntimeError(f"Tag '{tag_name}' is non-numeric: {raw!r}") from exc

    def _read_from_allen_bradley(self) -> List[GatewayReading]:
        ip = (self.config.plc_ip or "").strip()
        if not ip:
            raise RuntimeError("Allen-Bradley read failed: PLC IP is empty.")
        tags = self._get_read_tags()
        if not tags:
            raise RuntimeError("Allen-Bradley read failed: no tags configured.")
        candidate_paths = [ip] if "/" in ip else [ip, f"{ip}/1"]
        if self._ab_preferred_path:
            candidate_paths = [self._ab_preferred_path] + [p for p in candidate_paths if p != self._ab_preferred_path]

        # Primary: pycomm3 LogixDriver (ControlLogix/CompactLogix family).
        try:
            return self._read_from_allen_bradley_pycomm3(candidate_paths, tags)
        except Exception as pycomm3_exc:
            # Fallback: pylogix works with some AB targets where pycomm3 fails
            # during PLC-info handshake ("Failed to get PLC info").
            try:
                return self._read_from_allen_bradley_pylogix(candidate_paths, tags)
            except Exception as pylogix_exc:
                raise RuntimeError(
                    f"Allen-Bradley read failed. pycomm3='{pycomm3_exc}'; pylogix='{pylogix_exc}'"
                ) from pylogix_exc

    def _read_from_allen_bradley_pycomm3(self, candidate_paths: List[str], tags: List[str]) -> List[GatewayReading]:
        try:
            from pycomm3 import LogixDriver  # type: ignore
        except Exception as exc:
            raise RuntimeError(f"pycomm3 unavailable: {exc}") from exc

        last_error = ""

        def _norm_tag(t: str) -> str:
            return str(t or "").strip().replace(" ", "").lower()

        for path in candidate_paths:
            try:
                ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                out: List[GatewayReading] = []
                # Keep AB cycle lightweight: reuse session between polls.
                plc = self._ensure_ab_pycomm3_client(path, LogixDriver)
                known_tags = set()
                if isinstance(getattr(plc, "tags", None), dict):
                    known_tags = {str(k).strip() for k in plc.tags.keys() if str(k).strip()}
                if known_tags:
                    missing = [t for t in tags if t not in known_tags]
                    if missing:
                        raise RuntimeError(
                            f"Configured AB tags not found in controller ({len(missing)}): {', '.join(missing[:8])}"
                        )

                try:
                    results = plc.read(*tags)
                except Exception:
                    # One reconnect attempt on broken/stale session.
                    self._close_ab_pycomm3_client()
                    plc = self._ensure_ab_pycomm3_client(path, LogixDriver)
                    results = plc.read(*tags)

                if not isinstance(results, list):
                    results = [results]
                if not results:
                    raise RuntimeError("no responses")
                if len(results) != len(tags):
                    raise RuntimeError(f"requested {len(tags)} tags but got {len(results)} results")
                for idx, res in enumerate(results):
                    requested_tag = tags[idx]
                    reported_tag = str(getattr(res, "tag", "") or "")
                    if reported_tag and _norm_tag(reported_tag) != _norm_tag(requested_tag):
                        raise RuntimeError(
                            f"read mismatch on route {path}: requested '{requested_tag}' but got '{reported_tag}'"
                        )
                    status = str(getattr(res, "error", None) or getattr(res, "status", "") or "")
                    if status:
                        raise RuntimeError(f"read failed for '{requested_tag}': {status}")
                    value = self._coerce_value_to_float(getattr(res, "value", None), requested_tag or "<unknown>")
                    quality, quality_label = self._normalize_quality(self.config.gateway_type, raw_quality=192)
                    out.append(
                        GatewayReading(
                            ts_utc=ts,
                            tag_name=requested_tag,
                            value=value,
                            quality=quality,
                            quality_label=quality_label,
                            source=self.config.gateway_type,
                            site=self.config.site,
                            area=self.config.area,
                            equipment=self.config.equipment,
                        )
                    )
                if out:
                    self._ab_preferred_path = path
                    return out
                raise RuntimeError("all tags returned invalid values")
            except Exception as exc:
                if self._ab_pycomm3_path == path:
                    self._close_ab_pycomm3_client()
                last_error = str(exc)
                continue
        self._ab_preferred_path = None
        raise RuntimeError(f"all route attempts failed ({', '.join(candidate_paths)}): {last_error}")

    def _read_from_allen_bradley_pylogix(self, candidate_paths: List[str], tags: List[str]) -> List[GatewayReading]:
        try:
            from pylogix import PLC  # type: ignore
        except Exception as exc:
            raise RuntimeError(f"pylogix unavailable: {exc}") from exc

        last_error = ""
        base_ip = (self.config.plc_ip or "").strip().split("/", 1)[0]
        slots: List[int] = []
        for p in candidate_paths:
            slot = 0
            if "/" in p:
                try:
                    slot = int(str(p).split("/", 1)[1].strip())
                except Exception:
                    slot = 0
            if slot not in slots:
                slots.append(slot)
        if 0 not in slots:
            slots.append(0)

        for slot in slots:
            comm = None
            try:
                comm = self._ensure_ab_pylogix_client(base_ip, slot, PLC)
                ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                out: List[GatewayReading] = []
                results = comm.Read(tags)
                if not isinstance(results, list):
                    results = [results]
                if len(results) != len(tags):
                    raise RuntimeError(f"requested {len(tags)} tags but got {len(results)} results")
                for idx, res in enumerate(results):
                    tag = tags[idx]
                    status = str(getattr(res, "Status", "") or "")
                    status_ok = status.strip().lower() in ("success", "ok", "0")
                    if not status_ok:
                        raise RuntimeError(f"read failed for '{tag}': {status}")
                    value = self._coerce_value_to_float(getattr(res, "Value", None), tag)
                    quality, quality_label = self._normalize_quality(self.config.gateway_type, raw_quality=192)
                    out.append(
                        GatewayReading(
                            ts_utc=ts,
                            tag_name=tag,
                            value=value,
                            quality=quality,
                            quality_label=quality_label,
                            source=self.config.gateway_type,
                            site=self.config.site,
                            area=self.config.area,
                            equipment=self.config.equipment,
                        )
                    )
                if out:
                    return out
                raise RuntimeError("all tags returned invalid values")
            except Exception as exc:
                if self._ab_pylogix_ip == base_ip and self._ab_pylogix_slot == slot:
                    self._close_ab_pylogix_client()
                last_error = str(exc)
                continue
        raise RuntimeError(f"all slot attempts failed ({', '.join(str(s) for s in slots)}): {last_error}")

    def _parse_snap7_tag(self, raw_tag: str) -> tuple[str, int, int, str, int]:
        # Supported forms:
        # DB1,REAL0 | DB1,DINT4 | DB1,INT2 | DB1,WORD8 | DB1,BIT10.3 | DB1,BYTE12
        # M10.0 / M10.1, I0.0, Q0.0, MB10, MW12, MD20, IB0, IW2, QB4
        tag = str(raw_tag or "").strip().upper().replace(" ", "")
        if not tag:
            raise ValueError("Empty tag")

        if tag.startswith("DB"):
            left_right = tag.split(",", 1)
            if len(left_right) != 2:
                raise ValueError("DB tag format must be DB<number>,<type><offset>")
            db_no_txt = left_right[0][2:]
            spec = left_right[1]
            db_no = int(db_no_txt)
            if spec.startswith("BIT"):
                rest = spec[3:]
                byte_txt, bit_txt = rest.split(".", 1)
                byte_idx = int(byte_txt)
                bit_idx = int(bit_txt)
                if bit_idx < 0 or bit_idx > 7:
                    raise ValueError("BIT index must be 0..7")
                return ("DB", db_no, byte_idx, "BIT", bit_idx)
            if spec.startswith("REAL"):
                return ("DB", db_no, int(spec[4:]), "REAL", 0)
            if spec.startswith("DINT"):
                return ("DB", db_no, int(spec[4:]), "DINT", 0)
            if spec.startswith("DWORD"):
                return ("DB", db_no, int(spec[5:]), "DWORD", 0)
            if spec.startswith("INT"):
                return ("DB", db_no, int(spec[3:]), "INT", 0)
            if spec.startswith("WORD"):
                return ("DB", db_no, int(spec[4:]), "WORD", 0)
            if spec.startswith("BYTE"):
                return ("DB", db_no, int(spec[4:]), "BYTE", 0)
            raise ValueError("Unsupported DB type; use BIT/REAL/DINT/DWORD/INT/WORD/BYTE")

        area_prefix = tag[0]
        if area_prefix not in ("M", "I", "Q"):
            raise ValueError("Snap7 tag must start with DB, M, I, or Q")
        if "." in tag and len(tag) >= 4:
            byte_txt, bit_txt = tag[1:].split(".", 1)
            return (area_prefix, 0, int(byte_txt), "BIT", int(bit_txt))
        if len(tag) >= 3 and tag[1] in ("B", "W", "D"):
            width = tag[1]
            byte_idx = int(tag[2:])
            if width == "B":
                return (area_prefix, 0, byte_idx, "BYTE", 0)
            if width == "W":
                return (area_prefix, 0, byte_idx, "WORD", 0)
            return (area_prefix, 0, byte_idx, "DWORD", 0)
        # Default M10/I0/Q4 as BYTE.
        return (area_prefix, 0, int(tag[1:]), "BYTE", 0)

    def _read_from_snap7(self) -> List[GatewayReading]:
        try:
            import snap7  # type: ignore
            from snap7.util import get_bool, get_real, get_dint, get_int, get_word, get_dword  # type: ignore
            from snap7.type import Areas  # type: ignore
        except Exception as exc:
            raise RuntimeError(f"Siemens Snap7 reader unavailable (python-snap7 missing): {exc}") from exc

        ip = (self.config.plc_ip or "").strip()
        if not ip:
            raise RuntimeError("Siemens Snap7 read failed: PLC IP is empty.")
        tags = self._get_read_tags()
        if not tags:
            raise RuntimeError("Siemens Snap7 read failed: no tags configured.")

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        client = snap7.client.Client()
        out: List[GatewayReading] = []
        try:
            # Standard rack/slot for S7-1200/1500; make env-configurable.
            rack = int(os.environ.get("TRUSTNODE_S7_RACK", "0"))
            slot = int(os.environ.get("TRUSTNODE_S7_SLOT", "1"))
            client.connect(ip, rack, slot)
            if not client.get_connected():
                raise RuntimeError(f"Unable to establish Snap7 session to {ip} (rack={rack}, slot={slot}).")

            for raw_tag in tags:
                try:
                    area, db_no, byte_idx, dtype, bit_idx = self._parse_snap7_tag(raw_tag)
                    if area == "DB":
                        size = 4 if dtype in ("REAL", "DINT", "DWORD") else 2 if dtype in ("INT", "WORD") else 1
                        data = client.db_read(db_no, byte_idx, size)
                    else:
                        area_code = Areas.MK if area == "M" else Areas.PE if area == "I" else Areas.PA
                        size = 4 if dtype == "DWORD" else 2 if dtype == "WORD" else 1
                        data = client.read_area(area_code, 0, byte_idx, size)

                    if dtype == "BIT":
                        val = 1.0 if get_bool(data, 0, bit_idx) else 0.0
                    elif dtype == "REAL":
                        val = float(get_real(data, 0))
                    elif dtype == "DINT":
                        val = float(get_dint(data, 0))
                    elif dtype == "DWORD":
                        val = float(get_dword(data, 0))
                    elif dtype == "INT":
                        val = float(get_int(data, 0))
                    elif dtype == "WORD":
                        val = float(get_word(data, 0))
                    else:
                        val = float(data[0])

                    quality, quality_label = self._normalize_quality(self.config.gateway_type, raw_status=0)
                    out.append(
                        GatewayReading(
                            ts_utc=ts,
                            tag_name=raw_tag,
                            value=val,
                            quality=quality,
                            quality_label=quality_label,
                            source=self.config.gateway_type,
                            site=self.config.site,
                            area=self.config.area,
                            equipment=self.config.equipment,
                        )
                    )
                except Exception as tag_exc:
                    raise RuntimeError(f"Snap7 read failed for '{raw_tag}': {tag_exc}") from tag_exc
        finally:
            try:
                client.disconnect()
            except Exception:
                pass
        if not out:
            raise RuntimeError("Siemens Snap7 read failed: no values were read.")
        return out

    def _read_from_opcua(self) -> List[GatewayReading]:
        try:
            from opcua import Client  # type: ignore
        except Exception as exc:
            raise RuntimeError(f"OPC-UA client not installed: {exc}") from exc

        endpoint = (self.config.opc_url or "").strip() or f"opc.tcp://{self.config.plc_ip.strip()}:4840"
        requested_ids = split_requested_identifiers(self._get_read_tags())
        if not requested_ids:
            raise RuntimeError("OPC-UA read failed: no node ids/tags configured.")

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        out: List[GatewayReading] = []
        client = Client(endpoint, timeout=4.0)
        try:
            client.connect()
            cache_key = f"{endpoint}|{'|'.join(requested_ids)}"
            resolved_targets: list[tuple[str, str]]
            unresolved_items: list[str] = []
            if self._opc_resolve_cache_key == cache_key and self._opc_resolved_targets:
                resolved_targets = self._opc_resolved_targets
            else:
                resolved, unresolved_items = resolve_requested_nodes(client, requested_ids)
                resolved_targets = [(t.requested, t.resolved_node_id) for t in resolved]
                self._opc_resolved_targets = resolved_targets
                self._opc_resolve_cache_key = cache_key
            for unresolved in unresolved_items:
                out.append(
                    GatewayReading(
                        ts_utc=ts,
                        tag_name=unresolved,
                        value=0.0,
                        quality=0,
                        quality_label="BAD",
                        source=self.config.gateway_type,
                        site=self.config.site,
                        area=self.config.area,
                        equipment=self.config.equipment,
                    )
                )
            if not resolved_targets:
                raise RuntimeError("OPC-UA read failed: no variable nodes resolved from configured tags.")
            for requested_tag, node_id in resolved_targets:
                try:
                    node = client.get_node(node_id)
                    data_value = node.get_data_value()
                    value = data_value.Value.Value
                    status_name = str(data_value.StatusCode.name) if data_value and data_value.StatusCode else ""
                    quality, quality_label = self._normalize_quality(
                        self.config.gateway_type,
                        raw_status=status_name
                    )
                    out.append(
                        GatewayReading(
                            ts_utc=ts,
                            tag_name=requested_tag,
                            value=float(value) if value is not None else 0.0,
                            quality=quality,
                            quality_label=quality_label,
                            source=self.config.gateway_type,
                            site=self.config.site,
                            area=self.config.area,
                            equipment=self.config.equipment,
                        )
                    )
                except Exception:
                    out.append(
                        GatewayReading(
                            ts_utc=ts,
                            tag_name=requested_tag,
                            value=0.0,
                            quality=0,
                            quality_label="BAD",
                            source=self.config.gateway_type,
                            site=self.config.site,
                            area=self.config.area,
                            equipment=self.config.equipment,
                        )
                    )
            if not out:
                raise RuntimeError("No OPC-UA nodes returned readings.")
            return out
        finally:
            try:
                client.disconnect()
            except Exception:
                pass

    def _normalize_quality(self, gateway_type: str, raw_quality: Any = None, raw_status: Any = None) -> tuple[int, str]:
        gt = (gateway_type or "").strip().lower()
        if isinstance(raw_quality, int):
            return self._quality_pair(max(0, min(255, raw_quality)))
        if isinstance(raw_quality, bool):
            return self._quality_pair(192 if raw_quality else 0)
        if gt == "siemens_opcua" and isinstance(raw_status, str):
            s = raw_status.strip().lower()
            if "good" in s:
                return 192, "GOOD"
            if "uncertain" in s:
                return 64, "UNCERTAIN"
            if s:
                return 0, "BAD"
        if isinstance(raw_status, int):
            return self._quality_pair(192 if raw_status == 0 else 0)
        return 192, "GOOD"

    def _quality_pair(self, q: int) -> tuple[int, str]:
        if q >= 192:
            return q, "GOOD"
        if q >= 64:
            return q, "UNCERTAIN"
        return q, "BAD"

    def _mark_db_write_success(self, count: int) -> None:
        self.db_write_count += max(0, int(count))
        self.db_last_write_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        self.db_last_error = None
        self.last_error = None

    def _mark_db_write_error(self, msg: str) -> None:
        text = str(msg or "")
        self.db_last_error = text
        # Keep transient remote timeout errors visible for diagnostics, but do not
        # downgrade runtime state to hard "DB fails" while store-forward is active.
        s = text.lower()
        transient = (
            "connectiontimeout" in s
            or "connection timeout" in s
            or "timed out" in s
            or "read timeout" in s
            or "could not connect" in s
            or "network is unreachable" in s
        )
        self.last_error = None if transient else text

    def _dispose_db_engine(self) -> None:
        if self._db_engine is not None:
            try:
                self._db_engine.dispose()
            except Exception:
                pass
        self._db_engine = None
        self._db_engine_key = ""
        self._db_schema_ready_key = ""

    def _ensure_ab_pycomm3_client(self, path: str, logix_driver_cls):
        if self._ab_pycomm3_client is not None and self._ab_pycomm3_path == path:
            return self._ab_pycomm3_client
        self._close_ab_pycomm3_client()
        plc = logix_driver_cls(path, init_tags=False, init_program_tags=False)
        plc.open()
        self._ab_pycomm3_client = plc
        self._ab_pycomm3_path = path
        return plc

    def _close_ab_pycomm3_client(self) -> None:
        if self._ab_pycomm3_client is not None:
            try:
                self._ab_pycomm3_client.close()
            except Exception:
                pass
        self._ab_pycomm3_client = None
        self._ab_pycomm3_path = None

    def _ensure_ab_pylogix_client(self, ip: str, slot: int, plc_cls):
        if self._ab_pylogix_client is not None and self._ab_pylogix_ip == ip and self._ab_pylogix_slot == slot:
            return self._ab_pylogix_client
        self._close_ab_pylogix_client()
        comm = plc_cls()
        comm.IPAddress = ip
        comm.ProcessorSlot = int(slot)
        self._ab_pylogix_client = comm
        self._ab_pylogix_ip = ip
        self._ab_pylogix_slot = int(slot)
        return comm

    def _close_ab_pylogix_client(self) -> None:
        if self._ab_pylogix_client is not None:
            try:
                self._ab_pylogix_client.Close()
            except Exception:
                pass
        self._ab_pylogix_client = None
        self._ab_pylogix_ip = None
        self._ab_pylogix_slot = None

    def _dispose_gateway_clients(self) -> None:
        self._close_ab_pycomm3_client()
        self._close_ab_pylogix_client()

    def _sqlite_url_from_path(self, sqlite_path: str) -> str:
        path_norm = (sqlite_path or "").strip()
        if not path_norm:
            path_norm = self._default_data_file("trustnode_edge.db")
        if path_norm == ":memory:":
            return "sqlite+pysqlite:///:memory:"

        # Resolve relative paths under a writable user directory.
        if not os.path.isabs(path_norm):
            path_norm = os.path.join(self._default_data_dir(), path_norm)
        path_norm = os.path.abspath(path_norm)
        parent = os.path.dirname(path_norm)
        if parent:
            os.makedirs(parent, exist_ok=True)
        path_norm = path_norm.replace("\\", "/")

        if path_norm == ":memory:":
            return "sqlite+pysqlite:///:memory:"
        if len(path_norm) > 2 and path_norm[1] == ":":
            return f"sqlite+pysqlite:///{path_norm}"
        if path_norm.startswith("/"):
            return f"sqlite+pysqlite:///{path_norm}"
        return f"sqlite+pysqlite:///{path_norm}"

    def _default_data_dir(self) -> str:
        env = os.environ.get("TRUSTNODE_DATA_DIR", "").strip()
        if env:
            os.makedirs(env, exist_ok=True)
            return env
        base = os.path.join(os.path.expanduser("~"), ".trustnode_edge", "data")
        os.makedirs(base, exist_ok=True)
        return base

    def _default_data_file(self, filename: str) -> str:
        return os.path.join(self._default_data_dir(), filename)

    def _ensure_buffer_engine(self):
        from sqlalchemy import create_engine, text

        buffer_path = (self.db_sink or {}).get("store_forward_path") or self._default_data_file("trustnode_store_forward.db")
        key = self._sqlite_url_from_path(str(buffer_path))
        if self._buffer_engine is None or self._buffer_engine_key != key:
            if self._buffer_engine is not None:
                try:
                    self._buffer_engine.dispose()
                except Exception:
                    pass
            self._buffer_engine = create_engine(key, pool_pre_ping=True)
            self._buffer_engine_key = key
            with self._buffer_engine.begin() as conn:
                conn.execute(
                    text(
                        """
                        CREATE TABLE IF NOT EXISTS outbox_readings (
                          id INTEGER PRIMARY KEY AUTOINCREMENT,
                          gateway_id TEXT NOT NULL,
                          ts_utc TEXT NOT NULL,
                          tag_name TEXT NOT NULL,
                          value REAL NULL,
                          quality INTEGER NULL,
                          quality_label TEXT NULL,
                          source TEXT NULL,
                          site TEXT NULL,
                          area TEXT NULL,
                          equipment TEXT NULL,
                          raw_payload TEXT NULL,
                          sent_remote INTEGER NOT NULL DEFAULT 0,
                          retries INTEGER NOT NULL DEFAULT 0,
                          last_error TEXT NULL,
                          created_utc TEXT NOT NULL DEFAULT (datetime('now')),
                          sent_utc TEXT NULL
                        )
                        """
                    )
                )
                conn.execute(text("CREATE INDEX IF NOT EXISTS idx_outbox_unsent ON outbox_readings(sent_remote, id)"))
                conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS idx_outbox_gateway_unsent "
                        "ON outbox_readings(gateway_id, sent_remote, id)"
                    )
                )
        return self._buffer_engine

    def _enqueue_outbox(self, readings: List[GatewayReading]) -> int:
        from sqlalchemy import text

        engine = self._ensure_buffer_engine()
        rows = []
        for r in readings:
            rows.append(
                {
                    "gateway_id": self.gateway_id,
                    "ts_utc": r.ts_utc,
                    "tag_name": r.tag_name,
                    "value": r.value,
                    "quality": r.quality,
                    "quality_label": r.quality_label,
                    "source": r.source,
                    "site": r.site,
                    "area": r.area,
                    "equipment": r.equipment,
                    "raw_payload": json.dumps(r.model_dump()),
                }
            )
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO outbox_readings
                    (gateway_id, ts_utc, tag_name, value, quality, quality_label, source, site, area, equipment, raw_payload)
                    VALUES (:gateway_id, :ts_utc, :tag_name, :value, :quality, :quality_label, :source, :site, :area, :equipment, :raw_payload)
                    """
                ),
                rows,
            )
        return len(rows)

    def _load_pending(self, limit: int = 300) -> List[Dict[str, Any]]:
        from sqlalchemy import text

        engine = self._ensure_buffer_engine()
        with engine.begin() as conn:
            rs = conn.execute(
                text(
                    """
                    SELECT id, ts_utc, tag_name, value, quality, quality_label, source, site, area, equipment
                    FROM outbox_readings
                    WHERE sent_remote = 0 AND gateway_id = :gid
                    ORDER BY id ASC
                    LIMIT :lim
                    """
                ),
                {"gid": self.gateway_id, "lim": max(1, int(limit))},
            )
            return [dict(r._mapping) for r in rs]

    def _mark_sent(self, ids: List[int]) -> None:
        from sqlalchemy import text

        if not ids:
            return
        engine = self._ensure_buffer_engine()
        sent_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE outbox_readings SET sent_remote = 1, sent_utc = :su, last_error = NULL WHERE id = :id"),
                [{"id": int(i), "su": sent_utc} for i in ids],
            )

    def _mark_failed(self, ids: List[int], err: str) -> None:
        from sqlalchemy import text

        if not ids:
            return
        engine = self._ensure_buffer_engine()
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE outbox_readings SET retries = retries + 1, last_error = :err WHERE id = :id"),
                [{"id": int(i), "err": (err or '')[:1000]} for i in ids],
            )

    def _count_pending(self) -> int:
        from sqlalchemy import text

        engine = self._ensure_buffer_engine()
        with engine.begin() as conn:
            rs = conn.execute(
                text("SELECT COUNT(*) AS c FROM outbox_readings WHERE sent_remote = 0 AND gateway_id = :gid"),
                {"gid": self.gateway_id},
            )
            row = rs.first()
            return int(row[0] if row else 0)

    def _persist_readings(self, readings: List[GatewayReading]) -> None:
        if not readings or not self.db_sink:
            return
        engine = (self.db_sink.get("engine") or "").strip().lower()
        if engine == "postgresql":
            try:
                # Never block real-time collection loop on remote/cloud roundtrips.
                # Enqueue locally and flush in background thread.
                queued = self._enqueue_outbox(readings)
                if queued:
                    self.db_pending_count = max(0, int(self.db_pending_count or 0)) + int(queued)
                self._mark_db_write_success(len(readings))
                self._schedule_remote_flush(engine)
            except Exception as exc:
                self._mark_db_write_error(f"Store-forward pipeline error: {exc}")
        elif engine == "legacy_http":
            try:
                queued = self._enqueue_outbox(readings)
                if queued:
                    self.db_pending_count = max(0, int(self.db_pending_count or 0)) + int(queued)
                self._schedule_remote_flush(engine)
            except Exception as exc:
                self._mark_db_write_error(f"Store-forward pipeline error: {exc}")
        elif engine == "sqlite":
            self._persist_sqlite(readings)
            self.db_pending_count = 0
        elif engine == "csv_file":
            self._persist_csv_file(readings)
            self.db_pending_count = 0
        elif engine == "txt_file":
            self._persist_txt_file(readings)
            self.db_pending_count = 0
        else:
            self.db_pending_count = 0

        # Fan-out write to additional local/file sinks enabled for this gateway.
        for sink in (self.db_sinks or []):
            if not isinstance(sink, dict):
                continue
            if sink is self.db_sink:
                continue
            sink_engine = str(sink.get("engine") or "").strip().lower()
            if sink_engine == engine:
                continue
            try:
                if sink_engine == "csv_file":
                    self._persist_csv_file_for_sink(sink, readings)
                elif sink_engine == "txt_file":
                    self._persist_txt_file_for_sink(sink, readings)
                elif sink_engine == "sqlite":
                    self._persist_sqlite_for_sink(sink, readings)
            except Exception:
                # Parallel sinks are best-effort and should not block primary flow.
                pass

    def _persist_csv_file_for_sink(self, sink: Dict[str, Any], readings: List[GatewayReading]) -> bool:
        import csv

        try:
            file_path = self._resolve_output_file_path((sink or {}).get("file_path") or "", "trustnode_log.csv")
            write_header = (not os.path.exists(file_path)) or os.path.getsize(file_path) == 0
            with open(file_path, "a", encoding="utf-8", newline="") as f:
                writer = csv.writer(f)
                if write_header:
                    writer.writerow(["ts_utc", "tag_name", "value", "quality", "quality_label", "source", "site", "area", "equipment"])
                for r in readings:
                    writer.writerow([r.ts_utc, r.tag_name, r.value, r.quality, r.quality_label, r.source, r.site, r.area, r.equipment])
            return True
        except Exception:
            return False

    def _persist_txt_file_for_sink(self, sink: Dict[str, Any], readings: List[GatewayReading]) -> bool:
        try:
            file_path = self._resolve_output_file_path((sink or {}).get("file_path") or "", "trustnode_log.txt")
            with open(file_path, "a", encoding="utf-8") as f:
                for r in readings:
                    f.write(
                        f"{r.ts_utc}|{r.tag_name}|{r.value}|{r.quality}|{r.quality_label}|"
                        f"{r.source}|{r.site}|{r.area}|{r.equipment}\n"
                    )
            return True
        except Exception:
            return False

    def _persist_sqlite_for_sink(self, sink: Dict[str, Any], readings: List[GatewayReading]) -> bool:
        try:
            from sqlalchemy import create_engine, text
        except Exception:
            return False
        sqlite_path = (sink.get("sqlite_path") or "./data/trustnode_edge.db").strip()
        table = (sink.get("table") or "plc_readings").strip() or "plc_readings"
        url = self._sqlite_url_from_path(sqlite_path)
        engine = None
        try:
            engine = create_engine(url, pool_pre_ping=True)
            with engine.begin() as conn:
                conn.execute(
                    text(
                        f"""
                        CREATE TABLE IF NOT EXISTS "{table}" (
                          id INTEGER PRIMARY KEY AUTOINCREMENT,
                          ts_utc TEXT NOT NULL DEFAULT (datetime('now')),
                          tag_name TEXT NOT NULL,
                          value REAL NULL,
                          quality INTEGER NULL,
                          source TEXT NULL,
                          site TEXT NULL,
                          area TEXT NULL,
                          equipment TEXT NULL,
                          seq INTEGER NULL,
                          raw_payload TEXT NULL
                        )
                        """
                    )
                )
                conn.execute(
                    text(
                        f"""
                        INSERT INTO "{table}"
                        (ts_utc, tag_name, value, quality, source, site, area, equipment, raw_payload)
                        VALUES (:ts_utc, :tag_name, :value, :quality, :source, :site, :area, :equipment, :raw_payload)
                        """
                    ),
                    [
                        {
                            "ts_utc": r.ts_utc,
                            "tag_name": r.tag_name,
                            "value": r.value,
                            "quality": r.quality,
                            "source": r.source,
                            "site": r.site,
                            "area": r.area,
                            "equipment": r.equipment,
                            "raw_payload": json.dumps(r.model_dump()),
                        }
                        for r in readings
                    ],
                )
            return True
        except Exception:
            return False
        finally:
            if engine is not None:
                try:
                    engine.dispose()
                except Exception:
                    pass

    def _schedule_remote_flush(self, engine_name: str) -> None:
        now_mono = time.monotonic()
        with self._remote_flush_lock:
            if self._remote_flush_inflight:
                return
            if now_mono - self._remote_last_flush_started_monotonic < self._remote_flush_min_interval_seconds:
                return
            self._remote_flush_inflight = True
            self._remote_last_flush_started_monotonic = now_mono
        thread = threading.Thread(
            target=self._flush_remote_outbox_once,
            args=(engine_name,),
            daemon=True,
            name=f"tn-flush-{self.gateway_id}",
        )
        thread.start()

    def _flush_remote_outbox_once(self, engine_name: str) -> None:
        schedule_again = False
        try:
            max_batches = max(1, int(os.environ.get("TRUSTNODE_REMOTE_FLUSH_MAX_BATCHES", "12") or "12"))
            batch_limit = max(25, int(os.environ.get("TRUSTNODE_REMOTE_FLUSH_BATCH_LIMIT", "120") or "120"))
            for _ in range(max_batches):
                pending = self._load_pending(batch_limit)
                if not pending:
                    break
                pending_readings = [
                    GatewayReading(
                        ts_utc=str(r.get("ts_utc") or ""),
                        tag_name=str(r.get("tag_name") or ""),
                        value=float(r.get("value") if r.get("value") is not None else 0.0),
                        quality=int(r.get("quality") if r.get("quality") is not None else 0),
                        quality_label=str(r.get("quality_label") or "UNKNOWN"),
                        source=str(r.get("source") or ""),
                        site=str(r.get("site") or ""),
                        area=str(r.get("area") or ""),
                        equipment=str(r.get("equipment") or ""),
                    )
                    for r in pending
                ]
                ok = self._persist_postgresql(pending_readings) if engine_name == "postgresql" else self._persist_legacy_http(pending_readings)
                ids = [int(r["id"]) for r in pending if r.get("id") is not None]
                if ok:
                    self._mark_sent(ids)
                else:
                    self._mark_failed(ids, self.db_last_error or "remote write failed")
                    # Avoid tight retry loops on remote outage/timeouts.
                    time.sleep(max(0.2, float(os.environ.get("TRUSTNODE_REMOTE_FLUSH_FAIL_SLEEP_SECONDS", "1.0") or "1.0")))
                    break
            self.db_pending_count = self._count_pending()
            schedule_again = self.db_pending_count > 0
        except Exception as exc:
            self._mark_db_write_error(f"Store-forward flush error: {exc}")
            try:
                self.db_pending_count = self._count_pending()
                schedule_again = self.db_pending_count > 0
            except Exception:
                pass
        finally:
            with self._remote_flush_lock:
                self._remote_flush_inflight = False
        if schedule_again and self.running:
            self._schedule_remote_flush(engine_name)

    def _resolve_output_file_path(self, raw_path: str, fallback_name: str) -> str:
        path_in = (raw_path or "").strip()
        if not path_in:
            path_in = self._default_data_file(fallback_name)
        if not os.path.isabs(path_in):
            path_in = os.path.join(self._default_data_dir(), path_in)
        full = os.path.abspath(path_in)
        parent = os.path.dirname(full)
        if parent:
            os.makedirs(parent, exist_ok=True)
        return full

    def _persist_csv_file(self, readings: List[GatewayReading]) -> bool:
        import csv

        try:
            file_path = self._resolve_output_file_path((self.db_sink or {}).get("file_path") or "", "trustnode_log.csv")
            write_header = (not os.path.exists(file_path)) or os.path.getsize(file_path) == 0
            with open(file_path, "a", encoding="utf-8", newline="") as f:
                writer = csv.writer(f)
                if write_header:
                    writer.writerow(["ts_utc", "tag_name", "value", "quality", "quality_label", "source", "site", "area", "equipment"])
                for r in readings:
                    writer.writerow([r.ts_utc, r.tag_name, r.value, r.quality, r.quality_label, r.source, r.site, r.area, r.equipment])
            self._mark_db_write_success(len(readings))
            return True
        except Exception as exc:
            self._mark_db_write_error(f"DB write failed (csv_file): {exc}")
            return False

    def _persist_txt_file(self, readings: List[GatewayReading]) -> bool:
        try:
            file_path = self._resolve_output_file_path((self.db_sink or {}).get("file_path") or "", "trustnode_log.txt")
            with open(file_path, "a", encoding="utf-8") as f:
                for r in readings:
                    f.write(
                        f"{r.ts_utc}|{r.tag_name}|{r.value}|{r.quality}|{r.quality_label}|"
                        f"{r.source}|{r.site}|{r.area}|{r.equipment}\n"
                    )
            self._mark_db_write_success(len(readings))
            return True
        except Exception as exc:
            self._mark_db_write_error(f"DB write failed (txt_file): {exc}")
            return False

    def _persist_postgresql(self, readings: List[GatewayReading]) -> bool:
        try:
            from sqlalchemy import create_engine, text
        except Exception as exc:
            self._mark_db_write_error(f"DB writer unavailable (SQLAlchemy missing): {exc}")
            return False

        host = (self.db_sink.get("host") or "").strip()
        port = int(self.db_sink.get("port") or 0)
        database = (self.db_sink.get("database") or "").strip() or "postgres"
        username = (self.db_sink.get("username") or "").strip()
        password = self.db_sink.get("password") or ""
        schema = (self.db_sink.get("schema") or "public").strip() or "public"
        table = (self.db_sink.get("table") or "plc_readings").strip() or "plc_readings"
        tls = bool(self.db_sink.get("tls", True))
        if not host or not port or not username:
            self._mark_db_write_error("DB sink postgresql is missing host/port/username")
            return False

        targets: list[tuple[str, int, str, str]] = [(host, port, username, "primary")]
        try:
            auto_pooler = str(os.environ.get("TRUSTNODE_SUPABASE_POOLER_AUTO", "1") or "1").strip().lower() in {"1", "true", "yes", "on"}
            if auto_pooler and host.startswith("db.") and host.endswith(".supabase.co") and int(port) == 5432:
                project_ref = host[len("db.") : -len(".supabase.co")].strip()
                pooler_host = str(os.environ.get("TRUSTNODE_SUPABASE_POOLER_HOST", "aws-1-eu-west-1.pooler.supabase.com") or "").strip()
                pooler_port = int(os.environ.get("TRUSTNODE_SUPABASE_POOLER_PORT", "6543") or "6543")
                pooler_user = username
                if username == "postgres" and project_ref:
                    pooler_user = f"postgres.{project_ref}"
                if pooler_host:
                    targets.append((pooler_host, pooler_port, pooler_user, "supabase_pooler"))
        except Exception:
            pass

        last_exc: Exception | None = None
        for tgt_host, tgt_port, tgt_user, tgt_kind in targets:
            url = f"postgresql+psycopg://{quote_plus(tgt_user)}:{quote_plus(password)}@{tgt_host}:{tgt_port}/{quote_plus(database)}"
            key = f"pg|{url}|{schema}|{table}|{tls}|{tgt_kind}"
            try:
                if self._db_engine is None or self._db_engine_key != key:
                    self._dispose_db_engine()
                    self._db_engine = create_engine(
                        url,
                        pool_pre_ping=True,
                        pool_recycle=60,
                        connect_args={
                            "sslmode": "require" if tls else "disable",
                            "connect_timeout": max(1, int(os.environ.get("TRUSTNODE_PG_CONNECT_TIMEOUT_SECONDS", "2") or "2")),
                            "options": os.environ.get(
                                "TRUSTNODE_PG_OPTIONS",
                                "-c statement_timeout=5000 -c lock_timeout=1500 -c idle_in_transaction_session_timeout=5000",
                            ),
                            "prepare_threshold": None,
                        },
                    )
                    self._db_engine_key = key
                if self._db_schema_ready_key != key:
                    with self._db_engine.begin() as conn:
                        if schema != "public":
                            conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
                        conn.execute(
                            text(
                                f"""
                                CREATE TABLE IF NOT EXISTS "{schema}"."{table}" (
                                  id BIGSERIAL PRIMARY KEY,
                                  ts_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                                  tag_name TEXT NOT NULL,
                                  value DOUBLE PRECISION NULL,
                                  quality INTEGER NULL,
                                  quality_label TEXT NULL,
                                  source TEXT NULL,
                                  gateway_id TEXT NULL,
                                  gateway_name TEXT NULL,
                                  device_name TEXT NULL,
                                  plc_ip TEXT NULL,
                                  database_name TEXT NULL,
                                  site TEXT NULL,
                                  area TEXT NULL,
                                  equipment TEXT NULL,
                                  tenant_id TEXT NULL,
                                  seq BIGINT NULL,
                                  raw_payload JSONB NULL,
                                  created_utc TIMESTAMPTZ NULL
                                )
                                """
                            )
                        )
                        # Keep compatibility with already-provisioned tables that were
                        # created before cloud mirror columns existed.
                        conn.execute(text(f'ALTER TABLE "{schema}"."{table}" ADD COLUMN IF NOT EXISTS quality_label TEXT'))
                        conn.execute(text(f'ALTER TABLE "{schema}"."{table}" ADD COLUMN IF NOT EXISTS gateway_id TEXT'))
                        conn.execute(text(f'ALTER TABLE "{schema}"."{table}" ADD COLUMN IF NOT EXISTS gateway_name TEXT'))
                        conn.execute(text(f'ALTER TABLE "{schema}"."{table}" ADD COLUMN IF NOT EXISTS device_name TEXT'))
                        conn.execute(text(f'ALTER TABLE "{schema}"."{table}" ADD COLUMN IF NOT EXISTS plc_ip TEXT'))
                        conn.execute(text(f'ALTER TABLE "{schema}"."{table}" ADD COLUMN IF NOT EXISTS database_name TEXT'))
                        conn.execute(text(f'ALTER TABLE "{schema}"."{table}" ADD COLUMN IF NOT EXISTS tenant_id TEXT'))
                        conn.execute(text(f'ALTER TABLE "{schema}"."{table}" ADD COLUMN IF NOT EXISTS created_utc TIMESTAMPTZ'))
                        # Keep a lightweight latest-value table hot for cloud clients.
                        conn.execute(
                            text(
                                f"""
                                CREATE TABLE IF NOT EXISTS "{schema}"."live_latest" (
                                  tenant_id TEXT NOT NULL DEFAULT 'default',
                                  gateway_id TEXT NOT NULL,
                                  tag_name TEXT NOT NULL,
                                  ts_utc TIMESTAMPTZ NOT NULL,
                                  source TEXT NULL,
                                  gateway_name TEXT NULL,
                                  device_name TEXT NULL,
                                  plc_ip TEXT NULL,
                                  database_name TEXT NULL,
                                  value DOUBLE PRECISION NULL,
                                  quality INTEGER NULL,
                                  quality_label TEXT NULL,
                                  updated_utc TIMESTAMPTZ NULL,
                                  PRIMARY KEY (tenant_id, gateway_id, tag_name)
                                )
                                """
                            )
                        )
                        conn.execute(text(f'CREATE INDEX IF NOT EXISTS "ix_live_latest_tenant_ts" ON "{schema}"."live_latest"(tenant_id, ts_utc DESC)'))
                        conn.execute(text(f'CREATE INDEX IF NOT EXISTS "ix_live_latest_ts" ON "{schema}"."live_latest"(ts_utc DESC)'))
                        self._db_schema_ready_key = key
                tenant_id = normalize_tenant_id(str(self.db_sink.get("tenant_id") or os.environ.get("TRUSTNODE_TENANT_ID") or "default"))
                db_name = str(self.db_sink.get("name") or database or "").strip()
                now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                rows = [
                    {
                        "ts_utc": r.ts_utc,
                        "tag_name": r.tag_name,
                        "value": r.value,
                        "quality": r.quality,
                        "quality_label": r.quality_label,
                        "source": r.source,
                        "gateway_id": self.gateway_id,
                        "gateway_name": self.gateway_id,
                        "device_name": "",
                        "plc_ip": self.config.plc_ip,
                        "database_name": db_name,
                        "site": r.site,
                        "area": r.area,
                        "equipment": r.equipment,
                        "tenant_id": tenant_id,
                        "created_utc": now_utc,
                        "raw_payload": json.dumps(r.model_dump()),
                    }
                    for r in readings
                ]
                latest_by_tag: dict[str, dict[str, Any]] = {}
                for row in rows:
                    tag_name = str(row.get("tag_name") or "")
                    if not tag_name:
                        continue
                    prev = latest_by_tag.get(tag_name)
                    if not prev:
                        latest_by_tag[tag_name] = row
                        continue
                    prev_ts = str(prev.get("ts_utc") or "")
                    cur_ts = str(row.get("ts_utc") or "")
                    if cur_ts >= prev_ts:
                        latest_by_tag[tag_name] = row
                live_rows = [
                    {
                        "tenant_id": str(r.get("tenant_id") or tenant_id),
                        "gateway_id": str(r.get("gateway_id") or self.gateway_id),
                        "tag_name": str(r.get("tag_name") or ""),
                        "ts_utc": str(r.get("ts_utc") or now_utc),
                        "source": str(r.get("source") or ""),
                        "gateway_name": str(r.get("gateway_name") or self.gateway_id),
                        "device_name": str(r.get("device_name") or ""),
                        "plc_ip": str(r.get("plc_ip") or self.config.plc_ip),
                        "database_name": str(r.get("database_name") or db_name),
                        "value": r.get("value"),
                        "quality": r.get("quality"),
                        "quality_label": str(r.get("quality_label") or ""),
                        "updated_utc": now_utc,
                    }
                    for r in latest_by_tag.values()
                ]
                with self._db_engine.begin() as conn:
                    conn.execute(
                        text(
                            f"""
                            INSERT INTO "{schema}"."{table}"
                            (ts_utc, tag_name, value, quality, quality_label, source, gateway_id, gateway_name, device_name, plc_ip, database_name, site, area, equipment, tenant_id, created_utc, raw_payload)
                            VALUES (CAST(:ts_utc AS timestamptz), :tag_name, :value, :quality, :quality_label, :source, :gateway_id, :gateway_name, :device_name, :plc_ip, :database_name, :site, :area, :equipment, :tenant_id, CAST(:created_utc AS timestamptz), CAST(:raw_payload AS jsonb))
                            """
                        ),
                        rows,
                    )
                    if live_rows:
                        conn.execute(
                            text(
                                f"""
                                INSERT INTO "{schema}"."live_latest"
                                (tenant_id, gateway_id, tag_name, ts_utc, source, gateway_name, device_name, plc_ip, database_name, value, quality, quality_label, updated_utc)
                                VALUES
                                (:tenant_id, :gateway_id, :tag_name, CAST(:ts_utc AS timestamptz), :source, :gateway_name, :device_name, :plc_ip, :database_name, :value, :quality, :quality_label, CAST(:updated_utc AS timestamptz))
                                ON CONFLICT(tenant_id, gateway_id, tag_name) DO UPDATE SET
                                  ts_utc = excluded.ts_utc,
                                  source = excluded.source,
                                  gateway_name = excluded.gateway_name,
                                  device_name = excluded.device_name,
                                  plc_ip = excluded.plc_ip,
                                  database_name = excluded.database_name,
                                  value = excluded.value,
                                  quality = excluded.quality,
                                  quality_label = excluded.quality_label,
                                  updated_utc = excluded.updated_utc
                                """
                            ),
                            live_rows,
                        )
                    self._mark_db_write_success(len(rows))
                    return True
            except Exception as exc:
                last_exc = exc
                # Retry once with fallback target (e.g. Supabase pooler over IPv4).
                continue

        self._mark_db_write_error(f"DB write failed (postgresql): {last_exc}")
        return False

    def _persist_sqlite(self, readings: List[GatewayReading]) -> bool:
        try:
            from sqlalchemy import create_engine, text
        except Exception as exc:
            self._mark_db_write_error(f"DB writer unavailable (SQLAlchemy missing): {exc}")
            return False
        sqlite_path = (self.db_sink.get("sqlite_path") or "./data/trustnode_edge.db").strip()
        table = (self.db_sink.get("table") or "plc_readings").strip() or "plc_readings"
        url = self._sqlite_url_from_path(sqlite_path)
        key = f"sqlite|{url}|{table}"
        try:
            if self._db_engine is None or self._db_engine_key != key:
                self._dispose_db_engine()
                self._db_engine = create_engine(url, pool_pre_ping=True)
                self._db_engine_key = key
            if self._db_schema_ready_key != key:
                with self._db_engine.begin() as conn:
                    conn.execute(
                        text(
                            f"""
                            CREATE TABLE IF NOT EXISTS "{table}" (
                              id INTEGER PRIMARY KEY AUTOINCREMENT,
                              ts_utc TEXT NOT NULL DEFAULT (datetime('now')),
                              tag_name TEXT NOT NULL,
                              value REAL NULL,
                              quality INTEGER NULL,
                              source TEXT NULL,
                              site TEXT NULL,
                              area TEXT NULL,
                              equipment TEXT NULL,
                              seq INTEGER NULL,
                              raw_payload TEXT NULL
                            )
                            """
                        )
                    )
                self._db_schema_ready_key = key
            rows = [
                {
                    "ts_utc": r.ts_utc,
                    "tag_name": r.tag_name,
                    "value": r.value,
                    "quality": r.quality,
                    "source": r.source,
                    "site": r.site,
                    "area": r.area,
                    "equipment": r.equipment,
                    "raw_payload": json.dumps(r.model_dump()),
                }
                for r in readings
            ]
            with self._db_engine.begin() as conn:
                conn.execute(
                    text(
                        f"""
                        INSERT INTO "{table}"
                        (ts_utc, tag_name, value, quality, source, site, area, equipment, raw_payload)
                        VALUES (:ts_utc, :tag_name, :value, :quality, :source, :site, :area, :equipment, :raw_payload)
                        """
                    ),
                    rows,
                )
            self._mark_db_write_success(len(rows))
            return True
        except Exception as exc:
            self._mark_db_write_error(f"DB write failed (sqlite): {exc}")
            return False

    def _persist_legacy_http(self, readings: List[GatewayReading]) -> bool:
        try:
            import requests
        except Exception as exc:
            self._mark_db_write_error(f"Legacy writer unavailable (requests missing): {exc}")
            return False
        url = (self.db_sink.get("legacy_url") or "").strip()
        token = (self.db_sink.get("legacy_api_token") or "").strip()
        if not url or not token:
            self._mark_db_write_error("DB sink legacy_http is missing URL or API token")
            return False
        try:
            payload = {
                "readings": [r.model_dump() for r in readings],
                "source": self.db_sink.get("source") or "",
                "site": self.db_sink.get("site") or "",
                "area": self.db_sink.get("area") or "",
                "equipment": self.db_sink.get("equipment") or "",
            }
            response = requests.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json", "X-API-TOKEN": token},
                timeout=4.0,
            )
            if response.status_code not in (200, 201, 400):
                self._mark_db_write_error(f"DB write failed (legacy_http): HTTP {response.status_code}")
                return False
            self._mark_db_write_success(len(readings))
            return True
        except Exception as exc:
            self._mark_db_write_error(f"DB write failed (legacy_http): {exc}")
            return False


class PLCManager:
    def __init__(self) -> None:
        self.max_gateways = 5
        self.workers: Dict[str, GatewayWorker] = {}
        self.active_gateway_id: str | None = None
        self.legacy_config = GatewayConfig()
        self._subscribers: Set[asyncio.Queue] = set()
        self.global_collection_triggers: List[Dict[str, Any]] = []
        self.global_collection_trigger_mode: str = "any"
        self.global_live_values: Dict[str, Dict[str, Any]] = {}
        self.global_trigger_latches: Dict[str, bool] = {}
        self.global_collection_allowed: bool = True
        self.global_collection_reason: str | None = None

    def _normalize_tag(self, raw: str) -> str:
        return str(raw or "").strip().lower()

    def _compare_by_operator(self, value: float, operator: str, threshold: float) -> bool:
        if operator == "<":
            return value < threshold
        if operator == "<=":
            return value <= threshold
        if operator == ">":
            return value > threshold
        if operator == ">=":
            return value >= threshold
        return False

    def _refresh_global_triggers(self) -> None:
        merged: List[Dict[str, Any]] = []
        seen: Set[str] = set()
        keep_latches: Set[str] = set()
        mode = "any"
        for gid, w in self.workers.items():
            m = str(getattr(w.config, "collection_trigger_mode", "any") or "any").strip().lower()
            if m in ("any", "all"):
                mode = m
            for tr in (w.config.collection_triggers or []):
                if not bool(tr.get("enabled", True)):
                    continue
                tag = self._normalize_tag(str(tr.get("tag_name") or ""))
                if not tag:
                    continue
                trig_gid = str(tr.get("gateway_id") or gid)
                op = str(tr.get("operator") or ">=").strip()
                try:
                    val = float(tr.get("value"))
                except Exception:
                    continue
                trigger_type = str(tr.get("trigger_type") or "continuous").strip().lower()
                if trigger_type not in ("continuous", "one_time"):
                    trigger_type = "continuous"
                key = f"{trig_gid}|{tag}|{op}|{val}|{trigger_type}"
                if key in seen:
                    continue
                seen.add(key)
                keep_latches.add(key)
                merged.append(
                    {
                        "gateway_id": trig_gid,
                        "tag_name": tag,
                        "operator": op,
                        "value": val,
                        "trigger_type": trigger_type,
                        "trigger_key": key,
                        "enabled": True,
                    }
                )
        self.global_collection_triggers = merged
        self.global_collection_trigger_mode = mode
        # Drop stale latches that do not belong to current trigger set.
        self.global_trigger_latches = {k: v for k, v in self.global_trigger_latches.items() if k in keep_latches}
        # Recompute gate immediately when trigger set changes.
        self._evaluate_global_collection_gate("", [])

    def _clear_gateway_live_values(self, gateway_id: str) -> None:
        gid = str(gateway_id or "").strip()
        if not gid:
            return
        prefix = f"{gid}::"
        self.global_live_values = {k: v for k, v in self.global_live_values.items() if not k.startswith(prefix)}

    def _evaluate_global_collection_gate(
        self, gateway_id: str, readings: List[GatewayReading]
    ) -> tuple[bool, str | None]:
        now_epoch = time.time()
        for r in readings or []:
            tag = self._normalize_tag(r.tag_name)
            if not tag:
                continue
            self.global_live_values[f"{gateway_id}::{tag}"] = {"value": float(r.value), "ts_epoch": now_epoch}

        triggers = [t for t in self.global_collection_triggers if bool(t.get("enabled", True))]
        if not triggers:
            self.global_collection_allowed = True
            self.global_collection_reason = None
            return True, None

        mode = str(self.global_collection_trigger_mode or "any").lower()
        if mode not in ("any", "all"):
            mode = "any"
        evaluated = 0
        satisfied = 0
        for tr in triggers:
            trig_gid = str(tr.get("gateway_id") or "").strip()
            tag = self._normalize_tag(str(tr.get("tag_name") or ""))
            if not tag:
                continue
            value = None
            value_ts = None
            if trig_gid:
                entry = self.global_live_values.get(f"{trig_gid}::{tag}")
                if isinstance(entry, dict):
                    value = entry.get("value")
                    value_ts = entry.get("ts_epoch")
            else:
                for k, entry in self.global_live_values.items():
                    if k.endswith(f"::{tag}"):
                        if isinstance(entry, dict):
                            value = entry.get("value")
                            value_ts = entry.get("ts_epoch")
                        break
            if value is None:
                continue
            worker = self.workers.get(trig_gid) if trig_gid else None
            interval_ms = max(200, int(getattr(worker.config, "interval_ms", 1000) if worker else 1000))
            stale_after_sec = max(5.0, (interval_ms / 1000.0) * 4.0)
            if value_ts is None or (now_epoch - float(value_ts)) > stale_after_sec:
                continue
            evaluated += 1
            cur_ok = self._compare_by_operator(float(value), str(tr.get("operator") or ">=").strip(), float(tr.get("value")))
            trigger_type = str(tr.get("trigger_type") or "continuous").strip().lower()
            trigger_key = str(tr.get("trigger_key") or f"{trig_gid}|{tag}|{tr.get('operator')}|{tr.get('value')}|continuous")
            if trigger_type == "one_time":
                was_true = bool(self.global_trigger_latches.get(trigger_key, False))
                fired = bool(cur_ok and not was_true)
                self.global_trigger_latches[trigger_key] = bool(cur_ok)
                if fired:
                    satisfied += 1
            elif cur_ok:
                satisfied += 1

        if evaluated == 0:
            self.global_collection_allowed = False
            self.global_collection_reason = "Global trigger tags not yet available (collection/write paused)."
            return False, self.global_collection_reason

        if mode == "all":
            allowed = satisfied == evaluated
            reason = None if allowed else f"Global trigger mode ALL not satisfied ({satisfied}/{evaluated})."
        else:
            allowed = satisfied > 0
            reason = None if allowed else f"Global trigger mode ANY not satisfied (0/{evaluated})."

        self.global_collection_allowed = allowed
        self.global_collection_reason = reason if not allowed else None
        return allowed, self.global_collection_reason

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    async def _broadcast(self, message: Dict[str, Any]) -> None:
        # Persist historian at backend-side so collection does not depend on UI websocket state.
        try:
            if (
                isinstance(message, dict)
                and message.get("type") == "reading"
                and message.get("collection_allowed") is not False
                and bool(message.get("persisted_local"))
                and isinstance(message.get("readings"), list)
            ):
                from app.state import app_store  # local import to avoid circular import timing

                gateway_id = str(message.get("gateway_id") or "")
                worker = self.workers.get(gateway_id)
                db_name = ""
                try:
                    db_name = str((worker.db_sink or {}).get("name") or "")
                except Exception:
                    db_name = ""
                rows = []
                for r in message.get("readings") or []:
                    if not isinstance(r, dict):
                        continue
                    rows.append(
                        {
                            "ts_utc": str(r.get("ts_utc") or datetime.now(timezone.utc).isoformat()),
                            "source": str(r.get("source") or ""),
                            "gateway_id": gateway_id,
                            "gateway_name": gateway_id,
                            "device_name": "",
                            "plc_ip": str((worker.config.plc_ip if worker else "") or ""),
                            "database_name": db_name,
                            "tag_name": str(r.get("tag_name") or ""),
                            "value": r.get("value"),
                            "quality": r.get("quality"),
                            "quality_label": str(r.get("quality_label") or ""),
                        }
                    )
                if rows:
                    # SQLite writes can be heavy; keep event loop free.
                    await asyncio.to_thread(app_store.append_historian_rows, rows)
        except Exception:
            # Never block runtime broadcast due historian persistence error.
            pass

        dead: List[asyncio.Queue] = []
        for q in self._subscribers:
            try:
                if q.full():
                    _ = q.get_nowait()
                q.put_nowait(message)
            except Exception:
                dead.append(q)
        for q in dead:
            self._subscribers.discard(q)

    def _get_or_create_worker(
        self,
        gateway_id: str,
        config: GatewayConfig,
        db_sink: Dict[str, Any] | None,
        db_sinks: List[Dict[str, Any]] | None = None,
    ) -> GatewayWorker:
        if gateway_id in self.workers:
            w = self.workers[gateway_id]
            w.set_config(config)
            w.set_db_sink(db_sink, db_sinks)
            w.set_collection_gate_cb(self._evaluate_global_collection_gate)
            return w
        if len(self.workers) >= self.max_gateways:
            raise ValueError(f"Gateway limit reached ({self.max_gateways})")
        w = GatewayWorker(
            gateway_id=gateway_id,
            config=config,
            db_sink=db_sink,
            db_sinks=db_sinks,
            collection_gate_cb=self._evaluate_global_collection_gate,
        )
        self.workers[gateway_id] = w
        return w

    async def start_gateway(
        self,
        gateway_id: str,
        config: GatewayConfig,
        db_sink: Dict[str, Any] | None,
        db_sinks: List[Dict[str, Any]] | None = None,
    ) -> None:
        w = self._get_or_create_worker(gateway_id, config, db_sink, db_sinks)
        self._refresh_global_triggers()
        self.active_gateway_id = gateway_id
        await w.start(self._broadcast)

    async def stop_gateway(self, gateway_id: str) -> None:
        w = self.workers.get(gateway_id)
        if not w:
            return
        await w.stop()
        self._clear_gateway_live_values(gateway_id)
        self._refresh_global_triggers()

    async def stop_all_gateways(self) -> None:
        for w in list(self.workers.values()):
            await w.stop()
        self.global_live_values = {}
        self._refresh_global_triggers()

    def list_gateway_statuses(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for gid, w in self.workers.items():
            status = w.get_status().model_dump()
            status["gateway_id"] = gid
            out.append(status)
        return out

    def get_gateway_snapshot(self, gateway_id: str) -> List[GatewayReading]:
        w = self.workers.get(gateway_id)
        return w.latest_readings[:] if w else []

    # Backward compatibility (single-gateway endpoints).
    def set_config(self, new_config: GatewayConfig) -> GatewayConfig:
        self.legacy_config = new_config
        return self.legacy_config

    def get_config(self) -> GatewayConfig:
        if self.active_gateway_id and self.active_gateway_id in self.workers:
            return self.workers[self.active_gateway_id].config
        return self.legacy_config

    def get_status(self) -> GatewayStatus:
        if self.active_gateway_id and self.active_gateway_id in self.workers:
            st = self.workers[self.active_gateway_id].get_status()
            if st.running:
                return st
        for w in self.workers.values():
            st = w.get_status()
            if st.running:
                return st
        return GatewayStatus(
            running=False,
            gateway_type=self.legacy_config.gateway_type,
            plc_ip=self.legacy_config.plc_ip,
            interval_ms=self.legacy_config.interval_ms,
            tags=self.legacy_config.tags,
            last_error=None,
            db_sink_engine=None,
            db_write_count=0,
            db_last_write_utc=None,
            db_last_error=None,
            db_pending_count=0,
            collection_blocked=not self.global_collection_allowed,
            collection_block_reason=self.global_collection_reason,
        )

    def get_snapshot(self) -> List[GatewayReading]:
        if self.active_gateway_id and self.active_gateway_id in self.workers:
            return self.workers[self.active_gateway_id].latest_readings[:]
        return []

    async def start(self) -> None:
        await self.start_gateway("default", self.legacy_config, None)

    async def stop(self) -> None:
        await self.stop_all_gateways()

    def set_db_sink(self, sink: Dict[str, Any] | None) -> None:
        # Keep for legacy database activation flow.
        if "default" in self.workers:
            self.workers["default"].set_db_sink(sink)

    def get_db_sink(self) -> Dict[str, Any] | None:
        if "default" in self.workers and self.workers["default"].db_sink:
            safe = dict(self.workers["default"].db_sink)
            if "password" in safe:
                safe["password"] = "***"
            if "legacy_api_token" in safe:
                safe["legacy_api_token"] = "***"
            return safe
        return None
