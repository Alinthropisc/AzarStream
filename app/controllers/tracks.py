from pathlib import Path
import tempfile
import uuid

from litestar import Controller, get, post
from litestar.datastructures import UploadFile
from litestar.di import Provide
from litestar.enums import RequestEncodingType
from litestar.params import Body
from litestar.response import Redirect, Template
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.logging import get_logger
from app.middleware.auth import admin_guard
from database.connection import get_session
from models import (
    BotType,
    CacheChannel,
    IngestJob,
    IngestJobStatus,
    Track,
)
from repositories import IngestJobRepository, TrackRepository
from services.ingest import IngestService, IngestError

log = get_logger("controller.tracks")


INGEST_TMP_DIR = Path("storage/ingest_uploads")


class TrackController(Controller):
    path = "/admin/tracks"
    guards = [admin_guard]
    dependencies = {"session": Provide(get_session)}

    # ── Library list ─────────────────────────────────────────────────────────

    @get("/", name="tracks:list")
    async def list_tracks(
        self,
        session: AsyncSession,
        page: int = 1,
        limit: int = 30,
        search: str | None = None,
    ) -> Template:
        repo = TrackRepository(session)
        if search:
            rows, total = await repo.search(search, offset=(page - 1) * limit, limit=limit)
        else:
            rows_seq = await repo.get_all(
                offset=(page - 1) * limit, limit=limit, order_by="created_at"
            )
            rows = list(rows_seq)
            total = await repo.count()

        return Template(
            template_name="admin/tracks/list.html",
            context={
                "tracks": rows,
                "total": total,
                "page": page,
                "limit": limit,
                "search": search,
            },
        )

    @post("/{track_id:int}/delete", name="tracks:delete")
    async def delete_track(self, session: AsyncSession, track_id: int) -> Redirect:
        await session.execute(delete(Track).where(Track.id == track_id))
        await session.commit()
        return Redirect(path="/admin/tracks?message=Track deleted")

    # ── Ingest form ──────────────────────────────────────────────────────────

    @get("/ingest", name="tracks:ingest_form")
    async def ingest_form(self, session: AsyncSession) -> Template:
        stmt = (
            select(CacheChannel)
            .where(CacheChannel.bot_type == BotType.MEDIA_SEARCH, CacheChannel.is_active == True)
            .order_by(CacheChannel.name)
        )
        channels = list((await session.execute(stmt)).scalars().all())
        return Template(
            template_name="admin/tracks/ingest.html",
            context={"channels": channels},
        )

    @post(
        "/ingest",
        name="tracks:ingest_submit",
        media_type="text/html",
    )
    async def ingest_submit(
        self,
        session: AsyncSession,
        data: dict = Body(media_type=RequestEncodingType.MULTI_PART),
    ) -> Redirect:
        url = (data.get("url") or "").strip() if isinstance(data.get("url"), str) else ""
        target = data.get("target_cache_channel_id") or None
        if isinstance(target, str) and not target.strip():
            target = None

        upload = data.get("file")
        try:
            if upload is not None and isinstance(upload, UploadFile) and upload.filename:
                # Сохраняем во временную директорию; воркер удалит после загрузки.
                INGEST_TMP_DIR.mkdir(parents=True, exist_ok=True)
                safe_name = f"{uuid.uuid4().hex}_{Path(upload.filename).name}"
                dest = INGEST_TMP_DIR / safe_name
                content = await upload.read()
                dest.write_bytes(content)
                job = await IngestService.start_file_ingest(
                    source_filename=upload.filename,
                    local_path=str(dest),
                    requested_by_admin_id=None,
                    target_cache_channel_id=target,
                )
                return Redirect(path=f"/admin/tracks/jobs/{job.id}?message=Upload queued")

            if url:
                job = await IngestService.start_url_ingest(
                    url=url,
                    requested_by_admin_id=None,
                    target_cache_channel_id=target,
                )
                return Redirect(path=f"/admin/tracks/jobs/{job.id}?message=Ingest started")

            return Redirect(path="/admin/tracks/ingest?error=Введите URL или прикрепите файл")

        except IngestError as exc:
            return Redirect(path=f"/admin/tracks/ingest?error={exc}")
        except Exception as exc:
            log.exception("Ingest submit failed", error=str(exc))
            return Redirect(path=f"/admin/tracks/ingest?error={exc}")

    # ── Jobs ─────────────────────────────────────────────────────────────────

    @get("/jobs", name="tracks:jobs")
    async def list_jobs(self, session: AsyncSession) -> Template:
        repo = IngestJobRepository(session)
        jobs = await repo.list_recent(limit=100)
        return Template(
            template_name="admin/tracks/jobs.html",
            context={"jobs": jobs},
        )

    @get("/jobs/{job_id:int}", name="tracks:job_detail")
    async def job_detail(self, session: AsyncSession, job_id: int) -> Template:
        repo = IngestJobRepository(session)
        job = await repo.get_by_id(job_id)
        return Template(
            template_name="admin/tracks/job_detail.html",
            context={"job": job},
        )
