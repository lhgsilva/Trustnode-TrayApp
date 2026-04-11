import os
from pathlib import Path

from app.auth_device import create_device_access_token, decode_device_access_token
from app.routers.telemetry_v1 import _recompute_payload_hash
from app.services.ingest_store import IngestStore


def _sample(edge_record_id: str, seq: int, sample_ts: str):
    rec = {
        "edge_record_id": edge_record_id,
        "tenant_id": "tenant-a",
        "customer_id": "cust-a",
        "plant_id": "plant-1",
        "machine_id": "machine-1",
        "gateway_id": "gw-1",
        "collector_instance_id": "collector-1",
        "gateway_config_version": "cfg-1",
        "plc_driver_type": "allen_bradley",
        "plc_endpoint_id": "192.168.10.240",
        "sample_ts_utc": sample_ts,
        "edge_monotonic_seq": seq,
        "interval_ms": 1000,
        "tags_json": [{"tag_name": "A", "value": 1.0, "quality_code": 192, "quality_label": "GOOD"}],
        "quality_code": 192,
        "collection_status": "ok",
        "collected_at_edge_ts_utc": sample_ts,
        "time_status": "ok",
    }
    rec["payload_hash_sha256"] = _recompute_payload_hash(rec)
    return rec


def test_payload_hash_is_stable():
    rec = _sample("11111111-1111-1111-1111-111111111111", 1, "2026-04-11T10:00:00+00:00")
    h1 = _recompute_payload_hash(rec)
    h2 = _recompute_payload_hash(rec)
    assert h1 == h2


def test_ingest_duplicate_and_latest_ordering(tmp_path: Path):
    os.environ["TRUSTNODE_DATA_DIR"] = str(tmp_path)
    store = IngestStore()

    rec1 = _sample("11111111-1111-1111-1111-111111111111", 1, "2026-04-11T10:00:00+00:00")
    rec2 = _sample("22222222-2222-2222-2222-222222222222", 2, "2026-04-11T10:00:01+00:00")
    rec_old = _sample("33333333-3333-3333-3333-333333333333", 3, "2026-04-11T09:59:59+00:00")

    assert store.upsert_record(rec1, received_at_vps_ts_utc="2026-04-11T10:00:00+00:00") == "inserted"
    assert store.upsert_record(rec1, received_at_vps_ts_utc="2026-04-11T10:00:02+00:00") == "duplicate"
    assert store.upsert_record(rec2, received_at_vps_ts_utc="2026-04-11T10:00:03+00:00") == "inserted"
    assert store.upsert_record(rec_old, received_at_vps_ts_utc="2026-04-11T10:00:04+00:00") == "inserted"

    latest = store.query_latest("tenant-a", limit=1)[0]
    assert latest["sample_ts_utc"].startswith("2026-04-11T10:00:01")
    assert int(latest["edge_monotonic_seq"]) == 2


def test_device_token_scope_roundtrip(monkeypatch):
    monkeypatch.setenv("TRUSTNODE_DEVICE_AUTH_SECRET", "secret-test")
    tok = create_device_access_token(tenant_id="tenant-a", gateway_id="gw-1", expires_seconds=600)
    claims = decode_device_access_token(tok)
    assert claims["tenant_id"] == "tenant-a"
    assert claims["gateway_id"] == "gw-1"
