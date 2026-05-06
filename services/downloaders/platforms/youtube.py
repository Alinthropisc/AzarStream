from __future__ import annotations

from pathlib import Path
from typing import Any

from app.logging import get_logger

from services.downloaders.base import BasePlatformDownloader
from services.downloaders.ytdlp_engine import YtDlpEngine
from services.downloaders.types import DownloadOptions, DownloadResult, MediaType

log = get_logger(__name__)


class YouTubeDownloader(BasePlatformDownloader):
    """
    Загрузчик для YouTube.
    Переопределяет только то, что специфично для YouTube.
    """

    platform_name = "youtube"
    supported_domains = frozenset({
        "youtube.com",
        "www.youtube.com",
        "youtu.be",
        "m.youtube.com",
        "music.youtube.com",
    })
    use_cookies = True

    def _build_engine(self) -> YtDlpEngine:
        # YouTube работает лучше с yt-dlp-ejs плагинами
        return YtDlpEngine(use_ejs=True)

    async def _pre_download(
        self,
        url: str,
        options: DownloadOptions,
        result: DownloadResult,
    ) -> None:
        """YouTube: определить — музыка или видео"""
        if "music.youtube.com" in url:
            log.debug("YouTube Music detected, forcing audio mode")
            options.audio_only = True

    async def _post_process(
        self,
        paths: list[Path],
        options: DownloadOptions,
        result: DownloadResult,
    ) -> list[Path]:
        """YouTube: если audio — убедиться что mp3"""
        if options.audio_only:
            result.media_type = MediaType.AUDIO
        return paths

    def _detect_media_type(self, info: dict[str, Any]) -> MediaType:
        if info.get("categories") and "Music" in info.get("categories", []):
            return MediaType.AUDIO
        return super()._detect_media_type(info)
