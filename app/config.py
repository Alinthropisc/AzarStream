from functools import lru_cache
from pathlib import Path
from typing import Literal
from pydantic import Field, PostgresDsn, RedisDsn, AnyUrl  # ty:ignore[unresolved-import]
from pydantic_settings import BaseSettings, SettingsConfigDict  # ty:ignore[unresolved-import]

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # App
    app_name: str = "MediaFlow"
    debug: bool = False
    secret_key: str = Field(..., min_length=32)

    # Database
    database_url: AnyUrl = Field(...)
    database_echo: bool = False
    database_pool_size: int = 10

    # Redis
    redis_url: RedisDsn = Field(default="redis://localhost:6379/0")
    use_fakeredis: bool = False  # True for Windows dev

    # Telegram
    telegram_api_server: str | None = None  # Custom Bot API Server
    # Если self-hosted Bot API запущен с флагом --local, ставь в True —
    # aiogram будет передавать файлы по абсолютному пути (zero-copy upload),
    # это резко ускоряет отдачу больших видео (4-8 ГБ).
    telegram_api_local: bool = False

    # YouTube PO Token: можно задать через env, иначе будет искаться файл
    # storage/cookies/po_token.txt + visitor_data.txt, иначе wpc-плагин.
    youtube_po_token: str | None = None
    youtube_visitor_data: str | None = None
    telegram_upload_limit_mb: int = 4096  # Upload limit for Bot API multipart uploads (custom API server)
    storage_channel_id: int | None = Field(default=None)  # Fallback for caching media (deprecated: use CacheChannel CRUD)

    # Official MediaFlow Bot (for cache channel, ads, support — NOT for downloading)
    media_flow_bot_token: str | None = None  # Official @MediaFlow bot token
    media_flow_cache_channel_id: int | None = None  # Channel for caching ad media only

    # Admin
    admin_username: str = "admin"
    admin_password: str = Field(..., min_length=8)

    # Rate Limits
    rate_limit_global_requests: int = 1000
    rate_limit_global_window: int = 60
    rate_limit_user_requests: int = 30
    rate_limit_user_window: int = 60
    rate_limit_download_requests: int = 10
    rate_limit_download_window: int = 60
    downloads_per_user_hour: int = 50
    downloads_per_minute: int = 100

    # Paths
    temp_download_path: str = str(_PROJECT_ROOT / "storage" / "temp") + "/"

    # Webhook
    webhook_base_url: str | None = None  # https://yourdomain.com

    # Queue
    queue_workers: int = 10
    queue_max_per_user: int = 2
    queue_max_global: int = 50

    # Worker
    worker_broadcast_batch_size: int = 25
    worker_broadcast_delay_ms: int = 50


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()




class There:
    def __init__(self):
        pass
