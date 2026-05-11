from litestar import Controller, get
from litestar.response import Template
from sqlalchemy import select, func
from datetime import datetime
import psutil

from database.connection import db
from repositories import BotRepository, UserRepository, MediaRepository
from repositories.ad import AdRepository
from repositories.cache_channel import CacheChannelRepository
from services import cache as redis_cache
from app.logging import get_logger
from models import Download, Media

log = get_logger("controller.admin")


def _time_ago(dt: datetime) -> str:
    """Human-readable time ago."""
    if not dt:
        return "N/A"
    diff = datetime.now() - dt
    seconds = int(diff.total_seconds())
    if seconds < 60:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


class AdminController(Controller):
    path = "/admin"

    @get("/", name="admin:dashboard")
    async def dashboard(self) -> Template:
        async with db.session() as session:
            bot_repo = BotRepository(session)
            user_repo = UserRepository(session)
            media_repo = MediaRepository(session)
            ad_repo = AdRepository(session)
            cc_repo = CacheChannelRepository(session)

            bots = await bot_repo.get_all()
            total_users = await user_repo.count_unique_telegram_users()

            # Real download count (from downloads table — every download attempt)
            dl_count = await session.execute(select(func.count()).select_from(Download))
            total_downloads = dl_count.scalar() or 0

            # Cached media count (from Redis — media:* keys)
            try:
                await redis_cache.connect()
                cached_keys = await redis_cache.redis.keys("media:*")
                total_cached = len(cached_keys)
                log.info("Cached media count from Redis", count=total_cached)
            except Exception as e:
                log.error("Failed to count cached media", error=str(e))
                total_cached = 0

            # Platform breakdown — by actual downloads, not by cached-media table.
            source_rows = await session.execute(
                select(Download.source, func.count(Download.id))
                .group_by(Download.source)
            )
            source_stats = {
                (s.value if hasattr(s, "value") else str(s)): int(c or 0)
                for s, c in source_rows.all()
                if s is not None
            }
            source_stats = dict(sorted(source_stats.items(), key=lambda kv: kv[1], reverse=True))
            language_stats = await user_repo.get_language_stats()

            # Recent downloads (last 10)
            recent_stmt = (
                select(Download)
                .order_by(Download.created_at.desc())
                .limit(10)
            )
            recent_result = await session.execute(recent_stmt)
            recent_downloads_raw = list(recent_result.scalars().all())

            # Enrich with display-friendly data
            platform_icons = {
                "instagram": "📸",
                "tiktok": "🎵",
                "youtube": "📺",
                "pinterest": "📌",
                "vk": "💙",
                "twitter": "🐦",
                "other": "📁",
            }

            def shorten_url(url: str, max_len: int = 50) -> str:
                """Make URL display-friendly."""
                if not url:
                    return "Unknown"
                # Remove tracking params
                url = url.split("?")[0]
                # Shorten if too long
                if len(url) > max_len:
                    return url[:max_len - 3] + "..."
                return url

            recent_downloads = []
            for dl in recent_downloads_raw:
                source = dl.source.value if hasattr(dl.source, 'value') else str(dl.source)
                recent_downloads.append({
                    "id": dl.id,
                    "url": dl.original_url,
                    "short_url": shorten_url(dl.original_url),
                    "source": source,
                    "icon": platform_icons.get(source, "📁"),
                    "status": dl.status.value if hasattr(dl.status, 'value') else str(dl.status),
                    "created_at": dl.created_at,
                    "time_ago": _time_ago(dl.created_at) if dl.created_at else "N/A",
                })

            # Ad stats
            active_post_download = await ad_repo.count_active_post_download()
            active_broadcast = await ad_repo.count_active_broadcast()
            top_ads = await ad_repo.get_top_post_download_ads(limit=3)

            # Cache channel stats
            cache_channels = await cc_repo.get_all_active()

            # System stats
            try:
                cpu_pct = psutil.cpu_percent(interval=0)
                mem = psutil.virtual_memory()
                mem_pct = mem.percent
                disk = psutil.disk_usage("/")
                disk_pct = disk.percent
            except Exception:
                cpu_pct = mem_pct = disk_pct = 0

            # Redis stats
            try:
                redis_info = await redis_cache.redis.info("memory")
                redis_mem_mb = round(int(redis_info.get("used_memory", 0)) / 1024 / 1024, 1)
            except Exception:
                redis_mem_mb = 0

            return Template(
                template_name="admin/dashboard.html",
                context={
                    "stats": {
                        "total_downloads": total_downloads,
                        "total_users": total_users,
                        "total_bots": len(bots),
                        "total_cached": total_cached,
                        "active_post_download": active_post_download,
                        "active_broadcast": active_broadcast,
                        "cache_channels": len(cache_channels),
                        "cpu_percent": cpu_pct,
                        "mem_percent": mem_pct,
                        "disk_percent": disk_pct,
                        "redis_mem_mb": redis_mem_mb,
                    },
                    "recent_downloads": recent_downloads,
                    "bots": bots,
                    "total_media": total_cached,
                    "source_stats": source_stats,
                    "language_stats": language_stats,
                    "cache_channels": cache_channels,
                    "top_ads": top_ads,
                },
            )
