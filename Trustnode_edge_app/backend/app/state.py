from app.services.plc_manager import PLCManager
from app.services.app_store import AppStore
from app.services.telemetry_service import TelemetryService
from app.services.ingest_store import IngestStore

telemetry_service = TelemetryService()
ingest_store = IngestStore()
plc_manager = PLCManager()
app_store = AppStore()
