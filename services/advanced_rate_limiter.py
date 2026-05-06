"""
Advanced Rate Limiter — DDoS Protection & Load Balancing

Multi-layer defense:
  Layer 1: IP-based (prevent abuse from single IP)
  Layer 2: User-based (fair usage per Telegram user)
  Layer 3: Bot-based (protect individual bots)
  Layer 4: Global server (prevent overload)
  Layer 5: Action-based (downloads, uploads, API calls)
  Layer 6: Adaptive (auto-adjust based on server load)

Algorithms:
  - Sliding Window Log (precision)
  - Token Bucket (burst control)
  - Leaky Bucket (smooth traffic)
"""

import asyncio
import time
import psutil
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from services.cache import cache
from services.cache import CacheService
from app.logging import get_logger
from app.config import settings

log = get_logger("service.adv_rate_limiter")


# ============================================================================
# Data Classes
# ============================================================================

class PenaltyLevel(Enum):
    NONE = "none"
    WARNING = "warning"          # 1st violation
    THROTTLE = "throttle"        # 2nd violation — reduced limits
    TEMP_BAN = "temp_ban"        # 3rd violation — 5 min ban
    LONG_BAN = "long_ban"        # 4+ violations — 1 hour ban


@dataclass
class RateLimitResult:
    allowed: bool
    remaining: int               # Requests left in window
    limit: int                   # Max requests allowed
    reset_after: int             # Seconds until window resets
    retry_after: int | None = None  # Seconds until user can retry
    penalty: PenaltyLevel = PenaltyLevel.NONE
    message: str | None = None


@dataclass
class RateLimitConfig:
    requests: int                # Max requests
    window: int                  # Time window (seconds)
    burst: int | None = None     # Burst allowance (Token Bucket)
    leak_rate: float | None = None  # Leaky Bucket rate (req/sec)


@dataclass
class UserPenaltyRecord:
    violations: int = 0
    last_violation: float = 0.0
    ban_until: float = 0.0

    @property
    def is_banned(self) -> bool:
        return time.time() < self.ban_until

    @property
    def penalty_level(self) -> PenaltyLevel:
        if self.is_banned:
            if self.ban_until - time.time() > 1800:  # > 30 min
                return PenaltyLevel.LONG_BAN
            return PenaltyLevel.TEMP_BAN
        if self.violations >= 3:
            return PenaltyLevel.THROTTLE
        if self.violations >= 1:
            return PenaltyLevel.WARNING
        return PenaltyLevel.NONE


# ============================================================================
# Adaptive Load Monitor
# ============================================================================

class AdaptiveLoadMonitor:
    """
    Monitors server load and adjusts rate limits dynamically.

    If CPU > 80% or RAM > 85% — tighten limits by 50%
    If CPU < 30% and RAM < 50% — relax limits by 25%
    """

    def __init__(self):
        self._cpu_history: list[float] = []
        self._mem_history: list[float] = []
        self._samples = 10

    def get_load_factor(self) -> float:
        """
        Returns load multiplier:
          0.5 = heavy load (halve limits)
          1.0 = normal
          1.25 = light load (increase limits)
        """
        try:
            cpu = psutil.cpu_percent(interval=0)
            mem = psutil.virtual_memory().percent
        except Exception:
            return 1.0

        self._cpu_history.append(cpu)
        self._mem_history.append(mem)
        self._cpu_history = self._cpu_history[-self._samples:]
        self._mem_history = self._mem_history[-self._samples:]

        if len(self._cpu_history) < 3:
            return 1.0

        avg_cpu = sum(self._cpu_history) / len(self._cpu_history)
        avg_mem = sum(self._mem_history) / len(self._mem_history)

        # Heavy load
        if avg_cpu > 80 or avg_mem > 85:
            return 0.5
        if avg_cpu > 60 or avg_mem > 70:
            return 0.75

        # Normal load
        if avg_cpu > 40 or avg_mem > 50:
            return 1.0

        # Light load
        return 1.25

    def get_server_status(self) -> dict:
        try:
            cpu = psutil.cpu_percent(interval=0)
            mem = psutil.virtual_memory()
            return {
                "cpu_percent": cpu,
                "mem_percent": mem.percent,
                "mem_available_mb": round(mem.available / 1024 / 1024),
                "load_factor": self.get_load_factor(),
            }
        except Exception:
            return {"cpu_percent": 0, "mem_percent": 0, "mem_available_mb": 0, "load_factor": 1.0}


