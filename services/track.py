from dataclasses import dataclass
from math import ceil

from app.logging import get_logger
from models import Track
from repositories.uow import UnitOfWork

log = get_logger("service.track")


@dataclass
class TrackDTO:
    id: int
    title: str
    artist: str | None
    duration_sec: int | None
    file_id: str
    play_count: int
    likes_count: int = 0
    dislikes_count: int = 0

    @property
    def display_title(self) -> str:
        if self.artist:
            return f"{self.artist} — {self.title}"
        return self.title

    @classmethod
    def from_model(cls, model: Track) -> "TrackDTO":
        return cls(
            id=model.id,
            title=model.title,
            artist=model.artist,
            duration_sec=model.duration_sec,
            file_id=model.file_id,
            play_count=model.play_count,
            likes_count=getattr(model, "likes_count", 0) or 0,
            dislikes_count=getattr(model, "dislikes_count", 0) or 0,
        )


@dataclass
class SearchPage:
    items: list[TrackDTO]
    page: int
    total_pages: int
    total: int
    query: str
    search_query_id: int

    @property
    def has_prev(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.total_pages


PER_PAGE = 10


class TrackService:
    """
    Поиск + воспроизведение треков. Каждый запрос фиксируется в search_queries —
    его id используется для пагинации (callback data в боте остаётся коротким).
    """

    @staticmethod
    async def search(
        query: str,
        bot_id: int,
        user_telegram_id: int | None,
        page: int = 1,
    ) -> SearchPage:
        page = max(1, int(page or 1))
        offset = (page - 1) * PER_PAGE

        async with UnitOfWork() as uow:
            rows, total = await uow.tracks.search(query, offset=offset, limit=PER_PAGE)
            sq = await uow.search_queries.log(
                bot_id=bot_id,
                user_telegram_id=user_telegram_id,
                query=query,
                results_count=total,
            )
            await uow.commit()
            return SearchPage(
                items=[TrackDTO.from_model(t) for t in rows],
                page=page,
                total_pages=max(1, ceil(total / PER_PAGE)) if total else 0,
                total=total,
                query=query,
                search_query_id=sq.id,
            )

    @staticmethod
    async def paginate(search_query_id: int, page: int) -> SearchPage | None:
        """Загрузить страницу для существующего search_query_id (callback пагинации)."""
        async with UnitOfWork() as uow:
            sq = await uow.search_queries.get_by_id(search_query_id)
            if sq is None:
                return None

            page = max(1, int(page or 1))
            offset = (page - 1) * PER_PAGE
            rows, total = await uow.tracks.search(sq.query, offset=offset, limit=PER_PAGE)
            return SearchPage(
                items=[TrackDTO.from_model(t) for t in rows],
                page=page,
                total_pages=max(1, ceil(total / PER_PAGE)) if total else 0,
                total=total,
                query=sq.query,
                search_query_id=sq.id,
            )

    @staticmethod
    async def get(track_id: int) -> TrackDTO | None:
        async with UnitOfWork() as uow:
            track = await uow.tracks.get_by_id(track_id)
            return TrackDTO.from_model(track) if track else None

    @staticmethod
    async def increment_play(track_id: int) -> None:
        async with UnitOfWork() as uow:
            await uow.tracks.increment_play(track_id)
            await uow.commit()

    @staticmethod
    async def cast_vote(
        track_id: int,
        user_telegram_id: int,
        value: int,
    ) -> tuple[int, int, int] | None:
        """
        Поставить лайк/дизлайк. Возвращает (likes, dislikes, effective)
        или None, если трек не существует.
        effective: +1 (лайк), -1 (дизлайк), 0 (снято).
        """
        async with UnitOfWork() as uow:
            track = await uow.tracks.get_by_id(track_id)
            if track is None:
                return None
            likes, dislikes, effective = await uow.track_votes.cast(
                track_id=track_id,
                user_telegram_id=user_telegram_id,
                value=value,
            )
            await uow.commit()
            return likes, dislikes, effective
