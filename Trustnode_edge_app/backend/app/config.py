from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Operator 2026-06-19: pydantic-settings v2 defaults to extra='forbid'
    # which crashes the EXE on boot whenever the operator's .env contains
    # any field not declared below (Supabase keys, cloud DB credentials,
    # VPS creds, etc. — all stuff the local edge doesn't need but ops
    # tooling drops into .env). Switch to 'ignore' so unknown keys are
    # silently passed through; the per-feature env-var lookups elsewhere
    # in the codebase still pick them up via os.environ.get(...).
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    trustnode_env: str = "dev"
    trustnode_host: str = "127.0.0.1"
    trustnode_port: int = 8000
    # Operator 2026-07-02: include `null` and `file://` so the packaged
    # Electron app (which loads the SPA from file:// and therefore sends
    # `Origin: null` on cross-origin fetches to the loopback backend) passes
    # CORS preflight for POST/DELETE. Without this, POST/DELETE from the
    # desktop app failed preflight with 400 and fetch() rejected as
    # "Failed to fetch" (create chat / delete chat / send message). The
    # backend binds 127.0.0.1 only, so allowing the local app origin is safe.
    trustnode_cors_origins: str = "http://127.0.0.1:5173,http://localhost:5173,null,file://"

    @property
    def cors_origins(self) -> List[str]:
        return [origin.strip() for origin in self.trustnode_cors_origins.split(",") if origin.strip()]


settings = Settings()
