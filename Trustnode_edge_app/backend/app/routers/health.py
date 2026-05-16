from fastapi import APIRouter

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "api_build": "edge-2026-05-16-syncfix-3-cache",
        "capabilities": {
            "database_active_sink": True,
            "database_file_sinks": True,
            "plc_discover_tags": True,
            "plc_opcua_browse_tree": True,
            "plc_multi_gateway": True,
            "app_store_db_primary": True,
            "database_recovery_routines": True,
        },
    }
