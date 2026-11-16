import logging

import httpx

logger = logging.getLogger(__name__)


def send_message(webhook_url: str, text: str = "", attachments: list[dict] | None = None) -> bool:
    """POST a message to a Mattermost Incoming Webhook. Best-effort — never raises.

    Pass ``text`` for a plain message, ``attachments`` for a message-attachment
    payload (rendered by Mattermost with a coloured left border), or both.
    """
    if not webhook_url:
        return False
    payload: dict = {}
    if text:
        payload["text"] = text
    if attachments:
        payload["attachments"] = attachments
    if not payload:
        return False
    try:
        resp = httpx.post(webhook_url, json=payload, timeout=10)
        resp.raise_for_status()
        return True
    except httpx.HTTPStatusError as exc:
        # str(exc) contains the request URL, and the webhook URL is a secret
        logger.warning("Failed to post Mattermost message: HTTP %s", exc.response.status_code)
        return False
    except Exception as exc:
        logger.warning("Failed to post Mattermost message: %s", type(exc).__name__)
        return False
