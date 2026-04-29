from __future__ import annotations

import json
import os
import queue
import struct
import threading
import time
from datetime import datetime, timezone
from typing import Any

from pymodbus.client import ModbusTcpClient


REGISTER_PROFILES: dict[str, dict[str, int]] = {
    "weidmuller_em525_single_phase_basic": {
        "voltage_v": 19000,
        "current_a": 19012,
        "active_power_w": 19020,
        "power_factor": 19044,
        "frequency_hz": 19050,
        "energy_wh": 19054,
    },
    "weidmuller_em525_three_phase_basic": {
        "voltage_l1_v": 19000,
        "voltage_l2_v": 19002,
        "voltage_l3_v": 19004,
        "current_l1_a": 19012,
        "current_l2_a": 19014,
        "current_l3_a": 19016,
        "active_power_total_w": 19026,
        "power_factor_total": 19048,
        "frequency_hz": 19050,
        "energy_total_wh": 19060,
    },
    "weidmuller_em525_three_phase_extended": {
        "voltage_l1_v": 19000,
        "voltage_l2_v": 19002,
        "voltage_l3_v": 19004,
        "current_l1_a": 19012,
        "current_l2_a": 19014,
        "current_l3_a": 19016,
        "active_power_l1_w": 19020,
        "active_power_l2_w": 19022,
        "active_power_l3_w": 19024,
        "active_power_total_w": 19026,
        "power_factor_l1": 19044,
        "power_factor_l2": 19046,
        "power_factor_total": 19048,
        "frequency_hz": 19050,
        "energy_total_wh": 19060,
    },
}

PROFILE_BY_MODE: dict[str, str] = {
    "single_phase": "weidmuller_em525_single_phase_basic",
    "three_phase": "weidmuller_em525_three_phase_basic",
}

DEFAULT_PROFILE = PROFILE_BY_MODE["single_phase"]
DEFAULT_REGISTERS: dict[str, int] = dict(REGISTER_PROFILES[DEFAULT_PROFILE])


DEFAULT_DEVICE: dict[str, Any] = {
    "id": "power_meter_01",
    "name": "Power Meter 01",
    "description": "Weidmuller meter",
    "enabled": False,
    "type": "modbus_tcp",
    "protocol": "modbus_tcp",
    "ip": "192.168.10.117",
    "port": 502,
    "unit_id": 1,
    "poll_interval_ms": 1000,
    "electrical_mode": "single_phase",
    "register_profile": DEFAULT_PROFILE,
    "use_custom_registers": False,
    "wiring_type": "single_phase",
    "voltage_connected": True,
    "ct_connected": True,
    "ct_primary": 80.0,
    "ct_secondary": 5.0,
    "vt_primary": 230.0,
    "vt_secondary": 230.0,
    "registers": DEFAULT_REGISTERS,
    "register_scales": {k: 1.0 for k in DEFAULT_REGISTERS.keys()},
    "include_raw_tags": False,
}


DEFAULT_POWER_CONFIG: dict[str, Any] = {
    "enabled": False,
    "selected_device_id": "power_meter_01",
    "devices": [DEFAULT_DEVICE],
}


