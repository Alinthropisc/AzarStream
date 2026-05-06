"""Duration presets for Post Download Ads."""
from datetime import datetime, timedelta

DURATION_PRESETS = [
    {"label": "7 days", "days": 7, "icon": "⏱️"},
    {"label": "14 days", "days": 14, "icon": "📅"},
    {"label": "1 month", "days": 30, "icon": "🗓️"},
    {"label": "3 months", "days": 90, "icon": "📆"},
    {"label": "6 months", "days": 180, "icon": "📅"},
    {"label": "Forever", "days": None, "icon": "♾️"},
]


def calculate_expires_at(duration_days: int | None, start: datetime | None = None) -> datetime | None:
    """Calculate expiration date from duration_days."""
    if duration_days is None:
        return None
    start = start or datetime.now()
    return start + timedelta(days=duration_days)


def format_expires_at(expires_at: datetime | None) -> str:
    """Human-readable expiration date."""
    if expires_at is None:
        return "♾️ Forever"
    days_left = (expires_at - datetime.now()).days
    if days_left < 0:
        return f"❌ Expired {abs(days_left)} days ago"
    if days_left == 0:
        return "⚠️ Expires today"
    if days_left == 1:
        return "⏰ Expires tomorrow"
    return f"⏰ {days_left} days left ({expires_at.strftime('%Y-%m-%d')})"
