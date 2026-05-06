from arq.connections import RedisSettings
from app.config import settings


def get_redis_settings() -> RedisSettings:
    """Настройки Redis для ARQ"""
    url = str(settings.redis_url)

    if settings.use_fakeredis:
        return RedisSettings(
            host="localhost",
            port=6379,
            database=0,
            conn_timeout=5,  # 5 sec timeout
        )

    from urllib.parse import urlparse
    parsed = urlparse(url)

    return RedisSettings(
        host=parsed.hostname or "localhost",
        port=parsed.port or 6379,
        database=int(parsed.path.lstrip("/") or 0),
        password=parsed.password,
        conn_timeout=5,  # 5 sec connection timeout
    )


ARQ_SETTINGS = {
    "redis_settings": get_redis_settings(),
    "job_timeout": 3600,
    "max_jobs": 100,
    "job_retry": 3,
    "health_check_interval": 30,
    "queue_name": "mediadownloader",
}
