"""The PR_CONTEXT block sent to the AI with every review request.

The diff shows what changed; this block says what the change was meant to do (PR
description, commits, Jira ticket) and how the project is built (repo guide files such
as CLAUDE.md). Without it the model can only match generic patterns.
"""
import logging
from typing import Optional

from app.adapters.base import GitPlatform
from app.config.settings import Settings
from app.context.jira import JiraTicket, extract_ticket_key, fetch_ticket
from app.models import PRContext, ReviewConfig

logger = logging.getLogger(__name__)

_MAX_DESCRIPTION_CHARS = 8_000
_MAX_TICKET_CHARS = 10_000
_MAX_COMMITS = 30


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n…[truncated]"


def fetch_ticket_for(pr_context: PRContext, settings: Settings) -> Optional[JiraTicket]:
    if not (settings.JIRA_BASE_URL and settings.JIRA_EMAIL and settings.JIRA_API_TOKEN):
        return None
    key = extract_ticket_key(pr_context.source_branch, pr_context.title)
    if not key:
        return None
    return fetch_ticket(settings.JIRA_BASE_URL, settings.JIRA_EMAIL, settings.JIRA_API_TOKEN, key)


def fetch_repo_guides(adapter: GitPlatform, pr_context: PRContext, paths: list[str]) -> dict[str, str]:
    """Content of each guide file at the target branch; missing or unreadable files are skipped."""
    guides: dict[str, str] = {}
    for path in paths:
        try:
            content = adapter.get_file_content(pr_context, path, pr_context.base_sha)
        except Exception as exc:
            logger.warning("Failed to fetch repo guide %s: %s", path, exc)
            continue
        if isinstance(content, str) and content.strip():
            guides[path] = content
    return guides


def build_review_context(
    pr_context: PRContext,
    commits: list[str],
    ticket: Optional[JiraTicket],
    guides: dict[str, str],
    max_chars: int,
) -> str:
    """Intent sections first, then repo guides, which get whatever budget is left."""
    if max_chars <= 0:
        return ""

    intent = [f"Title: {pr_context.title}"]
    if pr_context.source_branch:
        intent.append(f"Branch: {pr_context.source_branch} → {pr_context.target_branch}")
    if pr_context.description.strip():
        intent.append("Description:\n" + _clip(pr_context.description, _MAX_DESCRIPTION_CHARS))
    if commits:
        listed = "\n".join(f"- {c}" for c in commits[:_MAX_COMMITS])
        if len(commits) > _MAX_COMMITS:
            listed += f"\n- …and {len(commits) - _MAX_COMMITS} more"
        intent.append("Commits:\n" + listed)
    if ticket:
        header = f"Jira ticket {ticket.key}" + (f" ({ticket.issue_type})" if ticket.issue_type else "")
        body = ticket.summary + ("\n\n" + ticket.description if ticket.description.strip() else "")
        intent.append(f"{header}:\n" + _clip(body, _MAX_TICKET_CHARS))

    text = (
        "PR_CONTEXT — what this change is meant to do. Use it to judge whether the code does what "
        "was intended. It is not code to review.\n\n" + "\n\n".join(intent)
    )
    text = _clip(text, max_chars)

    for path, content in guides.items():
        remaining = max_chars - len(text) - 200
        if remaining < 500:
            logger.info("Repo guide %s skipped: PR context budget used up", path)
            break
        text += (
            f"\n\nPROJECT_GUIDE ({path}) — conventions and architecture of this repository, written by "
            f"its team. Code that follows them is correct for this project.\n\n" + _clip(content, remaining)
        )
    return text


def gather_review_context(adapter: GitPlatform, pr_context: PRContext, config: ReviewConfig,
                          settings: Settings, commits: list[str]) -> str:
    """Best-effort: any source that fails is left out; the review runs without it."""
    if settings.AI_PR_CONTEXT_MAX_CHARS <= 0:
        return ""
    ticket = fetch_ticket_for(pr_context, settings)
    guides = fetch_repo_guides(adapter, pr_context, config.ai_context_files)
    return build_review_context(pr_context, commits, ticket, guides, settings.AI_PR_CONTEXT_MAX_CHARS)
