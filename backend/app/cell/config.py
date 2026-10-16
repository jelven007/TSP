from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class CellSettings(BaseSettings):
    # Never inherit the legacy .env, DATA_DIR or desktop authentication bypass.
    model_config = SettingsConfigDict(env_prefix="TSP_CELL_", extra="ignore", frozen=True)

    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    control_dsn: SecretStr
    data_dsn: SecretStr
    pool_size: int = Field(default=4, ge=1, le=32)
    pool_timeout: float = Field(default=5, gt=0, le=30)
    schema_version: int = Field(default=4, ge=4, le=6)
    session_ttl_seconds: int = Field(default=86400, ge=300, le=2592000)
    market_url: str = ""
    market_token: SecretStr = SecretStr("")
    stream_ticket_secret: SecretStr = SecretStr("")
    static_dir: Path | None = None
    cors_origins: list[str] = []
    worker_timeout: int = Field(default=60, ge=5, le=300)
    worker_kind: str = Field(default="realtime", pattern=r"^(realtime|strategies|notifications|automation)$")
    worker_concurrency: int = Field(default=2, ge=1, le=32)
    realtime_interval: float = Field(default=1, ge=0.25, le=60)
    smtp_host: str = ""
    smtp_port: int = Field(default=465, ge=1, le=65535)
    smtp_user: str = ""
    smtp_password: SecretStr = SecretStr("")
    smtp_from: str = ""
    smtp_security: str = Field(default="tls", pattern=r"^(tls|starttls)$")
    webhook_origins: list[str] = []
