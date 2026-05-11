from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from app.logging import get_logger

from services.downloaders.base import BasePlatformDownloader
from services.downloaders.ytdlp_engine import YtDlpEngine
from services.downloaders.types import DownloadOptions, DownloadResult, MediaType

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
AUDIO_EXTS = {".m4a", ".mp3", ".aac", ".opus", ".ogg", ".wav"}
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv"}

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
        """
        Instagram:
        - Carousel из нескольких файлов — оставляем как есть.
        - Photo + music (статичная картинка + аудиодорожка, без видео) —
          склеиваем в mp4, отдаём как VIDEO. Иначе музыка теряется,
          т.к. yt-dlp в таких постах возвращает image+audio раздельно.
        """
        videos = [p for p in paths if p.suffix.lower() in VIDEO_EXTS]
        images = [p for p in paths if p.suffix.lower() in IMAGE_EXTS]
        audios = [p for p in paths if p.suffix.lower() in AUDIO_EXTS]

        if not videos and len(images) == 1 and len(audios) == 1:
            merged = await self._merge_image_audio(images[0], audios[0])
            if merged is not None:
                log.info("Instagram photo+music merged to mp4", output=merged.name)
                result.media_type = MediaType.VIDEO
                return [merged]
            log.warning("Instagram photo+music merge failed, falling back to photo")

        if len(paths) > 1:
            log.info("Instagram carousel detected", files=len(paths))
        return paths

    async def _merge_image_audio(
        self,
        image_path: Path,
        audio_path: Path,
    ) -> Path | None:
        output = image_path.with_name(f"{image_path.stem}_merged.mp4")
        cmd = [
            "ffmpeg", "-y",
            "-loop", "1",
            "-i", str(image_path),
            "-i", str(audio_path),
            "-c:v", "libx264",
            "-tune", "stillimage",
            "-preset", "ultrafast",
            "-pix_fmt", "yuv420p",
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-c:a", "aac",
            "-b:a", "192k",
            "-shortest",
            "-movflags", "+faststart",
            str(output),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0 or not output.exists():
            log.error("ffmpeg merge failed", error=stderr.decode()[-500:])
            return None
        return output

    def _detect_media_type(self, info: dict[str, Any]) -> MediaType:
        if info.get("_type") == "playlist":
            entries = info.get("entries", [])
            if entries and entries[0].get("vcodec") == "none":
                return MediaType.PHOTO
            return MediaType.VIDEO
        return super()._detect_media_type(info)
