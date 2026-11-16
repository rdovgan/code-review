import logging
import re
from dataclasses import dataclass
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# BP-41912, PS-8090. Not preceded by a letter/digit and not followed by a digit, so it
# matches inside "fix/PS-8090/Mask-secrets" and "epic/BP-41912_Sending" alike.
_TICKET_KEY_RE = re.compile(r"(?<![A-Za-z0-9])([A-Z][A-Z0-9]+-\d+)(?!\d)")


@dataclass
class JiraTicket:
    key: str
    issue_type: str
    summary: str
    description: str


def extract_ticket_key(*texts: str) -> Optional[str]:
    """First Jira key found in the given texts, checked in order (branch first, then title)."""
    for text in texts:
        match = _TICKET_KEY_RE.search(text or "")
        if match:
            return match.group(1)
    return None


def fetch_ticket(base_url: str, email: str, api_token: str, key: str, timeout: float = 15) -> Optional[JiraTicket]:
    """The ticket's summary and description, or None if it can't be read. Never raises."""
    url = f"{base_url.rstrip('/')}/rest/api/2/issue/{key}"
    try:
        resp = httpx.get(
            url,
            params={"fields": "summary,description,issuetype"},
            auth=(email, api_token),
            timeout=timeout,
        )
        if resp.status_code == 404:
            logger.info("Jira ticket %s not found", key)
            return None
        resp.raise_for_status()
        fields = resp.json().get("fields", {})
    except Exception as exc:
        logger.warning("Failed to fetch Jira ticket %s: %s", key, exc)
        return None
    return JiraTicket(
        key=key,
        issue_type=(fields.get("issuetype") or {}).get("name", ""),
        summary=fields.get("summary") or "",
        description=fields.get("description") or "",
    )
