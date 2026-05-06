from __future__ import annotations

import asyncio, os, re, shutil, tempfile, yt_dlp, concurrent.futures,json, sys, subprocess
import datetime

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from functools import partial
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from dataclasses import dataclass
from datetime import datetime

from services.downloaders.downloader import BaseDownloader, DownloadRequest, DownloadResult, MediaPlatform
from services.user_agents import get_desktop_ua
from app.logging import get_logger

log = get_logger("downloader.youtube")


COOKIES_DIR = Path(__file__).resolve().parent.parent.parent / "storage" / "cookies"


def generate_tokens() -> tuple[str, str] | None:
    """Генерирует po_token и visitor_data через Node.js."""
    try:
        result = subprocess.run(
            ["npx", "--yes", "@iv-org/youtube-po-token-generator", "--json"],
            capture_output=True,
            text=True,
            timeout=60,
        )

        if result.returncode != 0:
            print(f"[ERROR] Generator failed: {result.stderr}", file=sys.stderr)
            return None

        # Парсим JSON вывод
        data = json.loads(result.stdout.strip())
        po_token = data.get("poToken") or data.get("po_token")
        visitor_data = data.get("visitorData") or data.get("visitor_data")

        if not po_token or not visitor_data:
            print(f"[ERROR] Unexpected output: {result.stdout}", file=sys.stderr)
            return None

        return po_token, visitor_data

    except subprocess.TimeoutExpired:
        print("[ERROR] Token generation timed out", file=sys.stderr)
        return None
    except json.JSONDecodeError as exc:
        print(f"[ERROR] Invalid JSON: {exc}", file=sys.stderr)
        return None
    except FileNotFoundError:
        print("[ERROR] npx not found. Install Node.js first.", file=sys.stderr)
        return None


def save_tokens(po_token: str, visitor_data: str) -> bool:
    """Сохраняет токены в файлы."""
    COOKIES_DIR.mkdir(parents=True, exist_ok=True)

    try:
        (COOKIES_DIR / "po_token.txt").write_text(po_token.strip(), encoding="utf-8")
        (COOKIES_DIR / "visitor_data.txt").write_text(visitor_data.strip(), encoding="utf-8")
        print(f"[OK] Tokens saved to {COOKIES_DIR}")
        print(f"     po_token:      {po_token[:20]}...")
        print(f"     visitor_data:  {visitor_data[:20]}...")
        return True
    except OSError as exc:
        print(f"[ERROR] Cannot save tokens: {exc}", file=sys.stderr)
        return False




class TokenManager:
    """Управляет жизненным циклом PO токена."""

    TOKEN_TTL_HOURS = 6  # токен живёт ~6 часов

    def __init__(self, cookies_dir: Path) -> None:
        self.cookies_dir = cookies_dir
        self._po_token: str | None = None
        self._visitor_data: str | None = None
        self._last_refresh: datetime | None = None
        self._lock = asyncio.Lock()

    @property
    def is_expired(self) -> bool:
        if not self._last_refresh:
            return True
        return datetime.now() - self._last_refresh > timedelta(hours=self.TOKEN_TTL_HOURS)

    @property
    def po_token(self) -> str | None:
        return self._po_token

    @property
    def visitor_data(self) -> str | None:
        return self._visitor_data

    def load_from_files(self) -> bool:
        """Загружает токены из файлов при старте."""
        po_file = self.cookies_dir / "po_token.txt"
        vd_file = self.cookies_dir / "visitor_data.txt"

        if po_file.exists() and vd_file.exists():
            try:
                self._po_token = po_file.read_text(encoding="utf-8").strip() or None
                self._visitor_data = vd_file.read_text(encoding="utf-8").strip() or None
                # Берём время изменения файла
                mtime = datetime.fromtimestamp(po_file.stat().st_mtime)
                self._last_refresh = mtime
                return bool(self._po_token)
            except OSError:
                pass
        return False

    async def refresh(self) -> bool:
        """Обновляет токены через Node.js генератор."""
        async with self._lock:
            if not self.is_expired:
                return True

            log.info("Refreshing PO token...")
            try:
                proc = await asyncio.create_subprocess_exec(
                    "npx", "--yes", "@iv-org/youtube-po-token-generator", "--json",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=60
                )

                if proc.returncode != 0:
                    log.error("Token generator failed", stderr=stderr.decode()[:200])
                    return False

                data = json.loads(stdout.decode().strip())
                po_token = data.get("poToken") or data.get("po_token")
                visitor_data = data.get("visitorData") or data.get("visitor_data")

                if not po_token:
                    log.error("No po_token in generator output", data=data)
                    return False

                self._po_token = po_token
                self._visitor_data = visitor_data
                self._last_refresh = datetime.now()

                # Сохраняем в файлы
                self.cookies_dir.mkdir(parents=True, exist_ok=True)
                (self.cookies_dir / "po_token.txt").write_text(po_token, encoding="utf-8")
                if visitor_data:
                    (self.cookies_dir / "visitor_data.txt").write_text(visitor_data, encoding="utf-8")

                log.info(
                    "PO token refreshed",
                    po_token=po_token[:20] + "...",
                    expires_in=f"{self.TOKEN_TTL_HOURS}h",
                )
                return True

            except asyncio.TimeoutError:
                log.error("Token refresh timed out")
                return False
            except Exception as exc:
                log.error("Token refresh error", error=str(exc))
                return False

    async def ensure_fresh(self) -> None:
        """Вызывай перед каждым запросом к YouTube."""
        if self.is_expired:
            await self.refresh()





