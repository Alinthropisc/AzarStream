from datetime import datetime

from sqlalchemy import func, select, text

from models import Track, TrackSource, TrackVote, TrackCacheMirror, SearchQuery
from repositories.base import BaseRepository


class TrackRepository(BaseRepository[Track]):
    model = Track

    async def get_by_source(self, platform: TrackSource, source_id: str) -> Track | None:
        return await self.get_one(source_platform=platform, source_id=source_id)

    async def search(
        self,
        query: str,
        offset: int = 0,
        limit: int = 10,
    ) -> tuple[list[Track], int]:
        """
        FULLTEXT-поиск по title + artist (ngram parser).
        Возвращает (rows, total_count).
        """
        query = (query or "").strip()
        if not query:
            return [], 0

        # Идём через raw SQL: SQLAlchemy text() не поддерживает .label/.desc
        # для участия в построенном `select`-объекте, а здесь нам нужны и
        # WHERE, и ORDER BY по результату MATCH().
        ids_sql = text(
            """
            SELECT id FROM tracks
            WHERE MATCH(title, artist) AGAINST (:q IN NATURAL LANGUAGE MODE)
            ORDER BY MATCH(title, artist) AGAINST (:q IN NATURAL LANGUAGE MODE) DESC,
                     play_count DESC
            LIMIT :lim OFFSET :off
            """
        )
        count_sql = text(
            "SELECT COUNT(*) FROM tracks "
            "WHERE MATCH(title, artist) AGAINST (:q IN NATURAL LANGUAGE MODE)"
        )

        id_rows = (await self.session.execute(
            ids_sql, {"q": query, "lim": limit, "off": offset}
        )).all()
        track_ids = [r[0] for r in id_rows]
        total = (await self.session.execute(count_sql, {"q": query})).scalar() or 0

        if not track_ids:
            return [], int(total)

        rows_seq = (await self.session.execute(
            select(Track).where(Track.id.in_(track_ids))
        )).scalars().all()
        # Сохраняем порядок по релевантности.
        order = {tid: i for i, tid in enumerate(track_ids)}
        rows = sorted(rows_seq, key=lambda t: order.get(t.id, 1 << 30))
        return list(rows), int(total)

    async def increment_play(self, track_id: int) -> None:
        track = await self.get_by_id(track_id)
        if track:
            track.play_count = (track.play_count or 0) + 1
            track.last_played_at = datetime.now()
            await self.session.flush()


class TrackCacheMirrorRepository(BaseRepository[TrackCacheMirror]):
    model = TrackCacheMirror

    async def add_mirror(
        self,
        track_id: int,
        cache_chat_id: int,
        cache_message_id: int,
        file_id: str,
        file_unique_id: str | None,
    ) -> TrackCacheMirror | None:
        existing = await self.get_one(track_id=track_id, cache_chat_id=cache_chat_id)
        if existing is not None:
            return existing
        return await self.create(
            track_id=track_id,
            cache_chat_id=cache_chat_id,
            cache_message_id=cache_message_id,
            file_id=file_id,
            file_unique_id=file_unique_id,
        )

    async def list_for_track(self, track_id: int) -> list[TrackCacheMirror]:
        rows = (await self.session.execute(
            select(TrackCacheMirror).where(TrackCacheMirror.track_id == track_id)
        )).scalars().all()
        return list(rows)


class TrackVoteRepository(BaseRepository[TrackVote]):
    model = TrackVote

    async def cast(
        self,
        track_id: int,
        user_telegram_id: int,
        value: int,
    ) -> tuple[int, int, int]:
        """
        Поставить голос (+1 или -1) или снять/переключить существующий.

        Возвращает (likes, dislikes, effective_value):
            effective_value = +1 если в итоге лайк, -1 если дизлайк, 0 если снято.

        Денормализованные счётчики на tracks обновляются атомарно через UPDATE.
        """
        assert value in (1, -1), "value must be +1 or -1"

        existing = await self.get_one(track_id=track_id, user_telegram_id=user_telegram_id)

        likes_delta = 0
        dislikes_delta = 0
        effective = 0

        if existing is None:
            await self.create(
                track_id=track_id,
                user_telegram_id=user_telegram_id,
                value=value,
            )
            if value > 0:
                likes_delta = 1
            else:
                dislikes_delta = 1
            effective = value
        elif existing.value == value:
            # тот же голос — снимаем
            await self.session.delete(existing)
            await self.session.flush()
            if value > 0:
                likes_delta = -1
            else:
                dislikes_delta = -1
            effective = 0
        else:
            # переключение
            existing.value = value
            existing.updated_at = datetime.now()
            await self.session.flush()
            if value > 0:
                likes_delta = 1
                dislikes_delta = -1
            else:
                likes_delta = -1
                dislikes_delta = 1
            effective = value

        if likes_delta or dislikes_delta:
            track = (await self.session.execute(
                select(Track).where(Track.id == track_id)
            )).scalar_one_or_none()
            if track is not None:
                track.likes_count = max(0, (track.likes_count or 0) + likes_delta)
                track.dislikes_count = max(0, (track.dislikes_count or 0) + dislikes_delta)
                await self.session.flush()

        track = (await self.session.execute(
            select(Track).where(Track.id == track_id)
        )).scalar_one_or_none()
        likes = int(track.likes_count or 0) if track else 0
        dislikes = int(track.dislikes_count or 0) if track else 0
        return likes, dislikes, effective

    async def get_user_vote(self, track_id: int, user_telegram_id: int) -> int:
        existing = await self.get_one(track_id=track_id, user_telegram_id=user_telegram_id)
        return existing.value if existing else 0


class SearchQueryRepository(BaseRepository[SearchQuery]):
    model = SearchQuery

    async def log(
        self,
        bot_id: int,
        user_telegram_id: int | None,
        query: str,
        results_count: int,
    ) -> SearchQuery:
        return await self.create(
            bot_id=bot_id,
            user_telegram_id=user_telegram_id,
            query=query,
            results_count=results_count,
        )
