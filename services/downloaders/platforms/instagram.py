from __future__ import annotations

from pathlib import Path
from typing import Any

from app.logging import get_logger

from services.downloaders.base import BasePlatformDownloader
from services.downloaders.ytdlp_engine import YtDlpEngine
from services.downloaders.types import DownloadOptions, DownloadResult, MediaType

log = get_logger(__name__)


class InstagramDownloader(BasePlatformDownloader):
    """
    Загрузчик для Instagram.
    Особенность: Reels, Stories, Carousel (несколько файлов).
    """

    platform_name = "instagram"
    supported_domains = frozenset({
        "instagram.com",
        "www.instagram.com",
        "instagr.am",
    })
    use_cookies = True  # Instagram требует авторизации

    def _build_engine(self) -> YtDlpEngine:
        return YtDlpEngine(use_ejs=False)

    async def _pre_download(
        self,
        url: str,
        options: DownloadOptions,
        result: DownloadResult,
    ) -> None:
        """Instagram: без куков большинство контента недоступно"""
        cookie_file = self._get_cookie_file()
        if not cookie_file:
            log.warning(
                "Instagram cookies not found! "
                "Private content will fail. "
                "Add storage/cookies/instagram.txt"
            )

    async def _post_process(
        self,
        paths: list[Path],
        options: DownloadOptions,
        result: DownloadResult,
    ) -> list[Path]:
        """Instagram Carousel: несколько файлов — это нормально"""
        if len(paths) > 1:
            log.info("Instagram carousel detected", files=len(paths))
        return paths

    def _detect_media_type(self, info: dict[str, Any]) -> MediaType:
        if info.get("_type") == "playlist":
            entries = info.get("entries", [])
            if entries and entries[0].get("vcodec") == "none":
                return MediaType.PHOTO
            return MediaType.VIDEO
        return super()._detect_media_type(info)
