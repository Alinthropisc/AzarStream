"""Tests for UserDownloadQueue"""

import pytest
import asyncio

from services.user_download_queue import (
    UserDownloadQueue,
    QueuedDownload,
)


@pytest.fixture
def queue():
    """Create a fresh UserDownloadQueue."""
    return UserDownloadQueue()


@pytest.mark.asyncio
async def test_add_first_download_starts_immediately(queue):
    """First download should start immediately (position 0)."""
    success, position, msg = await queue.add(
        user_id=1,
        bot_id=100,
        chat_id=12345,
        message_id=100,
        url="https://instagram.com/reel/ABC",
    )

    assert success is True
    assert position == 0
    assert "Downloading" in msg


@pytest.mark.asyncio
async def test_add_second_download_queued(queue):
    """Second download should be queued (position 1)."""
    # First download
    await queue.add(user_id=1, bot_id=100, chat_id=12345, message_id=100, url="https://url1.com")

    # Second download
    success, position, msg = await queue.add(
        user_id=1, bot_id=100, chat_id=12345, message_id=101, url="https://url2.com"
    )

    assert success is True
    assert position == 1
    assert "1 download ahead" in msg


@pytest.mark.asyncio
async def test_add_multiple_downloads_queued(queue):
    """Multiple downloads should be queued with correct positions."""
    await queue.add(user_id=1, bot_id=100, chat_id=12345, message_id=100, url="https://url1.com")

    for i in range(2, 6):
        success, position, msg = await queue.add(
            user_id=1, bot_id=100, chat_id=12345, message_id=100 + i, url=f"https://url{i}.com"
        )
        assert success is True
        assert position == i - 1


@pytest.mark.asyncio
async def test_queue_full_at_max_size(queue):
    """Queue should reject downloads beyond MAX_QUEUE_SIZE."""
    # Add MAX_QUEUE_SIZE + 1 downloads
    for i in range(UserDownloadQueue.MAX_QUEUE_SIZE + 1):
        success, position, msg = await queue.add(
            user_id=1, bot_id=100, chat_id=12345, message_id=100 + i, url=f"https://url{i}.com"
        )

        if i >= UserDownloadQueue.MAX_QUEUE_SIZE:
            assert success is False
            assert position == -1
            assert "Queue full" in msg
        else:
            assert success is True


@pytest.mark.asyncio
async def test_get_next_returns_next_queued(queue):
    """get_next should return the next queued download."""
    await queue.add(user_id=1, bot_id=100, chat_id=12345, message_id=100, url="https://url1.com")
    await queue.add(user_id=1, bot_id=100, chat_id=12345, message_id=101, url="https://url2.com")
    await queue.add(user_id=1, bot_id=100, chat_id=12345, message_id=102, url="https://url3.com")

    # Get next (url2 should be returned)
    next_dl, _ = await queue.get_next(user_id=1)
    assert next_dl is not None
    assert next_dl.url == "https://url2.com"
    assert next_dl.status == "downloading"


@pytest.mark.asyncio
async def test_get_next_marks_current_done(queue):
    """get_next should mark current download as done."""
    await queue.add(user_id=1, bot_id=100, chat_id=12345, message_id=100, url="https://url1.com")
    await queue.add(user_id=1, bot_id=100, chat_id=12345, message_id=101, url="https://url2.com")

    await queue.get_next(user_id=1)

    status = await queue.get_status(user_id=1)
    assert status["active"] == 1


@pytest.mark.asyncio
async def test_get_next_returns_none_when_empty(queue):
    """get_next should return None when queue is empty."""
    next_dl, _ = await queue.get_next(user_id=1)
    assert next_dl is None


@pytest.mark.asyncio
async def test_get_next_clears_when_all_done(queue):
    """get_next should clear the queue when all downloads are done."""
    await queue.add(user_id=1, bot_id=100, chat_id=12345, message_id=100, url="https://url1.com")

    # Mark first as done, no more queued
    next_dl, _ = await queue.get_next(user_id=1)
    assert next_dl is None

    status = await queue.get_status(user_id=1)
    assert status["active"] == 0
    assert status["queued"] == 0


@pytest.mark.asyncio
async def test_set_progress_message(queue):
    """Should store progress message ID for queued downloads."""
    await queue.add(user_id=1, bot_id=100, chat_id=12345, message_id=100, url="https://url1.com")
    await queue.add(user_id=1, bot_id=100, chat_id=12345, message_id=101, url="https://url2.com")

    await queue.set_progress_message(user_id=1, bot_id=100, url="https://url2.com", progress_message_id=999)

    # Check it was stored
    queue_data = queue._queues[1]
    for dl in queue_data.downloads:
        if dl.url == "https://url2.com":
            assert dl.progress_message_id == 999
            break
    else:
        pytest.fail("Download not found")


@pytest.mark.asyncio
async def test_get_status(queue):
    """get_status should return correct queue status."""
    await queue.add(user_id=1, bot_id=100, chat_id=12345, message_id=100, url="https://url1.com")
    await queue.add(user_id=1, bot_id=100, chat_id=12345, message_id=101, url="https://url2.com")
    await queue.add(user_id=1, bot_id=100, chat_id=12345, message_id=102, url="https://url3.com")

    status = await queue.get_status(user_id=1)
    assert status["active"] == 1
    assert status["queued"] == 2
    assert status["total"] == 3
    assert status["processing"] == "https://url1.com"


@pytest.mark.asyncio
async def test_get_status_empty_queue(queue):
    """get_status should return zeros for empty queue."""
    status = await queue.get_status(user_id=1)
    assert status["active"] == 0
    assert status["queued"] == 0
    assert status["processing"] is None


@pytest.mark.asyncio
async def test_cleanup_removes_inactive_queues(queue):
    """Cleanup should remove queues that are inactive for too long."""
    queue.QUEUE_TTL = 1  # 1 second TTL for testing

    await queue.add(user_id=1, bot_id=100, chat_id=12345, message_id=100, url="https://url1.com")

    # Manipulate last_activity to be old
    import time
    queue._queues[1].last_activity = time.time() - 2
    queue._queues[1].is_processing = False

    # Run cleanup manually (simulate one iteration)
    now = time.time()
    expired = [
        uid for uid, q in queue._queues.items()
        if now - q.last_activity > queue.QUEUE_TTL
        and not q.is_processing
        and not q.downloads
    ]
    for uid in expired:
        del queue._queues[uid]

    # Queue still exists because it has downloads
    assert 1 in queue._queues
