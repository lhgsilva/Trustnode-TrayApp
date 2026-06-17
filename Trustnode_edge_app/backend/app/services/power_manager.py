from __future__ import annotations

import json
import logging
import os
import queue
import struct
import threading
import time

logger = logging.getLogger(__name__)
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
    # Optional database connection id — matches PLC gateway behaviour. The
    # local app-store historian is ALWAYS written (so the dashboard and
    # reports stay live); this picks ADDITIONAL sinks (CSV/TXT/SQLite/etc.)
    # to mirror the data into, exactly like the PLC db_sinks pipeline.
    "database_id": "",
}


DEFAULT_POWER_CONFIG: dict[str, Any] = {
    "enabled": False,
    "selected_device_id": "",
    # Fresh installs start with NO power meters. Operator 2026-06-12:
    # "for every new installation the power meters is already loaded
    # automatically it should not be it." Existing deployments that
    # already persisted devices stay untouched — _normalize_config
    # passes through whatever the app store holds.
    "devices": [],
    # Electricity Tariff settings. Operator 2026-06-15: "we so we can
    # add new tarifs based on the time period, setting the time range
    # and type and description like, Flat, Peak, Flat, valley etc".
    # `energy_price_eur_kwh` is the legacy flat rate (kept for back-
    # compat); `electricity_tariffs` is the new list of per-window
    # rates evaluated by start_time/end_time (HH:MM in local time).
    "energy_price_eur_kwh": 0.25,
    "electricity_tariffs": [],
    # Downtime detection rules (added 2026-06-15). Each rule says
    # "machine is on but idling when these conditions all hold".
    # Shape: {id, name, meter_id, voltage_min_v, power_max_kw,
    # description}. The frontend evaluates rules against the live
    # historian rows and multiplies idle kWh by the tariff to
    # compute downtime energy cost.
    "downtime_rules": [],
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
        def _to_int(v: Any, fallback: int) -> int:
            try:
                if v is None or v == "":
                    return int(fallback)
                return int(float(v))
            except Exception:
                return int(fallback)

        def _to_float(v: Any, fallback: float) -> float:
            try:
                if v is None or v == "":
                    return float(fallback)
                return float(v)
            except Exception:
                return float(fallback)

        base["id"] = str(raw.get("id") or base["id"]).strip() or base["id"]
        base["name"] = str(raw.get("name") or base["name"]).strip() or base["id"]
        base["description"] = str(raw.get("description") or "")
        base["enabled"] = bool(raw.get("enabled", base["enabled"]))
        base["type"] = str(raw.get("type") or raw.get("protocol") or "modbus_tcp").strip().lower() or "modbus_tcp"
        base["protocol"] = base["type"]
        base["ip"] = str(raw.get("ip") or base["ip"]).strip() or base["ip"]
        base["port"] = _to_int(raw.get("port"), base["port"])
        base["unit_id"] = _to_int(raw.get("unit_id"), base["unit_id"])
        base["poll_interval_ms"] = max(250, _to_int(raw.get("poll_interval_ms"), base["poll_interval_ms"]))
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
        base["ct_primary"] = _to_float(raw.get("ct_primary"), base["ct_primary"])
        base["ct_secondary"] = max(0.0001, _to_float(raw.get("ct_secondary"), base["ct_secondary"]))
        base["vt_primary"] = _to_float(raw.get("vt_primary"), base["vt_primary"])
        base["vt_secondary"] = max(0.0001, _to_float(raw.get("vt_secondary"), base["vt_secondary"]))
        base["use_custom_registers"] = bool(raw.get("use_custom_registers", False))
        regs = raw.get("registers")
        # Operator 2026-06-12: "to add new registers and custom are not
        # working properly when I add still do not loaded ( make sure
        # the database doesnt have fixed values in the database in way
        # we cannot change, delete or add new ones)". The previous
        # behavior MERGED the user's registers on top of the profile
        # defaults, so removing a register would silently come back on
        # the next save. When use_custom_registers is True the user's
        # map is now authoritative — adds, edits, AND deletes survive.
        if base["use_custom_registers"] and isinstance(regs, dict):
            user_map: dict[str, int] = {}
            for k, v in regs.items():
                key = str(k or "").strip()
                if not key:
                    continue
                if v is None or v == "":
                    continue
                user_map[key] = _to_int(v, 0)
            resolved_registers = user_map
        elif isinstance(regs, dict) and raw.get("use_custom_registers") is None:
            # Legacy configs that pre-date the use_custom_registers flag:
            # if the user already stored a registers map, honor it as
            # custom (preserve behavior for existing installs).
            user_map = {}
            for k, v in regs.items():
                key = str(k or "").strip()
                if not key or v is None or v == "":
                    continue
                user_map[key] = _to_int(v, 0)
            if user_map:
                resolved_registers = user_map
                base["use_custom_registers"] = True
            else:
                resolved_registers = dict(REGISTER_PROFILES.get(base["register_profile"], DEFAULT_REGISTERS))
        else:
            resolved_registers = dict(REGISTER_PROFILES.get(base["register_profile"], DEFAULT_REGISTERS))
        base["registers"] = resolved_registers
        scale_map = {k: 1.0 for k in resolved_registers.keys()}
        raw_scales = raw.get("register_scales") if isinstance(raw, dict) else None
        if isinstance(raw_scales, dict):
            for k in resolved_registers.keys():
                parsed = _to_float(raw_scales.get(k), 1.0)
                scale_map[k] = parsed if parsed != 0 else 1.0
        base["register_scales"] = scale_map
        base["include_raw_tags"] = bool(raw.get("include_raw_tags", base.get("include_raw_tags", False)))
        # Operator-supplied register descriptions (added 2026-06-15).
        # Plain {register_key: text} map, persisted alongside the
        # address + scale maps so the UI can render meaningful labels.
        desc_map: dict[str, str] = {}
        desc_raw = raw.get("register_descriptions")
        if isinstance(desc_raw, dict):
            for k, v in desc_raw.items():
                key = str(k or "").strip()
                if not key:
                    continue
                desc_map[key] = str(v or "")
        base["register_descriptions"] = desc_map
        # Carry through the chosen database connection id. Empty string means
        # "local app-store only" (the default).
        base["database_id"] = str(raw.get("database_id") or "").strip()
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
        # Only seed the example meter when the caller didn't pass a `devices`
        # key at all (first-run / migration). If the caller explicitly sent
        # an empty list (deletion of the last device) honor that — re-adding
        # DEFAULT_DEVICE here made it impossible for operators to either
        # remove the seeded "power_meter_01" or replace it with their own
        # meter on a different IP.
        if "devices" not in (raw or {}) and not devices:
            devices = [self._deep_copy(DEFAULT_DEVICE)]
        base["devices"] = devices
        requested_selected = str(raw.get("selected_device_id") or "").strip()
        if requested_selected and any(str(d.get("id")) == requested_selected for d in devices):
            base["selected_device_id"] = requested_selected
        elif devices:
            base["selected_device_id"] = str(devices[0]["id"])
        else:
            base["selected_device_id"] = ""

        # Electricity tariff settings (added 2026-06-15). Legacy flat
        # rate stays as the safety default; the array is sanitized so
        # exotic operator input doesn't poison consumers.
        try:
            base["energy_price_eur_kwh"] = float(raw.get("energy_price_eur_kwh") or 0.0)
        except Exception:
            base["energy_price_eur_kwh"] = 0.0
        tariffs_raw = raw.get("electricity_tariffs")
        tariffs: list[dict[str, Any]] = []
        if isinstance(tariffs_raw, list):
            for t in tariffs_raw:
                if not isinstance(t, dict):
                    continue
                try:
                    rate = float(t.get("rate_eur_kwh") or 0.0)
                except Exception:
                    rate = 0.0
                row = {
                    "id": str(t.get("id") or "").strip() or f"tariff_{len(tariffs) + 1}",
                    "name": str(t.get("name") or "").strip() or "Untitled",
                    "type": str(t.get("type") or "flat").strip().lower(),
                    "rate_eur_kwh": rate,
                    "start_time": str(t.get("start_time") or "00:00").strip(),
                    "end_time": str(t.get("end_time") or "23:59").strip(),
                    "description": str(t.get("description") or ""),
                }
                if row["type"] not in {"flat", "peak", "off_peak", "valley", "shoulder"}:
                    row["type"] = "flat"
                tariffs.append(row)
        base["electricity_tariffs"] = tariffs

        # Downtime detection rules (added 2026-06-15).
        rules_raw = raw.get("downtime_rules")
        rules: list[dict[str, Any]] = []
        if isinstance(rules_raw, list):
            for r in rules_raw:
                if not isinstance(r, dict):
                    continue
                try:
                    v_min = float(r.get("voltage_min_v") or 0.0)
                except Exception:
                    v_min = 0.0
                try:
                    p_max = float(r.get("power_max_kw") or 0.0)
                except Exception:
                    p_max = 0.0
                rules.append({
                    "id": str(r.get("id") or "").strip() or f"dt_{len(rules) + 1}",
                    "name": str(r.get("name") or "").strip() or "Downtime",
                    "meter_id": str(r.get("meter_id") or "").strip(),
                    "voltage_min_v": v_min,
                    "power_max_kw": p_max,
                    "description": str(r.get("description") or ""),
                })
        base["downtime_rules"] = rules
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

        # Operator 2026-06-16: modern power meters (Weidmüller EM525,
        # Carlo Gavazzi EM340/530, Schneider iEM/PM, Janitza UMG…)
        # report PRIMARY-side values directly when the CTs are
        # configured on the meter. Multiplying by ct_primary /
        # ct_secondary in the gateway double-counts. Read whatever
        # the meter publishes and only apply the per-register
        # divider (`register_scales[key]`) when the operator needs
        # to compensate for an unusual register encoding (e.g.
        # "energy in 0.1 kWh units" → scale 10). Default is 1.0
        # i.e. pass-through.
        values_scaled: dict[str, float] = {}
        for key in list(raw_values.keys()):
            value = raw_values[key]
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
            # Operator 2026-06-15: Last Raw column was always blank
            # because include_raw_tags defaulted to False. The Power
            # Configuration register table needs both raw and scaled
            # values to populate, so emit raw rows unconditionally
            # when present. Storage cost is one extra row per
            # register per poll — acceptable for power meters which
            # ship 6-12 registers.
            if raw_val is not None:
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

        # Insight tags (operator 2026-06-15): emit synthesized KPI
        # rows so the values land in the historian AND in any
        # configured gateway DB sinks. The dashboard widget editor
        # picks them up just like real tags.
        try:
            insight_rows = self._compute_insight_rows(device, values_scaled, now)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("insight tags failed for %s: %s", device_id, exc)
            insight_rows = []
        rows.extend(insight_rows)

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

    # Per-device rolling state for insight tags. Operator 2026-06-15:
    # KPIs were a frontend overlay only — now they're emitted as real
    # historian rows so dashboards, gateway DB sinks and reports all
    # see them. We keep a small rolling window per device for peak +
    # efficiency math, plus a monotonic energy accumulator.
    def _insight_state(self, device_id: str) -> dict[str, Any]:
        st = getattr(self, "_insight_state_by_device", None)
        if st is None:
            st = {}
            self._insight_state_by_device = st
        bucket = st.get(device_id)
        if bucket is None:
            bucket = {
                "samples": [],  # list[(ts_mono, kw)]
                "energy_wh": 0.0,
                "downtime_wh": 0.0,
                "last_mono": None,
                # Operator 2026-06-16: per-tariff accumulators so the
                # dashboard can show "kWh consumed under PEAK" etc.
                # Keyed by tariff index (matches the active tariff
                # list order at integration time).
                "tariff_wh": {},      # {idx: float}
                "tariff_cost": {},    # {idx: float}
                # Operator 2026-06-16: throttle slow insight tags
                # (cumulative kWh / cost / efficiency / per-tariff
                # totals) to every Nth poll to relieve the SQLite
                # write lock. Fast insights (live_kw / current_a /
                # active_power_kw / active_tariff_index) still flush
                # every poll so dashboards stay responsive.
                "insight_skip": 0,
            }
            st[device_id] = bucket
        return bucket

    def _resolve_tariff_rate(self, ts: datetime) -> float:
        rate, _idx = self._resolve_tariff_rate_indexed(ts)
        return rate

    def _resolve_tariff_rate_indexed(self, ts: datetime) -> tuple[float, int]:
        """Like _resolve_tariff_rate but also returns the matching
        tariff index (0-based) or -1 if none matched and the flat
        fallback rate is in use. Operator 2026-06-16: dashboards need
        to know WHICH tariff is currently active.

        Operator 2026-06-17: tariff windows are entered by the
        operator in their LOCAL time (e.g. "09:00–21:20"). The
        previous implementation compared `ts.hour:ts.minute` of a UTC
        timestamp directly, so in any non-UTC zone the resolver
        silently returned -1 for the entire window and tariff cost
        stayed at zero. Convert the UTC timestamp into the machine's
        local time (matches what the UI displays) before extracting
        the wall-clock minutes-of-day.
        """
        tariffs = list(self._config.get("electricity_tariffs") or [])
        if tariffs:
            # Convert UTC → machine local. astimezone() without a
            # target zone uses the host's TZ as configured in the OS.
            try:
                local_ts = ts.astimezone()
            except Exception:
                local_ts = ts
            minutes = local_ts.hour * 60 + local_ts.minute
            for idx, t in enumerate(tariffs):
                try:
                    sh, sm = str(t.get("start_time") or "00:00").split(":")
                    eh, em = str(t.get("end_time") or "23:59").split(":")
                    start = int(sh) * 60 + int(sm)
                    end = int(eh) * 60 + int(em)
                except Exception:
                    continue
                in_window = (start <= end and start <= minutes <= end) or (start > end and (minutes >= start or minutes <= end))
                if in_window:
                    try:
                        return float(t.get("rate_eur_kwh") or 0.0), idx
                    except Exception:
                        return 0.0, idx
        try:
            return float(self._config.get("energy_price_eur_kwh") or 0.0), -1
        except Exception:
            return 0.0, -1

    def _compute_insight_rows(self, device: dict[str, Any], values: dict[str, float], now: str) -> list[dict[str, Any]]:
        device_id = str(device.get("id") or "")
        gw_name = str(device.get("name") or device_id)
        ip = str(device.get("ip") or "")
        # Live kW prefers active_power_total_w/active_power_w; fall
        # back to 0 so the tag still emits (otherwise dashboards lose
        # the series during a brief read fault).
        watts = None
        for key in ("active_power_total_w", "active_power_w"):
            v = values.get(key)
            if v is not None:
                try:
                    watts = float(v)
                    break
                except Exception:
                    continue
        live_kw = (watts or 0.0) / 1000.0

        st = self._insight_state(device_id)
        now_mono = time.monotonic()
        st["samples"].append((now_mono, live_kw))
        # Keep the last hour at most so peak/avg reflect a useful
        # window without unbounded growth.
        cutoff = now_mono - 3600.0
        st["samples"] = [(t, kw) for (t, kw) in st["samples"] if t >= cutoff]

        # Energy accumulator — integrate kW * dt(h).
        last_mono = st.get("last_mono")
        dt_h = 0.0
        if last_mono is not None:
            dt_s = max(0.0, now_mono - float(last_mono))
            # Discard gaps > 5 min so a meter offline window doesn't
            # tank or inflate the integral.
            if dt_s <= 300.0:
                dt_h = dt_s / 3600.0
                st["energy_wh"] += live_kw * 1000.0 * dt_h
        st["last_mono"] = now_mono

        peak_kw = max((kw for (_, kw) in st["samples"]), default=0.0)
        avg_kw = (sum(kw for (_, kw) in st["samples"]) / len(st["samples"])) if st["samples"] else 0.0
        efficiency_pct = max(0.0, min(100.0, (avg_kw / peak_kw) * 100.0)) if peak_kw > 0 else 0.0

        # Tariff-aware cost for the window's accumulated kWh.
        ts_now = datetime.now(timezone.utc)
        rate, active_idx = self._resolve_tariff_rate_indexed(ts_now)
        total_kwh = st["energy_wh"] / 1000.0
        total_cost = total_kwh * rate

        # Accumulate per-tariff kWh / cost so dashboards can plot
        # "consumption under PEAK" etc. Trapezoidal integration was
        # already applied above into st["energy_wh"]; here we just
        # split the increment into the active tariff bucket.
        if dt_h > 0 and active_idx >= 0:
            incr_kwh = live_kw * dt_h
            st["tariff_wh"][active_idx] = float(st["tariff_wh"].get(active_idx, 0.0)) + incr_kwh * 1000.0
            st["tariff_cost"][active_idx] = float(st["tariff_cost"].get(active_idx, 0.0)) + incr_kwh * rate

        # Downtime cost: when voltage >= min and active power <= max
        # for the matching rule, accumulate live_kw * dt(h) * rate.
        rules = list(self._config.get("downtime_rules") or [])
        if rules and dt_h > 0:
            voltage_v = float(values.get("voltage_v") or 0.0)
            for rule in rules:
                if rule.get("meter_id") and str(rule.get("meter_id")) != device_id:
                    continue
                vmin = float(rule.get("voltage_min_v") or 0.0)
                pmax = float(rule.get("power_max_kw") or 0.0)
                if voltage_v >= vmin and live_kw <= pmax:
                    st["downtime_wh"] += live_kw * 1000.0 * dt_h
                    break
        downtime_kwh = st["downtime_wh"] / 1000.0
        downtime_cost = downtime_kwh * rate

        def _row(tag: str, value: float) -> dict[str, Any]:
            return {
                "ts_utc": now,
                "gateway_id": device_id,
                "gateway_name": gw_name,
                "device_name": gw_name,
                "plc_ip": ip,
                "database_name": "Power Management",
                "tag_name": tag,
                "value": float(value),
                "quality": 192,
                "quality_label": "GOOD",
                "source": "power_insight",
            }

        # Convenience aliases for the redesigned KPI strip
        # (operator 2026-06-15). Backend was already publishing
        # live_kw / peak_kw / total_kwh / cost / efficiency /
        # downtime; the strip also asks for Current (A), Active
        # Power (kW) and a "Power Usage (kWh)" alias for the
        # rolling-window kWh integral.
        current_a = float(values.get("current_a") or values.get("current_l1_a") or 0.0)
        # Operator 2026-06-16: every insight tag is written at the
        # gateway's configured poll interval. The previous fast/slow
        # split caused dashboard charts that included an "insight.*"
        # tag with a different cadence to misalign visually with the
        # raw register tags ("gaps"). The operator's invariant is
        # explicit: "we should not have slow tags, fast tags for
        # meters, all of them should follow the gateways collection".
        rows = [
            _row("insight.live_kw", live_kw),
            _row("insight.active_power_kw", live_kw),       # alias for KPI strip
            _row("insight.current_a", current_a),
            _row("insight.active_tariff_index", float(active_idx)),
            _row("insight.power_usage_kwh", total_kwh),     # window kWh
            _row("insight.peak_kw", peak_kw),
            _row("insight.energy_efficiency_pct", efficiency_pct),
            _row("insight.total_kwh", total_kwh),
            _row("insight.energy_cost_eur", total_cost),
            _row("insight.downtime_cost_eur", downtime_cost),
            _row("insight.active_tariff_rate_eur_kwh", float(rate)),
        ]
        # Per-tariff totals — one pair of tags per configured tariff,
        # also written every poll so dashboards stay in lockstep.
        if True:
            configured = list(self._config.get("electricity_tariffs") or [])
            for idx, _t in enumerate(configured):
                kwh_i = float(st["tariff_wh"].get(idx, 0.0)) / 1000.0
                cost_i = float(st["tariff_cost"].get(idx, 0.0))
                rows.append(_row(f"insight.tariff_{idx + 1}_kwh", kwh_i))
                rows.append(_row(f"insight.tariff_{idx + 1}_cost_eur", cost_i))
        return rows

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
            # Side-channel fan-out to extra sinks chosen per device (CSV/TXT
            # mirrors etc.). Best-effort: never blocks or fails the local
            # historian write.
            try:
                self._fan_out_rows_to_device_sinks(batch)
            except Exception:
                pass
            # Operator 2026-06-17 (M4): mirror power-meter rows into
            # the customer DB when the operator has flipped
            # database_mode = customer_sql. The local SQLite remains
            # canonical for the desktop UI; the customer DB is the
            # LAN-shared store the Lite reads from.
            try:
                self._mirror_to_customer_db(batch)
            except Exception:
                # Mirror failures must never block the local writer.
                pass

            # Operator 2026-06-17 (Phase 3): publish to OPC UA / MQTT.
            try:
                self._publish_to_outbound_connections(batch)
            except Exception:
                pass

    def _publish_to_outbound_connections(self, batch: list[dict[str, Any]]) -> None:
        if not batch:
            return
        try:
            from app.services import connections_publish as cp
        except Exception:
            return
        for r in batch:
            try:
                gid = str(r.get("gateway_id") or "")
                gname = str(r.get("gateway_name") or gid)
                dname = str(r.get("device_name") or "device")
                tname = str(r.get("tag_name") or "")
                value = r.get("value")
                ts = str(r.get("ts_utc") or "")
                quality = str(r.get("quality_label") or "good")
                cp.publish_opcua(
                    gateway_id=gid, device_name=dname, tag_name=tname,
                    value=value, ts_utc=ts, quality=quality,
                )
                cp.publish_mqtt(
                    gateway_id=gid, gateway_name=gname, device_name=dname,
                    tag_name=tname, value=value, ts_utc=ts, quality=quality,
                )
            except Exception:
                pass

    def _mirror_to_customer_db(self, batch: list[dict[str, Any]]) -> None:
        """Best-effort mirror of meter rows into the customer Postgres
        (operator 2026-06-17, M4). Only runs when database_mode is
        customer_sql AND a target is configured. Schema is bootstrapped
        on first contact via sinks_sql.bootstrap_customer_db.
        """
        if not batch:
            return
        try:
            settings = self._app_store.get_bootstrap(prefer_cloud_reads=False) or {}
        except Exception:
            return
        app_settings = settings.get("app_settings") if isinstance(settings.get("app_settings"), dict) else {}
        if str(app_settings.get("database_mode") or "local_sqlite").lower() != "customer_sql":
            return
        target = app_settings.get("customer_sql_target") if isinstance(app_settings.get("customer_sql_target"), dict) else None
        if not target:
            return
        try:
            from app.services import customer_sql as _cs
            from app.services import sinks_sql as _ss
        except Exception:
            return
        engine, _err = _cs.get_engine(target)
        if engine is None:
            return
        # Re-bootstrap is cheap (cache short-circuit) but guarantees
        # the schema is present even if the DBA recreated it.
        try:
            _ss.bootstrap_customer_db(engine, schema=str(target.get("schema") or "public"), note="power_manager")
        except Exception:
            return
        # Convert the in-flight rows to the dict shape sinks_sql wants.
        rows = []
        for r in batch:
            rows.append({
                "tenant_id": str(r.get("tenant_id") or "default"),
                "ts_utc": str(r.get("ts_utc") or ""),
                "gateway_id": str(r.get("gateway_id") or ""),
                "gateway_name": str(r.get("gateway_name") or ""),
                "device_name": str(r.get("device_name") or ""),
                "plc_ip": str(r.get("plc_ip") or ""),
                "database_name": str(r.get("database_name") or ""),
                "tag_name": str(r.get("tag_name") or r.get("tag") or ""),
                "value": r.get("value"),
                "value_text": r.get("value_text"),
                "quality": r.get("quality"),
                "quality_label": str(r.get("quality_label") or ""),
                "source": str(r.get("source") or ""),
            })
        try:
            _ss.write_historian_batch(engine, rows, schema=str(target.get("schema") or "public"))
            _ss.upsert_live_latest(engine, rows, schema=str(target.get("schema") or "public"))
        except Exception:
            # Drop on the floor — the local SQLite already has the
            # rows, and the next batch will retry the mirror. A real
            # outbox for power-meter mirror lives in M4b if needed.
            return

    def _lookup_db_connection(self, db_id: str) -> dict[str, Any] | None:
        if not db_id:
            return None
        try:
            boot = self._app_store.get_bootstrap(prefer_cloud_reads=False) or {}
            rows = boot.get("database_configurations")
            if not isinstance(rows, list):
                return None
            for r in rows:
                if isinstance(r, dict) and str(r.get("id") or "") == str(db_id):
                    return r
        except Exception:
            return None
        return None

    def _fan_out_rows_to_device_sinks(self, batch: list[dict[str, Any]]) -> None:
        """Mirror power-meter rows to per-device extra sinks (CSV/TXT/SQLite).

        The local app-store historian remains the source of truth — this just
        adds optional side-channel writes so operators can ship the same data
        to a file path or an external SQLite for downstream tooling. Mirrors
        the PLC gateway fan-out behaviour so the UX is consistent.
        """
        if not batch:
            return
        # Group rows by device → look up the device's database_id once.
        rows_by_device: dict[str, list[dict[str, Any]]] = {}
        for row in batch:
            did = str((row or {}).get("gateway_id") or "")
            if not did:
                continue
            rows_by_device.setdefault(did, []).append(row)
        if not rows_by_device:
            return
        cfg = self.get_config() or {}
        devices = {str(d.get("id") or ""): d for d in (cfg.get("devices") or []) if isinstance(d, dict)}
        for did, rows in rows_by_device.items():
            device = devices.get(did) or {}
            db_id = str(device.get("database_id") or "").strip()
            if not db_id:
                continue
            sink = self._lookup_db_connection(db_id)
            if not sink:
                continue
            engine = str(sink.get("engine") or "").strip().lower()
            try:
                if engine == "csv_file":
                    self._sink_write_csv(sink, rows)
                elif engine == "txt_file":
                    self._sink_write_txt(sink, rows)
                elif engine == "sqlite":
                    self._sink_write_sqlite(sink, rows)
                # Other engines (postgres/mysql/...) are not handled by the
                # side-channel yet; the local app-store still has the data
                # and can be synced upstream via the regular cloud pipeline.
            except Exception:
                pass

    def _utc_str_to_local_iso(self, ts: str) -> str:
        raw = str(ts or "").strip()
        if not raw:
            return ""
        cand = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
            try:
                parsed = datetime.strptime(cand.split("+", 1)[0], fmt)
                return parsed.replace(tzinfo=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            except Exception:
                continue
        try:
            return datetime.fromisoformat(cand).astimezone().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        except Exception:
            return raw

    def _sink_write_csv(self, sink: dict[str, Any], rows: list[dict[str, Any]]) -> None:
        import csv as _csv

        file_path = str(sink.get("file_path") or "").strip()
        if not file_path:
            return
        new_file = not os.path.exists(file_path) or os.path.getsize(file_path) == 0
        os.makedirs(os.path.dirname(file_path) or ".", exist_ok=True)
        with open(file_path, "a", encoding="utf-8", newline="") as f:
            writer = _csv.writer(f)
            if new_file:
                writer.writerow(["ts_local", "ts_utc", "gateway_id", "gateway_name", "tag_name", "value", "quality", "quality_label", "source"])
            for r in rows:
                ts_utc = str(r.get("ts_utc") or "")
                writer.writerow([
                    self._utc_str_to_local_iso(ts_utc),
                    ts_utc,
                    str(r.get("gateway_id") or ""),
                    str(r.get("gateway_name") or ""),
                    str(r.get("tag_name") or ""),
                    r.get("value"),
                    r.get("quality"),
                    str(r.get("quality_label") or ""),
                    str(r.get("source") or ""),
                ])

    def _sink_write_txt(self, sink: dict[str, Any], rows: list[dict[str, Any]]) -> None:
        file_path = str(sink.get("file_path") or "").strip()
        if not file_path:
            return
        os.makedirs(os.path.dirname(file_path) or ".", exist_ok=True)
        with open(file_path, "a", encoding="utf-8") as f:
            for r in rows:
                ts_utc = str(r.get("ts_utc") or "")
                f.write(
                    f"{self._utc_str_to_local_iso(ts_utc)}|{ts_utc}|"
                    f"{r.get('gateway_id', '')}|{r.get('gateway_name', '')}|"
                    f"{r.get('tag_name', '')}|{r.get('value')}|{r.get('quality')}|"
                    f"{r.get('quality_label', '')}|{r.get('source', '')}\n"
                )

    def _sink_write_sqlite(self, sink: dict[str, Any], rows: list[dict[str, Any]]) -> None:
        try:
            from sqlalchemy import create_engine, text  # type: ignore
        except Exception:
            return
        sqlite_path = str(sink.get("sqlite_path") or "").strip()
        if not sqlite_path:
            return
        table = str(sink.get("table") or "power_readings").strip() or "power_readings"
        url = f"sqlite:///{sqlite_path}"
        try:
            os.makedirs(os.path.dirname(sqlite_path) or ".", exist_ok=True)
        except Exception:
            pass
        engine = create_engine(url, pool_pre_ping=True)
        try:
            with engine.begin() as conn:
                conn.execute(text(
                    f"""
                    CREATE TABLE IF NOT EXISTS "{table}" (
                      id INTEGER PRIMARY KEY AUTOINCREMENT,
                      ts_utc TEXT NOT NULL,
                      gateway_id TEXT NULL,
                      gateway_name TEXT NULL,
                      tag_name TEXT NOT NULL,
                      value REAL NULL,
                      quality INTEGER NULL,
                      quality_label TEXT NULL,
                      source TEXT NULL
                    )
                    """
                ))
                conn.execute(
                    text(
                        f"""
                        INSERT INTO "{table}"
                        (ts_utc, gateway_id, gateway_name, tag_name, value, quality, quality_label, source)
                        VALUES (:ts_utc, :gateway_id, :gateway_name, :tag_name, :value, :quality, :quality_label, :source)
                        """
                    ),
                    [
                        {
                            "ts_utc": r.get("ts_utc"),
                            "gateway_id": r.get("gateway_id"),
                            "gateway_name": r.get("gateway_name"),
                            "tag_name": r.get("tag_name"),
                            "value": r.get("value"),
                            "quality": r.get("quality"),
                            "quality_label": r.get("quality_label"),
                            "source": r.get("source"),
                        }
                        for r in rows
                    ],
                )
        finally:
            try:
                engine.dispose()
            except Exception:
                pass

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
                # Always overlay the current `enabled` flag from cfg.
                # `_status_by_device` is only refreshed by the poll loop, so
                # if the loop is sleeping (device disabled), it carries a
                # stale `enabled=true` from the last successful poll. Without
                # this overlay the UI keeps showing "Running" for up to one
                # full poll cycle (~1 s) AND for as long as the loop is in
                # the disabled-sleep state.
                st["enabled"] = bool(d.get("enabled", True))
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
        host = str(target.get("ip") or "")
        port = int(target.get("port") or 502)
        endpoint = f"{host}:{port}"
        client = ModbusTcpClient(
            host=host,
            port=port,
            timeout=max(0.5, float(timeout_s)),
        )
        try:
            # Pre-flight the TCP port directly so we can distinguish
            # "no route / wrong subnet" from "host up, port closed".
            # ModbusTcpClient.connect() returns False for both and the
            # operator had no way to tell which one applied.
            try:
                import socket as _sock
                with _sock.create_connection((host, port), timeout=max(0.5, float(timeout_s))):
                    pass
            except _sock.timeout:
                return {"ok": False, "message": f"Timeout connecting to {endpoint} (host unreachable or firewall blocking)"}
            except OSError as exc:
                # ConnectionRefused → host up, port closed.
                # Network unreachable → wrong subnet / IP typo.
                detail = str(exc) or exc.__class__.__name__
                return {"ok": False, "message": f"Cannot reach {endpoint}: {detail}"}

            if not client.connect():
                return {"ok": False, "message": f"Modbus TCP handshake failed at {endpoint}"}
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

            # CT/VT auto-scaling dropped 2026-06-16 — see comment in
            # _read_device. Pass meter values through; apply only the
            # per-register divider.
            values_scaled: dict[str, float] = {}
            for key in list(values_raw.keys()):
                value = values_raw[key]
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
