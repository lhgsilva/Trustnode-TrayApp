from app.services.plc_manager import PLCManager
from app.services.app_store import AppStore
from app.services.control_plane_store import ControlPlaneStore
from app.services.telemetry_service import TelemetryService
from app.services.ingest_store import IngestStore
from app.services.power_manager import PowerManager

telemetry_service = TelemetryService()
ingest_store = IngestStore()
plc_manager = PLCManager()
app_store = AppStore()
power_manager = PowerManager(app_store)
control_plane_store = ControlPlaneStore()
