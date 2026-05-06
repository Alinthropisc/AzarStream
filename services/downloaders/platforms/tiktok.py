from __future__ import annotations

from pathlib import Path
from typing import Any

from app.logging import get_logger

from services.downloaders.base import BasePlatformDownloader
from services.downloaders.ytdlp_engine import YtDlpEngine
from services.downloaders.types import DownloadOptions, MediaType

log = get_logger(__name__)


class TikTokDownloader(BasePlatformDownloader):
    """
    Загрузчик для TikTok.
    Особенность: watermark removal, slideshow (фото+музыка).
    """

    platform_name = "tiktok"
    supported_domains = frozenset({
        "tiktok.com",
        "www.tiktok.com",
        "vm.tiktok.com",
        "vt.tiktok.com",
    })
    use_cookies = True

    def _build_engine(self) -> YtDlpEngine:
        return YtDlpEngine(use_ejs=True)

    async def _pre_download(
        self,
        url: str,
        options: DownloadOptions,
        result,
    ) -> None:
        log.debug("TikTok: preparing no-watermark download", url=url)

    async def _do_download(
        self,
        url: str,
        options: DownloadOptions,
        output_dir: Path,
        cookie_file: Path | None,
    ) -> list[Path]:
        """TikTok: пробуем без водяного знака через специальный формат."""
        no_wm_options = DownloadOptions(
            quality=options.quality,
            audio_only=options.audio_only,
            use_aria2c=False,
            use_cookies=options.use_cookies,
            extra={"format": "download_addr-0/mp4/best"},
        )

        try:
            return await self._engine.download(
                url=url,
                options=no_wm_options,
                output_dir=output_dir,
                cookie_file=cookie_file,
            )
        except Exception:
            log.warning("TikTok no-watermark failed, trying standard download")
            return await self._engine.download(
                url=url,
                options=options,
                output_dir=output_dir,
                cookie_file=cookie_file,
            )

    def _detect_media_type(self, info: dict[str, Any]) -> MediaType:
        if info.get("_type") == "playlist" or info.get("album"):
            return MediaType.PHOTO
        return MediaType.VIDEO
