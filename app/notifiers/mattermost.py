import logging

import httpx

logger = logging.getLogger(__name__)


def send_message(webhook_url: str, text: str) -> bool:
    """POST a message to a Mattermost Incoming Webhook. Best-effort — never raises."""
    if not webhook_url:
        return False
    try:
        resp = httpx.post(webhook_url, json={"text": text}, timeout=10)
        resp.raise_for_status()
        return True
    except Exception as exc:
        logger.warning("Failed to post Mattermost message: %s", exc)
        return False
