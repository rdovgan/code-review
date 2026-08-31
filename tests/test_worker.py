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
def test_process_review_verifies_semgrep_findings_with_ai(mock_config, mock_semgrep_cls, mock_ai_cls, mock_get_adapter):
    mock_adapter = MagicMock()
    mock_adapter.get_diff.return_value = "\n".join(f"line {i}" for i in range(10))
    mock_adapter.get_changed_files.return_value = ["Foo.java"]
    mock_adapter.get_existing_bot_comments.return_value = []
    mock_get_adapter.return_value = mock_adapter

    config = ReviewConfig(semgrep_ai_verify=True)
    mock_config.return_value = config

    raw_finding = Finding(
        severity=Severity.BUG, file="Foo.java", line=1,
        message="raw semgrep hit", suggestion="fix", source="semgrep",
    )
    confirmed_finding = Finding(
        severity=Severity.BUG, file="Foo.java", line=1,
        message="confirmed real bug", suggestion="fix", source="semgrep",
    )
    mock_semgrep_cls.return_value.run.return_value = [raw_finding]
    mock_ai_cls.return_value.verify_semgrep_findings.return_value = [confirmed_finding]
    mock_ai_cls.return_value.review.return_value = []

    from app.workers.celery_app import process_review

    ctx = _make_pr_context()
    payload = {"platform": "bitbucket", "diff": ctx.diff, **asdict(ctx)}
    payload["platform"] = "bitbucket"

    result = process_review.run(payload)

    mock_ai_cls.return_value.verify_semgrep_findings.assert_called_once()
    called_findings = mock_ai_cls.return_value.verify_semgrep_findings.call_args[0][0]
    assert called_findings == [raw_finding]
    assert result["findings"] == 1


@patch("app.workers.celery_app.get_adapter")
@patch("app.workers.celery_app.AIReviewer")
@patch("app.workers.celery_app.SemgrepRunner")
@patch("app.workers.celery_app.load_project_config")
def test_process_review_semgrep_verification_disabled_skips_ai_call(mock_config, mock_semgrep_cls, mock_ai_cls, mock_get_adapter):
    mock_adapter = MagicMock()
    mock_adapter.get_diff.return_value = "\n".join(f"line {i}" for i in range(10))
    mock_adapter.get_changed_files.return_value = ["Foo.java"]
    mock_adapter.get_existing_bot_comments.return_value = []
    mock_get_adapter.return_value = mock_adapter

    config = ReviewConfig(semgrep_ai_verify=False, ai_review=False)
    mock_config.return_value = config

    raw_finding = Finding(
        severity=Severity.BUG, file="Foo.java", line=1,
        message="raw semgrep hit", suggestion="fix", source="semgrep",
    )
    mock_semgrep_cls.return_value.run.return_value = [raw_finding]

    from app.workers.celery_app import process_review

    ctx = _make_pr_context()
    payload = {"platform": "bitbucket", "diff": ctx.diff, **asdict(ctx)}
    payload["platform"] = "bitbucket"

    result = process_review.run(payload)

    mock_ai_cls.return_value.verify_semgrep_findings.assert_not_called()
    mock_ai_cls.assert_not_called()
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


def test_notify_mattermost_format():
    from app.workers import celery_app

    adapter = MagicMock()
    adapter.get_pr_commits.return_value = ["feat: add login", "fix: token expiry"]
    ctx = _make_pr_context(
        repo_full_name="workspace/myrepo",
        title="Add user authentication feature",
        source_branch="feature/auth",
        target_branch="main",
        author="dev",
        is_new_pr=True,
    )
    config = ReviewConfig(notify_authors=["dev"])

    with patch.object(celery_app.settings, "MATTERMOST_WEBHOOK_URL", "https://mm.example/hook/xxx"):
        with patch("app.workers.celery_app.mattermost.send_message") as mock_send:
            celery_app._notify_mattermost(adapter, ctx, config, "success", critical_count=1, bug_count=2,
                                          summary_comment_id="4242")

    mock_send.assert_called_once()
    url = mock_send.call_args[0][0]
    attachment = mock_send.call_args.kwargs["attachments"][0]
    text = attachment["text"]
    assert url == "https://mm.example/hook/xxx"
    assert attachment["color"] == celery_app._MM_COLOR_PASS
    assert "**[myrepo / Add user authentication feature](https://bitbucket.org/workspace/myrepo/pull-requests/1)**" in text
    assert "`feature/auth` → `main`" in text
    assert "dev" in text
    assert "**Commits:**" in text
    assert "- feat: add login" in text
    assert "- fix: token expiry" in text
    assert "Critical: **1**" in text
    assert "Bugs: **2**" in text
    assert "[View review comment →](https://bitbucket.org/workspace/myrepo/pull-requests/1#comment-4242)" in text


def test_notify_mattermost_commits_fetch_failure_is_best_effort():
    from app.workers import celery_app

    adapter = MagicMock()
    adapter.get_pr_commits.side_effect = RuntimeError("api down")
    ctx = _make_pr_context(
        repo_full_name="workspace/myrepo",
        source_branch="feature/auth",
        author="dev",
        is_new_pr=True,
    )
    config = ReviewConfig(notify_authors=["dev"])

    with patch.object(celery_app.settings, "MATTERMOST_WEBHOOK_URL", "https://mm.example/hook/xxx"):
        with patch("app.workers.celery_app.mattermost.send_message") as mock_send:
            celery_app._notify_mattermost(adapter, ctx, config, "failure", critical_count=0, bug_count=0)

    mock_send.assert_called_once()
    attachment = mock_send.call_args.kwargs["attachments"][0]
    text = attachment["text"]
    assert attachment["color"] == celery_app._MM_COLOR_FAIL
    assert "**Commits:**" not in text
    assert "❌" in text
    assert "No critical issues or bugs found" in text


def test_notify_mattermost_skipped_when_not_in_notify_authors():
    from app.workers import celery_app

    adapter = MagicMock()
    ctx = _make_pr_context(author="someone-else", is_new_pr=True)
    config = ReviewConfig(notify_authors=["dev"])

    with patch.object(celery_app.settings, "MATTERMOST_WEBHOOK_URL", "https://mm.example/hook/xxx"):
        with patch("app.workers.celery_app.mattermost.send_message") as mock_send:
            celery_app._notify_mattermost(adapter, ctx, config, "success", critical_count=0, bug_count=0)

    mock_send.assert_not_called()
