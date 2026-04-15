from app.services.power_manager import PowerManager


class _FakeAppStore:
    def __init__(self):
        self.bootstrap = {"power_management_config": {"enabled": False, "devices": []}}
        self.upserts = []
        self.historian_rows = []
        self.log_rows = []

    def get_bootstrap(self, prefer_cloud_reads=False):
        return self.bootstrap

    def upsert_domain(self, domain, payload, actor="system"):
        self.upserts.append((domain, payload, actor))
        self.bootstrap[domain] = payload
        return {"ok": True}

    def append_historian_rows(self, rows):
        self.historian_rows.extend(rows or [])

    def append_log_rows(self, rows):
        self.log_rows.extend(rows or [])


def test_power_config_normalization_and_selection():
    store = _FakeAppStore()
    mgr = PowerManager(store)
    try:
        cfg = mgr.update_config(
            {
                "enabled": True,
                "selected_device_id": "meter_b",
                "devices": [
                    {"id": "meter_a", "ip": "192.168.10.10", "wiring_type": "invalid"},
                    {"id": "meter_b", "ip": "192.168.10.11", "poll_interval_ms": 100},
                    {"id": "meter_b", "ip": "192.168.10.12"},
                ],
            },
            actor="test",
        )
        assert cfg["enabled"] is True
        assert len(cfg["devices"]) == 2
        assert cfg["selected_device_id"] == "meter_b"
        meter_a = next(d for d in cfg["devices"] if d["id"] == "meter_a")
        meter_b = next(d for d in cfg["devices"] if d["id"] == "meter_b")
        assert meter_a["wiring_type"] == "single_phase"
        assert meter_a["electrical_mode"] == "single_phase"
        assert str(meter_a["register_profile"]).startswith("weidmuller_em525_single_phase")
        assert int(meter_b["poll_interval_ms"]) >= 250
    finally:
        mgr.shutdown()


def test_power_profiles_exposed():
    store = _FakeAppStore()
    mgr = PowerManager(store)
    try:
        data = mgr.get_profiles()
        assert "profiles" in data
        assert "weidmuller_em525_single_phase_basic" in data["profiles"]
        assert "weidmuller_em525_three_phase_basic" in data["profiles"]
        assert data["mode_defaults"]["single_phase"] == "weidmuller_em525_single_phase_basic"
    finally:
        mgr.shutdown()


def test_start_stop_device_updates_enabled_flag():
    store = _FakeAppStore()
    mgr = PowerManager(store)
    try:
        cfg = mgr.update_config(
            {
                "enabled": False,
                "selected_device_id": "meter_a",
                "devices": [
                    {"id": "meter_a", "enabled": True, "ip": "192.168.10.10"},
                ],
            },
            actor="test",
        )
        assert cfg["devices"][0]["enabled"] is True
        cfg = mgr.set_device_enabled("meter_a", False, actor="test")
        assert cfg["devices"][0]["enabled"] is False
        cfg = mgr.set_device_enabled("meter_a", True, actor="test")
        assert cfg["devices"][0]["enabled"] is True
    finally:
        mgr.shutdown()


def test_register_scales_normalized_with_register_map():
    store = _FakeAppStore()
    mgr = PowerManager(store)
    try:
        cfg = mgr.update_config(
            {
                "enabled": True,
                "selected_device_id": "meter_scale",
                "devices": [
                    {
                        "id": "meter_scale",
                        "ip": "192.168.10.117",
                        "register_profile": "weidmuller_em525_single_phase_basic",
                        "use_custom_registers": True,
                        "registers": {"active_power_w": 19020, "energy_wh": 19054},
                        "register_scales": {"active_power_w": 1000, "energy_wh": 1000},
                    }
                ],
            },
            actor="test",
        )
        meter = cfg["devices"][0]
        assert meter["register_scales"]["active_power_w"] == 1000
        assert meter["register_scales"]["energy_wh"] == 1000
        # Missing keys default to 1.0 and zero scale is rejected.
        cfg2 = mgr.update_config(
            {
                "enabled": True,
                "selected_device_id": "meter_scale",
                "devices": [
                    {
                        "id": "meter_scale",
                        "ip": "192.168.10.117",
                        "use_custom_registers": True,
                        "registers": {"active_power_w": 19020},
                        "register_scales": {"active_power_w": 0},
                    }
                ],
            },
            actor="test",
        )
        assert cfg2["devices"][0]["register_scales"]["active_power_w"] == 1.0
    finally:
        mgr.shutdown()
