"""
User Download Queue — Sequential Processing (per-user, per-bot isolated)

Each bot gets its own queue per user so that multiple bots on the same
server never bleed into each other's downloads.
"""

import asyncio
import json
import time
from dataclasses import dataclass, field
from collections import defaultdict

from app.logging import get_logger
from services.cache import cache

log = get_logger("service.user_queue")


@dataclass
class QueuedDownload:
    """A single download task in user's queue"""
    url: str
    bot_id: int
    user_id: int
    chat_id: int
    message_id: int  # Telegram message ID (the original link message)
    progress_message_id: int | None = None  # Bot's "Downloading..." message
    status: str = "queued"  # "queued", "downloading", "done", "failed"
    added_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None


@dataclass
class UserQueue:
    """Per-user download queue"""
    user_id: int
    downloads: list[QueuedDownload] = field(default_factory=list)
    is_processing: bool = False
    last_activity: float = field(default_factory=time.time)


class UserDownloadQueue:
    """
    Manages per-user sequential download queues.

    Each user gets their own queue. Only 1 download runs at a time per user.
    Additional links are queued and processed in FIFO order.
    """

    MAX_QUEUE_SIZE = 10  # Max queued downloads per user
    QUEUE_TTL = 3600     # Remove inactive queues after 1 hour
    STALE_DOWNLOAD_TIMEOUT = 600  # Reset is_processing if current download is older than 10 min

    def __init__(self):
        self._queues: dict[int, UserQueue] = {}
        self._lock = asyncio.Lock()
        self._cleanup_task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start background cleanup"""
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        log.info("User download queue started")

    async def stop(self) -> None:
        """Stop background cleanup"""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        log.info("User download queue stopped")

    async def _cleanup_loop(self) -> None:
        """Remove inactive queues"""
        while True:
            try:
                await asyncio.sleep(300)  # Every 5 minutes
                now = time.time()
                expired = [
                    uid for uid, q in self._queues.items()
                    if now - q.last_activity > self.QUEUE_TTL
                    and not q.is_processing
                    and not q.downloads
                ]
                for uid in expired:
                    del self._queues[uid]
                if expired:
                    log.debug("Cleaned up inactive queues", count=len(expired))
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("Cleanup error", error=str(e))

    def _get_queue(self, user_id: int) -> UserQueue:
        """Get or create user queue"""
        if user_id not in self._queues:
            self._queues[user_id] = UserQueue(user_id=user_id)
        self._queues[user_id].last_activity = time.time()
        return self._queues[user_id]

    async def add(
        self,
        user_id: int,
        bot_id: int,
        chat_id: int,
        message_id: int,
        url: str,
    ) -> tuple[bool, int, str]:
        """
        Add download to user's queue.

        Returns:
            (success: bool, position: int, message: str)
            - position 0 = starts downloading immediately
            - position > 0 = in queue
        """
        async with self._lock:
            queue = self._get_queue(user_id)

            # Stale detection: if there's an "active" download that has been running
            # longer than STALE_DOWNLOAD_TIMEOUT, the queue advance was likely missed
            # (e.g., user opened YouTube format buttons but never clicked). Reset state.
            if queue.is_processing:
                now = time.time()
                active = next((dl for dl in queue.downloads if dl.status == "downloading"), None)
                if active is None or (active.started_at and now - active.started_at > self.STALE_DOWNLOAD_TIMEOUT):
                    log.warning(
                        "Stale queue detected, resetting",
                        user_id=user_id,
                        had_active=active is not None,
                        age=(now - active.started_at) if (active and active.started_at) else None,
                    )
                    queue.downloads.clear()
                    queue.is_processing = False

            # Check queue size limit
            if len(queue.downloads) >= self.MAX_QUEUE_SIZE:
                return False, -1, f"Queue full — max {self.MAX_QUEUE_SIZE} downloads. Wait for current to finish."

            download = QueuedDownload(
                url=url,
                bot_id=bot_id,
                user_id=user_id,
                chat_id=chat_id,
                message_id=message_id,
            )

            # If nothing is processing, start immediately
            if not queue.is_processing:
                queue.is_processing = True
                download.status = "downloading"
                download.started_at = time.time()
                queue.downloads.append(download)
                queue.last_activity = time.time()
                return True, 0, "⏬ Downloading..."

            # Otherwise, add to queue
            queue.downloads.append(download)
            # position = len(queue.downloads) - 1  # -1 because first is processing
            position = sum(1 for dl in queue.downloads if dl.status == "queued")
            queue.last_activity = time.time()

            if position == 1:
                msg = "⏳ In queue — 1 download ahead"
            else:
                msg = f"⏳ In queue — {position} downloads ahead"

            return True, position, msg

    async def set_progress_message(
            self, user_id: int, bot_id: int, url: str, progress_message_id: int
    ) -> None:
        """Store the progress message ID for a queued download."""
        async with self._lock:
            queue = self._queues.get(user_id)  # ВАЖНО: user_id, не (user_id, bot_id)
            if not queue:
                return

            for dl in queue.downloads:
                if (
                        dl.url == url
                        and dl.bot_id == bot_id
                        and dl.progress_message_id is None
                ):
                    dl.progress_message_id = progress_message_id
                    break

    async def get_waiting_downloads(self, user_id: int) -> list[QueuedDownload]:
        """Return downloads that are still waiting in queue."""
        async with self._lock:
            queue = self._queues.get(user_id)
            if not queue:
                return []

            return [dl for dl in queue.downloads if dl.status == "queued"]

    async def get_next(self, user_id: int) -> tuple[QueuedDownload | None, list[QueuedDownload]]:
        """
        Mark current as done and get next from queue.

        Returns:
            (next_download, remaining_queued_items)
            remaining_queued_items - items still waiting (for updating their messages)
        """
        async with self._lock:
            queue = self._queues.get(user_id)
            if not queue or not queue.downloads:
                return None, []

            # Mark current as done
            for dl in queue.downloads:
                if dl.status == "downloading":
                    dl.status = "done"
                    dl.finished_at = time.time()
                    break

            # Find next queued
            next_dl = None
            for dl in queue.downloads:
                if dl.status == "queued":
                    dl.status = "downloading"
                    dl.started_at = time.time()
                    queue.last_activity = time.time()
                    next_dl = dl
                    break

            if next_dl is None:
                # Nothing left
                queue.is_processing = False
                queue.downloads.clear()
                return None, []

            # Collect remaining queued items (after the one we just started)
            remaining = [dl for dl in queue.downloads if dl.status == "queued"]

            # Return as explicit tuple of (QueuedDownload, list)
            return next_dl, remaining

    async def get_queue_snapshot(self, user_id: int) -> list[QueuedDownload]:
        """Get current queued (waiting) downloads in order."""
        async with self._lock:
            queue = self._queues.get(user_id)
            if not queue:
                return []
            return [dl for dl in queue.downloads if dl.status == "queued"]

    async def get_status(self, user_id: int) -> dict:
        """Get queue status for user"""
        queue = self._queues.get(user_id)
        if not queue:
            return {"active": 0, "queued": 0, "processing": None}

        processing = None
        for dl in queue.downloads:
            if dl.status == "downloading":
                processing = dl.url
                break

        queued = sum(1 for dl in queue.downloads if dl.status == "queued")

        return {
            "active": 1 if queue.is_processing else 0,
            "queued": queued,
            "processing": processing,
            "total": len(queue.downloads),
        }


# === Singleton ===
user_download_queue = UserDownloadQueue()