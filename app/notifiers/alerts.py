"""Operator alerts to Mattermost: limits hit, budget running out, reviews degraded.

They go to MATTERMOST_ALERTS_WEBHOOK_URL, or to the PR channel when that is empty.

Each alert has a key; the same key is sent at most once per ALERT_REPEAT_HOURS, so a
repo that hits its step limit on every PR posts one message, not one per PR.
Best-effort — never raises.
"""
import logging
from datetime import date

import redis

from app.config.settings import Settings
from app.notifiers import mattermost

logger = logging.getLogger(__name__)

_COLOR = "#E8A33D"  # amber: needs attention, not a failed review


def alerts_webhook(settings: Settings) -> str:
    """Operator channel; falls back to the PR channel when no separate one is set."""
    return settings.MATTERMOST_ALERTS_WEBHOOK_URL or settings.MATTERMOST_WEBHOOK_URL


def send_alert(settings: Settings, key: str, title: str, details: str = "",
               client: "redis.Redis | None" = None) -> bool:
    """True when the alert was posted (False: alerts off, already sent recently, or post failed)."""
    webhook = alerts_webhook(settings)
    if not webhook or settings.ALERT_REPEAT_HOURS <= 0:
        return False
    try:
        client = client or redis.from_url(settings.REDIS_URL)
        if not client.set(f"alert:{key}", 1, nx=True, ex=int(settings.ALERT_REPEAT_HOURS * 3600)):
            return False
    except Exception as exc:
        logger.warning("Alert %s not sent: %s", key, type(exc).__name__)
        return False
    text = f"⚠️ **Code review bot: {title}**" + (f"\n{details}" if details else "")
    logger.info("Alert sent: %s", key)
    return mattermost.send_message(webhook,
                                   attachments=[{"color": _COLOR, "fallback": f"Code review bot: {title}",
                                                 "text": text}])


def today() -> str:
    return date.today().isoformat()
