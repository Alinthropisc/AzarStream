from __future__ import annotations

from typing import TYPE_CHECKING

from app.logging import get_logger

if TYPE_CHECKING:
    from services.downloaders.base import BasePlatformDownloader

log = get_logger(__name__)


class PlatformRegistry:
    """
    Registry Pattern.
    Хранит все зарегистрированные платформенные загрузчики.
    Новую платформу добавить = одна строка.
    """

    def __init__(self) -> None:
        self._downloaders: dict[str, "BasePlatformDownloader"] = {}

    def register(self, downloader: "BasePlatformDownloader") -> None:
        name = downloader.platform_name.lower()
        self._downloaders[name] = downloader
        log.info("Platform registered", platform=name)

    def get(self, platform: str) -> "BasePlatformDownloader | None":
        return self._downloaders.get(platform.lower())

    def find_by_url(self, url: str) -> "BasePlatformDownloader | None":
        for downloader in self._downloaders.values():
            if downloader.can_handle(url):
                return downloader
        return None

    def all_platforms(self) -> list[str]:
        return list(self._downloaders.keys())

    def __repr__(self) -> str:
        return f"<PlatformRegistry platforms={self.all_platforms()}>"


# Global registry
registry = PlatformRegistry()