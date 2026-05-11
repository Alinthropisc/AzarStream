from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base, TimestampMixin
from models.bot import _enum_values


class TrackSource(StrEnum):
    YOUTUBE = "youtube"
    SOUNDCLOUD = "soundcloud"
    UPLOAD = "upload"
    TELEGRAM_FORWARD = "telegram_forward"


class Track(Base, TimestampMixin):
    """
    Аудио-трек в библиотеке Media Search.

    Файл хранится в Telegram cache-канале (file_id), на сервере локально ничего
    не лежит. Поиск идёт через MySQL FULLTEXT(title, artist) с ngram-парсером.
    """

    __tablename__ = "tracks"
    __table_args__ = (
        UniqueConstraint("source_platform", "source_id", name="uq_tracks_source"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    title: Mapped[str] = mapped_column(String(512), nullable=False)
    artist: Mapped[str | None] = mapped_column(String(256))
    duration_sec: Mapped[int | None] = mapped_column(Integer)

    source_platform: Mapped[TrackSource] = mapped_column(
        SAEnum(TrackSource, values_callable=_enum_values, name="track_source"),
        index=True,
        nullable=False,
    )
    source_url: Mapped[str | None] = mapped_column(String(1024))
    source_id: Mapped[str | None] = mapped_column(String(128))

    # Telegram cache
    cache_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cache_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    file_id: Mapped[str] = mapped_column(String(256), nullable=False)
    file_unique_id: Mapped[str | None] = mapped_column(String(128), index=True)
    file_size: Mapped[int | None] = mapped_column(BigInteger)
    thumbnail_file_id: Mapped[str | None] = mapped_column(String(256))

    # Audit
    added_by_admin_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("admin_users.id", ondelete="SET NULL")
    )

    # Stats
    play_count: Mapped[int] = mapped_column(Integer, default=0, index=True)
    last_played_at: Mapped[datetime | None] = mapped_column(DateTime)
    likes_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    dislikes_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class TrackCacheMirror(Base):
    """
    Копия трека в дополнительном cache-канале (для отказоустойчивости).
    Первичный канал хранится прямо в tracks (cache_chat_id/file_id);
    зеркала — здесь. При отвале основного канала переключаемся на любое
    рабочее зеркало.
    """

    __tablename__ = "track_cache_mirrors"
    __table_args__ = (
        UniqueConstraint("track_id", "cache_chat_id", name="uq_track_cache_mirrors_track_chat"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    track_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("tracks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    cache_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cache_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    file_id: Mapped[str] = mapped_column(String(256), nullable=False)
    file_unique_id: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class TrackVote(Base, TimestampMixin):
    """
    Голос пользователя по треку (лайк/дизлайк).
    Один пользователь — один голос на трек. Повторное нажатие той же кнопки
    снимает голос, противоположной — переключает.
    """

    __tablename__ = "track_votes"
    __table_args__ = (
        UniqueConstraint("track_id", "user_telegram_id", name="uq_track_votes_track_user"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    track_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("tracks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    value: Mapped[int] = mapped_column(SmallInteger, nullable=False)  # +1 / -1


class SearchQuery(Base):
    """Снимок поискового запроса — для аналитики и для пагинации (state)."""

    __tablename__ = "search_queries"

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    user_telegram_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    query: Mapped[str] = mapped_column(String(256), nullable=False)
    results_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, server_default=None
    )


class IngestJobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"


class IngestSourceType(StrEnum):
    YOUTUBE = "youtube"
    SOUNDCLOUD = "soundcloud"
    FILE_UPLOAD = "file_upload"
    TELEGRAM_FORWARD = "telegram_forward"


class IngestJob(Base, TimestampMixin):
    """Длинная задача загрузки плейлиста/трека/файла в библиотеку Media Search."""

    __tablename__ = "ingest_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)

    source_type: Mapped[IngestSourceType] = mapped_column(
        SAEnum(IngestSourceType, values_callable=_enum_values, name="ingest_source"),
        nullable=False,
    )
    source_url: Mapped[str | None] = mapped_column(String(1024))
    source_filename: Mapped[str | None] = mapped_column(String(512))

    requested_by_admin_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("admin_users.id", ondelete="SET NULL")
    )
    target_cache_channel_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("cache_channels.id", ondelete="SET NULL")
    )

    status: Mapped[IngestJobStatus] = mapped_column(
        SAEnum(IngestJobStatus, values_callable=_enum_values, name="ingest_status"),
        default=IngestJobStatus.PENDING,
        index=True,
        nullable=False,
    )
    total_count: Mapped[int] = mapped_column(Integer, default=0)
    processed_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)

    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