class PowerManager:
    def __init__(self, app_store: Any) -> None:
        self._app_store = app_store
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="tn-power-manager")
        self._writer_stop = threading.Event()
        self._writer_thread = threading.Thread(target=self._writer_loop, daemon=True, name="tn-power-writer")
        self._config: dict[str, Any] = self._load_config()
        # Manual-start safety default: keep power gateways stopped after app boot.
        # Existing deployments can opt back into auto-start with TRUSTNODE_POWER_AUTO_START=1.
        if str(os.environ.get("TRUSTNODE_POWER_AUTO_START", "0") or "0").strip().lower() not in {"1", "true", "yes", "on"}:
            self._config = self._force_stopped_config(self._config)
            try:
                self._app_store.upsert_domain("power_management_config", self._config, actor="system")
            except Exception:
                pass
        self._clients: dict[str, ModbusTcpClient] = {}
        self._last_samples: dict[str, dict[str, Any]] = {}
        self._status_by_device: dict[str, dict[str, Any]] = {}
        self._register_backoff_until: dict[str, dict[int, float]] = {}
        self._rows_queue: queue.Queue[list[dict[str, Any]]] = queue.Queue(
            maxsize=max(50, int(os.environ.get("TRUSTNODE_POWER_ROWS_QUEUE_MAX", "1000") or "1000"))
        )
        self._worker_threads: dict[str, threading.Thread] = {}
        self._worker_stops: dict[str, threading.Event] = {}
        self._metrics_by_device: dict[str, dict[str, Any]] = {}
        self._dropped_rows = 0
        self._writer_batches = 0
        self._thread.start()
        self._writer_thread.start()

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _deep_copy(obj: Any) -> Any:
        return json.loads(json.dumps(obj))

    def shutdown(self) -> None:
        self._stop.set()
        self._writer_stop.set()
        with self._lock:
            for ev in self._worker_stops.values():
                ev.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2.0)
        if self._writer_thread.is_alive():
            self._writer_thread.join(timeout=2.0)
        with self._lock:
            workers = list(self._worker_threads.values())
        for t in workers:
            if t.is_alive():
                t.join(timeout=1.5)
        with self._lock:
            for client in self._clients.values():
                try:
                    client.close()
                except Exception:
                    pass
            self._clients = {}

    def _normalize_device(self, raw: Any) -> dict[str, Any]:
        base = self._deep_copy(DEFAULT_DEVICE)
        if not isinstance(raw, dict):
            return base
        base["id"] = str(raw.get("id") or base["id"]).strip() or base["id"]
        base["name"] = str(raw.get("name") or base["name"]).strip() or base["id"]
        base["description"] = str(raw.get("description") or "")
        base["enabled"] = bool(raw.get("enabled", base["enabled"]))
        base["type"] = str(raw.get("type") or raw.get("protocol") or "modbus_tcp").strip().lower() or "modbus_tcp"
        base["protocol"] = base["type"]
        base["ip"] = str(raw.get("ip") or base["ip"]).strip() or base["ip"]
        base["port"] = int(raw.get("port") or base["port"])
        base["unit_id"] = int(raw.get("unit_id") or base["unit_id"])
        base["poll_interval_ms"] = max(250, int(raw.get("poll_interval_ms") or base["poll_interval_ms"]))
        mode = str(raw.get("electrical_mode") or raw.get("wiring_type") or base["electrical_mode"]).strip().lower()
        if mode in {"three_phase_3w", "three_phase_4w"}:
            mode = "three_phase"
        if mode not in {"single_phase", "three_phase"}:
            mode = "single_phase"
        base["electrical_mode"] = mode
        profile = str(raw.get("register_profile") or "").strip()
        if profile not in REGISTER_PROFILES:
            profile = PROFILE_BY_MODE.get(mode, DEFAULT_PROFILE)
        base["register_profile"] = profile
        base["wiring_type"] = mode
        base["voltage_connected"] = bool(raw.get("voltage_connected", True))
        base["ct_connected"] = bool(raw.get("ct_connected", True))
        base["ct_primary"] = float(raw.get("ct_primary") or base["ct_primary"])
        base["ct_secondary"] = max(0.0001, float(raw.get("ct_secondary") or base["ct_secondary"]))
        base["vt_primary"] = float(raw.get("vt_primary") or base["vt_primary"])
        base["vt_secondary"] = max(0.0001, float(raw.get("vt_secondary") or base["vt_secondary"]))
        base["use_custom_registers"] = bool(raw.get("use_custom_registers", False))
        resolved_registers = dict(REGISTER_PROFILES.get(base["register_profile"], DEFAULT_REGISTERS))
        regs = raw.get("registers")
        if isinstance(regs, dict):
            merged = dict(resolved_registers)
            for k, v in regs.items():
                if v is None:
                    continue
                key = str(k or "").strip()
                if not key:
                    continue
                merged[key] = int(v)
            resolved_registers = merged
            if raw.get("use_custom_registers") is None:
                base["use_custom_registers"] = True
        if base["use_custom_registers"] is False:
            resolved_registers = dict(REGISTER_PROFILES.get(base["register_profile"], DEFAULT_REGISTERS))
        base["registers"] = resolved_registers
        scale_map = {k: 1.0 for k in resolved_registers.keys()}
        raw_scales = raw.get("register_scales") if isinstance(raw, dict) else None
        if isinstance(raw_scales, dict):
            for k in resolved_registers.keys():
                parsed = float(raw_scales.get(k) or 1.0)
                scale_map[k] = parsed if parsed != 0 else 1.0
        base["register_scales"] = scale_map
        base["include_raw_tags"] = bool(raw.get("include_raw_tags", base.get("include_raw_tags", False)))
        return base

    def _normalize_config(self, raw: Any) -> dict[str, Any]:
        base = self._deep_copy(DEFAULT_POWER_CONFIG)
        if not isinstance(raw, dict):
            return base
        if "devices" not in raw and any(k in raw for k in ("device_id", "ip", "port", "unit_id")):
            compat_device = self._normalize_device(
                {
                    "id": str(raw.get("device_id") or "power_meter_01"),
                    "name": str(raw.get("device_id") or "power_meter_01"),
                    "ip": raw.get("ip"),
                    "port": raw.get("port"),
                    "unit_id": raw.get("unit_id"),
                    "poll_interval_ms": raw.get("poll_interval_ms"),
                    "ct_primary": raw.get("ct_primary"),
                    "ct_secondary": raw.get("ct_secondary"),
                    "vt_primary": raw.get("vt_primary"),
                    "vt_secondary": raw.get("vt_secondary"),
                    "registers": raw.get("registers"),
                }
            )
            return {"enabled": bool(raw.get("enabled", False)), "selected_device_id": compat_device["id"], "devices": [compat_device]}

        base["enabled"] = bool(raw.get("enabled", False))
        devices_raw = raw.get("devices")
        devices: list[dict[str, Any]] = []
        if isinstance(devices_raw, list):
            seen: set[str] = set()
            for item in devices_raw:
                d = self._normalize_device(item)
                if d["id"] in seen:
                    continue
                seen.add(d["id"])
                devices.append(d)
        if not devices:
            devices = [self._deep_copy(DEFAULT_DEVICE)]
        base["devices"] = devices
        requested_selected = str(raw.get("selected_device_id") or "").strip()
        if requested_selected and any(str(d.get("id")) == requested_selected for d in devices):
            base["selected_device_id"] = requested_selected
        else:
            base["selected_device_id"] = str(devices[0]["id"])
        return base

    def _force_stopped_config(self, cfg: dict[str, Any]) -> dict[str, Any]:
        out = self._normalize_config(cfg or {})
        out["enabled"] = False
        devices = []
        for d in list(out.get("devices") or []):
            nd = dict(d)
            nd["enabled"] = False
            devices.append(nd)
        out["devices"] = devices
        return out

    def _load_config(self) -> dict[str, Any]:
        try:
            boot = self._app_store.get_bootstrap(prefer_cloud_reads=False) or {}
            raw = boot.get("power_management_config")
            if raw is None:
                self._app_store.upsert_domain("power_management_config", DEFAULT_POWER_CONFIG, actor="system")
                return self._deep_copy(DEFAULT_POWER_CONFIG)
            return self._normalize_config(raw)
        except Exception:
            return self._deep_copy(DEFAULT_POWER_CONFIG)

    def get_config(self) -> dict[str, Any]:
        with self._lock:
            return self._deep_copy(self._config)

    def get_profiles(self) -> dict[str, Any]:
        return {
            "profiles": self._deep_copy(REGISTER_PROFILES),
            "mode_defaults": self._deep_copy(PROFILE_BY_MODE),
        }

    def update_config(self, payload: dict[str, Any], actor: str = "admin") -> dict[str, Any]:
        cfg = self._normalize_config(payload or {})
        with self._lock:
            self._config = cfg
            valid_ids = {str(d.get("id")) for d in cfg.get("devices", [])}
            for device_id in list(self._clients.keys()):
                if device_id not in valid_ids:
                    try:
                        self._clients[device_id].close()
                    except Exception:
                        pass
                    self._clients.pop(device_id, None)
        self._app_store.upsert_domain("power_management_config", cfg, actor=actor)
        return self.get_config()

    def set_device_enabled(self, device_id: str, enabled: bool, actor: str = "admin") -> dict[str, Any]:
        target_id = str(device_id or "").strip()
        if not target_id:
            return self.get_config()
        with self._lock:
            cfg = self._deep_copy(self._config)
        devices = list(cfg.get("devices") or [])
        changed = False
        for idx, device in enumerate(devices):
            if str(device.get("id") or "") != target_id:
                continue
            next_device = dict(device)
            next_device["enabled"] = bool(enabled)
            devices[idx] = next_device
            changed = True
            break
        if not changed:
            return self.get_config()
        cfg["devices"] = devices
        if bool(enabled):
            cfg["enabled"] = True
        return self.update_config(cfg, actor=actor)

    @staticmethod
    def _decode_float32_be(registers: list[int]) -> float:
        if len(registers) < 2:
            return 0.0
        packed = struct.pack(">HH", int(registers[0]) & 0xFFFF, int(registers[1]) & 0xFFFF)
        return float(struct.unpack(">f", packed)[0])

    def _get_client(self, device: dict[str, Any]) -> ModbusTcpClient:
        device_id = str(device.get("id") or "")
        client = self._clients.get(device_id)
        host = str(device.get("ip") or "")
        port = int(device.get("port") or 502)
        poll_interval_ms = max(250, int(device.get("poll_interval_ms") or 1000))
        # Keep request timeout bounded so 1s intervals are not dominated by network timeouts.
        target_timeout = max(0.15, min(0.45, (poll_interval_ms / 1000.0) * 0.45))
        if client is None:
            client = ModbusTcpClient(host=host, port=port, timeout=target_timeout)
            self._clients[device_id] = client
            return client
        same_target = str(getattr(client, "host", "") or "") == host and int(getattr(client, "port", 0) or 0) == port
        timeout_changed = abs(float(getattr(client, "timeout", target_timeout) or target_timeout) - target_timeout) > 0.05
        if not same_target:
            try:
                client.close()
            except Exception:
                pass
            client = ModbusTcpClient(host=host, port=port, timeout=target_timeout)
            self._clients[device_id] = client
        elif timeout_changed:
            try:
                client.timeout = target_timeout
            except Exception:
                pass
        return client

    def _read_block_pairs(
        self,
        client: ModbusTcpClient,
        unit_id: int,
        block_start: int,
        block_end: int,
        addr_to_keys: dict[int, list[str]],
        raw_values: dict[str, float],
        now_mono: float,
        device_backoff: dict[int, float],
        min_span: int = 8,
    ) -> None:
        count = max(2, int(block_end - block_start + 1))
        try:
            res = client.read_input_registers(address=int(block_start), count=count, slave=unit_id)
            if getattr(res, "isError", lambda: True)():
                raise RuntimeError(f"Read failed for block {block_start}:{block_end}")
            regs = list(getattr(res, "registers", []) or [])
            for addr, keys in addr_to_keys.items():
                if addr < block_start or addr > block_end:
                    continue
                off = int(addr - block_start)
                pair = regs[off : off + 2]
                if len(pair) < 2:
                    continue
                val = self._decode_float32_be(pair)
                for key in keys:
                    raw_values[key] = val
                device_backoff.pop(int(addr), None)
            return
        except Exception:
            pass

        # Fallback strategy: split failed blocks into smaller ranges.
        span = int(block_end - block_start)
        if span <= min_span:
            for addr in range(int(block_start), int(block_end) + 1):
                keys = addr_to_keys.get(addr) or []
                if not keys:
                    continue
                try:
                    res = client.read_input_registers(address=int(addr), count=2, slave=unit_id)
                    if getattr(res, "isError", lambda: True)():
                        raise RuntimeError("single read failed")
                    pair = list(getattr(res, "registers", []) or [0, 0])
                    val = self._decode_float32_be(pair)
                    for key in keys:
                        raw_values[key] = val
                    device_backoff.pop(int(addr), None)
                except Exception:
                    device_backoff[int(addr)] = now_mono + 12.0
            return

        mid = int((block_start + block_end) // 2)
        self._read_block_pairs(
            client,
            unit_id,
            block_start,
            mid,
            addr_to_keys,
            raw_values,
            now_mono,
            device_backoff,
            min_span=min_span,
        )
        self._read_block_pairs(
            client,
            unit_id,
            mid + 1,
            block_end,
            addr_to_keys,
            raw_values,
            now_mono,
            device_backoff,
            min_span=min_span,
        )

    def _poll_device(self, device: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
        device_id = str(device.get("id") or "")
        unit_id = int(device.get("unit_id") or 1)
        registers = dict(device.get("registers") or {})
        register_scales = dict(device.get("register_scales") or {})
        client = self._get_client(device)
        now = self._utc_now()
        try:
            is_connected = bool(getattr(client, "connected", False))
        except Exception:
            is_connected = False
        if not is_connected and not client.connect():
            raise RuntimeError("Unable to connect")
        raw_values: dict[str, float] = {}
        addr_to_keys: dict[int, list[str]] = {}
        now_mono = time.monotonic()
        device_backoff = self._register_backoff_until.setdefault(device_id, {})
        for key, addr in registers.items():
            addr_int = int(addr)
            fail_until = float(device_backoff.get(addr_int, 0.0) or 0.0)
            if fail_until > now_mono:
                continue
            addr_to_keys.setdefault(addr_int, []).append(str(key))
        sorted_addrs = sorted(addr_to_keys.keys())
        blocks: list[tuple[int, int]] = []
        max_gap = max(1, int(float(os.environ.get("TRUSTNODE_POWER_BLOCK_MERGE_GAP", "16") or "16")))
        max_span = max(8, int(float(os.environ.get("TRUSTNODE_POWER_BLOCK_MAX_SPAN", "120") or "120")))
        if sorted_addrs:
            start = sorted_addrs[0]
            end = start + 1
            for addr in sorted_addrs[1:]:
                # Merge near/contiguous ranges to reduce Modbus round-trips.
                if addr <= end + max_gap and (addr - start) <= max_span:
                    end = max(end, addr + 1)
                    continue
                blocks.append((start, end))
                start = addr
                end = addr + 1
            blocks.append((start, end))
        for block_start, block_end in blocks:
            self._read_block_pairs(
                client=client,
                unit_id=unit_id,
                block_start=block_start,
                block_end=block_end,
                addr_to_keys=addr_to_keys,
                raw_values=raw_values,
                now_mono=now_mono,
                device_backoff=device_backoff,
                min_span=max(4, int(float(os.environ.get("TRUSTNODE_POWER_BLOCK_MIN_SPAN", "8") or "8"))),
            )
        if not raw_values:
            raise RuntimeError("Read failed for all configured registers")

        ct_ratio = 1.0
        vt_ratio = 1.0
        if bool(device.get("ct_connected", True)):
            ct_ratio = float(device.get("ct_primary") or 1.0) / max(float(device.get("ct_secondary") or 1.0), 0.0001)
        if bool(device.get("voltage_connected", True)):
            vt_ratio = float(device.get("vt_primary") or 1.0) / max(float(device.get("vt_secondary") or 1.0), 0.0001)
        ratio = ct_ratio * vt_ratio

        values_scaled: dict[str, float] = {}
        for key in list(raw_values.keys()):
            low = key.lower()
            value = raw_values[key]
            if "current" in low:
                value = value * ct_ratio
            elif "voltage" in low:
                value = value * vt_ratio
            elif "power" in low or "energy" in low:
                value = value * ratio
            reg_scale = float(register_scales.get(key) or 1.0)
            if reg_scale == 0:
                reg_scale = 1.0
            value = value / reg_scale
            values_scaled[key] = value

        sample = {
            "ts": now,
            "device": device_id,
            "values": values_scaled,
            "values_scaled": values_scaled,
            "values_raw": raw_values,
        }

        rows: list[dict[str, Any]] = []
        for key, val in values_scaled.items():
            rows.append(
                {
                    "ts_utc": now,
                    "gateway_id": device_id,
                    "gateway_name": str(device.get("name") or device_id),
                    "device_name": str(device.get("name") or device_id),
                    "plc_ip": str(device.get("ip") or ""),
                    "database_name": "Power Management",
                    "tag_name": str(key),
                    "value": float(val),
                    "quality": 192,
                    "quality_label": "GOOD",
                    "source": "power_modbus",
                }
            )
            raw_val = raw_values.get(key)
            if bool(device.get("include_raw_tags", False)) and raw_val is not None:
                rows.append(
                    {
                        "ts_utc": now,
                        "gateway_id": device_id,
                        "gateway_name": str(device.get("name") or device_id),
                        "device_name": str(device.get("name") or device_id),
                        "plc_ip": str(device.get("ip") or ""),
                        "database_name": "Power Management",
                        "tag_name": f"{str(key)}_raw",
                        "value": float(raw_val),
                        "quality": 192,
                        "quality_label": "GOOD",
                        "source": "power_modbus",
                    }
                )
        status = {
            "device_id": device_id,
            "name": str(device.get("name") or device_id),
            "connected": True,
            "enabled": bool(device.get("enabled", True)),
            "last_error": "",
            "last_poll_utc": now,
            "last_success_utc": now,
            "ip": str(device.get("ip") or ""),
            "port": int(device.get("port") or 502),
            "unit_id": int(device.get("unit_id") or 1),
            "poll_interval_ms": int(device.get("poll_interval_ms") or 1000),
        }
        return sample, rows, status

    def _mark_device_error(self, device: dict[str, Any], err: str) -> None:
        device_id = str(device.get("id") or "")
        now = self._utc_now()
        with self._lock:
            prev = dict(self._status_by_device.get(device_id) or {})
            self._status_by_device[device_id] = {
                "device_id": device_id,
                "name": str(device.get("name") or device_id),
                "connected": False,
                "enabled": bool(device.get("enabled", True)),
                "last_error": err,
                "last_poll_utc": now,
                "last_success_utc": str(prev.get("last_success_utc") or ""),
                "ip": str(device.get("ip") or ""),
                "port": int(device.get("port") or 502),
                "unit_id": int(device.get("unit_id") or 1),
                "poll_interval_ms": int(device.get("poll_interval_ms") or 1000),
            }

    def _enqueue_rows(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        try:
            self._rows_queue.put_nowait(rows)
        except queue.Full:
            try:
                dropped = self._rows_queue.get_nowait()
                self._dropped_rows += len(dropped or [])
            except Exception:
                pass
            try:
                self._rows_queue.put_nowait(rows)
            except Exception:
                self._dropped_rows += len(rows or [])

    def _writer_loop(self) -> None:
        max_batch_rows = max(100, int(os.environ.get("TRUSTNODE_POWER_WRITER_MAX_ROWS", "4000") or "4000"))
        flush_wait_s = max(0.02, float(os.environ.get("TRUSTNODE_POWER_WRITER_FLUSH_WAIT_SECONDS", "0.08") or "0.08"))
        while not self._writer_stop.is_set():
            batch: list[dict[str, Any]] = []
            try:
                first = self._rows_queue.get(timeout=0.1)
                if first:
                    batch.extend(first)
            except queue.Empty:
                continue
            deadline = time.monotonic() + flush_wait_s
            while len(batch) < max_batch_rows:
                remaining = max(0.0, deadline - time.monotonic())
                if remaining <= 0:
                    break
                try:
                    more = self._rows_queue.get(timeout=remaining)
                    if more:
                        batch.extend(more)
                except queue.Empty:
                    break
            if not batch:
                continue
            try:
                self._app_store.append_historian_rows(batch)
                self._writer_batches += 1
            except Exception:
                # Keep runtime resilient; failed write gets retried by requeueing once.
                try:
                    self._rows_queue.put_nowait(batch)
                except Exception:
                    self._dropped_rows += len(batch)
                time.sleep(0.05)

    def _run_device_loop(self, device_id: str, stop_evt: threading.Event) -> None:
        next_due = time.monotonic()
        prev_start = 0.0
        while not self._stop.is_set() and not stop_evt.is_set():
            cfg = self.get_config()
            device = next((dict(d) for d in (cfg.get("devices") or []) if str(d.get("id") or "") == device_id), None)
            if not device or not bool(cfg.get("enabled", True)) or not bool(device.get("enabled", True)):
                time.sleep(0.2)
                next_due = time.monotonic()
                continue

            interval_s = max(0.25, float(device.get("poll_interval_ms") or 1000) / 1000.0)
            wait_s = max(0.0, next_due - time.monotonic())
            if stop_evt.wait(wait_s):
                break

            scheduled_at = next_due
            started = time.monotonic()
            lag_ms = max(0.0, (started - scheduled_at) * 1000.0)
            skipped_cycles = 0
            try:
                sample, rows, status = self._poll_device(device)
                self._enqueue_rows(rows)
                with self._lock:
                    self._last_samples[device_id] = sample
                    self._status_by_device[device_id] = status
            except Exception as exc:
                self._mark_device_error(device, str(exc))
                try:
                    self._app_store.append_log_rows(
                        [
                            {
                                "ts_utc": self._utc_now(),
                                "level": "warning",
                                "category": "power_management",
                                "message": f"Power meter {device_id} read failed: {str(exc)}",
                                "gateway_id": device_id,
                                "gateway_name": str(device.get("name") or device_id),
                                "device_name": str(device.get("name") or device_id),
                                "database_name": "Power Management",
                            }
                        ]
                    )
                except Exception:
                    pass
            finished = time.monotonic()
            duration_ms = max(0.0, (finished - started) * 1000.0)
            effective_interval_ms = None
            if prev_start > 0.0:
                effective_interval_ms = max(0.0, (started - prev_start) * 1000.0)
            prev_start = started

            next_due = scheduled_at + interval_s
            while next_due <= finished:
                next_due += interval_s
                skipped_cycles += 1
            with self._lock:
                self._metrics_by_device[device_id] = {
                    "poll_duration_ms": round(duration_ms, 2),
                    "schedule_lag_ms": round(lag_ms, 2),
                    "effective_interval_ms": round(float(effective_interval_ms), 2) if effective_interval_ms is not None else None,
                    "skipped_cycles": int(skipped_cycles),
                    "writer_queue_depth": int(self._rows_queue.qsize()),
                    "writer_dropped_rows": int(self._dropped_rows),
                    "writer_batches": int(self._writer_batches),
                    "updated_utc": self._utc_now(),
                }

    def _reconcile_workers(self) -> None:
        cfg = self.get_config()
        desired_ids = {
            str(d.get("id") or "")
            for d in (cfg.get("devices") or [])
            if str(d.get("id") or "").strip() and bool(d.get("enabled", True)) and bool(cfg.get("enabled", True))
        }
        with self._lock:
            current_ids = set(self._worker_threads.keys())

        for gid in sorted(current_ids - desired_ids):
            with self._lock:
                ev = self._worker_stops.pop(gid, None)
                th = self._worker_threads.pop(gid, None)
            if ev:
                ev.set()
            if th and th.is_alive():
                th.join(timeout=0.8)

        for gid in sorted(desired_ids - current_ids):
            ev = threading.Event()
            th = threading.Thread(target=self._run_device_loop, args=(gid, ev), daemon=True, name=f"tn-power-{gid}")
            with self._lock:
                self._worker_stops[gid] = ev
                self._worker_threads[gid] = th
            th.start()

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._reconcile_workers()
            except Exception:
                pass
            time.sleep(0.2)

    def get_status(self) -> dict[str, Any]:
        cfg = self.get_config()
        selected_id = str(cfg.get("selected_device_id") or "")
        with self._lock:
            devices_out: list[dict[str, Any]] = []
            for d in cfg.get("devices") or []:
                did = str(d.get("id") or "")
                st = dict(self._status_by_device.get(did) or {})
                metrics = dict(self._metrics_by_device.get(did) or {})
                if not st:
                    st = {
                        "device_id": did,
                        "name": str(d.get("name") or did),
                        "connected": False,
                        "enabled": bool(d.get("enabled", True)),
                        "last_error": "",
                        "last_poll_utc": "",
                        "last_success_utc": "",
                        "ip": str(d.get("ip") or ""),
                        "port": int(d.get("port") or 502),
                        "unit_id": int(d.get("unit_id") or 1),
                        "poll_interval_ms": int(d.get("poll_interval_ms") or 1000),
                    }
                st.update(
                    {
                        "poll_duration_ms": metrics.get("poll_duration_ms"),
                        "effective_interval_ms": metrics.get("effective_interval_ms"),
                        "schedule_lag_ms": metrics.get("schedule_lag_ms"),
                        "skipped_cycles": int(metrics.get("skipped_cycles") or 0),
                        "writer_queue_depth": int(metrics.get("writer_queue_depth") or self._rows_queue.qsize()),
                        "writer_dropped_rows": int(metrics.get("writer_dropped_rows") or self._dropped_rows),
                    }
                )
                devices_out.append(st)
            selected_status = next((x for x in devices_out if str(x.get("device_id")) == selected_id), None)
            any_connected = any(bool(x.get("connected")) for x in devices_out)
            return {
                "enabled": bool(cfg.get("enabled", True)),
                "selected_device_id": selected_id,
                "connected": bool(selected_status.get("connected")) if selected_status else any_connected,
                "last_error": str(selected_status.get("last_error") or "") if selected_status else "",
                "devices": devices_out,
            }

    def get_latest(self, device_id: str | None = None) -> dict[str, Any]:
        cfg = self.get_config()
        selected_id = str(device_id or cfg.get("selected_device_id") or "")
        with self._lock:
            if selected_id:
                return self._deep_copy(self._last_samples.get(selected_id) or {})
            if not self._last_samples:
                return {}
            latest = max(self._last_samples.values(), key=lambda x: str(x.get("ts") or ""))
            return self._deep_copy(latest)

    def get_diagnostics(self) -> dict[str, Any]:
        cfg = self.get_config()
        with self._lock:
            metrics = self._deep_copy(self._metrics_by_device)
            statuses = self._deep_copy(self._status_by_device)
            worker_ids = list(self._worker_threads.keys())
        return {
            "enabled": bool(cfg.get("enabled", True)),
            "selected_device_id": str(cfg.get("selected_device_id") or ""),
            "worker_count": len(worker_ids),
            "worker_ids": worker_ids,
            "writer_queue_depth": int(self._rows_queue.qsize()),
            "writer_dropped_rows": int(self._dropped_rows),
            "writer_batches": int(self._writer_batches),
            "devices_metrics": metrics,
            "devices_status": statuses,
        }

    def test_connection(self, payload: dict[str, Any] | None = None, timeout_s: float = 3.0) -> dict[str, Any]:
        target = self._normalize_device(payload or self._deep_copy(DEFAULT_DEVICE))
        client = ModbusTcpClient(
            host=str(target.get("ip") or ""),
            port=int(target.get("port") or 502),
            timeout=max(0.5, float(timeout_s)),
        )
        try:
            if not client.connect():
                return {"ok": False, "message": "Unable to connect to Modbus TCP endpoint"}
            regs = dict(target.get("registers") or {})
            reg_scales = dict(target.get("register_scales") or {})
            if not regs:
                return {"ok": False, "message": "No register map configured"}
            unit_id = int(target.get("unit_id") or 1)
            tested_at = self._utc_now()
            register_results: dict[str, Any] = {}
            values_raw: dict[str, float] = {}
            for key, addr in regs.items():
                reg_key = str(key)
                reg_addr = int(addr)
                try:
                    res = client.read_input_registers(address=reg_addr, count=2, slave=unit_id)
                    if getattr(res, "isError", lambda: True)():
                        register_results[reg_key] = {
                            "ok": False,
                            "address": reg_addr,
                            "error": f"Read failed at register {reg_addr}",
                        }
                        continue
                    raw_val = self._decode_float32_be(list(getattr(res, "registers", []) or [0, 0]))
                    values_raw[reg_key] = raw_val
                    register_results[reg_key] = {
                        "ok": True,
                        "address": reg_addr,
                        "value_raw": raw_val,
                    }
                except Exception as exc:
                    register_results[reg_key] = {
                        "ok": False,
                        "address": reg_addr,
                        "error": str(exc),
                    }

            ct_ratio = 1.0
            vt_ratio = 1.0
            if bool(target.get("ct_connected", True)):
                ct_ratio = float(target.get("ct_primary") or 1.0) / max(float(target.get("ct_secondary") or 1.0), 0.0001)
            if bool(target.get("voltage_connected", True)):
                vt_ratio = float(target.get("vt_primary") or 1.0) / max(float(target.get("vt_secondary") or 1.0), 0.0001)
            ratio = ct_ratio * vt_ratio

            values_scaled: dict[str, float] = {}
            for key in list(values_raw.keys()):
                low = key.lower()
                value = values_raw[key]
                if "current" in low:
                    value = value * ct_ratio
                elif "voltage" in low:
                    value = value * vt_ratio
                elif "power" in low or "energy" in low:
                    value = value * ratio
                reg_scale = float(reg_scales.get(key) or 1.0)
                if reg_scale == 0:
                    reg_scale = 1.0
                value = value / reg_scale
                values_scaled[key] = value
                result_row = register_results.get(key) or {}
                if bool(result_row.get("ok")):
                    result_row["value_scaled"] = value
                    register_results[key] = result_row

            success_count = sum(1 for row in register_results.values() if bool(row.get("ok")))
            fail_count = sum(1 for row in register_results.values() if not bool(row.get("ok")))
            ok = fail_count == 0 and success_count > 0
            message = (
                "Connection and register read successful"
                if ok
                else f"Connection successful, register read errors: {fail_count} failed / {success_count} passed"
            )
            return {
                "ok": ok,
                "message": message,
                "tested_at_utc": tested_at,
                "sample": values_scaled,
                "sample_raw": values_raw,
                "register_results": register_results,
            }
        except Exception as exc:
            return {"ok": False, "message": str(exc)}
        finally:
            try:
                client.close()
            except Exception:
                pass
