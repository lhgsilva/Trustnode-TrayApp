; TrustNode NSIS hooks — register Windows Firewall rules for the
; bundled backend at install time and remove them at uninstall time.
; Operator 2026-06-17.
;
; Why this exists: the FastAPI backend binds 0.0.0.0 (LAN sharing,
; OPC UA, MQTT). Windows shows a Defender Firewall prompt with the
; full install path the first time it tries. Pre-creating the rules
; here means the customer never sees that dialog.
;
; The installer is built with electron-builder. It exposes two macro
; hooks: customInstall (fires at end of install) and customUnInstall
; (fires at start of uninstall). Both have admin privileges because
; we set `requestExecutionLevel admin` in package.json.

!macro customInstall
  ; Operator 2026-06-18: prior version used backslash line continuations
  ; inside nsExec::Exec single-quoted strings. NSIS passed those literally
  ; to cmd.exe, which doesn't honor backslash continuation — netsh saw a
  ; malformed command and silently failed. Every fresh install left the
  ; firewall unconfigured, and Windows prompted the customer on every
  ; backend boot. Each command is now ONE LINE.

  ; Remove any leftover rules from a prior install before re-adding.
  ; netsh returns non-zero when there's nothing to delete; suppress it.
  nsExec::Exec 'netsh advfirewall firewall delete rule name="TrustNode Backend"'
  nsExec::Exec 'netsh advfirewall firewall delete rule name="TrustNode OPC UA"'
  nsExec::Exec 'netsh advfirewall firewall delete rule name="TrustNode MQTT"'

  ; Backend (covers LAN sharing primary + secondary uvicorn).
  nsExec::Exec 'netsh advfirewall firewall add rule name="TrustNode Backend" dir=in action=allow protocol=TCP program="$INSTDIR\resources\backend\trustnode-service.exe" enable=yes profile=any description="Allow TrustNode local backend (Lite + control APIs)."'

  ; OPC UA sidecar (.NET self-contained).
  nsExec::Exec 'netsh advfirewall firewall add rule name="TrustNode OPC UA" dir=in action=allow protocol=TCP program="$INSTDIR\resources\backend\_internal\sidecars\opcua\TrustNodeOpcUa.exe" enable=yes profile=any description="Allow TrustNode OPC UA server (.NET)."'

  ; MQTT broker (Eclipse Mosquitto).
  nsExec::Exec 'netsh advfirewall firewall add rule name="TrustNode MQTT" dir=in action=allow protocol=TCP program="$INSTDIR\resources\backend\_internal\sidecars\mosquitto\mosquitto.exe" enable=yes profile=any description="Allow TrustNode MQTT broker (Mosquitto)."'

  ; --- Operator 2026-06-18 (Phase 2b): create %ProgramData%\TrustNode\edge
  ; with a restrictive ACL so the app-store SQLite buffer DB can't be
  ; opened or edited by regular Windows users. SYSTEM + Administrators
  ; get full control; everyone else gets read-only. The backend writes
  ; the DB here on first run (see app_store._resolve_db_path).
  CreateDirectory "C:\ProgramData\TrustNode"
  CreateDirectory "C:\ProgramData\TrustNode\edge"

  ; Reset inheritance first so explicit ACEs win.
  nsExec::Exec 'icacls "C:\ProgramData\TrustNode\edge" /inheritance:r'
  ; SYSTEM + Administrators: full control (this is who the service runs as).
  nsExec::Exec 'icacls "C:\ProgramData\TrustNode\edge" /grant:r "SYSTEM:(OI)(CI)F"'
  nsExec::Exec 'icacls "C:\ProgramData\TrustNode\edge" /grant:r "Administrators:(OI)(CI)F"'
  ; Authenticated Users: read + execute only. They can't edit the DB
  ; but they can run the tray app (which talks to the backend over
  ; loopback HTTP, not by opening the DB directly).
  nsExec::Exec 'icacls "C:\ProgramData\TrustNode\edge" /grant:r "Authenticated Users:(OI)(CI)RX"'
!macroend

!macro customUnInstall
  nsExec::Exec 'netsh advfirewall firewall delete rule name="TrustNode Backend"'
  nsExec::Exec 'netsh advfirewall firewall delete rule name="TrustNode OPC UA"'
  nsExec::Exec 'netsh advfirewall firewall delete rule name="TrustNode MQTT"'

  ; --- Operator 2026-06-18: clean up the ProgramData store on uninstall.
  ; Customer data (historian, dashboards, licenses) lives here — DO NOT
  ; delete on a plain uninstall, only on a full purge. NSIS doesn't give
  ; us a "purge?" toggle here, so we leave the folder behind and the
  ; admin can manually remove %ProgramData%\TrustNode if they want to
  ; reset everything. This matches how Postgres / SQL Server installers
  ; behave on Windows.
!macroend
