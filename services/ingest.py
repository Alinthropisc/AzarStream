import re
from urllib.parse import urlparse

from app.logging import get_logger
from models import IngestJob, IngestJobStatus, IngestSourceType
from repositories.uow import UnitOfWork

log = get_logger("service.ingest")


YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
SOUNDCLOUD_HOSTS = {"soundcloud.com", "www.soundcloud.com", "m.soundcloud.com"}


class IngestError(Exception):
    pass


def detect_source(url: str) -> IngestSourceType:
    host = (urlparse(url).hostname or "").lower()
    if host in YOUTUBE_HOSTS:
        return IngestSourceType.YOUTUBE
    if host in SOUNDCLOUD_HOSTS:
        return IngestSourceType.SOUNDCLOUD
    raise IngestError(f"Unsupported source host: {host or url}")


class IngestService:
    """
    Создаёт IngestJob, кладёт ARQ-таску в очередь. Сам процесс загрузки
    плейлиста и заливки треков в кеш-канал — в воркере (workers/tasks.py).
    """

    @staticmethod
    async def start_url_ingest(
        url: str,
        requested_by_admin_id: str | None,
        target_cache_channel_id: str | None = None,
    ) -> IngestJob:
        url = (url or "").strip()
        if not url:
            raise IngestError("URL пустой")
        source_type = detect_source(url)

        async with UnitOfWork() as uow:
            job = await uow.ingest_jobs.create(
                source_type=source_type,
                source_url=url,
                requested_by_admin_id=requested_by_admin_id,
                target_cache_channel_id=target_cache_channel_id,
                status=IngestJobStatus.PENDING,
            )
            await uow.commit()
            job_id = job.id

        await _enqueue(job_id)
        log.info("Ingest job created (URL)", job_id=job_id, source=source_type.value, url=url[:80])
        # перечитываем для отдачи (commit закрыл сессию)
        async with UnitOfWork() as uow:
            fresh = await uow.ingest_jobs.get_by_id(job_id)
            return fresh  # type: ignore[return-value]

    @staticmethod
    async def start_file_ingest(
        source_filename: str,
        local_path: str,
        requested_by_admin_id: str | None,
        target_cache_channel_id: str | None = None,
    ) -> IngestJob:
        async with UnitOfWork() as uow:
            job = await uow.ingest_jobs.create(
                source_type=IngestSourceType.FILE_UPLOAD,
                source_filename=source_filename,
                source_url=local_path,  # путь к временному файлу — используется воркером
                requested_by_admin_id=requested_by_admin_id,
                target_cache_channel_id=target_cache_channel_id,
                status=IngestJobStatus.PENDING,
            )
            await uow.commit()
            job_id = job.id

        await _enqueue(job_id)
        log.info("Ingest job created (file)", job_id=job_id, filename=source_filename)
        async with UnitOfWork() as uow:
            fresh = await uow.ingest_jobs.get_by_id(job_id)
            return fresh  # type: ignore[return-value]


async def _enqueue(job_id: int) -> None:
    """Положить ARQ-таску в очередь."""
    from arq import create_pool
    from workers.config import get_redis_settings

    pool = await create_pool(get_redis_settings(), default_queue_name="mediadownloader")
    try:
        await pool.enqueue_job("ingest_audio", job_id)
    finally:
        await pool.close()