@dataclass
class VideoFormat:
    format_id: str
    resolution: str
    filesize: int
    filesize_str: str
    format_note: str = ""


class SilentLogger:
    """Полностью подавляет вывод yt-dlp в терминал"""

    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        pass

    def error(self, msg: str) -> None:
        # Логируем только в наш logger, не в терминал
        if msg and not any(skip in msg for skip in [
            "Failed to extract any player response",
            "Requested format is not available",
            "please report this issue",
        ]):
            log.debug("yt-dlp error", msg=msg[:200])


class YouTubeDownloader(BaseDownloader):

    platform = MediaPlatform.YOUTUBE

    # Показываем пользователю только эти качества; всё ниже 360p отбрасываем,
    # выше — мапим на ближайшее снизу (например, 4320p → 2160p).
    ALLOWED_QUALITIES = ["360p", "480p", "720p", "1080p", "1440p", "2160p"]

    SHORTS_FAST_FORMAT = (
        "best[ext=mp4][vcodec!=none][acodec!=none]/"
        "best[ext=mp4]/best"
    )

    STANDARD_CHUNK_SIZE = 10 * 1024 * 1024
    SHORTS_CHUNK_SIZE = 32 * 1024 * 1024
    STANDARD_CONCURRENT_FRAGMENTS = 16
    SHORTS_CONCURRENT_FRAGMENTS = 64

    URL_PATTERNS = [
        r"(?:https?://)?(?:www\.)?youtube\.com/watch\?v=[\w-]+",
        r"(?:https?://)?(?:www\.)?youtube\.com/shorts/[\w-]+",
        r"(?:https?://)?youtu\.be/[\w-]+",
        r"(?:https?://)?(?:www\.)?youtube\.com/embed/[\w-]+",
        r"(?:https?://)?(?:www\.)?youtube\.com/playlist\?list=[\w-]+",
    ]

    # Стратегии клиентов — порядок важен!
    # tv_embedded даёт 1080p даже когда web забанен
    CLIENT_STRATEGIES: list[list[str]] = [
        ["tv_embedded"],
        ["tv_embedded", "android"],
        ["android"],
        ["ios"],
        ["mweb"],
        ["web"],
    ]

    SHORTS_CLIENT_STRATEGIES: list[list[str]] = [
        ["android"],
        ["ios"],
        ["mweb"],
        ["web"],
    ]

    AUTH_ERROR_MARKERS = (
        "sign in to confirm you're not a bot",
        "login required",
        "private video",
        "age-restricted",
        "members-only",
        "this video is unavailable",
    )

    AUTH_COOKIE_NAMES = {
        "SID", "HSID", "SSID", "APISID", "SAPISID", "LOGIN_INFO",
        "PREF", "YSC", "CONSENT", "SOCS",
        "VISITOR_INFO1_LIVE", "VISITOR_PRIVACY_METADATA", "__Secure-YEC",
        "__Secure-1PSID", "__Secure-1PSIDTS", "__Secure-1PSIDCC",
        "__Secure-1PAPISID", "__Secure-3PSID", "__Secure-3PSIDTS",
        "__Secure-3PSIDCC", "__Secure-3PAPISID", "__Secure-ROLLOUT_TOKEN",
        "SIDCC", "CONSISTENCY", "GPS", "__Secure-BUCKET",
    }

    def __init__(self) -> None:
        super().__init__()
        self.executor = ThreadPoolExecutor(max_workers=16)
        self.semaphore = asyncio.Semaphore(8)

        self.__po_token = self._load_token_file("po_token.txt")
        self.__visitor_data = self._load_token_file("visitor_data.txt")

        if not self.__po_token:
            log.warning("PO_TOKEN not found — wpc plugin will handle it automatically")
        if not self.__visitor_data:
            log.warning("VISITOR_DATA not found")

        self._xvfb_proc = self._start_virtual_display()

        cookies_dir = Path(__file__).resolve().parent.parent.parent / "storage" / "cookies"

        self._browser_path = self._find_browser()
        if self._browser_path:
            log.info("Browser found for wpc plugin", path=self._browser_path)
        else:
            log.warning("No browser found — install chromium for wpc plugin")


        self.aria2c_args = [
            "-x", "16", "-s", "16", "-k", "4M",
            "--min-split-size=4M",
            "--max-connection-per-server=16",
            "--max-concurrent-downloads=16",
            "--max-tries=5",
            "--retry-wait=1",
            "--timeout=10",
            "--connect-timeout=5",
            "--summary-interval=0",
            "--download-result=hide",
            "--quiet=true",
            "--enable-http-keep-alive=true",
            "--enable-http-pipelining=true",
            "--file-allocation=none",
            "--no-conf=true",
            "--async-dns=true",
            "--async-dns-server=8.8.8.8,1.1.1.1",
        ]

        self.has_aria2c = shutil.which("aria2c") is not None
        self.has_ffmpeg = shutil.which("ffmpeg") is not None
        log.info("YouTube downloader ready", aria2c=self.has_aria2c, ffmpeg=self.has_ffmpeg)

        self._cookies_path = self._find_cookies_path()
        self._runtime_cookiefile = self._build_runtime_cookiefile(self._cookies_path)
        self._prefer_cookies = self._runtime_cookiefile is not None

        if self._cookies_path:
            log.info("YouTube cookies found", path=str(self._cookies_path))
        else:
            log.warning("YouTube cookies NOT found")

    def _start_virtual_display(self):
        """Запускает Xvfb если нет дисплея (для headless серверов)."""
        if os.environ.get("DISPLAY"):
            log.info("Display found", display=os.environ["DISPLAY"])
            return None  # дисплей уже есть

        try:
            proc = subprocess.Popen(
                ["Xvfb", ":99", "-screen", "0", "1280x720x24"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            os.environ["DISPLAY"] = ":99"
            log.info("Virtual display started", display=":99", pid=proc.pid)
            return proc
        except FileNotFoundError:
            log.warning("Xvfb not found — wpc plugin may fail on headless server")
            return None
        except Exception as exc:
            log.error("Failed to start virtual display", error=str(exc))
            return None

    def __del__(self):
        """Останавливаем Xvfb при завершении."""
        if hasattr(self, "_xvfb_proc") and self._xvfb_proc:
            self._xvfb_proc.terminate()

    def _find_browser(self) -> str | None:
        """Ищет браузер для wpc плагина."""
        candidates = [
            "chromium-browser",
            "chromium",
            "google-chrome",
            "google-chrome-stable",
            "/usr/bin/chromium-browser",
            "/usr/bin/chromium",
            "/snap/bin/chromium",
        ]
        for candidate in candidates:
            path = shutil.which(candidate) or (
                candidate if Path(candidate).exists() else None
            )
            if path:
                return path
        return None

    # ─────────────────────────── helpers ────────────────────────────

    @property
    def _po_token(self) -> str | None:
        return self.__po_token

    @_po_token.setter
    def _po_token(self, value: str | None) -> None:
        self.__po_token = value

    @property
    def _visitor_data(self) -> str | None:
        return self.__visitor_data

    @_visitor_data.setter
    def _visitor_data(self, value: str | None) -> None:
        self.__visitor_data = value

    def _load_token_file(self, filename: str) -> str | None:
        # 1) ENV приоритетнее файлов — удобно для деплоя.
        try:
            from app.config import settings
            env_map = {
                "po_token.txt": settings.youtube_po_token,
                "visitor_data.txt": settings.youtube_visitor_data,
            }
            env_val = env_map.get(filename)
            if env_val and env_val.strip():
                log.info(f"Loaded {filename} from ENV")
                return env_val.strip()
        except Exception:
            pass

        project_root = Path(__file__).resolve().parent.parent.parent
        candidates = [
            project_root / "storage" / "cookies" / filename,
            project_root / filename,
            Path.cwd() / "storage" / "cookies" / filename,
            Path.cwd() / filename,
        ]
        for path in candidates:
            if path.exists() and path.stat().st_size > 0:
                try:
                    # Берём ТОЛЬКО первую строку, убираем все переносы и пробелы
                    val = path.read_text(encoding="utf-8").splitlines()[0].strip()
                    if val:
                        log.info(f"Loaded {filename}", path=str(path))
                        return val
                except OSError as exc:
                    log.error(f"Cannot read {filename}", path=str(path), error=str(exc))
        return None

    def _find_cookies_path(self) -> Path | None:
        project_root = Path(__file__).resolve().parent.parent.parent
        candidates = [
            project_root / "storage" / "cookies" / "youtube.txt",
            project_root / "storage" / "cookies" / "youtube_cookies.txt",
            Path.cwd() / "storage" / "cookies" / "youtube.txt",
            Path.cwd() / "storage" / "cookies" / "youtube_cookies.txt",
            Path.home() / ".config" / "yt-dlp" / "cookies.txt",
        ]
        for path in candidates:
            if path.exists() and path.stat().st_size > 0:
                return path
        return None

    def _get_cookies_path(self) -> Path | None:
        return self._runtime_cookiefile or self._cookies_path

    def _build_runtime_cookiefile(self, source: Path | None) -> Path | None:
        if not source or not source.exists():
            return None
        try:
            lines = source.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            return None

        kept_lines: list[str] = []
        kept_names: set[str] = set()

        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("#"):
                kept_lines.append(line)
                continue
            parts = line.split("\t")
            if len(parts) < 7:
                continue
            name = parts[5].strip()
            if name in self.AUTH_COOKIE_NAMES:
                kept_lines.append(line)
                kept_names.add(name)

        if not kept_names:
            return source  # нет нужных кук — используем оригинал

        if not any("Netscape HTTP Cookie File" in ln for ln in kept_lines):
            kept_lines.insert(0, "# Netscape HTTP Cookie File")

        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                suffix=".txt",
                prefix="youtube-cookies-",
                dir=self.temp_dir,
                delete=False,
            ) as fh:
                fh.write("\n".join(kept_lines) + "\n")
                return Path(fh.name)
        except OSError:
            return source

    def _is_auth_error(self, error: str | None) -> bool:
        if not error:
            return False
        normalized = error.lower()
        return any(marker in normalized for marker in self.AUTH_ERROR_MARKERS)

    def _iter_cookie_modes(self) -> list[bool]:
        if not self._get_cookies_path():
            return [False]
        return [True, False] if self._prefer_cookies else [False, True]

    def _format_duration(self, seconds: int | float | None) -> str:
        total = int(seconds or 0)
        m, s = divmod(total, 60)
        h, m = divmod(m, 60)
        return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

    def _size_str(self, size: int | None) -> str:
        # Без префикса "~" — клавиатура сама добавит его на основе filesize_exact,
        # иначе на кнопках получается "~~10.5 MB".
        if not size or size <= 0:
            return "? MB"
        if size >= 1024 * 1024:
            return f"{size / (1024 * 1024):.1f} MB"
        return f"{size / 1024:.1f} KB"

    def _estimate_size(self, fmt: dict, duration: float | None) -> int:
        tbr = fmt.get("tbr") or (fmt.get("vbr") or 0) + (fmt.get("abr") or 0)
        if not tbr:
            h = fmt.get("height") or 0
            tbr = {
                2160: 15000, 1440: 8000, 1080: 4500,
                720: 2500, 480: 1200, 360: 700,
            }.get(h, 500)
        return int(float(duration or 0) * (float(tbr) * 1000 / 8))

    # ─────────────────────────── URL helpers ────────────────────────

    def match_url(self, url: str) -> bool:
        return any(re.match(pattern, url) for pattern in self.URL_PATTERNS)

    def extract_id(self, url: str) -> str | None:
        if "/shorts/" in url:
            return url.split("/shorts/")[-1].split("?")[0].split("&")[0]
        parsed = urlparse(url)
        if parsed.netloc in ("youtu.be", "www.youtu.be"):
            return parsed.path.lstrip("/")
        query = parse_qs(parsed.query)
        if "v" in query:
            return query["v"][0]
        if "/embed/" in url:
            return url.split("/embed/")[-1].split("?")[0]
        return None

    # ─────────────────────────── yt-dlp opts ────────────────────────

    def _base_extractor_args(self, clients: list[str]) -> dict:
        """
        С плагином wpc — po_token генерируется автоматически.
        Нам нужно только указать клиентов.
        """
        ea: dict = {"player_client": clients}

        # Если есть ручные токены — используем их (быстрее чем запускать браузер).
        # Формат yt-dlp >=2024.10: "{client}.{context}+{token}", где context это
        # "gvs" (для videoplayback) или "player" (для player_response).
        # Для каждого клиента генерим оба контекста — yt-dlp сам выберет нужный.
        if self._po_token and self._visitor_data:
            tokens: list[str] = []
            for client in clients:
                tokens.append(f"{client}.gvs+{self._po_token}")
                tokens.append(f"{client}.player+{self._po_token}")
            ea["po_token"] = tokens
            ea["visitor_data"] = [self._visitor_data]
        elif self._visitor_data:
            ea["visitor_data"] = [self._visitor_data]
        # Если токенов нет — плагин wpc сам всё сделает

        return ea

    def _get_wpc_browser_path(self) -> str | None:
        """Ищем браузер для плагина wpc."""
        candidates = [
            "chromium-browser",
            "chromium",
            "google-chrome",
            "google-chrome-stable",
            "/usr/bin/chromium-browser",
            "/usr/bin/chromium",
            "/usr/bin/google-chrome",
        ]
        for candidate in candidates:
            path = shutil.which(candidate)
            if path:
                return path
        return None

    def _get_info_opts(self, clients: list[str], use_cookies: bool) -> dict:
        opts: dict = {
            "logger": SilentLogger(),
            "quiet": True,
            "no_warnings": True,
            "ignoreerrors": True,
            "socket_timeout": 15,
            "nocheckcertificate": True,
            "geo_bypass": True,
            "sleep_interval": 0,
            "max_sleep_interval": 1,
            "noplaylist": True,
            "extract_flat": "in_playlist",
            "extractor_args": {
                "youtube": self._base_extractor_args(clients),
            },
            "http_headers": {
                "User-Agent": get_desktop_ua(),
                "Referer": "https://www.youtube.com/",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        }

        # wpc плагин — указываем браузер если есть
        if self._browser_path:
            opts["extractor_args"]["youtubepot-wpc"] = {
                "browser_path": [self._browser_path],
                "headless": ["true"],
            }

        # bgutil HTTP provider — если задан BGUTIL_POT_PROVIDER_URL или
        # сервис крутится на дефолтном 127.0.0.1:4416 (см. bgutil-ytdlp-pot-provider)
        bgutil_url = os.environ.get("BGUTIL_POT_PROVIDER_URL", "http://127.0.0.1:4416")
        if bgutil_url:
            opts["extractor_args"]["youtubepot-bgutilhttp"] = {
                "base_url": [bgutil_url],
            }

        cookies_path = self._get_cookies_path()
        if use_cookies and cookies_path:
            opts["cookiefile"] = str(cookies_path)

        return opts

    def _get_download_opts(
            self,
            output_path: str,
            is_shorts: bool,
            use_cookies: bool,
            clients: list[str],
            needs_merge: bool = False,
    ) -> dict:
        opts: dict = {
            "outtmpl": output_path,
            "logger": SilentLogger(),
            "quiet": True,
            "no_warnings": True,
            "ignoreerrors": False,
            "noprogress": True,
            "socket_timeout": 30,
            "retries": 10,
            "fragment_retries": 10,
            "extractor_retries": 5,
            "http_chunk_size": (
                self.SHORTS_CHUNK_SIZE if is_shorts else self.STANDARD_CHUNK_SIZE
            ),
            "concurrent_fragment_downloads": 16,
            # При merge видео+аудио — копировать стримы без перекодирования.
            "postprocessor_args": {"FFmpegMerger": ["-c", "copy"]},
            "nocheckcertificate": True,
            "geo_bypass": True,
            "writethumbnail": False,
            "writeinfojson": False,
            "noplaylist": True,
            "extractor_args": {"youtube": self._base_extractor_args(clients)},
            "http_headers": {
                "User-Agent": get_desktop_ua(),
                "Referer": "https://www.youtube.com/",
                "Origin": "https://www.youtube.com",
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.9",
            },
        }

        # Браузер для wpc
        browser_path = self._get_wpc_browser_path()
        if browser_path:
            opts["extractor_args"]["youtubepot-wpc"] = {
                "browser_path": [browser_path]
            }

        # bgutil HTTP provider
        bgutil_url = os.environ.get("BGUTIL_POT_PROVIDER_URL", "http://127.0.0.1:4416")
        if bgutil_url:
            opts["extractor_args"]["youtubepot-bgutilhttp"] = {
                "base_url": [bgutil_url],
            }

        cookies_path = self._get_cookies_path()
        if use_cookies and cookies_path:
            opts["cookiefile"] = str(cookies_path)

        if self.has_aria2c and not needs_merge:
            opts["external_downloader"] = "aria2c"
            opts["external_downloader_args"] = {"default": self.aria2c_args}

        if self.has_ffmpeg:
            opts["ffmpeg_location"] = shutil.which("ffmpeg")

        return opts

    # ─────────────────────────── attempt plan ───────────────────────

    def _build_attempt_plan(
        self,
        client_strategies: list[list[str]],
        preferred_context: dict | None = None,
    ) -> list[tuple[list[str], bool]]:
        attempts: list[tuple[list[str], bool]] = []
        seen: set[tuple[tuple[str, ...], bool]] = set()

        def add(clients: list[str], use_cookies: bool) -> None:
            key = (tuple(clients), use_cookies)
            if key in seen:
                return
            seen.add(key)
            attempts.append((list(clients), use_cookies))

        # Сначала пробуем контекст который уже работал при get_info
        if preferred_context:
            pref_clients = preferred_context.get("clients")
            pref_cookies = preferred_context.get("use_cookies")
            if isinstance(pref_clients, list) and isinstance(pref_cookies, bool):
                add(pref_clients, pref_cookies)

        for clients in client_strategies:
            for use_cookies in self._iter_cookie_modes():
                add(clients, use_cookies)

        return attempts

    # ─────────────────────────── format parsing ─────────────────────

    def _parse_formats(
        self, raw_formats: list[dict], duration: float | None
    ) -> list[dict]:
        # Лучший аудио-трек
        audio_only = [
            f for f in raw_formats
            if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")
        ]
        best_audio = max(audio_only, key=lambda f: f.get("tbr") or 0, default=None)
        audio_id = best_audio.get("format_id") if best_audio else None
        audio_size = (
            best_audio.get("filesize")
            or best_audio.get("filesize_approx")
            or self._estimate_size(best_audio, duration)
        ) if best_audio else 0

        # Лучший видео-трек на каждое качество
        quality_map: dict[str, dict] = {}
        for f in raw_formats:
            if f.get("vcodec") in (None, "none"):
                continue
            h = f.get("height")
            if not h:
                continue
            res = next(
                (q for q in reversed(self.ALLOWED_QUALITIES) if int(q[:-1]) <= h),
                None,
            )
            if not res:
                continue

            # Предпочитаем: реальный размер > есть аудио > mp4 > высокий битрейт
            score = (
                1 if f.get("filesize") or f.get("filesize_approx") else 0,
                1 if f.get("acodec") not in (None, "none") else 0,
                1 if f.get("ext") == "mp4" else 0,
                f.get("tbr") or 0,
            )
            if res not in quality_map or score > quality_map[res]["score"]:
                quality_map[res] = {"fmt": f, "score": score}

        formats: list[dict] = []
        seen_qualities = set()
        for res in sorted(quality_map, key=lambda r: int(r[:-1])):
            if res in seen_qualities:
                continue
            seen_qualities.add(res)
            
            f = quality_map[res]["fmt"]
            v_size = (
                f.get("filesize")
                or f.get("filesize_approx")
                or self._estimate_size(f, duration)
            )
            has_audio = f.get("acodec") not in (None, "none")
            total_size = v_size + (0 if has_audio else audio_size)

            formats.append({
                "format_id": f["format_id"],
                "quality": res,
                "resolution": f"{f['height']}p",
                "download_format": (
                    f["format_id"] if has_audio
                    else f"{f['format_id']}+{audio_id}"
                ),
                "filesize": total_size,
                "filesize_str": self._size_str(total_size),
                "ext": "mp4",
            })

        # Аудио-формат: после FFmpegExtractAudio → MP3 320kbps,
        # размер источника (opus/m4a) не отражает финальный файл.
        if best_audio:
            mp3_size = int(float(duration or 0) * (320 * 1000 / 8)) if duration else audio_size
            formats.append({
                "format_id": audio_id,
                "quality": "audio",
                "resolution": "🎵 Audio",
                "download_format": audio_id,
                "filesize": mp3_size,
                "filesize_str": self._size_str(mp3_size),
                "ext": "mp3",
                "is_audio_only": True,
            })

        return formats

    def _build_info_dict(self, info: dict, formats: list[dict], url: str) -> dict:
        d = info.get("duration", 0)
        return {
            "id": info.get("id"),
            "title": info.get("title"),
            "duration": d,
            "duration_str": self._format_duration(d),
            "thumbnail": info.get("thumbnail"),
            "uploader": info.get("uploader") or info.get("channel") or "",
            "view_count": info.get("view_count", 0),
            "formats": formats,
            "url": url,
        }

    # ─────────────────────────── info fetching ──────────────────────

    def _probe_single(
        self, url: str, clients: list[str], use_cookies: bool
    ) -> dict | None:
        """Один запрос метаданных — тихий, без вывода в терминал."""
        opts = self._get_info_opts(clients, use_cookies)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if not info:
                    return None
                formats = self._parse_formats(
                    info.get("formats", []), info.get("duration")
                )
                if not formats:
                    return None
                result = self._build_info_dict(info, formats, url)
                result["_probe_context"] = {
                    "clients": list(clients),
                    "use_cookies": use_cookies,
                }
                return result
        except Exception as exc:
            log.debug(
                "Probe failed",
                clients=clients,
                use_cookies=use_cookies,
                error=str(exc)[:150],
            )
            return None

    def _get_info_sync(self, url: str) -> dict | None:
        """
        Параллельно пробует все стратегии.
        Возвращает первый результат с HD (1080p/720p),
        иначе — результат с наибольшим числом форматов.
        """
        strategies = (
            self.SHORTS_CLIENT_STRATEGIES
            if "/shorts/" in url
            else self.CLIENT_STRATEGIES
        )

        tasks = [
            (clients, use_cookies)
            for clients in strategies
            for use_cookies in self._iter_cookie_modes()
        ]

        best_info: dict | None = None
        HD_QUALITIES = {"1080p", "720p", "1440p", "2160p"}

        # Use shared executor to avoid pool overhead and blocking on shutdown
        future_map: dict[concurrent.futures.Future, tuple] = {
            self.executor.submit(self._probe_single, url, c, ck): (c, ck)
            for c, ck in tasks
        }

        try:
            for future in concurrent.futures.as_completed(
                future_map, timeout=40
            ):
                try:
                    res = future.result()
                except Exception:
                    continue

                if not res:
                    continue

                qualities = {f["quality"] for f in res.get("formats", [])}

                # Нашли HD — сразу возвращаем, отменяем остальное
                if qualities & HD_QUALITIES:
                    for f in future_map:
                        if not f.done():
                            f.cancel()
                    log.info(
                        "Got HD info",
                        qualities=sorted(qualities),
                        clients=res.get("_probe_context", {}).get("clients"),
                    )
                    return res

                # Сохраняем лучший не-HD результат
                if not best_info or len(res.get("formats", [])) > len(
                    best_info.get("formats", [])
                ):
                    best_info = res

        except concurrent.futures.TimeoutError:
            log.warning("Info probing timed out, returning best available")
            for f in future_map:
                if not f.done():
                    f.cancel()

        if best_info:
            qualities = {f["quality"] for f in best_info.get("formats", [])}
            log.info("Best available info", qualities=sorted(qualities))

        return best_info

    async def get_video_info(self, url: str) -> dict | None:

        if "list=" in url and "v=" not in url:
            return {"error": "playlist"}
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self.executor, partial(self._get_info_sync, url)
        )

    async def get_available_formats(self, url: str) -> list[dict]:
        info = await self.get_video_info(url)
        return info.get("formats", []) if info else []

    # ─────────────────────────── download ───────────────────────────

    def _find_requested_format(
        self, info: dict | None, requested: str
    ) -> dict | None:
        if not info:
            return None
        norm = requested.strip().lower()
        for fmt in info.get("formats", []):
            if (
                str(fmt.get("quality", "")).lower() == norm
                or str(fmt.get("format_id", "")).lower() == norm
            ):
                return fmt
        return None

    def _download_sync(self, url: str, ydl_opts: dict) -> dict:
        """Синхронная загрузка — полностью тихая."""
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if not info:
                    return {"success": False, "error": "No info returned"}

                path: str | None = None

                # Пробуем получить путь из requested_downloads
                requested = info.get("requested_downloads") or []
                if requested:
                    path = requested[-1].get("filepath")

                # Фоллбэк — через prepare_filename
                if not path:
                    path = ydl.prepare_filename(info)

                # Если расширение не то — ищем по соседним
                if path and not Path(path).exists():
                    base = Path(path).with_suffix("")
                    for ext in ("mp4", "mkv", "webm", "mp3", "m4a", "opus"):
                        candidate = base.with_suffix(f".{ext}")
                        if candidate.exists():
                            path = str(candidate)
                            break

                if not path or not Path(path).exists():
                    return {"success": False, "error": "Downloaded file not found"}

                return {
                    "success": True,
                    "file_path": path,
                    "title": info.get("title"),
                    "duration": info.get("duration"),
                }
        except yt_dlp.utils.DownloadError as exc:
            return {"success": False, "error": str(exc)}
        except Exception as exc:
            log.error("Unexpected download error", error=str(exc))
            return {"success": False, "error": str(exc)}

    async def download(self, request: DownloadRequest) -> DownloadResult:

        if "list=" in request.url and "v=" not in request.url:
            return DownloadResult(success=False, error="playlist_detected")

        video_id = self.extract_id(request.url)
        if not video_id:
            return DownloadResult(success=False, error="Invalid YouTube URL")

        is_shorts = "/shorts/" in request.url

        if request.format == "audio":
            return await self._download_audio(request.url, video_id, is_shorts)
        if request.format:
            return await self._download_video_format(
                request.url, video_id, request.format, is_shorts
            )
        return await self._download_video_best(request.url, video_id, is_shorts)

    async def _download_audio(
        self, url: str, video_id: str, is_shorts: bool = False
    ) -> DownloadResult:
        async with self.semaphore:
            output_path = str(
                self.temp_dir / f"%(title)s [{video_id}].%(ext)s"
            )
            info = await self.get_video_info(url)
            audio_meta = self._find_requested_format(info, "audio") if info else None
            preferred_context = (
                info.get("_probe_context") if isinstance(info, dict) else None
            )

            audio_strategies = [["tv_embedded"], ["android"], ["ios"]]
            last_error = "Download failed"

            for clients, use_cookies in self._build_attempt_plan(
                audio_strategies, preferred_context
            ):
                ydl_opts = self._get_download_opts(
                    output_path, is_shorts, use_cookies, clients
                )
                ydl_opts.update({
                    "format": "bestaudio/best",
                    "postprocessors": [{
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "320",
                    }],
                })

                result = await asyncio.get_event_loop().run_in_executor(
                    self.executor, partial(self._download_sync, url, ydl_opts)
                )
                if result["success"]:
                    return DownloadResult(
                        success=True,
                        file_path=Path(result["file_path"]),
                        title=result.get("title", "Audio"),
                        duration=result.get("duration"),
                        media_type="audio",
                        quality="audio",
                        filesize_str=(audio_meta or {}).get("filesize_str"),
                    )
                last_error = result.get("error", "Download failed")
                log.debug("Audio attempt failed", clients=clients, error=last_error[:100])

            return DownloadResult(success=False, error=last_error)

    async def _download_video_format(
        self,
        url: str,
        video_id: str,
        format_id: str,
        is_shorts: bool = False,
    ) -> DownloadResult:
        async with self.semaphore:
            info = await self.get_video_info(url)
            if not info or not info.get("formats"):
                return DownloadResult(
                    success=False, error="Failed to fetch formats"
                )

            target = self._find_requested_format(info, format_id)
            if not target:
                return DownloadResult(
                    success=False, error=f"Quality {format_id} not available"
                )

            download_format = target.get("download_format") or target.get("format_id")
            needs_merge = "+" in str(download_format)
            output_path = str(
                self.temp_dir / f"%(title)s_({format_id})_[{video_id}].%(ext)s"
            )
            preferred_context = info.get("_probe_context")

            for clients, use_cookies in self._build_attempt_plan(
                self.CLIENT_STRATEGIES, preferred_context
            ):
                ydl_opts = self._get_download_opts(
                    output_path, is_shorts, use_cookies, clients, needs_merge
                )
                ydl_opts["format"] = download_format
                if needs_merge:
                    ydl_opts["merge_output_format"] = "mp4"

                result = await asyncio.get_event_loop().run_in_executor(
                    self.executor, partial(self._download_sync, url, ydl_opts)
                )
                if result["success"]:
                    return DownloadResult(
                        success=True,
                        file_path=Path(result["file_path"]),
                        title=result.get("title", "Video"),
                        duration=result.get("duration"),
                        media_type="video",
                        quality=target.get("quality"),
                        filesize_str=target.get("filesize_str"),
                    )
                log.debug(
                    "Format attempt failed",
                    clients=clients,
                    format=download_format,
                    error=result.get("error", "")[:100],
                )

            return DownloadResult(
                success=False, error="Failed to download requested quality"
            )

    async def _download_video_best(
        self, url: str, video_id: str, is_shorts: bool = False
    ) -> DownloadResult:
        async with self.semaphore:
            output_path = str(
                self.temp_dir / f"%(title)s [{video_id}].%(ext)s"
            )
            strategies = (
                self.SHORTS_CLIENT_STRATEGIES if is_shorts else self.CLIENT_STRATEGIES
            )

            for clients, use_cookies in self._build_attempt_plan(strategies):
                needs_merge = not is_shorts
                ydl_opts = self._get_download_opts(
                    output_path, is_shorts, use_cookies, clients, needs_merge
                )
                ydl_opts["format"] = (
                    self.SHORTS_FAST_FORMAT
                    if is_shorts
                    else "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"
                )
                if not is_shorts:
                    ydl_opts["merge_output_format"] = "mp4"

                result = await asyncio.get_event_loop().run_in_executor(
                    self.executor, partial(self._download_sync, url, ydl_opts)
                )
                if result["success"]:
                    return DownloadResult(
                        success=True,
                        file_path=Path(result["file_path"]),
                        title=result.get("title", "Video"),
                        duration=result.get("duration"),
                    )
                log.debug(
                    "Best quality attempt failed",
                    clients=clients,
                    error=result.get("error", "")[:100],
                )

            return DownloadResult(success=False, error="Failed to download video")






def main() -> int:
    print("[*] Generating YouTube PO Token...")
    tokens = generate_tokens()

    if not tokens:
        print("[FAIL] Could not generate tokens", file=sys.stderr)
        return 1

    po_token, visitor_data = tokens

    if not save_tokens(po_token, visitor_data):
        return 1

    print("[DONE] Tokens updated successfully")
    return 0



if __name__ == "__main__":
    sys.exit(main())


