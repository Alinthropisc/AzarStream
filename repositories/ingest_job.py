from datetime import datetime

from sqlalchemy import select, desc

from models import IngestJob, IngestJobStatus
from repositories.base import BaseRepository


class IngestJobRepository(BaseRepository[IngestJob]):
    model = IngestJob

    async def list_recent(self, limit: int = 50) -> list[IngestJob]:
        stmt = select(IngestJob).order_by(desc(IngestJob.created_at)).limit(limit)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def mark_running(self, job_id: int) -> None:
        job = await self.get_by_id(job_id)
        if job:
            job.status = IngestJobStatus.RUNNING
            job.started_at = datetime.now()
            await self.session.flush()

    async def mark_progress(
        self,
        job_id: int,
        processed: int | None = None,
        failed: int | None = None,
        total: int | None = None,
    ) -> None:
        job = await self.get_by_id(job_id)
        if not job:
            return
        if processed is not None:
            job.processed_count = processed
        if failed is not None:
            job.failed_count = failed
        if total is not None:
            job.total_count = total
        await self.session.flush()

    async def mark_finished(
        self,
        job_id: int,
        status: IngestJobStatus,
        error_message: str | None = None,
    ) -> None:
        job = await self.get_by_id(job_id)
        if not job:
            return
        job.status = status
        job.finished_at = datetime.now()
        if error_message:
            job.error_message = error_message
        await self.session.flush()
