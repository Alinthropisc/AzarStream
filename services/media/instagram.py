from __future__ import annotations

import asyncio
import http.cookiejar
import random
import re
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

import instaloader
from instaloader import Post, Profile, StoryItem

from services.downloaders.downloader import BaseDownloader, DownloadRequest, DownloadResult, MediaPlatform
from services.user_agents import get_desktop_ua
from app.config import settings
from app.logging import get_logger

log = get_logger("downloader.instagram")


class InstagramDownloader(BaseDownloader):

    platform = MediaPlatform.INSTAGRAM

    URL_PATTERNS = [
        r"(?:https?://)?(?:www\.)?instagram\.com/p/[\w-]+",
        r"(?:https?://)?(?:www\.)?instagram\.com/reel/[\w-]+",
        r"(?:https?://)?(?:www\.)?instagram\.com/reels/[\w-]+",
        r"(?:https?://)?(?:www\.)?instagram\.com/stories/[\w.]+/[\d]+",
        r"(?:https?://)?(?:www\.)?instagram\.com/[\w.]+/(?:p|reel|reels)/[\w-]+",
    ]

    _MEDIA_EXTENSIONS = {".mp4", ".jpg", ".jpeg", ".png", ".webp", ".gif", ".mov"}
    # Local Bot API даёт лимит 2GB; обычный Bot API — 50MB.
    # Если включён local API, перекодирование запускается только для реально больших файлов.
    _MAX_VIDEO_SIZE_MB = 1900 if settings.telegram_api_local else 45
    # Куки файл на диске — используем напрямую для yt-dlp
    _COOKIES_FILE = Path("storage/cookies/instagram.txt")

    def __init__(self, bot_id: int | None = None) -> None:
        super().__init__()
        self.bot_id = bot_id
        self.semaphore = asyncio.Semaphore(2)
        self.has_ffmpeg = shutil.which("ffmpeg") is not None
        self._ffmpeg_path = shutil.which("ffmpeg")
        self._loader = self._build_loader()

        log.info(
            "InstagramDownloader initialized",
            has_ffmpeg=self.has_ffmpeg,
            session_loaded=self._has_valid_session(),
            cookies_file_exists=self._COOKIES_FILE.exists(),
        )

    # ══════════════════════ SESSION / LOADER ══════════════════════

    def _build_loader(self) -> instaloader.Instaloader:
        loader = instaloader.Instaloader(
            download_comments=False,
            download_video_thumbnails=False,
            save_metadata=False,
            download_geotags=False,
            post_metadata_txt_pattern="",
            storyitem_metadata_txt_pattern="",
            quiet=True,
            compress_json=False,
            max_connection_attempts=3,
            request_timeout=30,
        )
        loader.context._session.headers.update({
            "User-Agent": get_desktop_ua(),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "DNT": "1",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Cache-Control": "max-age=0",
        })
        self._try_load_session(loader)
        return loader

    def _try_load_session(self, loader: instaloader.Instaloader) -> None:
        """
        Загружает сессию.
        Порядок: bot_id cookies → общие cookies → файл на диске.
        """
        # 1. Пробуем через cookie_manager
        try:
            from services.downloaders.cookie_manager import cookie_manager
            session_content: str | None = None
            if self.bot_id:
                session_content = cookie_manager.get_cookies_for_bot(self.bot_id, "instagram")
            if not session_content:
                session_content = cookie_manager.load_cookies("instagram")
            if session_content:
                if "# Netscape HTTP Cookie File" in session_content:
                    self._load_netscape_cookies(loader, session_content)
                else:
                    self._load_instaloader_session(loader, session_content)
                return
        except Exception as exc:
            log.debug("cookie_manager failed", error=str(exc))

        # 2. Прямой файл на диске
        if self._COOKIES_FILE.exists() and self._COOKIES_FILE.stat().st_size > 0:
            try:
                content = self._COOKIES_FILE.read_text(encoding="utf-8")
                if "# Netscape HTTP Cookie File" in content:
                    self._load_netscape_cookies(loader, content)
                    log.info("Loaded cookies from disk file", path=str(self._COOKIES_FILE))
                    return
            except Exception as exc:
                log.error("Failed to load cookies from disk", error=str(exc))

        log.warning("No Instagram cookies found — working anonymously")

    def _load_netscape_cookies(
        self, loader: instaloader.Instaloader, content: str
    ) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as tmp:
            tmp.write(content)
            tmp_path = tmp.name
        try:
            cj = http.cookiejar.MozillaCookieJar(tmp_path)
            cj.load(ignore_discard=True, ignore_expires=True)
            loader.context._session.cookies.update(cj)

            csrf_token = next((c.value for c in cj if c.name == "csrftoken"), None)
            sessionid = next((c.value for c in cj if c.name == "sessionid"), None)

            if csrf_token:
                loader.context._session.headers.update({
                    "X-CSRFToken": csrf_token,
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": "https://www.instagram.com/",
                })

            if sessionid:
                log.info("Instagram Netscape cookies loaded", has_csrf=bool(csrf_token))
            else:
                log.warning("sessionid not found in cookies file")
        except Exception as exc:
            log.error("Failed to load Netscape cookies", error=str(exc))
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    def _load_instaloader_session(
        self, loader: instaloader.Instaloader, content: str
    ) -> None:
        tmp = Path(tempfile.mktemp(suffix=".session"))
        try:
            tmp.write_text(content)
            loader.load_session_from_file(username="", filename=str(tmp))
            log.info("Instaloader session loaded")
        except Exception as exc:
            log.error("Failed to load instaloader session", error=str(exc))
        finally:
            tmp.unlink(missing_ok=True)

    def _has_valid_session(self) -> bool:
        try:
            return bool(self._loader.context._session.cookies.get("sessionid"))
        except Exception:
            return False

    def _reload_session(self) -> None:
        """Перезагружает сессию (вызывается после 403)."""
        try:
            self._loader.context._session.cookies.clear_session_cookies()
            self._try_load_session(self._loader)
        except Exception as exc:
            log.warning("Session reload failed", error=str(exc))

    # ══════════════════════ URL HELPERS ══════════════════════

    def match_url(self, url: str) -> bool:
        return "instagram.com" in url

    def extract_id(self, url: str) -> str | None:
        patterns = [
            r"/p/([\w-]+)",
            r"/reel/([\w-]+)",
            r"/reels/([\w-]+)",
            r"/stories/[\w.]+/([\d]+)",
        ]
        for pattern in patterns:
            m = re.search(pattern, url)
            if m:
                return m.group(1)
        return None

    @staticmethod
    def _shortcode_from_url(url: str) -> str:
        return urlparse(url.split("?")[0].rstrip("/")).path.split("/")[-1]

    def _detect_content_type(self, url: str) -> str:
        if "/p/" in url:
            return "post"
        if "/reel/" in url or "/reels/" in url:
            return "reel"
        if "/stories/" in url:
            return "story"
        raise ValueError(f"Unsupported Instagram URL: {url}")

    # ══════════════════════ MAIN DOWNLOAD ══════════════════════

    async def download(self, request: DownloadRequest) -> DownloadResult:
        shortcode = self.extract_id(request.url) or "unknown"

        async with self.semaphore:
            output_dir = self.temp_dir / f"instagram_{shortcode}"
            output_dir.mkdir(parents=True, exist_ok=True)

            try:
                result = await asyncio.to_thread(
                    self._download_sync, request.url, output_dir
                )

                if not result["success"]:
                    return DownloadResult(success=False, error=result.get("error"))

                file_paths = result.get("file_paths", [])
                if not file_paths:
                    return DownloadResult(success=False, error="No media files found")

                paths = [Path(p) for p in file_paths]
                paths = await self._compress_videos(paths)
                # Фильтруем несуществующие файлы
                paths = [p for p in paths if p.exists()]
                paths.sort(key=lambda p: p.stat().st_size, reverse=True)

                media_type = result.get("media_type", "video")
                if len(paths) > 1:
                    media_type = "album"

                return DownloadResult(
                    success=True,
                    file_paths=paths,
                    title=result.get("title", f"Instagram {shortcode}"),
                    media_type=media_type,
                )

            except Exception as exc:
                log.exception("Instagram download failed", error=str(exc))
                return DownloadResult(success=False, error=str(exc))

    def _download_sync(self, url: str, output_dir: Path) -> dict:
        try:
            content_type = self._detect_content_type(url)
            log.info("Starting download", content_type=content_type, url=url[:80])

            # Анти-бан задержка
            time.sleep(random.uniform(1.0, 2.5))

            if content_type in ("post", "reel", "reels"):
                return self._handle_post_sync(url, output_dir)
            if content_type == "story":
                return self._handle_story_sync(url, output_dir)

            return {"success": False, "error": f"Unsupported content type: {content_type}"}
        except Exception as exc:
            return {"success": False, "error": self._humanize_error(str(exc))}

    # ══════════════════════ POST HANDLING ══════════════════════

    def _handle_post_sync(self, url: str, output_dir: Path) -> dict:
        try:
            shortcode = self._shortcode_from_url(url)
            log.info("Fetching post metadata", shortcode=shortcode)

            post = self._fetch_post_with_retry(shortcode)
            if not post:
                log.warning("Metadata fetch failed, trying yt-dlp directly")
                target = output_dir / shortcode
                target.mkdir(parents=True, exist_ok=True)
                ytdlp_result = self._try_ytdlp_download(url, target, shortcode)
                if ytdlp_result:
                    files, media_type = ytdlp_result
                    return {
                        "success": True,
                        "file_paths": files,
                        "title": f"Instagram {shortcode}",
                        "media_type": media_type,
                    }
                return {"success": False, "error": "Failed to fetch post"}

            target = output_dir / shortcode
            target.mkdir(parents=True, exist_ok=True)

            log.info(
                "Post info",
                shortcode=shortcode,
                typename=post.typename,
                is_video=post.is_video,
                has_video_url=bool(getattr(post, "video_url", None)),
            )

            # ── Карусель ─────────────────────────────────────────────
            if post.typename in ("GraphSidecar", "XDTGraphSidecar"):
                log.info("Downloading carousel/album", shortcode=shortcode)
                files = self._download_sidecar(post, target)

                if not files:
                    # Последний шанс — yt-dlp
                    log.warning("Sidecar empty, trying yt-dlp")
                    ytdlp_result = self._try_ytdlp_download(url, target, shortcode)
                    if ytdlp_result:
                        files, media_type = ytdlp_result
                        return {
                            "success": True,
                            "file_paths": files,
                            "title": (getattr(post, "caption", None) or shortcode)[:100],
                            "media_type": media_type,
                        }
                    return {"success": False, "error": "Failed to download carousel"}

                # Для карусели media_type зависит от содержимого
                has_video = any(Path(f).suffix.lower() == ".mp4" for f in files)
                media_type = "album" if len(files) > 1 else ("video" if has_video else "photo")
                return self._make_result(files, post, shortcode, media_type)

            # ── Видео / Reel ──────────────────────────────────────────
            if post.is_video:
                log.info("Downloading video/reel", shortcode=shortcode)
                return self._download_video_post(post, url, target, shortcode)

            # ── Фото (возможно с музыкой) ─────────────────────────────
            log.info("Downloading photo post", shortcode=shortcode)
            return self._handle_photo_post(post, url, target, shortcode)

        except instaloader.exceptions.LoginRequiredException:
            return {
                "success": False,
                "error": "Login required — content is private or cookies expired",
            }
        except instaloader.exceptions.BadResponseException as exc:
            if "403" in str(exc):
                return {
                    "success": False,
                    "error": "Instagram blocked (403) — update cookies or wait 10 min",
                }
            return {"success": False, "error": self._humanize_error(str(exc))}
        except Exception as exc:
            return {"success": False, "error": self._humanize_error(str(exc))}

    def _fetch_post_with_retry(
        self, shortcode: str, max_attempts: int = 3
    ) -> Post | None:
        """Получает метаданные поста с повторными попытками."""
        last_error = ""
        for attempt in range(max_attempts):
            try:
                if attempt > 0:
                    time.sleep(attempt * 3)
                    self._reload_session()
                post = Post.from_shortcode(self._loader.context, shortcode)
                log.debug("Post fetched successfully", attempt=attempt + 1)
                return post
            except instaloader.exceptions.LoginRequiredException:
                raise  # Не повторяем — нужен логин
            except Exception as exc:
                last_error = str(exc)
                log.warning(
                    "Post fetch attempt failed",
                    attempt=attempt + 1,
                    max=max_attempts,
                    error=last_error[:80],
                )
                if "403" in last_error or "401" in last_error:
                    time.sleep((attempt + 1) * 5)
                elif "404" in last_error or "not found" in last_error.lower():
                    break  # Пост не существует — не повторяем
        log.error("All fetch attempts failed", shortcode=shortcode, error=last_error)
        return None

    def _download_video_post(
        self, post: Post, url: str, target: Path, shortcode: str
    ) -> dict:
        """Скачивает видео/Reel пост."""
        # Сначала пробуем yt-dlp (лучшее качество)
        ytdlp_result = self._try_ytdlp_download(url, target, shortcode)
        if ytdlp_result:
            files, media_type = ytdlp_result
            return self._make_result(files, post, shortcode, media_type)

        # Fallback: instaloader
        try:
            self._loader.download_post(post, target=target)
            self._cleanup_metadata(target)
            files = self._collect_media(target)
            # Предпочитаем mp4
            mp4_files = [f for f in files if Path(f).suffix.lower() == ".mp4"]
            files = mp4_files if mp4_files else files
            return self._make_result(files, post, shortcode, "video")
        except Exception as exc:
            log.error("Video download failed", error=str(exc))
            return {"success": False, "error": self._humanize_error(str(exc))}

    def _handle_photo_post(
        self, post: Post, url: str, target: Path, shortcode: str
    ) -> dict:
        """
        Обрабатывает фото-посты.

        Порядок попыток:
        1. yt-dlp — умеет видеть фото+музыка как mp4
        2. Прямой video_url из метаданных (фото с музыкой)
        3. GraphQL API запрос за video_url
        4. FFmpeg merge фото + audio_url
        5. Fallback — просто фото
        """
        # ── 1. yt-dlp ──────────────────────────────────────────────
        ytdlp_result = self._try_ytdlp_download(url, target, shortcode)
        if ytdlp_result:
            files, media_type = ytdlp_result
            return self._make_result(files, post, shortcode, media_type)

        # ── 2. video_url из метаданных instaloader ─────────────────
        video_url = self._extract_video_url(post)

        # ── 3. GraphQL API ─────────────────────────────────────────
        if not video_url:
            api_data = self._fetch_post_api(shortcode)
            if api_data:
                video_url = self._recursive_find_video_url(api_data, 0, 6)
                if video_url:
                    log.info("video_url found via API", shortcode=shortcode)

        # ── Скачиваем video_url ────────────────────────────────────
        if video_url:
            downloaded = self._download_url_as_file(
                video_url, target / f"{shortcode}_with_audio.mp4"
            )
            if downloaded:
                return self._make_result([str(downloaded)], post, shortcode, "video")

        # ── 4. instaloader скачивает фото ──────────────────────────
        try:
            self._loader.download_post(post, target=target)
            self._cleanup_metadata(target)
        except Exception as exc:
            log.warning("instaloader photo download failed", error=str(exc))

        photo_files = self._collect_media(target)

        # ── 5. FFmpeg merge фото + audio_url ───────────────────────
        node = getattr(post, "_node", {})
        if isinstance(node, dict) and photo_files and self.has_ffmpeg:
            audio_url = self._extract_audio_url(node)
            if audio_url:
                log.info("Trying FFmpeg merge photo+audio", shortcode=shortcode)
                merged = self._merge_photo_audio_sync(
                    photo_path=Path(photo_files[0]),
                    audio_url=audio_url,
                    output_path=target / f"{shortcode}_merged.mp4",
                )
                if merged:
                    return self._make_result([str(merged)], post, shortcode, "video")

        # ── Fallback: просто фото ──────────────────────────────────
        if photo_files:
            log.info("Returning pure photo", shortcode=shortcode)
            return self._make_result(photo_files, post, shortcode, "photo")

        return {"success": False, "error": "No media found"}

    # ══════════════════════ CAROUSEL / SIDECAR ══════════════════════

    def _download_sidecar(self, post: Post, target: Path) -> list[str]:
        log.info("Downloading sidecar/carousel")

        nodes = list(enumerate(post.get_sidecar_nodes()))
        if not nodes:
            try:
                self._loader.download_post(post, target=target)
                self._cleanup_metadata(target)
                return self._collect_media(target)
            except Exception as exc:
                log.error("Sidecar fallback failed", error=str(exc))
                return []

        def _download_node(index_node: tuple[int, object]) -> str | None:
            index, node = index_node
            time.sleep(random.uniform(0.3, 1.0))
            try:
                date_str = f"{post.date_utc:%Y-%m-%d_%H-%M-%S}_UTC"

                if node.is_video:
                    filename = f"{date_str}_{index + 1}.mp4"
                    url = node.video_url
                else:
                    filename = f"{date_str}_{index + 1}.jpg"
                    url = node.display_url

                log.debug(
                    f"Downloading node {index + 1}",
                    filename=filename,
                    url=url[:60] if url else "NO URL",
                    is_video=node.is_video,
                )

                if not url:
                    log.warning(f"Node {index + 1} has no URL, skipping")
                    return None

                output_path = target / filename

                # ── Способ 1: instaloader download_pic ──────────────────
                try:
                    self._loader.download_pic(
                        filename=str(output_path),
                        url=url,
                        mtime=post.date_utc,
                    )
                    if output_path.exists() and output_path.stat().st_size > 1024:
                        log.debug(f"Node {index + 1} downloaded via instaloader")
                        return str(output_path)
                except Exception as exc:
                    log.warning(
                        f"instaloader download_pic failed for node {index + 1}",
                        error=str(exc)[:100],
                    )

                # ── Способ 2: прямой HTTP запрос ─────────────────────────
                try:
                    headers = dict(self._loader.context._session.headers)
                    headers["Referer"] = "https://www.instagram.com/"
                    resp = self._loader.context._session.get(
                        url, headers=headers, stream=True, timeout=30
                    )
                    if resp.status_code == 200:
                        with open(output_path, "wb") as fh:
                            for chunk in resp.iter_content(chunk_size=65536):
                                if chunk:
                                    fh.write(chunk)
                        if output_path.exists() and output_path.stat().st_size > 1024:
                            log.debug(f"Node {index + 1} downloaded via direct HTTP")
                            return str(output_path)
                    else:
                        log.warning(
                            f"Direct HTTP failed for node {index + 1}",
                            status=resp.status_code,
                        )
                except Exception as exc:
                    log.warning(
                        f"Direct HTTP download failed for node {index + 1}",
                        error=str(exc)[:100],
                    )

                return None

            except Exception as exc:
                log.warning(f"Node {index + 1} completely failed", error=str(exc)[:100])
                return None

        with ThreadPoolExecutor(max_workers=1) as pool:
            results = list(pool.map(_download_node, nodes))

        self._cleanup_metadata(target)
        files = [f for f in results if f and Path(f).exists()]
        log.info("Sidecar downloaded", total=len(nodes), success=len(files))

        # ── Fallback: download_post целиком если всё упало ───────────
        if not files:
            log.warning("All nodes failed, trying download_post fallback")
            try:
                self._loader.download_post(post, target=target)
                self._cleanup_metadata(target)
                files = self._collect_media(target)
                log.info("Fallback download_post success", files=len(files))
            except Exception as exc:
                log.error("Fallback download_post failed", error=str(exc))

        return files

    # ══════════════════════ VIDEO URL EXTRACTION ══════════════════════

    def _extract_video_url(self, post: Post) -> str | None:
        """Ищет video_url во всех известных местах объекта Post."""
        # Прямой атрибут
        url = getattr(post, "video_url", None)
        if url and isinstance(url, str) and url.startswith("http"):
            log.debug("video_url from post.video_url")
            return url

        node = getattr(post, "_node", {})
        if not isinstance(node, dict):
            return None

        # Прямо в node
        url = node.get("video_url")
        if url and isinstance(url, str) and url.startswith("http"):
            log.debug("video_url from node root")
            return url

        # Известные вложенные пути
        nested_paths = [
            ["video_versions", 0, "url"],
            ["clips_media_info", "video_url"],
            ["clips_media_info", "video_versions", 0, "url"],
            ["xdt_api__v1__media__shortcode__web_info", "data", "xdt_shortcode_media", "video_url"],
            ["edge_media_to_video", "edges", 0, "node", "video_url"],
            ["inline_video", "url"],
        ]
        for path in nested_paths:
            val = self._deep_get(node, path)
            if val and isinstance(val, str) and val.startswith("http"):
                log.debug("video_url found via nested path", path=str(path))
                return val

        # Рекурсивный поиск (последний шанс)
        url = self._recursive_find_video_url(node, depth=0, max_depth=5)
        if url:
            log.debug("video_url found via recursive search")
            return url

        return None

    def _fetch_post_api(self, shortcode: str) -> dict | None:
        """Запрашивает метаданные через Instagram API."""
        api_urls = [
            f"https://www.instagram.com/p/{shortcode}/?__a=1&__d=dis",
            (
                f"https://www.instagram.com/graphql/query/"
                f"?query_hash=2b0673e0dc4580674a88d426fe00ea90"
                f"&variables=%7B%22shortcode%22%3A%22{shortcode}%22%7D"
            ),
        ]
        headers = dict(self._loader.context._session.headers)
        headers.update({
            "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"https://www.instagram.com/p/{shortcode}/",
        })

        for api_url in api_urls:
            try:
                resp = self._loader.context._session.get(
                    api_url, headers=headers, timeout=15
                )
                if resp.status_code == 200:
                    data = resp.json()
                    log.debug("API response received", keys=list(data.keys())[:5])
                    return data
            except Exception as exc:
                log.debug("API request failed", url=api_url[:60], error=str(exc)[:60])

        return None

    @staticmethod
    def _deep_get(data: dict | list, path: list) -> str | None:
        """Безопасное получение вложенного значения по пути ключей."""
        current = data
        for key in path:
            try:
                if isinstance(current, dict):
                    current = current[key]
                elif isinstance(current, list):
                    current = current[int(key)]
                else:
                    return None
            except (KeyError, IndexError, TypeError):
                return None
        return current if isinstance(current, str) else None

    def _recursive_find_video_url(
        self, data: object, depth: int, max_depth: int
    ) -> str | None:
        """Рекурсивно ищет video_url в JSON-дереве."""
        if depth > max_depth:
            return None

        if isinstance(data, dict):
            # Ключи с видео URL
            for key in ("video_url", "playback_url"):
                val = data.get(key)
                if val and isinstance(val, str) and val.startswith("http"):
                    return val

            # video_versions — список качеств, берём первое (лучшее)
            versions = data.get("video_versions")
            if isinstance(versions, list) and versions:
                first = versions[0]
                if isinstance(first, dict):
                    url = first.get("url")
                    if url and isinstance(url, str) and url.startswith("http"):
                        return url

            for val in data.values():
                if isinstance(val, (dict, list)):
                    result = self._recursive_find_video_url(val, depth + 1, max_depth)
                    if result:
                        return result

        elif isinstance(data, list):
            for item in data:
                if isinstance(item, (dict, list)):
                    result = self._recursive_find_video_url(item, depth + 1, max_depth)
                    if result:
                        return result

        return None

    # ══════════════════════ YT-DLP ══════════════════════

    def _try_ytdlp_download(
        self, url: str, target: Path, shortcode: str
    ) -> tuple[list[str], str] | None:
        """
        Пробует скачать через yt-dlp.
        Преимущества перед instaloader:
        - Корректно скачивает фото+музыка → mp4
        - Лучше работает с Reels
        - Поддерживает приватный контент через cookies файл
        """
        try:
            import yt_dlp
        except ImportError:
            log.warning("yt-dlp not available")
            return None

        output_template = str(target / f"{shortcode}_ytdlp.%(ext)s")

        # Используем файл cookies напрямую с диска (наиболее надёжно)
        cookies_file: str | None = None
        if self._COOKIES_FILE.exists():
            cookies_file = str(self._COOKIES_FILE)
        else:
            cookies_file = self._export_cookies_from_session()

        class _SilentLogger:
            def debug(self, msg: str) -> None: pass
            def info(self, msg: str) -> None: pass
            def warning(self, msg: str) -> None: pass
            def error(self, msg: str) -> None:
                if msg and "No video formats" not in msg:
                    log.debug("yt-dlp error", msg=msg[:150])

        ydl_opts: dict = {
            "outtmpl": output_template,
            "logger": _SilentLogger(),
            "quiet": True,
            "no_warnings": True,
            # Видео + аудио; если нет отдельного видео — берём лучшее доступное
            "format": "bestvideo[ext=mp4]+bestaudio/bestvideo+bestaudio/mp4/best",
            "merge_output_format": "mp4",
            "socket_timeout": 30,
            "retries": 3,
            "fragment_retries": 5,
            # Ускорение: параллельные фрагменты + крупные HTTP-чанки + copy при merge.
            # Aria2c для Instagram НЕ ставим — агрессивные коннекшены под куками
            # быстро ловят rate limit / challenge.
            "concurrent_fragment_downloads": 8,
            "http_chunk_size": 10485760,  # 10MB
            "postprocessor_args": {"FFmpegMerger": ["-c", "copy"]},
            "http_headers": {
                "User-Agent": get_desktop_ua(),
                "Referer": "https://www.instagram.com/",
                "Origin": "https://www.instagram.com",
            },
        }

        if cookies_file:
            ydl_opts["cookiefile"] = cookies_file

        try:
            log.info("yt-dlp attempt", url=url[:80])
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)

            if not info:
                log.debug("yt-dlp: no info returned")
                return None

            # Собираем только файлы от yt-dlp (по маркеру _ytdlp в имени)
            all_files = self._collect_media(target)
            ytdlp_files = [f for f in all_files if "_ytdlp" in Path(f).name]

            if not ytdlp_files:
                log.debug("yt-dlp: output files not found")
                return None

            has_video = any(
                Path(f).suffix.lower() in (".mp4", ".mov", ".mkv")
                for f in ytdlp_files
            )
            has_audio = (
                info.get("acodec") and info.get("acodec") not in ("none", None)
            )
            media_type = "video" if has_video else "photo"

            log.info(
                "yt-dlp success",
                files=len(ytdlp_files),
                media_type=media_type,
                has_audio=has_audio,
                ext=info.get("ext"),
            )
            return ytdlp_files, media_type

        except Exception as exc:
            err = str(exc)
            if "No video formats found" in err or "Unsupported URL" in err:
                log.debug("yt-dlp: no video format available")
            else:
                log.debug("yt-dlp failed", error=err[:120])
            return None

    def _export_cookies_from_session(self) -> str | None:
        """Экспортирует cookies из сессии в Netscape формат для yt-dlp."""
        try:
            cookies = self._loader.context._session.cookies
            if not cookies:
                return None

            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".txt", delete=False, prefix="ig_ck_"
            ) as tmp:
                tmp.write("# Netscape HTTP Cookie File\n")
                tmp.write("# Generated automatically\n\n")
                for cookie in cookies:
                    domain = cookie.domain or ".instagram.com"
                    flag = "TRUE" if domain.startswith(".") else "FALSE"
                    path = cookie.path or "/"
                    secure = "TRUE" if cookie.secure else "FALSE"
                    expires = int(cookie.expires) if cookie.expires else 0
                    tmp.write(
                        f"{domain}\t{flag}\t{path}\t"
                        f"{secure}\t{expires}\t{cookie.name}\t{cookie.value or ''}\n"
                    )
                return tmp.name
        except Exception as exc:
            log.debug("Cookie export failed", error=str(exc))
            return None

    # ══════════════════════ DOWNLOAD HELPERS ══════════════════════

    def _download_url_as_file(self, url: str, output_path: Path) -> Path | None:
        """Скачивает URL напрямую через сессию instaloader."""
        try:
            headers = dict(self._loader.context._session.headers)
            headers["Referer"] = "https://www.instagram.com/"

            resp = self._loader.context._session.get(
                url, headers=headers, stream=True, timeout=60
            )

            if resp.status_code != 200:
                log.warning("Direct download failed", status=resp.status_code, url=url[:60])
                return None

            with open(output_path, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        fh.write(chunk)

            if output_path.exists() and output_path.stat().st_size > 4096:
                size_mb = output_path.stat().st_size / 1024 / 1024
                log.info("Direct download success", size_mb=round(size_mb, 2))
                return output_path

            log.warning("Downloaded file too small, ignoring")
            output_path.unlink(missing_ok=True)
            return None

        except Exception as exc:
            log.warning("Direct download error", error=str(exc)[:80])
            output_path.unlink(missing_ok=True)
            return None

    # ══════════════════════ AUDIO HELPERS ══════════════════════

    def _extract_audio_url(self, node: dict) -> str | None:
        """Ищет URL аудио-трека в метаданных поста."""
        if not isinstance(node, dict):
            return None

        # clips_music_attribution_info — основной источник
        clips = node.get("clips_music_attribution_info", {})
        if isinstance(clips, dict):
            for key in ("audio_asset_url", "song_url", "audio_url"):
                val = clips.get(key)
                if val and isinstance(val, str) and val.startswith("http"):
                    return val

        # Прямой audio_url
        audio_url = node.get("audio_url")
        if audio_url and isinstance(audio_url, str):
            return audio_url

        return None

    def _merge_photo_audio_sync(
        self,
        photo_path: Path,
        audio_url: str,
        output_path: Path,
    ) -> Path | None:
        """FFmpeg: статичное фото + аудио-трек → mp4 видео."""
        if not self.has_ffmpeg:
            return None

        audio_path = photo_path.parent / f"_audio_{photo_path.stem}.tmp"
        try:
            # Скачиваем аудио
            resp = self._loader.context._session.get(audio_url, timeout=30)
            if resp.status_code != 200:
                log.warning("Audio download failed", status=resp.status_code)
                return None
            audio_path.write_bytes(resp.content)

            if audio_path.stat().st_size < 1024:
                log.warning("Audio file too small")
                return None

            cmd = [
                self._ffmpeg_path,
                "-loop", "1",
                "-i", str(photo_path),
                "-i", str(audio_path),
                "-c:v", "libx264",
                "-tune", "stillimage",
                "-c:a", "aac",
                "-b:a", "192k",
                "-pix_fmt", "yuv420p",
                "-shortest",
                "-movflags", "+faststart",
                "-y",
                str(output_path),
            ]

            proc = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=120,
            )

            if output_path.exists() and output_path.stat().st_size > 4096:
                size_mb = output_path.stat().st_size / 1024 / 1024
                log.info("FFmpeg merge success", size_mb=round(size_mb, 2))
                return output_path

            log.warning("FFmpeg merge failed", stderr=proc.stderr.decode()[-200:])
            return None

        except subprocess.TimeoutExpired:
            log.error("FFmpeg merge timed out")
            return None
        except Exception as exc:
            log.error("FFmpeg merge error", error=str(exc))
            return None
        finally:
            audio_path.unlink(missing_ok=True)
            # Чистим пустой output если он создался
            if output_path.exists() and output_path.stat().st_size <= 4096:
                output_path.unlink(missing_ok=True)

    # ══════════════════════ STORY ══════════════════════

    def _handle_story_sync(self, url: str, output_dir: Path) -> dict:
        try:
            parts = url.split("/stories/")[1].rstrip("/").split("/")
            username = parts[0]
            story_id = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None

            log.info("Downloading story", username=username, story_id=story_id)

            profile = Profile.from_username(self._loader.context, username)
            target = output_dir / "stories"
            target.mkdir(parents=True, exist_ok=True)

            items: list[StoryItem] = []
            for story in self._loader.get_stories(userids=[profile.userid]):
                for item in story.get_items():
                    if story_id and item.mediaid != story_id:
                        continue
                    items.append(item)
                    if story_id:
                        break  # Нашли нужный элемент

            if not items:
                return {
                    "success": False,
                    "error": "Story not found or expired (stories last 24h)",
                }

            def _download_item(item: StoryItem) -> list[str]:
                time.sleep(random.uniform(0.2, 0.6))
                item_dir = target / str(item.mediaid)
                item_dir.mkdir(parents=True, exist_ok=True)
                try:
                    self._loader.download_storyitem(item, target=item_dir)
                    self._cleanup_metadata(item_dir)
                    return self._collect_media(item_dir)
                except Exception as exc:
                    log.warning("Story item download failed", error=str(exc)[:60])
                    return []

            with ThreadPoolExecutor(max_workers=1) as pool:
                results = list(pool.map(_download_item, items))

            all_files = [f for files in results for f in files]
            if not all_files:
                return {"success": False, "error": "Failed to download story items"}

            log.info("Stories downloaded", count=len(all_files))
            return {
                "success": True,
                "file_paths": all_files,
                "title": f"Instagram story @{username}",
                "media_type": (
                    "video" if any(f.endswith(".mp4") for f in all_files) else "photo"
                ),
            }

        except instaloader.exceptions.LoginRequiredException:
            return {
                "success": False,
                "error": "Stories require authentication — add Instagram cookies",
            }
        except Exception as exc:
            return {"success": False, "error": self._humanize_error(str(exc))}

    # ══════════════════════ COMPRESSION ══════════════════════

    async def _compress_videos(self, file_paths: list[Path]) -> list[Path]:
        if not self.has_ffmpeg:
            return file_paths
        tasks = [
            self._compress_if_needed(p)
            if p.suffix.lower() in {".mp4", ".mov"}
            else asyncio.sleep(0, result=p)
            for p in file_paths
        ]
        return list(await asyncio.gather(*tasks))

    async def _compress_if_needed(self, file_path: Path) -> Path:
        if not file_path.exists():
            return file_path

        size_mb = file_path.stat().st_size / (1024 * 1024)
        if size_mb <= self._MAX_VIDEO_SIZE_MB:
            return file_path

        log.info(
            "Compressing video",
            file=file_path.name,
            current_mb=round(size_mb, 1),
            target_mb=self._MAX_VIDEO_SIZE_MB,
        )

        output = file_path.with_name(f"{file_path.stem}_c.mp4")
        duration = await self._get_video_duration(file_path)
        if duration <= 0:
            duration = 60

        target_bitrate = max(
            200, int((self._MAX_VIDEO_SIZE_MB * 0.85 * 8192) / duration)
        )

        cmd = [
            self._ffmpeg_path,
            "-i", str(file_path),
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "28",
            "-b:v", f"{target_bitrate}k",
            "-maxrate", f"{int(target_bitrate * 1.5)}k",
            "-bufsize", f"{target_bitrate * 2}k",
            "-c:a", "aac",
            "-b:a", "96k",
            "-threads", "2",
            "-movflags", "+faststart",
            "-y",
            str(output),
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()

            if output.exists() and output.stat().st_size > 4096:
                new_size_mb = output.stat().st_size / (1024 * 1024)
                log.info(
                    "Video compressed",
                    original_mb=round(size_mb, 1),
                    new_mb=round(new_size_mb, 1),
                    saved_pct=round((1 - new_size_mb / size_mb) * 100, 1),
                )
                file_path.unlink(missing_ok=True)
                return output

            log.warning("Compression produced empty file, using original")
            output.unlink(missing_ok=True)
            return file_path

        except Exception as exc:
            log.error("Compression error", error=str(exc))
            output.unlink(missing_ok=True)
            return file_path

    async def _get_video_duration(self, file_path: Path) -> float:
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(file_path),
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            return float(stdout.decode().strip())
        except Exception:
            return 60.0

    # ══════════════════════ STATIC HELPERS ══════════════════════

    @staticmethod
    def _make_result(
        files: list[str],
        post: Post,
        shortcode: str,
        media_type: str,
    ) -> dict:
        if not files:
            return {"success": False, "error": "No media files found after download"}
        title = (getattr(post, "caption", None) or f"Instagram {shortcode}")
        title = title.strip()[:100] if title else f"Instagram {shortcode}"
        return {
            "success": True,
            "file_paths": files,
            "title": title,
            "media_type": media_type,
        }

    @staticmethod
    def _collect_media(directory: Path) -> list[str]:
        extensions = {".mp4", ".jpg", ".jpeg", ".png", ".webp", ".gif", ".mov"}
        files = [
            str(f)
            for f in directory.rglob("*")
            if f.is_file()
            and f.suffix.lower() in extensions
            and f.stat().st_size > 1024
        ]
        # Сортируем: сначала видео, потом по размеру
        def sort_key(path: str) -> tuple:
            p = Path(path)
            is_video = p.suffix.lower() in (".mp4", ".mov")
            return (0 if is_video else 1, -p.stat().st_size)

        return sorted(files, key=sort_key)

    @staticmethod
    def _cleanup_metadata(directory: Path) -> None:
        for pattern in ("*.json.xz", "*.txt", "*.json", "*.xz", "*.xml"):
            for f in directory.rglob(pattern):
                try:
                    f.unlink()
                except OSError:
                    pass

    @staticmethod
    def _humanize_error(error: str) -> str:
        e = error.lower()
        if "login required" in e or "loginrequired" in e:
            return "Login required — private content needs authentication"
        if "private" in e:
            return "This account is private"
        if "not found" in e or "404" in e:
            return "Post not found (deleted or invalid URL)"
        if "expired" in e:
            return "Story expired (stories last 24 hours)"
        if "rate" in e and "limit" in e:
            return "Instagram rate limit — wait 5-10 minutes"
        if "checkpoint" in e or "challenge" in e:
            return "Instagram requires verification — update cookies"
        if "connection" in e or "timeout" in e:
            return "Connection timeout — check internet connection"
        if "403" in e or "forbidden" in e:
            return "Instagram blocked request (403) — update cookies or wait 10 min"
        if "400" in e or "bad request" in e:
            return "Instagram bad request (400) — cookies may be expired"
        return error[:200]





