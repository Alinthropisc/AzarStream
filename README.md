<div align="center">

# 📥 MediaFlow
### Advanced Multi-Platform Telegram Bot Manager & Downloader

[![Python 3.12](https://img.shields.io/badge/Python-3.12-blue?logo=python&logoColor=white)](https://python.org)
[![Litestar](https://img.shields.io/badge/Framework-Litestar-6B21A8?logo=fastapi&logoColor=white)](https://litestar.dev)
[![SQLAlchemy 2.0](https://img.shields.io/badge/ORM-SQLAlchemy%202.0-red?logo=sqlalchemy&logoColor=white)](https://sqlalchemy.org)
[![Redis](https://img.shields.io/badge/Cache-Redis-DC382D?logo=redis&logoColor=white)](https://redis.io)
[![ARQ](https://img.shields.io/badge/Queue-ARQ-3776AB)](https://github.com/samuelcolvin/arq)
[![i18n](https://img.shields.io/badge/i18n-EN%20%C2%B7%20RU%20%C2%B7%20UZ-ff6a1a)](#-internationalization)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

---

**Self-hosted control deck for a fleet of Telegram media-downloader bots — with a glassmorphic dark admin dashboard, per-user localisation, and a Redis-backed task pipeline.**

[Features](#-key-features) • [Stack](#-technology-stack) • [Install](#-installation) • [Architecture](#-architecture) • [i18n](#-internationalization)

</div>

## ✨ Key Features

- **📥 Universal Downloader** — Instagram, TikTok, YouTube (incl. Shorts & playlists), Pinterest, VK. YouTube can be converted to MP3/MP4.
- **🤖 Multi-Bot Hub** — Run and supervise multiple Telegram bots from one unified panel.
- **🌑 Premium Glass Admin** — Dark, floating glass left rail (independent panel, like a phone screen), live status bar, ember/flame palette.
- **🌍 Per-User Localisation** — Each user's language (EN / RU / UZ) is stored in DB and applied automatically on every message.
- **📊 Real-time Metrics** — Downloads, user growth, platform success rates, language distribution, queue/worker telemetry.
- **📢 Broadcast System** — Mass-message a bot's audience, optionally filtered by language.
- **🛡️ Enterprise Grade** — Rate limiting, structured logging, subscription-gate, async retry-aware download pipeline.
- **⚡ High Performance** — Litestar (ASGI) + ARQ workers + per-user/global concurrency limits.

## 🛠️ Technology Stack

| Layer | Technology |
| :--- | :--- |
| **Core** | [Python 3.12+](https://python.org) |
| **API / Web** | [Litestar](https://litestar.dev) (ASGI), [Granian](https://github.com/emmett-framework/granian) |
| **Bot SDK** | [aiogram 3](https://docs.aiogram.dev) |
| **Database** | [SQLAlchemy 2.0](https://sqlalchemy.org) + Alembic, PostgreSQL / SQLite |
| **Queue / Tasks** | [ARQ](https://github.com/samuelcolvin/arq) on Redis, APScheduler |
| **Downloaders** | yt-dlp, gallery-dl, custom platform handlers |
| **Frontend** | Jinja2 + Tailwind CSS (CDN), custom glass panels |
| **Logging** | Structlog + Loguru |
| **i18n** | In-tree dictionary (`i18n/lang.py`) — EN / RU / UZ |

## 🚀 Installation

### 1. Clone & setup
```bash
git clone https://github.com/AIAnsar1/MediaFlow.git
cd MediaFlow
uv venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
uv sync
```

### 2. Configure
```bash
cp .env.example .env
# fill in BOT tokens, DATABASE_URL, REDIS_URL, ADMIN credentials
```

### 3. Migrate
```bash
alembic upgrade head
```

### 4. Run
```bash
python main.py
# bot + web in one process — see start_both.sh for split mode
```

> Admin panel: `http://127.0.0.1:8000/admin`

## 🏗️ Architecture

```
┌──────────────┐    ┌────────────────┐    ┌──────────────┐
│  Telegram    │ ←→ │  aiogram bots  │ ←→ │  Processor   │
└──────────────┘    └────────────────┘    └──────┬───────┘
                                                 │
                          ┌──────────────────────┼──────────────────────┐
                          ▼                      ▼                      ▼
                    ┌──────────┐          ┌────────────┐         ┌──────────────┐
                    │  Queue   │          │ Downloaders│         │ Subscription │
                    │ (ARQ +   │          │ (yt-dlp,   │         │   Gate       │
                    │  Redis)  │          │  gallery-  │         │ (channel sub │
                    └────┬─────┘          │  dl, ...)  │         │   check)     │
                         │                └────────────┘         └──────────────┘
                         ▼
                  ┌─────────────┐         ┌──────────────────────────────┐
                  │ ARQ Workers │ ──────▶ │  Cache Channels (Telegram)   │
                  └─────────────┘         └──────────────────────────────┘

                  ┌────────────────────┐
                  │  Litestar Admin    │  Glass dashboard, telemetry,
                  │  /admin            │  bots, users, ads, queues,
                  └────────────────────┘  cookies, broadcasts, stats
```

- **Per-user concurrency** — bounded number of active downloads per user, FIFO queue with priorities.
- **Cache channels** — uploaded media is mirrored to Telegram channels and re-served from there on repeat requests.
- **Cookie pool** — rotating cookie storage for platforms that require auth.
- **Subscription gate** — optional required-channel checks before download.

## 🌍 Internationalization

Bots speak **English, Russian and Uzbek**. The chosen language is per-user and persistent.

| Where | What happens |
| :--- | :--- |
| `/start` for new user | Language picker is shown |
| `set_language:<code>` callback | `UserService.update_language` writes `User.language` in DB |
| Every incoming update | `Processor` loads `db_user.language` into `ctx.language` |
| Message rendering | `MESSAGES[key][ctx.language]` with fallback `ru` → `en` → key |

Translations live in a single dictionary at `i18n/lang.py`. To add a new key, supply all three languages — the helper `get_message(key, lang, **fmt)` handles fallbacks.

## 📊 Statistics Dashboard

Built-in stats module gives a bird's-eye view of the bot network:

- Daily / monthly downloads
- User-language distribution
- Platform success rates and error breakdown
- Active worker / queue depth
- Per-bot telemetry

## 🛡️ Service Stability

The project ships with a comprehensive test suite (135+ tests) covering:

- ✅ Database mapping integrity
- ✅ Multi-source parsing reliability
- ✅ High-load queue stability
- ✅ Event-loop-safe service operations
- ✅ End-to-end download flow

```bash
pytest               # full suite
pytest tests/unit    # unit only
```

## 📚 Further Reading

- [`DEPLOYMENT.md`](DEPLOYMENT.md) — production deployment notes
- [`PERFORMANCE.md`](PERFORMANCE.md) — tuning concurrency, queue, cache
- [`COOKIES.md`](COOKIES.md) — cookie pool setup
- [`BROADCAST_SETUP.md`](BROADCAST_SETUP.md) — mass-messaging configuration
- [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) — common issues
- [`TELEGRAM_FORMATTING.md`](TELEGRAM_FORMATTING.md) — message formatting reference

## 📜 License

Distributed under the **MIT License**. See `LICENSE` for more information.

---

<div align="center">
    Made with ❤️ for the AIAnsar
</div>
