from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    trustnode_env: str = "dev"
    trustnode_host: str = "127.0.0.1"
    trustnode_port: int = 8000
    trustnode_cors_origins: str = "http://127.0.0.1:5173,http://localhost:5173"

    @property
    def cors_origins(self) -> List[str]:
        return [origin.strip() for origin in self.trustnode_cors_origins.split(",") if origin.strip()]


settings = Settings()
