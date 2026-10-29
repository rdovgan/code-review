import hashlib
import hmac
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from app.adapters.base import hmac_verify
from app.adapters.bitbucket import BitbucketAdapter

FIXTURES = Path(__file__).parent / "fixtures"


def _make_adapter(secret: str = "mysecret") -> BitbucketAdapter:
    return BitbucketAdapter(username="user", app_password="pass", webhook_secret=secret)


def _sign(secret: str, body: bytes) -> str:
    mac = hmac.new(secret.encode(), body, "sha256")
    return "sha256=" + mac.hexdigest()


def test_bitbucket_hmac_valid():
    body = b'{"test": "payload"}'
    sig = _sign("mysecret", body)
    assert hmac_verify("mysecret", body, sig[len("sha256="):]) is True


def test_bitbucket_hmac_invalid():
    body = b'{"test": "payload"}'
    tampered = b'{"test": "tampered"}'
    sig = _sign("mysecret", body)
    assert hmac_verify("mysecret", tampered, sig[len("sha256="):]) is False


def test_bitbucket_validate_webhook_valid():
    adapter = _make_adapter()
    body = b'{"event": "pullrequest:created"}'
    sig = _sign("mysecret", body)
    assert adapter.validate_webhook(body, {"x-hub-signature": sig}) is True


def test_bitbucket_validate_webhook_invalid():
    adapter = _make_adapter()
    body = b'{"event": "pullrequest:created"}'
    tampered = b'{"event": "something else"}'
    sig = _sign("mysecret", body)
    assert adapter.validate_webhook(tampered, {"x-hub-signature": sig}) is False


def test_bitbucket_parse_created():
    adapter = _make_adapter()
    payload = json.loads((FIXTURES / "bitbucket_payload.json").read_text())
    ctx = adapter.parse_webhook(payload)
    assert ctx is not None
    assert ctx.pr_id == 42
    assert ctx.repo_full_name == "myworkspace/myrepo"
    assert ctx.base_sha == "deadbeef1234"
    assert ctx.head_sha == "abc123def456"
    assert ctx.author == "Jane Developer"
    assert ctx.title == "Add user authentication feature"
    assert ctx.platform == "bitbucket"
    assert ctx.source_branch == "feature/auth"
    assert ctx.target_branch == "main"


def test_bitbucket_parse_ignored_event():
    adapter = _make_adapter()
    payload = {"event": "repo:push", "repository": {"full_name": "workspace/repo"}}
    ctx = adapter.parse_webhook(payload)
    assert ctx is None


def test_bitbucket_author_is_pr_author_not_actor():
    adapter = _make_adapter()
    payload = json.loads((FIXTURES / "bitbucket_payload.json").read_text())
    payload["actor"] = {"display_name": "Someone Who Commented"}
    payload["pullrequest"]["author"] = {"display_name": "Jane Developer"}
    ctx = adapter.parse_webhook(payload)
    assert ctx.author == "Jane Developer"


def test_bitbucket_list_endpoints_follow_pagination():
    from unittest.mock import MagicMock

    from app.models import PRContext

    adapter = _make_adapter()
    page1 = MagicMock()
    page1.json.return_value = {
        "values": [{"id": 1, "content": {"raw": "## AI Code Review Summary"}}],
        "next": "https://api.bitbucket.org/2.0/next-page",
    }
    page2 = MagicMock()
    page2.json.return_value = {"values": [{"id": 2, "content": {"raw": "**[BUG]** old"}}]}
    adapter._client = MagicMock()
    adapter._client.get.side_effect = [page1, page2]

    ctx = PRContext(platform="bitbucket", repo_full_name="ws/repo", pr_id=7, base_sha="b", head_sha="h",
                    author="a", title="t", language="java", diff="")
    comments = adapter.get_existing_bot_comments(ctx)

    assert [c["id"] for c in comments] == ["1", "2"]
    first, second = adapter._client.get.call_args_list
    assert first.kwargs["params"] == {"pagelen": 100}
    assert second.args[0] == "https://api.bitbucket.org/2.0/next-page"
    assert second.kwargs["params"] is None
