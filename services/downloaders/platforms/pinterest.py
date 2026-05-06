from __future__ import annotations

from typing import Any

from app.logging import get_logger

from services.downloaders.base import BasePlatformDownloader
from services.downloaders.ytdlp_engine import YtDlpEngine
from services.downloaders.types import MediaType

log = get_logger(__name__)


class PinterestDownloader(BasePlatformDownloader):
    """
    Загрузчик для Pinterest.
    Особенность: пины могут быть видео или фото.
    """

    platform_name = "pinterest"
    supported_domains = frozenset({
        "pinterest.com",
        "www.pinterest.com",
        "pin.it",
        "pinterest.ru",
    })
    use_cookies = True

    def _build_engine(self) -> YtDlpEngine:
        return YtDlpEngine(use_ejs=False)

    def _detect_media_type(self, info: dict[str, Any]) -> MediaType:
        vcodec = info.get("vcodec", "none")
        if not vcodec or vcodec == "none":
            return MediaType.PHOTO
        return MediaType.VIDEO
