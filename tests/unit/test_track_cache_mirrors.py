"""Тесты для TrackCacheMirrorRepository: добавление/чтение зеркал трека."""

import pytest

from models import TrackSource
from repositories.track import TrackRepository, TrackCacheMirrorRepository


@pytest.fixture
async def track(test_db):
    repo = TrackRepository(test_db)
    t = await repo.create(
        title="Mirror Test",
        artist="Artist",
        duration_sec=120,
        source_platform=TrackSource.YOUTUBE,
        source_url="https://youtube.com/watch?v=mirr",
        source_id="mirr",
        cache_chat_id=-100,
        cache_message_id=42,
        file_id="MAIN_FILE_ID",
        file_unique_id="MAIN_UNIQ",
    )
    await test_db.commit()
    return t


class TestMirrors:
    async def test_add_first_mirror(self, test_db, track):
        repo = TrackCacheMirrorRepository(test_db)
        m = await repo.add_mirror(
            track_id=track.id,
            cache_chat_id=-200,
            cache_message_id=10,
            file_id="MAIN_FILE_ID",
            file_unique_id="MAIN_UNIQ",
        )
        await test_db.commit()
        assert m.id is not None
        assert m.cache_chat_id == -200

    async def test_idempotent_same_channel(self, test_db, track):
        repo = TrackCacheMirrorRepository(test_db)
        m1 = await repo.add_mirror(
            track_id=track.id, cache_chat_id=-200, cache_message_id=10,
            file_id="X", file_unique_id="Y",
        )
        await test_db.commit()
        m2 = await repo.add_mirror(
            track_id=track.id, cache_chat_id=-200, cache_message_id=99,
            file_id="Z", file_unique_id="W",
        )
        # Повторный add для (track_id, chat_id) возвращает существующий, не дублирует
        assert m1.id == m2.id
        all_mirrors = await repo.list_for_track(track.id)
        assert len(all_mirrors) == 1

    async def test_multiple_channels(self, test_db, track):
        repo = TrackCacheMirrorRepository(test_db)
        for chat_id in (-200, -300, -400):
            await repo.add_mirror(
                track_id=track.id, cache_chat_id=chat_id, cache_message_id=1,
                file_id="X", file_unique_id="Y",
            )
        await test_db.commit()
        mirrors = await repo.list_for_track(track.id)
        assert sorted(m.cache_chat_id for m in mirrors) == [-400, -300, -200]
