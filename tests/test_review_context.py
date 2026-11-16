from unittest.mock import MagicMock

import httpx
import respx

from app.config.settings import Settings
from app.context.jira import JiraTicket, extract_ticket_key, fetch_ticket
from app.context.review_context import build_review_context, gather_review_context
from app.models import PRContext, ReviewConfig


def _ctx(**kwargs) -> PRContext:
    defaults = dict(
        platform="bitbucket", repo_full_name="ws/repo", pr_id=1, base_sha="master", head_sha="abc",
        author="dev", title="Mask secrets in logs", language="java", diff="",
        source_branch="fix/PS-8090/Mask-secrets-in-logs", target_branch="master",
        description="Payment logs printed card tokens.",
    )
    defaults.update(kwargs)
    return PRContext(**defaults)


def test_extract_ticket_key_from_branch_then_title():
    assert extract_ticket_key("fix/PS-8090/Mask-secrets-in-logs") == "PS-8090"
    assert extract_ticket_key("epic/BP-41912_Sending-invoices") == "BP-41912"
    assert extract_ticket_key("feature/no-ticket", "BP-7 Add invoices") == "BP-7"
    assert extract_ticket_key("demo-dev", "Fix typo") is None
    assert extract_ticket_key("utf-8-fix") is None


@respx.mock
def test_fetch_ticket_reads_summary_and_description():
    respx.get("https://jira.example/rest/api/2/issue/PS-8090").mock(return_value=httpx.Response(200, json={
        "fields": {"summary": "Mask secrets", "description": "Card tokens must not be logged",
                   "issuetype": {"name": "Bug"}},
    }))
    ticket = fetch_ticket("https://jira.example/", "bot@example.com", "token", "PS-8090")
    assert ticket == JiraTicket("PS-8090", "Bug", "Mask secrets", "Card tokens must not be logged")


@respx.mock
def test_fetch_ticket_returns_none_on_failure():
    respx.get("https://jira.example/rest/api/2/issue/PS-1").mock(return_value=httpx.Response(500))
    assert fetch_ticket("https://jira.example", "e", "t", "PS-1") is None
    respx.get("https://jira.example/rest/api/2/issue/PS-2").mock(return_value=httpx.Response(404))
    assert fetch_ticket("https://jira.example", "e", "t", "PS-2") is None


def test_build_review_context_has_intent_and_guides():
    ticket = JiraTicket("PS-8090", "Bug", "Mask secrets", "Card tokens must not be logged")
    text = build_review_context(_ctx(), ["Mask token in PaymentLogger"], ticket,
                                {"CLAUDE.md": "Use MaskUtils for secrets"}, max_chars=10_000)
    assert text.startswith("PR_CONTEXT")
    for expected in ("Title: Mask secrets in logs", "fix/PS-8090/Mask-secrets-in-logs → master",
                     "Payment logs printed card tokens.", "- Mask token in PaymentLogger",
                     "Jira ticket PS-8090 (Bug)", "Card tokens must not be logged",
                     "PROJECT_GUIDE (CLAUDE.md)", "Use MaskUtils for secrets"):
        assert expected in text


def test_build_review_context_respects_budget_and_cuts_guides_first():
    text = build_review_context(_ctx(), [], None, {"CLAUDE.md": "g" * 50_000}, max_chars=3_000)
    assert len(text) <= 3_000
    assert "Payment logs printed card tokens." in text
    assert "…[truncated]" in text
    assert build_review_context(_ctx(), [], None, {}, max_chars=0) == ""


def test_gather_review_context_skips_jira_when_not_configured_and_missing_guides():
    adapter = MagicMock()
    adapter.get_file_content.return_value = None
    settings = Settings(JIRA_BASE_URL="", AI_PR_CONTEXT_MAX_CHARS=10_000)
    text = gather_review_context(adapter, _ctx(), ReviewConfig(), settings, commits=[])
    adapter.get_file_content.assert_called_once_with(_ctx(), "CLAUDE.md", "master")
    assert "Jira ticket" not in text
    assert "PROJECT_GUIDE" not in text
    assert "Title: Mask secrets in logs" in text
