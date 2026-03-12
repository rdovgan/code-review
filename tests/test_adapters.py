import hashlib
import hmac
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.adapters.base import hmac_verify
from app.adapters.bitbucket import BitbucketAdapter
from app.config.settings import Settings

FIXTURES = Path(__file__).parent / "fixtures"


def _make_settings(**kwargs) -> Settings:
    defaults = dict(
        ANTHROPIC_API_KEY="test",
        REDIS_URL="redis://localhost:6379/0",
        BITBUCKET_WEBHOOK_SECRET="mysecret",
        BITBUCKET_USERNAME="user",
        BITBUCKET_APP_PASSWORD="pass",
    )
    defaults.update(kwargs)
    return Settings(**defaults)


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
    settings = _make_settings()
    adapter = BitbucketAdapter(settings)
    body = b'{"event": "pullrequest:created"}'
    sig = _sign("mysecret", body)
    assert adapter.validate_webhook(body, {"x-hub-signature": sig}) is True


def test_bitbucket_validate_webhook_invalid():
    settings = _make_settings()
    adapter = BitbucketAdapter(settings)
    body = b'{"event": "pullrequest:created"}'
    tampered = b'{"event": "something else"}'
    sig = _sign("mysecret", body)
    assert adapter.validate_webhook(tampered, {"x-hub-signature": sig}) is False


def test_bitbucket_parse_created():
    settings = _make_settings()
    adapter = BitbucketAdapter(settings)
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


def test_bitbucket_parse_ignored_event():
    settings = _make_settings()
    adapter = BitbucketAdapter(settings)
    # Payload without a "pullrequest" key → should return None
    payload = {"event": "repo:push", "repository": {"full_name": "workspace/repo"}}
    ctx = adapter.parse_webhook(payload)
    assert ctx is None
