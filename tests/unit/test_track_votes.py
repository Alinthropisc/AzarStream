"""Тесты для TrackVoteRepository (лайк/дизлайк + денормализованные счётчики)."""

import pytest

from models import Track, TrackSource
from repositories.track import TrackRepository, TrackVoteRepository


@pytest.fixture
async def track(test_db):
    repo = TrackRepository(test_db)
    t = await repo.create(
        title="Test Song",
        artist="Test Artist",
        duration_sec=180,
        source_platform=TrackSource.YOUTUBE,
        source_url="https://youtube.com/watch?v=abc",
        source_id="abc",
        cache_chat_id=-100,
        cache_message_id=1,
        file_id="FILE_ID",
        file_unique_id="UNIQ",
    )
    await test_db.commit()
    return t


class TestTrackVoteCast:
    async def test_first_like_increments_likes(self, test_db, track):
        votes = TrackVoteRepository(test_db)
        likes, dislikes, eff = await votes.cast(track.id, user_telegram_id=1, value=1)
        assert (likes, dislikes, eff) == (1, 0, 1)

    async def test_first_dislike_increments_dislikes(self, test_db, track):
        votes = TrackVoteRepository(test_db)
        likes, dislikes, eff = await votes.cast(track.id, user_telegram_id=1, value=-1)
        assert (likes, dislikes, eff) == (0, 1, -1)

    async def test_same_vote_twice_removes(self, test_db, track):
        votes = TrackVoteRepository(test_db)
        await votes.cast(track.id, user_telegram_id=1, value=1)
        likes, dislikes, eff = await votes.cast(track.id, user_telegram_id=1, value=1)
        assert (likes, dislikes, eff) == (0, 0, 0)

    async def test_switch_like_to_dislike(self, test_db, track):
        votes = TrackVoteRepository(test_db)
        await votes.cast(track.id, user_telegram_id=1, value=1)
        likes, dislikes, eff = await votes.cast(track.id, user_telegram_id=1, value=-1)
        assert (likes, dislikes, eff) == (0, 1, -1)

    async def test_switch_dislike_to_like(self, test_db, track):
        votes = TrackVoteRepository(test_db)
        await votes.cast(track.id, user_telegram_id=1, value=-1)
        likes, dislikes, eff = await votes.cast(track.id, user_telegram_id=1, value=1)
        assert (likes, dislikes, eff) == (1, 0, 1)

    async def test_multiple_users_independent(self, test_db, track):
        votes = TrackVoteRepository(test_db)
        await votes.cast(track.id, user_telegram_id=1, value=1)
        await votes.cast(track.id, user_telegram_id=2, value=1)
        likes, dislikes, _ = await votes.cast(track.id, user_telegram_id=3, value=-1)
        assert (likes, dislikes) == (2, 1)

    async def test_counters_never_negative(self, test_db, track):
        votes = TrackVoteRepository(test_db)
        # Один лайк → снять → снова снять (этого не должно случиться через handle_vote,
        # но репозиторий должен быть устойчив).
        await votes.cast(track.id, user_telegram_id=1, value=1)
        await votes.cast(track.id, user_telegram_id=1, value=1)  # снимает
        likes, dislikes, _ = await votes.cast(track.id, user_telegram_id=2, value=-1)
        assert likes == 0 and dislikes == 1

    async def test_get_user_vote(self, test_db, track):
        votes = TrackVoteRepository(test_db)
        assert await votes.get_user_vote(track.id, user_telegram_id=1) == 0
        await votes.cast(track.id, user_telegram_id=1, value=1)
        assert await votes.get_user_vote(track.id, user_telegram_id=1) == 1
        await votes.cast(track.id, user_telegram_id=1, value=-1)
        assert await votes.get_user_vote(track.id, user_telegram_id=1) == -1


class TestTrackVoteValidation:
    async def test_invalid_value_raises(self, test_db, track):
        votes = TrackVoteRepository(test_db)
        with pytest.raises(AssertionError):
            await votes.cast(track.id, user_telegram_id=1, value=0)
        with pytest.raises(AssertionError):
            await votes.cast(track.id, user_telegram_id=1, value=2)
