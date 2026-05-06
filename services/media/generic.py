"""
Generic yt-dlp downloader base + concrete platforms (Twitter/X, SoundCloud,
Reddit, Vimeo). yt-dlp умеет эти сайты "из коробки", поэтому хватает тонкой
обёртки с URL-детектором и парой настроек.

Если нужно добавить новую платформу — наследуйся от GenericYtDlpDownloader
и переопредели platform/domains/use_cookies/audio_only — больше ничего.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import yt_dlp

from pathlib import Path

from services.downloaders.downloader import (
    BaseDownloader,
    DownloadRequest,
    DownloadResult,
    MediaPlatform,
)


class GenericYtDlpDownloader(BaseDownloader):
    """База для платформ, где yt-dlp работает без специфики."""

    platform: MediaPlatform = MediaPlatform.OTHER
    # Список доменов для определения URL (substring match).
    domains: tuple[str, ...] = ()
    # Если True — при отсутствии cookies загрузка с приватного контента не пойдёт.
    use_cookies: bool = False
    # True для платформ типа SoundCloud где смысла качать видео нет.
    audio_only: bool = False
    # Семафор по платформе чтоб не бомбить параллелями.
    max_concurrent: int = 6

    def __init__(self) -> None:
        super().__init__()
        self.semaphore = asyncio.Semaphore(self.max_concurrent)
        self.has_ffmpeg = shutil.which("ffmpeg") is not None
        self.has_aria2c = shutil.which("aria2c") is not None

    def match_url(self, url: str) -> bool:
        return any(d in url for d in self.domains)

    def extract_id(self, url: str) -> str | None:
        m = re.search(r"/([\w\-]{6,})/?$", url.split("?")[0])
        return m.group(1) if m else None

    # ------------------------------------------------------------------
    # Скачивание
    # ------------------------------------------------------------------

    async def download(self, request: DownloadRequest) -> DownloadResult:
        item_id = self.extract_id(request.url) or "item"

        async with self.semaphore:
            output_path = str(
                self.temp_dir / f"{self.platform.value}_{item_id}.%(ext)s"
            )
            ydl_opts = self._build_ydl_opts(output_path, request)

            try:
                result = await asyncio.to_thread(
                    self._download_sync, request.url, ydl_opts
                )
                if not result["success"]:
                    return DownloadResult(success=False, error=result.get("error"))

                file_path = result.get("file_path")
                if not file_path or not Path(file_path).exists():
                    matches = list(
                        self.temp_dir.glob(f"{self.platform.value}_{item_id}*")
                    )
                    if matches:
                        file_path = str(matches[0])

                if not file_path or not Path(file_path).exists():
                    return DownloadResult(success=False, error="File not found")

                return DownloadResult(
                    success=True,
                    file_path=Path(file_path),
                    title=result.get("title") or self.platform.value.title(),
                    duration=result.get("duration"),
                    media_type="audio" if self.audio_only else "video",
                    platform=self.platform,
                    platform_icon=self.get_platform_icon(),
                    original_url=request.url,
                )
            except Exception as exc:
                self.log.exception(
                    "Generic download failed",
                    platform=self.platform.value,
                    error=str(exc),
                )
                return DownloadResult(success=False, error=str(exc))

    def _build_ydl_opts(self, output_path: str, request: DownloadRequest) -> dict:
        if self.audio_only:
            fmt = "bestaudio/best"
        else:
            fmt = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"

        opts: dict = {
            "format": fmt,
            "outtmpl": output_path,
            "merge_output_format": "mp4",
            "quiet": True,
            "no_warnings": True,
            "ignoreerrors": True,
            "noplaylist": True,
            "writethumbnail": False,
            "writeinfojson": False,
            "socket_timeout": 30,
            "retries": 5,
            "fragment_retries": 5,
            "nocheckcertificate": True,
            # Скоростные оптимизации (как в TikTok/Pinterest).
            "concurrent_fragment_downloads": 8,
            "http_chunk_size": 10485760,  # 10MB
            "postprocessor_args": {"FFmpegMerger": ["-c", "copy"]},
            "http_headers": self.get_headers(),
        }

        if self.has_ffmpeg:
            opts["ffmpeg_location"] = shutil.which("ffmpeg")

        # aria2c — только для платформ без авторизации.
        if self.has_aria2c and not self.use_cookies:
            opts["external_downloader"] = "aria2c"
            opts["external_downloader_args"] = {
                "default": [
                    "-x", "16", "-s", "16", "-k", "4M",
                    "--min-split-size=4M",
                    "--max-connection-per-server=16",
                    "--max-concurrent-downloads=8",
                    "--max-tries=5",
                    "--retry-wait=1",
                    "--timeout=15",
                    "--connect-timeout=10",
                    "--summary-interval=0",
                    "--quiet=true",
                    "--file-allocation=none",
                ]
            }

        # Cookies (если нужны и есть).
        if self.use_cookies:
            cookies_path = self.get_cookies_path(bot_id=request.bot_id)
            if cookies_path:
                opts["cookiefile"] = str(cookies_path)

        # Audio-only постобработка → mp3 для удобной отдачи в Telegram.
        if self.audio_only:
            opts["postprocessors"] = [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ]

        return opts

    def _download_sync(self, url: str, ydl_opts: dict) -> dict:
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if not info:
                    return {"success": False, "error": "Failed to extract info"}

                file_path = (
                    info.get("requested_downloads", [{}])[0].get("filepath")
                    or ydl.prepare_filename(info)
                )
                # При audio_only постпроцессор меняет расширение на .mp3.
                if self.audio_only and file_path:
                    candidate = Path(file_path).with_suffix(".mp3")
                    if candidate.exists():
                        file_path = str(candidate)

                return {
                    "success": True,
                    "file_path": file_path,
                    "title": info.get("title"),
                    "duration": info.get("duration"),
                }
        except Exception as exc:
            return {"success": False, "error": str(exc)}


# ----------------------------------------------------------------------
# Концретные платформы
# ----------------------------------------------------------------------


class TwitterDownloader(GenericYtDlpDownloader):
    """Twitter / X — публичные посты без cookies; приватные/возрастные — с cookies."""

    platform = MediaPlatform.TWITTER
    domains = ("twitter.com", "x.com", "mobile.twitter.com", "vxtwitter.com", "fxtwitter.com")
    use_cookies = True  # без них NSFW и protected недоступны


class SoundCloudDownloader(GenericYtDlpDownloader):
    """SoundCloud — только аудио."""

    platform = MediaPlatform.SOUNDCLOUD
    domains = ("soundcloud.com", "snd.sc", "on.soundcloud.com")
    audio_only = True
    use_cookies = False


class RedditDownloader(GenericYtDlpDownloader):
    """Reddit — посты с видео/гифкой. Без cookies работает на 95%."""

    platform = MediaPlatform.REDDIT
    domains = ("reddit.com", "redd.it", "v.redd.it", "old.reddit.com")
    use_cookies = False


class VimeoDownloader(GenericYtDlpDownloader):
    """Vimeo — публичные видео без cookies; приватные требуют пароль/логин."""

    platform = MediaPlatform.VIMEO
    domains = ("vimeo.com", "player.vimeo.com")
    use_cookies = True


class FacebookDownloader(GenericYtDlpDownloader):
    """Facebook — публичные видео/Reels. Без cookies большинство постов недоступно."""

    platform = MediaPlatform.FACEBOOK
    domains = ("facebook.com", "fb.watch", "fb.com", "m.facebook.com")
    use_cookies = True


class TwitchDownloader(GenericYtDlpDownloader):
    """Twitch — клипы и VOD. Live-стримы yt-dlp умеет, но мы их не качаем."""

    platform = MediaPlatform.TWITCH
    domains = ("twitch.tv", "clips.twitch.tv", "m.twitch.tv")
    use_cookies = False


class DailymotionDownloader(GenericYtDlpDownloader):
    """Dailymotion — обычные видео без cookies."""

    platform = MediaPlatform.DAILYMOTION
    domains = ("dailymotion.com", "dai.ly")
    use_cookies = False


class TumblrDownloader(GenericYtDlpDownloader):
    """Tumblr — видео-посты. Cookies нужны для NSFW."""

    platform = MediaPlatform.TUMBLR
    domains = ("tumblr.com",)
    use_cookies = True


class ThreadsDownloader(GenericYtDlpDownloader):
    """Threads (Meta) — нужны те же cookies что у Instagram, но отдельный файл."""

    platform = MediaPlatform.THREADS
    domains = ("threads.net", "threads.com")
    use_cookies = True


class SnapchatDownloader(GenericYtDlpDownloader):
    """Snapchat — публичные Spotlight'ы. yt-dlp поддерживает нестабильно."""

    platform = MediaPlatform.SNAPCHAT
    domains = ("snapchat.com", "story.snapchat.com")
    use_cookies = False


class LikeeDownloader(GenericYtDlpDownloader):
    """Likee — short-video платформа. Публичные видео, без cookies."""

    platform = MediaPlatform.LIKEE
    domains = ("likee.video", "l.likee.video", "likee.com")
    use_cookies = False
