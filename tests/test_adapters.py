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


def test_bitbucket_parse_ignored_event():
    adapter = _make_adapter()
    payload = {"event": "repo:push", "repository": {"full_name": "workspace/repo"}}
    ctx = adapter.parse_webhook(payload)
    assert ctx is None