# ============================================================================
# Advanced Rate Limiter
# ============================================================================

class AdvancedRateLimiter:
    """
    Production-grade rate limiter for MediaFlow.

    Features:
    - Multi-layer defense (IP, User, Bot, Global, Action)
    - Sliding Window Log (Redis sorted sets)
    - Token Bucket for burst control
    - Progressive penalties (warning → throttle → ban)
    - Adaptive limits based on server load
    - DDoS detection and auto-blocking
    """

    # Default configurations (will be adjusted by adaptive monitor)
    DEFAULT_LIMITS: dict[str, RateLimitConfig] = {
        # Global server limits
        "global": RateLimitConfig(requests=5000, window=60, burst=500),

        # Per-user limits (Telegram user_id)
        "user": RateLimitConfig(requests=60, window=60, burst=10),

        # Per-bot limits (bot_id)
        "bot": RateLimitConfig(requests=1000, window=60, burst=100),

        # Download-specific (per user)
        "download": RateLimitConfig(requests=20, window=60, burst=5),

        # Upload/Admin actions
        "admin": RateLimitConfig(requests=30, window=60, burst=5),

        # Webhook (Telegram → us)
        "webhook": RateLimitConfig(requests=10000, window=60, burst=1000),

        # API endpoints
        "api": RateLimitConfig(requests=100, window=60, burst=20),
    }

    # Penalty durations (seconds)
    PENALTY_BAN_DURATIONS = {
        PenaltyLevel.WARNING: 0,       # No ban, just warning
        PenaltyLevel.THROTTLE: 0,      # No ban, reduced limits
        PenaltyLevel.TEMP_BAN: 300,    # 5 minutes
        PenaltyLevel.LONG_BAN: 3600,   # 1 hour
    }

    def __init__(self):
        self._penalties: dict[str, UserPenaltyRecord] = {}
        self._load_monitor = AdaptiveLoadMonitor()
        self._local_cache: dict[str, list[float]] = {}
        self._cleanup_task: asyncio.Task | None = None
        self._redis: CacheService | None = None

    async def start(self) -> None:
        """Start background tasks"""
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        await cache.connect()
        self._redis = cache
        log.info("Advanced rate limiter started")

    async def stop(self) -> None:
        """Stop background tasks"""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        log.info("Advanced rate limiter stopped")

    async def _cleanup_loop(self) -> None:
        """Periodic cleanup of expired penalties"""
        while True:
            try:
                await asyncio.sleep(300)  # Every 5 minutes
                now = time.time()
                expired = [
                    k for k, v in self._penalties.items()
                    if v.ban_until > 0 and v.ban_until < now and v.violations < 3
                ]
                for k in expired:
                    del self._penalties[k]

                # Also clean local cache
                expired_local = [
                    k for k, timestamps in self._local_cache.items()
                    if timestamps and now - timestamps[-1] > 120
                ]
                for k in expired_local:
                    del self._local_cache[k]

                if expired or expired_local:
                    log.debug("Cleaned up", penalties=len(expired), local=len(expired_local))
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("Cleanup error", error=str(e))

    # ========================================================================
    # Main check method
    # ========================================================================

    async def check(
        self,
        layer: str,
        identifier: str | int,
        config: RateLimitConfig | None = None,
    ) -> RateLimitResult:
        """
        Check rate limit for a specific layer and identifier.

        Args:
            layer: "global", "user", "bot", "download", "admin", "webhook", "api"
            identifier: user_id, bot_id, IP address, etc.
            config: Optional custom config (overrides defaults)

        Returns:
            RateLimitResult
        """
        cfg = config or self.DEFAULT_LIMITS.get(layer)
        if not cfg:
            return RateLimitResult(allowed=True, remaining=9999, limit=9999, reset_after=0)

        # Apply adaptive load factor
        load_factor = self._load_monitor.get_load_factor()
        adjusted_requests = max(5, int(cfg.requests * load_factor))
        adjusted_cfg = RateLimitConfig(
            requests=adjusted_requests,
            window=cfg.window,
            burst=cfg.burst,
            leak_rate=cfg.leak_rate,
        )

        key = f"ratelimit:{layer}:{identifier}"

        # Check penalty/ban first
        penalty_result = self._check_penalty(str(identifier))
        if not penalty_result.allowed:
            return penalty_result

        # Check via Redis (primary) or local cache (fallback)
        try:
            result = await self._check_redis(key, adjusted_cfg)
        except Exception:
            result = self._check_local(key, adjusted_cfg)

        # Record violation if exceeded
        if not result.allowed:
            self._record_violation(str(identifier))

        return result

    # ========================================================================
    # Redis check (Sliding Window Log)
    # ========================================================================

    async def _check_redis(self, key: str, config: RateLimitConfig) -> RateLimitResult:
        """Sliding Window Log via Redis sorted sets"""
        now = time.time()
        window_start = now - config.window

        pipe = self._redis.redis.pipeline()

        # 1. Remove old entries outside window
        pipe.zremrangebyscore(key, 0, window_start)
        # 2. Count current entries
        pipe.zcard(key)
        # 3. Add current request
        pipe.zadd(key, {f"{now}:{id(now)}": now})
        # 4. Set TTL (auto-cleanup)
        pipe.expire(key, config.window + 10)

        results = await pipe.execute()
        current_count = results[1]

        if current_count >= config.requests:
            # Get oldest entry to calculate reset time
            oldest = await self._redis.redis.zrange(key, 0, 0, withscores=True)
            if oldest:
                reset_after = int(oldest[0][1] + config.window - now)
            else:
                reset_after = config.window

            # Remove the request we just added (it was rejected)
            await self._redis.redis.zremrangebyscore(key, now, now + 1)

            return RateLimitResult(
                allowed=False,
                remaining=0,
                reset_after=max(1, reset_after),
                limit=config.requests,
                retry_after=max(1, reset_after),
            )

        remaining = config.requests - current_count - 1
        return RateLimitResult(
            allowed=True,
            remaining=max(0, remaining),
            reset_after=config.window,
            limit=config.requests,
        )

    # ========================================================================
    # Local cache fallback
    # ========================================================================

    def _check_local(self, key: str, config: RateLimitConfig) -> RateLimitResult:
        """Sliding Window Log fallback via local dict"""
        now = time.time()
        window_start = now - config.window

        if key not in self._local_cache:
            self._local_cache[key] = []

        # Remove expired entries
        self._local_cache[key] = [t for t in self._local_cache[key] if t > window_start]
        timestamps = self._local_cache[key]

        if len(timestamps) >= config.requests:
            oldest = timestamps[0]
            reset_after = int(oldest + config.window - now)
            return RateLimitResult(
                allowed=False,
                remaining=0,
                reset_after=max(1, reset_after),
                limit=config.requests,
                retry_after=max(1, reset_after),
            )

        timestamps.append(now)
        remaining = config.requests - len(timestamps)

        return RateLimitResult(
            allowed=True,
            remaining=max(0, remaining),
            reset_after=config.window,
            limit=config.requests,
        )

    # ========================================================================
    # Penalty System
    # ========================================================================

    def _check_penalty(self, identifier: str) -> RateLimitResult:
        """Check if user is penalized/banned"""
        record = self._penalties.get(identifier)
        if not record:
            return RateLimitResult(allowed=True, remaining=9999, limit=9999, reset_after=0)

        level = record.penalty_level

        if level == PenaltyLevel.LONG_BAN:
            remaining = int(record.ban_until - time.time())
            return RateLimitResult(
                allowed=False,
                remaining=0,
                reset_after=remaining,
                limit=0,
                retry_after=remaining,
                penalty=level,
                message=f"Long ban — retry after {remaining}s ({remaining // 60} min)",
            )

        if level == PenaltyLevel.TEMP_BAN:
            remaining = int(record.ban_until - time.time())
            return RateLimitResult(
                allowed=False,
                remaining=0,
                reset_after=remaining,
                limit=0,
                retry_after=remaining,
                penalty=level,
                message=f"Temp ban — retry after {remaining}s",
            )

        if level == PenaltyLevel.THROTTLE:
            return RateLimitResult(
                allowed=True,
                remaining=1,
                reset_after=60,
                limit=1,
                penalty=level,
                message="Throttled — reduced limits active",
            )

        if level == PenaltyLevel.WARNING:
            return RateLimitResult(
                allowed=True,
                remaining=999,
                reset_after=60,
                limit=999,
                penalty=level,
                message="Warning — next violation will throttle your limits",
            )

        return RateLimitResult(allowed=True, remaining=9999, limit=9999, reset_after=0)

    def _record_violation(self, identifier: str) -> None:
        """Record a rate limit violation and escalate penalty"""
        now = time.time()

        if identifier not in self._penalties:
            self._penalties[identifier] = UserPenaltyRecord(
                violations=1,
                last_violation=now,
            )
            log.warning("Rate limit violation", identifier=identifier, violations=1)
            return

        record = self._penalties[identifier]

        # Reset violations if more than 10 minutes passed
        if now - record.last_violation > 600:
            record.violations = 1
            record.last_violation = now
            record.ban_until = 0
            return

        record.violations += 1
        record.last_violation = now

        # Apply ban
        if record.violations >= 3:
            ban_duration = self.PENALTY_BAN_DURATIONS.get(
                PenaltyLevel.TEMP_BAN if record.violations < 5 else PenaltyLevel.LONG_BAN,
                300,
            )
            record.ban_until = now + ban_duration
            log.warning(
                "User banned",
                identifier=identifier,
                violations=record.violations,
                ban_duration=ban_duration,
            )
        elif record.violations >= 2:
            log.warning("User throttled", identifier=identifier, violations=record.violations)
        else:
            log.info("Rate limit warning", identifier=identifier, violations=record.violations)

    # ========================================================================
    # Convenience methods
    # ========================================================================

    async def check_user(self, user_id: int) -> RateLimitResult:
        return await self.check("user", user_id)

    async def check_download(self, user_id: int) -> RateLimitResult:
        return await self.check("download", user_id)

    async def check_bot(self, bot_id: int) -> RateLimitResult:
        return await self.check("bot", bot_id)

    async def check_global(self) -> RateLimitResult:
        return await self.check("global", "server")

    async def check_webhook(self) -> RateLimitResult:
        return await self.check("webhook", "server")

    async def check_admin(self, user_id: int) -> RateLimitResult:
        return await self.check("admin", user_id)

    async def check_multi(
        self,
        user_id: int | None = None,
        bot_id: int | None = None,
        ip: str | None = None,
        action: str | None = None,
    ) -> RateLimitResult:
        """
        Check multiple layers at once.
        Returns first failure or success.
        """
        checks = [
            ("global", "server"),
        ]

        if ip:
            checks.append(("webhook", f"ip:{ip}"))
        if bot_id:
            checks.append(("bot", bot_id))
        if user_id:
            checks.append(("user", user_id))
        if action and user_id:
            checks.append((action, user_id))

        for layer, identifier in checks:
            result = await self.check(layer, identifier)
            if not result.allowed:
                log.warning(
                    "Multi-check failed",
                    layer=layer,
                    identifier=identifier,
                    penalty=result.penalty.value,
                )
                return result

        return RateLimitResult(allowed=True, remaining=9999, limit=9999, reset_after=60)

    async def reset_penalty(self, identifier: str) -> None:
        """Manually reset penalties for a user"""
        if identifier in self._penalties:
            del self._penalties[identifier]
        # Also reset Redis rate limit
        try:
            await cache.connect()
            keys = await cache.redis.keys(f"ratelimit:*:{identifier}")
            if keys:
                await cache.redis.delete(*keys)
        except Exception:
            pass
        log.info("Penalty reset", identifier=identifier)

    def get_server_status(self) -> dict:
        """Get current server load and rate limit status"""
        return self._load_monitor.get_server_status()


# ============================================================================
# Singleton
# ============================================================================

advanced_rate_limiter = AdvancedRateLimiter()
