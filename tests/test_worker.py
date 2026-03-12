from dataclasses import asdict
from unittest.mock import MagicMock, patch

import pytest

from app.models import Finding, PRContext, ReviewConfig, Severity


def _make_pr_context(**kwargs) -> PRContext:
    defaults = dict(
        platform="bitbucket",
        repo_full_name="workspace/repo",
        pr_id=1,
        base_sha="base",
        head_sha="head",
        author="dev",
        title="Test PR",
        language="java",
        diff="\n".join(f"line {i}" for i in range(10)),
        changed_files=["Foo.java"],
    )
    defaults.update(kwargs)
    return PRContext(**defaults)


def _make_finding(severity: Severity = Severity.SUGGEST) -> Finding:
    return Finding(
        severity=severity,
        file="Foo.java",
        line=1,
        message="Test issue",
        suggestion="Fix it",
        source="ai",
    )


@patch("app.workers.celery_app.get_adapter")
@patch("app.workers.celery_app.AIReviewer")
@patch("app.workers.celery_app.SemgrepRunner")
@patch("app.workers.celery_app.load_project_config")
def test_process_review_posts_comments(mock_config, mock_semgrep_cls, mock_ai_cls, mock_get_adapter):
    mock_adapter = MagicMock()
    mock_adapter.get_diff.return_value = "\n".join(f"line {i}" for i in range(10))
    mock_adapter.get_changed_files.return_value = ["Foo.java"]
    mock_adapter.get_existing_bot_comments.return_value = []
    mock_get_adapter.return_value = mock_adapter

    config = ReviewConfig()
    mock_config.return_value = config

    finding = _make_finding(Severity.SUGGEST)
    mock_ai_cls.return_value.review.return_value = [finding]
    mock_semgrep_cls.return_value.run.return_value = []

    from app.workers.celery_app import process_review

    ctx = _make_pr_context()
    payload = {"platform": "bitbucket", "diff": ctx.diff, **asdict(ctx)}
    payload.pop("platform", None)
    payload["platform"] = "bitbucket"

    # Call the underlying function directly (bypassing Celery)
    result = process_review.run(payload)

    mock_adapter.post_inline_comment.assert_called_once()
    mock_adapter.post_summary_comment.assert_called_once()
    assert result["findings"] == 1


@patch("app.workers.celery_app.get_adapter")
@patch("app.workers.celery_app.AIReviewer")
@patch("app.workers.celery_app.SemgrepRunner")
@patch("app.workers.celery_app.load_project_config")
def test_process_review_failure_status_on_critical(mock_config, mock_semgrep_cls, mock_ai_cls, mock_get_adapter):
    mock_adapter = MagicMock()
    mock_adapter.get_diff.return_value = "\n".join(f"line {i}" for i in range(10))
    mock_adapter.get_changed_files.return_value = ["Foo.java"]
    mock_adapter.get_existing_bot_comments.return_value = []
    mock_get_adapter.return_value = mock_adapter

    config = ReviewConfig(block_merge_on=["CRITICAL"])
    mock_config.return_value = config

    finding = _make_finding(Severity.CRITICAL)
    mock_ai_cls.return_value.review.return_value = [finding]
    mock_semgrep_cls.return_value.run.return_value = []

    from app.workers.celery_app import process_review

    ctx = _make_pr_context()
    payload = {"platform": "bitbucket", "diff": ctx.diff, **asdict(ctx)}
    payload["platform"] = "bitbucket"

    result = process_review.run(payload)

    # Check set_review_status called with "failure"
    status_calls = mock_adapter.set_review_status.call_args_list
    final_call = status_calls[-1]
    assert final_call[0][1] == "failure"
    assert result["status"] == "failure"


@patch("app.workers.celery_app.get_adapter")
@patch("app.workers.celery_app.load_project_config")
def test_process_review_too_large(mock_config, mock_get_adapter):
    mock_adapter = MagicMock()
    mock_adapter.get_changed_files.return_value = ["Foo.java"]
    mock_get_adapter.return_value = mock_adapter

    config = ReviewConfig(max_diff_lines=5)
    mock_config.return_value = config

    large_diff = "\n".join(f"line {i}" for i in range(100))
    ctx = _make_pr_context(diff=large_diff)
    payload = {"platform": "bitbucket", "diff": large_diff, **asdict(ctx)}
    payload["platform"] = "bitbucket"

    from app.workers.celery_app import process_review

    result = process_review.run(payload)

    mock_adapter.post_summary_comment.assert_called_once()
    call_body = mock_adapter.post_summary_comment.call_args[0][1]
    assert "too large" in call_body.lower()
    assert result["reason"] == "diff_too_large"
