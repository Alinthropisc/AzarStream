from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

from models.download import DownloadStatus

__all__ = [
    "Platform",
    "MediaType",
    "Quality",
    "DownloadStatus",
    "DownloadOptions",
    "MediaInfo",
    "DownloadResult",
]


class Platform(StrEnum):
    YOUTUBE = "youtube"
    TIKTOK = "tiktok"
    INSTAGRAM = "instagram"
    PINTEREST = "pinterest"
    VK = "vk"
    TWITTER = "twitter"
    UNKNOWN = "unknown"


class MediaType(StrEnum):
    VIDEO = "video"
    AUDIO = "audio"
    PHOTO = "photo"
    DOCUMENT = "document"


class Quality(StrEnum):
    BEST = "best"
    Q_2160 = "2160p"
    Q_1440 = "1440p"
    Q_1080 = "1080p"
    Q_720 = "720p"
    Q_480 = "480p"
    Q_360 = "360p"
    AUDIO = "audio"


@dataclass
class DownloadOptions:
    quality: Quality = Quality.BEST
    audio_only: bool = False
    max_filesize_mb: int = 2000
    proxy: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    use_aria2c: bool = True
    use_cookies: bool = True
    embed_thumbnail: bool = False
    embed_metadata: bool = True
    format_id: str | None = None

    def with_quality(self, quality: Quality) -> "DownloadOptions":
        return replace(self, quality=quality)


@dataclass
class MediaInfo:
    """Метаданные медиа до скачивания"""

    url: str
    title: str = "Unknown"
    uploader: str = ""
    duration: int = 0
    filesize: int = 0
    thumbnail_url: str = ""
    platform: Platform = Platform.UNKNOWN
    media_type: MediaType = MediaType.VIDEO
    formats: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def filesize_str(self) -> str:
        if not self.filesize:
            return "Unknown"
        size = float(self.filesize)
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"


@dataclass
class DownloadResult:
    """Результат загрузки (новая система паттернов)"""

    original_url: str
    platform: Platform
    status: DownloadStatus = DownloadStatus.PENDING

    file_paths: list[Path] = field(default_factory=list)
    media_type: MediaType = MediaType.VIDEO
    quality: Quality = Quality.BEST

    title: str = ""
    uploader: str = ""
    duration: int = 0
    filesize: int = 0
    filesize_str: str = ""
    thumbnail_url: str = ""

    telegram_file_id: str | None = None
    caption: str = ""

    error: str | None = None
    is_too_large: bool = False

    @property
    def success(self) -> bool:
        return self.status == DownloadStatus.DONE and bool(self.file_paths)

    @property
    def failed(self) -> bool:
        return self.status == DownloadStatus.FAILED

    def mark_failed(self, error: str) -> "DownloadResult":
        self.status = DownloadStatus.FAILED
        self.error = error
        return self

    def mark_done(self, paths: list[Path]) -> "DownloadResult":
        self.status = DownloadStatus.DONE
        self.file_paths = paths
        return self
