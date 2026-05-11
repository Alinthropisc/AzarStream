from typing import TYPE_CHECKING
from enum import StrEnum
from sqlalchemy import String, Text, Boolean, BigInteger, Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship
from models.base import Base, TimestampMixin


def _enum_values(enum_cls):
    """Хранить и читать значения членов StrEnum (не имена)."""
    return [e.value for e in enum_cls]

if TYPE_CHECKING:
    from models.download import Download
    from models.user import TelegramUser


class BotStatus(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    BANNED = "banned"


class BotType(StrEnum):
    MEDIA_STREAM = "media_stream"
    MEDIA_SEARCH = "media_search"


class Bot(Base, TimestampMixin):
    __tablename__ = "bots"

    id: Mapped[int] = mapped_column(primary_key=True)
    token: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    bot_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str | None] = mapped_column(Text)

    status: Mapped[BotStatus] = mapped_column(default=BotStatus.ACTIVE)
    bot_type: Mapped[BotType] = mapped_column(
        SAEnum(BotType, values_callable=_enum_values, name="bot_type"),
        default=BotType.MEDIA_STREAM,
        index=True,
    )
    is_webhook: Mapped[bool] = mapped_column(default=True)
    webhook_url: Mapped[str | None] = mapped_column(String(512))
    webhook_secret: Mapped[str | None] = mapped_column(String(64))

    # Stats (cached, updated periodically)
    total_users: Mapped[int] = mapped_column(default=0)
    active_users: Mapped[int] = mapped_column(default=0)  # Last 30 days
    total_downloads: Mapped[int] = mapped_column(default=0)

    # Relations
    users: Mapped[list["TelegramUser"]] = relationship(back_populates="bot", cascade="all, delete-orphan")
    downloads: Mapped[list["Download"]] = relationship(back_populates="bot")
