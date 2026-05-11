from __future__ import annotations

import asyncio
import shutil
import time
import re
from functools import partial
from pathlib import Path

import aiohttp
import requests

try:
    import pinterest_dl
    from pinterest_dl import PinterestDL
    HAS_PINTEREST_DL = True
except ImportError:
    HAS_PINTEREST_DL = False

import yt_dlp

from services.downloaders.cookie_manager import cookie_manager
from services.downloaders.downloader import (
    BaseDownloader,
    DownloadRequest,
    DownloadResult,
    MediaPlatform,
)

from app.logging import get_logger

log = get_logger("downloader.pinterest")



_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4_1 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

_MEDIA_EXTENSIONS = {".mp4", ".webm", ".jpg", ".jpeg", ".png", ".webp", ".gif"}
_VIDEO_EXTENSIONS = {".mp4", ".webm"}
_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


class PinterestDownloader(BaseDownloader):
    """
    Pinterest загрузчик.

    Стратегия:
    1. pinterest-dl with_api  → видео, фото, альбомы
    2. yt-dlp                 → fallback для видео
    3. requests + HTML парсинг → fallback для фото
    """

    platform = MediaPlatform.PINTEREST

    URL_PATTERNS = [
        r"(?:https?://)?(?:www\.)?pinterest\.com/pin/[\d]+",
        r"(?:https?://)?(?:www\.)?pinterest\.\w+/pin/[\d]+",
        r"(?:https?://)?pin\.it/[\w]+",
    ]

    def __init__(self, bot_id: int | None = None):
        super().__init__()
        self.bot_id = bot_id
        self.semaphore = asyncio.Semaphore(6)
        self._ua_index = 0

        if not HAS_PINTEREST_DL:
            self.log.warning("pinterest-dl not installed! Run: pip install pinterest-dl")

    # ──────────────────────────────────────────────────────────────────────────
    # URL helpers
    # ──────────────────────────────────────────────────────────────────────────

    def match_url(self, url: str) -> bool:
        return "pinterest" in url.lower() or "pin.it" in url.lower()

    def extract_id(self, url: str) -> str | None:
        m = re.search(r"/pin/(\d+)", url)
        if m:
            return m.group(1)
        m = re.search(r"pin\.it/([\w]+)", url)
        if m:
            return m.group(1)
        return None

    @staticmethod
    def _clean_pinterest_url(url: str) -> str:
        """
        Обрезаем до чистого pinterest.com/pin/ID/
        Обрабатываем:
        - /sent/?invite_code=...
        - /feedback/?invite_code=...  ← новый случай
        - /sent/
        - любые query params
        """
        # Извлекаем только домен + /pin/ID
        m = re.search(r"(https?://(?:www\.)?pinterest\.\w+/pin/\d+)", url)
        if m:
            return m.group(1) + "/"
        return url

    # ──────────────────────────────────────────────────────────────────────────
    # Headers / UA
    # ──────────────────────────────────────────────────────────────────────────

    def _next_ua(self) -> str:
        ua = _USER_AGENTS[self._ua_index % len(_USER_AGENTS)]
        self._ua_index += 1
        return ua

    def _page_headers(self) -> dict:
        return {
            "User-Agent": self._next_ua(),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
        }

    def _media_headers(self) -> dict:
        return {
            "User-Agent": self._next_ua(),
            "Referer": "https://www.pinterest.com/",
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        }

    def _video_headers(self) -> dict:
        return {
            "User-Agent": self._next_ua(),
            "Referer": "https://www.pinterest.com/",
            "Accept": "video/mp4,video/*;q=0.9,*/*;q=0.8",
            "Origin": "https://www.pinterest.com",
        }

    # ──────────────────────────────────────────────────────────────────────────
    # Cookies
    # ──────────────────────────────────────────────────────────────────────────

    def _get_cookies_path(self) -> Path | None:
        if self.bot_id:
            path = cookie_manager.get_cookie_file_path("pinterest", bot_id=self.bot_id)
            if path.exists():
                return path

        path = cookie_manager.get_cookie_file_path("pinterest")
        if path.exists():
            return path

        fallbacks = [
            Path("storage/cookies/pinterest.txt"),
            Path("storage/cookies/pinterest_cookies.txt"),
            Path.cwd() / "storage" / "cookies" / "pinterest.txt",
            Path(__file__).resolve().parent.parent.parent
            / "storage" / "cookies" / "pinterest.txt",
        ]
        for fp in fallbacks:
            if fp.exists():
                return fp
        return None

    def _load_cookies_dict(self) -> dict:
        path = self._get_cookies_path()
        if not path:
            return {}
        cookies: dict = {}
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) >= 7:
                    cookies[parts[5]] = parts[6]
        except Exception as e:
            self.log.warning("Cookie parse error", error=str(e))
        return cookies

    # ──────────────────────────────────────────────────────────────────────────
    # Main
    # ──────────────────────────────────────────────────────────────────────────

    async def download(self, request: DownloadRequest) -> DownloadResult:
        async with self.semaphore:
            try:
                real_url = await self._resolve_url(request.url)
                clean_url = self._clean_pinterest_url(real_url)
                pin_id = self.extract_id(clean_url) or str(int(time.time()))

                self.log.info(
                    "Pinterest download",
                    original=request.url,
                    clean=clean_url,
                    pin_id=pin_id,
                )

                output_dir = self.temp_dir / f"pinterest_{pin_id}"
                output_dir.mkdir(parents=True, exist_ok=True)

                is_single_pin = "/pin/" in clean_url

                # 1. yt-dlp (видео) — для одиночных пинов запускаем первым,
                #    т.к. pinterest-dl.scrape() для /pin/ URL отдаёт ПОХОЖИЕ
                #    пины (часто фото-обложку чужого пина), а не само видео.
                self.log.info("Trying yt-dlp")
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    partial(self._strategy_ytdlp, clean_url, pin_id, output_dir),
                )
                if result and result["success"]:
                    self.log.info(
                        "✅ Pinterest via yt-dlp",
                        files=len(result.get("file_paths", [])),
                        type=result.get("media_type"),
                    )
                    return self._build_result(result)

                # 2. HTML парсинг (видео + фото оригинального пина)
                self.log.info("Trying HTML + requests fallback")
                result = await self._strategy_html_media(clean_url, pin_id, output_dir)
                if result and result["success"]:
                    self.log.info(
                        "✅ Pinterest via HTML fallback",
                        type=result.get("media_type"),
                    )
                    return self._build_result(result)

                # 3. pinterest-dl — только если это НЕ одиночный пин (борды/секции).
                if HAS_PINTEREST_DL and not is_single_pin:
                    result = await asyncio.get_event_loop().run_in_executor(
                        None,
                        partial(self._strategy_pinterest_dl, clean_url, pin_id, output_dir),
                    )
                    if result and result["success"]:
                        self.log.info(
                            "✅ Pinterest via pinterest-dl",
                            files=len(result.get("file_paths", [])),
                            type=result.get("media_type"),
                        )
                        return self._build_result(result)

                return DownloadResult(
                    success=False,
                    error="❌ Не удалось скачать медиа с Pinterest",
                )

            except Exception as e:
                self.log.exception("Pinterest download failed", error=str(e))
                return DownloadResult(success=False, error=str(e))

    # ──────────────────────────────────────────────────────────────────────────
    # Strategy 1: pinterest-dl
    # ──────────────────────────────────────────────────────────────────────────

    def _strategy_pinterest_dl(
            self,
            url: str,
            pin_id: str,
            output_dir: Path,
    ) -> dict:
        try:
            cookies = self._load_cookies_dict()
            cookies_path = self._get_cookies_path()

            # Правильная инициализация через with_cookies_path или with_cookies
            client = None

            if cookies_path:
                try:
                    client = PinterestDL.with_api("", "")
                    # Применяем cookies
                    if hasattr(client, "with_cookies_path"):
                        client = client.with_cookies_path(str(cookies_path))
                    elif hasattr(client, "with_cookies"):
                        client = client.with_cookies(cookies)
                except Exception as e:
                    self.log.debug("PinterestDL with_cookies failed", error=str(e))

            if not client:
                try:
                    client = PinterestDL.with_api("", "")
                except Exception as e:
                    self.log.debug("PinterestDL init failed", error=str(e))
                    return {"success": False, "error": str(e)}

            # scrape требует num — для одного пина передаём 1
            pins = None
            if hasattr(client, "scrape"):
                try:
                    # num=1 для одного пина
                    pins = client.scrape(url, num=1)
                    self.log.debug("scrape result", count=len(pins) if pins else 0)
                except Exception as e:
                    self.log.debug("scrape failed", error=str(e))

            # Другие методы
            if not pins:
                for method_name in ["scrape_and_download", "search"]:
                    if hasattr(client, method_name):
                        try:
                            method = getattr(client, method_name)
                            pins = method(url, num=1)
                            if pins:
                                break
                        except Exception as e:
                            self.log.debug(f"{method_name} failed", error=str(e))

            if not pins:
                return {"success": False, "error": "pinterest-dl: no results"}

            return self._process_pinterest_dl_result(pins, pin_id, output_dir)

        except Exception as e:
            self.log.debug("pinterest-dl strategy failed", error=str(e))
            return {"success": False, "error": str(e)}


    def _process_pinterest_dl_result(self, pins, pin_id: str, output_dir: Path) -> dict:
        """Обработать результат pinterest-dl."""
        if not pins:
            return {"success": False, "error": "Empty result"}

        # Нормализуем в список
        if not isinstance(pins, (list, tuple)):
            pins = [pins]

        file_paths: list[str] = []
        title = ""
        has_video = False

        for i, pin in enumerate(pins):
            # Логируем структуру первого пина
            if i == 0:
                self.log.info(
                    "PinData first item",
                    type=type(pin).__name__,
                    has_dict=hasattr(pin, "__dict__"),
                    attrs={
                        k: str(getattr(pin, k, "N/A"))[:80]
                        for k in dir(pin)
                        if not k.startswith("_") and not callable(getattr(pin, k, None))
                    } if hasattr(pin, "__dict__") else str(pin)[:200],
                )

            pin_data = self._extract_pin_data_v2(pin)
            if not pin_data:
                continue

            if not title:
                title = pin_data.get("title", "")

            media_url = pin_data.get("url")
            media_type = pin_data.get("type", "image")

            if not media_url:
                continue

            if media_type == "video":
                has_video = True
                filename = f"video_{i:03d}_{pin_id}.mp4"
                headers = self._video_headers()
            else:
                ext = self._guess_ext(media_url, "image")
                filename = f"photo_{i:03d}_{pin_id}.{ext}"
                headers = self._media_headers()

            out_path = output_dir / filename
            if self._download_file_sync(media_url, out_path, headers):
                file_paths.append(str(out_path))

        if not file_paths:
            return {"success": False, "error": "No files downloaded"}

        return {
            "success": True,
            "file_paths": file_paths,
            "title": title,
            "media_type": "video" if has_video else ("album" if len(file_paths) > 1 else "image"),
        }

    def _extract_pin_data_v2(self, pin) -> dict | None:
        """Универсальный экстрактор для любой версии pinterest-dl."""
        try:
            title = ""
            video_url = None
            image_url = None

            if isinstance(pin, dict):
                # Dict формат
                title = pin.get("description") or pin.get("title") or pin.get("alt") or ""
                # pinterest-dl PinterestMedia.to_dict() -> media_stream.video.url
                media_stream = pin.get("media_stream") or {}
                video_block = media_stream.get("video") if isinstance(media_stream, dict) else None
                if isinstance(video_block, dict):
                    video_url = video_block.get("url")
                if not video_url:
                    video_url = (
                        pin.get("video_url") or pin.get("video_src")
                        or (pin.get("videos") or {}).get("url")
                    )
                image_url = (
                    pin.get("src") or pin.get("url") or pin.get("image_url")
                    or (pin.get("images") or {}).get("orig", {}).get("url")
                )
            else:
                # Object формат
                title = (
                    str(getattr(pin, "alt", "") or "")
                    or str(getattr(pin, "title", "") or "")
                    or str(getattr(pin, "description", "") or "")
                )

                # pinterest-dl PinterestMedia: видео лежит в .video_stream.url
                vs = getattr(pin, "video_stream", None)
                if vs is not None:
                    vs_url = getattr(vs, "url", None)
                    if vs_url and isinstance(vs_url, str) and vs_url.startswith("http"):
                        video_url = vs_url

                # Запасные имена атрибутов на всякий случай
                if not video_url:
                    for attr in ["video_url", "video_src", "vid_url", "mp4_url"]:
                        val = getattr(pin, attr, None)
                        if val and isinstance(val, str) and val.startswith("http"):
                            video_url = val
                            break

                # Ищем image URL (обложка/фото)
                for attr in ["src", "url", "image_url", "img_url", "original_url"]:
                    val = getattr(pin, attr, None)
                    if val and isinstance(val, str) and val.startswith("http"):
                        image_url = val
                        break

            if video_url:
                return {"type": "video", "url": video_url, "title": str(title)[:100]}
            if image_url:
                return {"type": "image", "url": image_url, "title": str(title)[:100]}

            return None

        except Exception as e:
            self.log.debug("Pin data extract error", error=str(e))
            return None

    # ──────────────────────────────────────────────────────────────────────────
    # Strategy 2: yt-dlp (только видео)
    # ──────────────────────────────────────────────────────────────────────────

    def _strategy_ytdlp(
            self,
            url: str,
            pin_id: str,
            output_dir: Path,
    ) -> dict:
        # Базовые opts — quiet подавляет большинство вывода
        ydl_opts = {
            "outtmpl": str(output_dir / f"%(autonumber)03d_{pin_id}.%(ext)s"),
            "format": (
                "bestvideo[ext=mp4]+bestaudio[ext=m4a]"
                "/bestvideo+bestaudio/best[ext=mp4]/best"
            ),
            "merge_output_format": "mp4",
            "quiet": True,
            "no_warnings": True,
            "ignoreerrors": True,  # ← True чтобы не бросал exception для фото-пинов
            "noprogress": True,
            "socket_timeout": 30,
            "retries": 3,
            "nocheckcertificate": True,
            "writethumbnail": False,
            "logtostderr": False,  # ← не писать в stderr
            # Параллелизм + крупные чанки → быстрее на больших пинах/HLS.
            "concurrent_fragment_downloads": 8,
            "http_chunk_size": 10485760,  # 10MB
            # Гарантированно копируем стримы при merge (без перекодирования).
            "postprocessor_args": {"FFmpegMerger": ["-c", "copy"]},
            "http_headers": {
                "User-Agent": self._next_ua(),
                "Referer": "https://www.pinterest.com/",
                "Accept-Language": "en-US,en;q=0.9",
            },
            # ← Перехватываем весь вывод yt-dlp через logger
            "logger": self._make_ytdlp_logger(),
        }

        cookies_path = self._get_cookies_path()
        if cookies_path:
            ydl_opts["cookiefile"] = str(cookies_path)

        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg:
            ydl_opts["ffmpeg_location"] = ffmpeg

        # aria2c — Pinterest не требует авторизации, безопасно гонять параллельно.
        if shutil.which("aria2c"):
            ydl_opts["external_downloader"] = "aria2c"
            ydl_opts["external_downloader_args"] = {
                "aria2c": [
                    "--max-connection-per-server=16",
                    "--split=16",
                    "--min-split-size=1M",
                    "--max-concurrent-downloads=4",
                    "--continue=true",
                    "--auto-file-renaming=false",
                    "--summary-interval=0",
                ]
            }

        info = None
        is_photo_pin = False

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
        except Exception as e:
            err = str(e)
            if "No video formats" in err or "no suitable formats" in err.lower():
                is_photo_pin = True
            else:
                self.log.debug("yt-dlp error", error=err[:200])

        # Проверяем скачанный info на наличие видео форматов
        if info and not is_photo_pin:
            formats = info.get("formats", [])
            has_video_fmt = any(
                f.get("vcodec", "none") != "none"
                for f in formats
            )
            if not has_video_fmt:
                is_photo_pin = True

        if not is_photo_pin:
            video_files = sorted(
                [
                    f for f in output_dir.rglob("*")
                    if f.suffix.lower() in _VIDEO_EXTENSIONS
                       and f.is_file()
                       and f.stat().st_size > 1024
                ],
                key=lambda f: f.stat().st_size,
                reverse=True,
            )

            title = ""
            if info:
                title = (info.get("description") or info.get("title") or "")[:100]

            if video_files:
                # Чистим лишние файлы
                for f in output_dir.rglob("*"):
                    if f.suffix.lower() in _IMAGE_EXTENSIONS and f.is_file():
                        f.unlink(missing_ok=True)

                return {
                    "success": True,
                    "file_paths": [str(video_files[0])],
                    "title": title,
                    "media_type": "video",
                }

        return {"success": False, "error": "No video found (photo pin)"}

    def _make_ytdlp_logger(self):
        """
        Кастомный logger для yt-dlp.
        Перехватывает все сообщения и пишет в наш structlog.
        ERROR для фото-пинов → DEBUG (не показываем пользователю).
        """
        downloader_log = self.log

        class YtDlpLogger:
            def debug(self, msg: str):
                if msg.startswith("[debug]"):
                    pass  # Игнорируем debug
                else:
                    downloader_log.debug("yt-dlp", msg=msg[:200])

            def info(self, msg: str):
                downloader_log.debug("yt-dlp info", msg=msg[:200])

            def warning(self, msg: str):
                # Фильтруем известные не-критичные предупреждения
                if any(x in msg for x in [
                    "No video formats",
                    "Requested format is not available",
                    "no suitable formats",
                ]):
                    downloader_log.debug("yt-dlp photo pin detected", msg=msg[:100])
                else:
                    downloader_log.debug("yt-dlp warning", msg=msg[:200])

            def error(self, msg: str):
                # Фото-пины генерируют ERROR — переводим в debug
                if any(x in msg for x in [
                    "No video formats found",
                    "Requested format is not available",
                    "no suitable formats",
                    "no video",
                ]):
                    downloader_log.debug("yt-dlp photo pin (suppressed)", msg=msg[:100])
                else:
                    downloader_log.warning("yt-dlp error", msg=msg[:200])

        return YtDlpLogger()

    # ──────────────────────────────────────────────────────────────────────────
    # Strategy 3: HTML парсинг для фото-пинов
    # ──────────────────────────────────────────────────────────────────────────

    async def _strategy_html_media(
        self,
        url: str,
        pin_id: str,
        output_dir: Path,
    ) -> dict | None:
        """
        HTML-фолбэк для оригинального пина:
        1. Скачиваем HTML страницы пина
        2. Сначала ищем URL видео (v.pinimg.com/videos/.../*.mp4)
        3. Если видео нет — ищем URL оригинального изображения
        4. Скачиваем через requests
        """
        try:
            html = await self._fetch_html(url)
            if not html:
                return None

            title = self._find_title_in_html(html)

            video_url = self._find_video_in_html(html)
            if video_url:
                self.log.info("Found video URL in HTML", url=video_url[:80])
                ext = self._guess_ext(video_url, "video")
                out_path = output_dir / f"video_{pin_id}.{ext}"
                ok = await asyncio.get_event_loop().run_in_executor(
                    None,
                    partial(self._download_file_sync, video_url, out_path, self._video_headers()),
                )
                if ok:
                    return {
                        "success": True,
                        "file_paths": [str(out_path)],
                        "title": title,
                        "media_type": "video",
                    }

            image_url = self._find_image_in_html(html)
            if not image_url:
                self.log.warning("No media URL found in HTML")
                return None

            self.log.info("Found image URL in HTML", url=image_url[:80])

            ext = self._guess_ext(image_url, "image")
            out_path = output_dir / f"photo_{pin_id}.{ext}"

            ok = await asyncio.get_event_loop().run_in_executor(
                None,
                partial(self._download_file_sync, image_url, out_path, self._media_headers()),
            )

            if ok:
                return {
                    "success": True,
                    "file_paths": [str(out_path)],
                    "title": title,
                    "media_type": "image",
                }

            return None

        except Exception as e:
            self.log.warning("HTML media strategy failed", error=str(e))
            return None

    def _find_video_in_html(self, html: str) -> str | None:
        """Найти прямой MP4 URL видео в HTML страницы пина."""
        patterns = [
            r'(https://v1?\.pinimg\.com/videos/[^\s"\'\\<>]+\.mp4)',
            r'"url"\s*:\s*"(https://v1?\.pinimg\.com/videos/[^"]+\.mp4)"',
            r'"contentUrl"\s*:\s*"(https://v1?\.pinimg\.com/videos/[^"]+\.mp4)"',
        ]
        candidates: list[str] = []
        for pat in patterns:
            for m in re.finditer(pat, html):
                u = m.group(1).replace("\\/", "/").replace("\\u002F", "/").strip().split("?")[0]
                if u not in candidates:
                    candidates.append(u)

        if not candidates:
            return None

        # Приоритет: HD (720p) > 480p > что есть.
        for tag in ("720p", "480p", "_720p", "hd"):
            for u in candidates:
                if tag in u.lower():
                    return u
        return candidates[0]

    async def _fetch_html(self, url: str) -> str | None:
        """Загрузить HTML страницы.

        ВАЖНО: без cookies. С залогиненными Pinterest-cookies сервер отдаёт
        SPA-скелет без inline видео URL — остаются только превью-картинки
        связанных пинов, из-за чего видео-пин подменяется фото-обложкой.
        """
        for attempt in range(3):
            try:
                connector = aiohttp.TCPConnector(ssl=False)
                timeout = aiohttp.ClientTimeout(total=20, connect=8)
                async with aiohttp.ClientSession(
                    connector=connector,
                    timeout=timeout,
                ) as session:
                    async with session.get(
                        url,
                        headers=self._page_headers(),
                        allow_redirects=True,
                    ) as resp:
                        if resp.status == 200:
                            html = await resp.text(encoding="utf-8", errors="replace")
                            if len(html) > 5000:
                                return html
                        elif resp.status == 429:
                            await asyncio.sleep(2 ** (attempt + 1))
                        else:
                            return None
            except Exception as e:
                self.log.debug("HTML fetch error", error=str(e), attempt=attempt)
                await asyncio.sleep(1 + attempt)
        return None

    def _find_image_in_html(self, html: str) -> str | None:
        """Найти URL оригинального изображения в HTML."""
        patterns = [
            # Оригинал — лучшее качество
            r'(https://i\.pinimg\.com/originals/[^\s"\'\\<>]+\.(?:jpg|jpeg|png|webp))',
            r'"url"\s*:\s*"(https://i\.pinimg\.com/originals/[^"]+)"',
            # 736x — хорошее качество
            r'(https://i\.pinimg\.com/736x/[^\s"\'\\<>]+\.(?:jpg|jpeg|png|webp))',
            r'"url"\s*:\s*"(https://i\.pinimg\.com/736x/[^"]+)"',
            # thumbnailUrl из JSON-LD
            r'"thumbnailUrl"\s*:\s*"(https://i\.pinimg\.com/[^"]+)"',
            # contentUrl из JSON-LD
            r'"contentUrl"\s*:\s*"(https://i\.pinimg\.com/[^"]+)"',
        ]

        found_urls = []
        for pattern in patterns:
            for m in re.finditer(pattern, html):
                url = m.group(1).replace("\\/", "/").replace("\\u002F", "/").strip()
                if url not in found_urls:
                    found_urls.append(url)

        if not found_urls:
            return None

        # Приоритет: originals > 736x > остальное
        for url in found_urls:
            if "/originals/" in url:
                return url.split("?")[0]

        for url in found_urls:
            if "/736x/" in url:
                return url.split("?")[0]

        return found_urls[0].split("?")[0]

    def _find_title_in_html(self, html: str) -> str:
        """Найти заголовок/описание пина."""
        # JSON-LD description
        m = re.search(r'"description"\s*:\s*"([^"]{3,200})"', html)
        if m:
            return m.group(1)

        # Title тег
        m = re.search(r'<title>([^<]+)</title>', html)
        if m:
            return m.group(1).replace(" | Pinterest", "").strip()

        return ""

    # ──────────────────────────────────────────────────────────────────────────
    # File download
    # ──────────────────────────────────────────────────────────────────────────

    def _download_file_sync(
        self,
        url: str,
        out_path: Path,
        headers: dict,
    ) -> bool:
        """Скачать файл синхронно."""
        for attempt in range(3):
            try:
                resp = requests.get(
                    url,
                    headers=headers,
                    cookies=self._load_cookies_dict(),
                    timeout=60,
                    stream=True,
                    allow_redirects=True,
                )

                if resp.status_code == 404:
                    return False

                resp.raise_for_status()

                with open(out_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=65536):
                        if chunk:
                            f.write(chunk)

                if out_path.exists() and out_path.stat().st_size > 1024:
                    return True

                out_path.unlink(missing_ok=True)
                return False

            except Exception as e:
                self.log.debug("Download attempt failed", attempt=attempt + 1, error=str(e))
                if attempt < 2:
                    time.sleep(1 + attempt)

        return False

    # ──────────────────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────────────────

    async def _resolve_url(self, url: str) -> str:
        if "pin.it" not in url:
            return url
        try:
            async with aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(ssl=False),
                timeout=aiohttp.ClientTimeout(total=10),
            ) as session:
                async with session.head(
                    url,
                    allow_redirects=True,
                    headers={"User-Agent": self._next_ua()},
                ) as resp:
                    return str(resp.url)
        except Exception as e:
            self.log.warning("URL resolve failed", error=str(e))
            return url

    @staticmethod
    def _guess_ext(url: str, media_type: str) -> str:
        clean = url.split("?")[0].lower()
        for ext in ("mp4", "webm", "gif", "webp", "png", "jpg", "jpeg"):
            if clean.endswith(f".{ext}"):
                return ext
        return "mp4" if media_type == "video" else "jpg"

    @staticmethod
    def _build_result(raw: dict) -> DownloadResult:
        paths = [
            Path(p) for p in raw.get("file_paths", [])
            if Path(p).exists() and Path(p).stat().st_size > 0
        ]
        if not paths:
            return DownloadResult(success=False, error="Файлы не найдены")

        ext = paths[0].suffix.lower()
        raw_type = raw.get("media_type", "")

        if raw_type == "video" or ext in (".mp4", ".webm"):
            media_type = "video"
        elif raw_type == "album" or len(paths) > 1:
            media_type = "album"
        elif raw_type == "audio" or ext in (".mp3", ".m4a"):
            media_type = "audio"
        else:
            media_type = "photo"

        return DownloadResult(
            success=True,
            file_path=paths[0] if len(paths) == 1 else None,
            file_paths=paths if len(paths) > 1 else None,
            title=raw.get("title") or "",
            media_type=media_type,
            platform_icon="📌",
            file_count=len(paths),
        )


