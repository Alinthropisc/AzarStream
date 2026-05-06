from typing import TYPE_CHECKING
import uuid as uuid_lib
from enum import StrEnum
from datetime import datetime
from sqlalchemy import String, Text, ForeignKey, BigInteger, JSON, Integer
from sqlalchemy.orm import Mapped, mapped_column, relationship

from models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from models.bot import Bot
    from models.user import TelegramUser


class AdMediaType(StrEnum):
    NONE = "none"
    PHOTO = "photo"
    VIDEO = "video"
    ANIMATION = "animation"


class AdStatus(StrEnum):
    DRAFT = "draft"
    SCHEDULED = "scheduled"
    SENDING = "sending"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class AdType(StrEnum):
    BROADCAST = "broadcast"           # Mass mailing to all users
    POST_DOWNLOAD = "post_download"    # Shown after each download
    SUBSCRIPTION_GATE = "subscription_gate"  # Force-subscribe campaign with goal


class Ad(Base, TimestampMixin):
    __tablename__ = "ads"

    id: Mapped[int] = mapped_column(primary_key=True)
    ad_uuid: Mapped[str] = mapped_column(String(36),default=lambda: str(uuid_lib.uuid4()),unique=True,index=True)
    name: Mapped[str] = mapped_column(String(128))  # Internal name for admin

    content: Mapped[str] = mapped_column(Text)  # Message text
    media_type: Mapped[AdMediaType] = mapped_column(default=AdMediaType.NONE)
    media_file_id: Mapped[str | None] = mapped_column(String(256))
    cache_channel_message_id: Mapped[int | None] = mapped_column(BigInteger)  # Message ID in cache channel for forwarding

    button_text: Mapped[str | None] = mapped_column(String(64))
    button_url: Mapped[str | None] = mapped_column(String(512))
    # Multi-button inline keyboard: list[{"text": str, "url": str, "row": int}]
    buttons: Mapped[list | None] = mapped_column(JSON)

    # Subscription-gate campaign fields (AdType.SUBSCRIPTION_GATE)
    subscription_channel_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    subscription_channel_username: Mapped[str | None] = mapped_column(String(128))
    subscription_channel_title: Mapped[str | None] = mapped_column(String(256))
    subscriber_goal: Mapped[int | None] = mapped_column(Integer)
    subscribers_gained: Mapped[int] = mapped_column(Integer, default=0)

    target_language: Mapped[str | None] = mapped_column(String(10), index=True)  # None = all languages

    status: Mapped[AdStatus] = mapped_column(default=AdStatus.DRAFT)
    ad_type: Mapped[AdType] = mapped_column(default=AdType.BROADCAST)
    is_active: Mapped[bool] = mapped_column(default=True)

    # Post-download ad duration settings
    duration_days: Mapped[int | None] = mapped_column()  # How many days to show (None = forever)
    expires_at: Mapped[datetime | None] = mapped_column(index=True)  # Auto-calculated expiry date

    # Auto-delete the sent ad message N seconds after delivery (None/0 = keep forever)
    auto_delete_seconds: Mapped[int | None] = mapped_column(Integer)

    scheduled_at: Mapped[datetime | None] = mapped_column()
    started_at: Mapped[datetime | None] = mapped_column()
    completed_at: Mapped[datetime | None] = mapped_column()

    total_recipients: Mapped[int] = mapped_column(default=0)
    sent_count: Mapped[int] = mapped_column(default=0)
    failed_count: Mapped[int] = mapped_column(default=0)

    target_bots: Mapped[list["AdBot"]] = relationship(back_populates="ad",cascade="all, delete-orphan")
    deliveries: Mapped[list["AdDelivery"]] = relationship(back_populates="ad",cascade="all, delete-orphan")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "ad_uuid": self.ad_uuid,
            "name": self.name,
            "content": self.content,
            "media_type": self.media_type.value,
            "target_language": self.target_language,
            "status": self.status.value,
            "sent_count": self.sent_count,
        }


class AdBot(Base):
    """Many-to-Many: Ad <-> Bot (which bots will send this ad)"""
    __tablename__ = "ad_bots"

    id: Mapped[int] = mapped_column(primary_key=True)
    ad_id: Mapped[int] = mapped_column(ForeignKey("ads.id", ondelete="CASCADE"), index=True)
    bot_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("bots.bot_id", ondelete="CASCADE"), index=True)

    ad: Mapped["Ad"] = relationship(back_populates="target_bots")
    bot: Mapped["Bot"] = relationship()


class AdDelivery(Base, TimestampMixin):
    """Track individual ad deliveries to users"""
    __tablename__ = "ad_deliveries"

    id: Mapped[int] = mapped_column(primary_key=True)
    ad_id: Mapped[int] = mapped_column(ForeignKey("ads.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("telegram_users.id", ondelete="CASCADE"), index=True)
    bot_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("bots.bot_id", ondelete="CASCADE"), index=True)

    # Telegram message info (for deletion)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    telegram_chat_id: Mapped[int | None] = mapped_column(BigInteger)

    # Status
    is_sent: Mapped[bool] = mapped_column(default=False)
    error_message: Mapped[str | None] = mapped_column(String(256))

    # Relations
    ad: Mapped["Ad"] = relationship(back_populates="deliveries")
    user: Mapped["TelegramUser"] = relationship(back_populates="ad_deliveries")
    bot: Mapped["Bot"] = relationship()
