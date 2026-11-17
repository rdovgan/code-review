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

    finding = _make_finding(Severity.BUG)  # BUG posts inline; SUGGEST is summary-only
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


@patch("app.workers.celery_app.gather_review_context", return_value="PR_CONTEXT — intent")
@patch("app.workers.celery_app.get_adapter")
@patch("app.workers.celery_app.AIReviewer")
@patch("app.workers.celery_app.SemgrepRunner")
@patch("app.workers.celery_app.load_project_config")
def test_process_review_sends_pr_context_and_reuses_commits(mock_config, mock_semgrep_cls, mock_ai_cls,
                                                            mock_get_adapter, mock_gather):
    mock_adapter = MagicMock()
    mock_adapter.get_existing_bot_comments.return_value = []
    mock_adapter.get_pr_commits.return_value = ["feat: login"]
    mock_get_adapter.return_value = mock_adapter
    mock_config.return_value = ReviewConfig(static_analysis=False, notify_authors=["dev"])
    mock_ai_cls.return_value.review.return_value = []

    from app.workers import celery_app

    ctx = _make_pr_context(is_new_pr=True)
    payload = {"platform": "bitbucket", "diff": ctx.diff, **asdict(ctx)}
    with patch.object(celery_app.settings, "MATTERMOST_WEBHOOK_URL", "https://mm.example/hook"), \
         patch("app.workers.celery_app.mattermost.send_message"):
        celery_app.process_review.run(payload)

    assert mock_ai_cls.return_value.review.call_args.kwargs["context"] == "PR_CONTEXT — intent"
    assert mock_gather.call_args.args[4] == ["feat: login"]
    mock_adapter.get_pr_commits.assert_called_once()  # shared by the AI context and Mattermost


@patch("app.workers.celery_app.gather_review_context", side_effect=RuntimeError("jira down"))
@patch("app.workers.celery_app.get_adapter")
@patch("app.workers.celery_app.AIReviewer")
@patch("app.workers.celery_app.SemgrepRunner")
@patch("app.workers.celery_app.load_project_config")
def test_process_review_runs_without_pr_context_when_gathering_fails(mock_config, mock_semgrep_cls, mock_ai_cls,
                                                                    mock_get_adapter, mock_gather):
    mock_adapter = MagicMock()
    mock_adapter.get_existing_bot_comments.return_value = []
    mock_get_adapter.return_value = mock_adapter
    mock_config.return_value = ReviewConfig(static_analysis=False)
    mock_ai_cls.return_value.review.return_value = []

    from app.workers.celery_app import process_review

    ctx = _make_pr_context()
    result = process_review.run({"platform": "bitbucket", "diff": ctx.diff, **asdict(ctx)})

    assert mock_ai_cls.return_value.review.call_args.kwargs["context"] == ""
    assert result["status"] == "success"


@patch("app.workers.celery_app.get_adapter")
@patch("app.workers.celery_app.AIReviewer")
@patch("app.workers.celery_app.SemgrepRunner")
@patch("app.workers.celery_app.load_project_config")
def test_process_review_passes_soft_limit_deadline_to_reviewer(mock_config, mock_semgrep_cls, mock_ai_cls, mock_get_adapter):
    """The AI chunk loop must know the task's soft time limit so it can stop
    early with partial findings instead of being SIGKILLed (PR #3565 bug)."""
    mock_adapter = MagicMock()
    mock_adapter.get_diff.return_value = "x"
    mock_adapter.get_changed_files.return_value = ["Foo.java"]
    mock_adapter.get_existing_bot_comments.return_value = []
    mock_get_adapter.return_value = mock_adapter
    mock_config.return_value = ReviewConfig()
    mock_semgrep_cls.return_value.run.return_value = []

    from app.workers.celery_app import process_review

    ctx = _make_pr_context()
    payload = {"platform": "bitbucket", "diff": ctx.diff, **asdict(ctx)}
    payload["platform"] = "bitbucket"

    process_review.request.timelimit = (945, 885)  # (hard, soft) as delivered by Celery
    try:
        with patch("app.workers.celery_app.time.monotonic", return_value=1000.0):
            process_review.run(payload)
    finally:
        process_review.request.timelimit = None

    kwargs = mock_ai_cls.return_value.review.call_args.kwargs
    assert kwargs["deadline"] == 1000.0 + 885


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
@patch("app.workers.celery_app.AIReviewer")
@patch("app.workers.celery_app.SemgrepRunner")
@patch("app.workers.celery_app.load_project_config")
def test_process_review_truncates_large_diff(mock_config, mock_semgrep_cls, mock_ai_cls, mock_get_adapter):
    mock_adapter = MagicMock()
    mock_adapter.get_changed_files.return_value = ["Foo.java"]
    mock_adapter.get_existing_bot_comments.return_value = []
    mock_get_adapter.return_value = mock_adapter

    config = ReviewConfig(max_diff_lines=5)
    mock_config.return_value = config
    mock_semgrep_cls.return_value.run.return_value = []
    mock_ai_cls.return_value.review.return_value = []

    large_diff = "\n".join(f"line {i}" for i in range(100))
    ctx = _make_pr_context(diff=large_diff)
    payload = {"platform": "bitbucket", "diff": large_diff, **asdict(ctx)}
    payload["platform"] = "bitbucket"

    from app.workers.celery_app import process_review

    result = process_review.run(payload)

    # Review continues on the truncated diff; with no findings nothing is posted, only the status
    mock_adapter.post_summary_comment.assert_not_called()
    assert result["status"] == "success"
    assert mock_adapter.set_review_status.call_args_list[-1].args[1:] == ("success", "No issues found")


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
    assert "- " not in text  # no commit bullets when the fetch fails
    assert "❌" in text
    assert "No critical issues or bugs found" in text


def test_notify_mattermost_commits_truncated_to_max():
    from app.workers import celery_app

    adapter = MagicMock()
    adapter.get_pr_commits.return_value = [
        "feat: add login",
        "fix: token expiry",
        "refactor: extract helper",
        "chore: bump deps",
        "docs: update readme",
    ]
    ctx = _make_pr_context(author="dev", is_new_pr=True)
    config = ReviewConfig(notify_authors=["dev"])

    with patch.object(celery_app.settings, "MATTERMOST_WEBHOOK_URL", "https://mm.example/hook/xxx"):
        with patch("app.workers.celery_app.mattermost.send_message") as mock_send:
            celery_app._notify_mattermost(adapter, ctx, config, "success", critical_count=0, bug_count=0)

    text = mock_send.call_args.kwargs["attachments"][0]["text"]
    assert "- feat: add login" in text
    assert "- fix: token expiry" in text
    assert "- refactor: extract helper" in text
    assert "- chore: bump deps" not in text
    assert "- docs: update readme" not in text
    assert "…and 2 more" in text


def test_notify_mattermost_skipped_when_not_in_notify_authors():
    from app.workers import celery_app

    adapter = MagicMock()
    ctx = _make_pr_context(author="someone-else", is_new_pr=True)
    config = ReviewConfig(notify_authors=["dev"])

    with patch.object(celery_app.settings, "MATTERMOST_WEBHOOK_URL", "https://mm.example/hook/xxx"):
        with patch("app.workers.celery_app.mattermost.send_message") as mock_send:
            celery_app._notify_mattermost(adapter, ctx, config, "success", critical_count=0, bug_count=0)

    mock_send.assert_not_called()


def _run_with(mock_config, mock_semgrep_cls, mock_ai_cls, mock_get_adapter, ai_result, config=None, diff=None):
    mock_adapter = MagicMock()
    mock_adapter.get_existing_bot_comments.return_value = []
    mock_get_adapter.return_value = mock_adapter
    mock_config.return_value = config or ReviewConfig(static_analysis=False)
    mock_semgrep_cls.return_value.run.return_value = []
    if isinstance(ai_result, Exception):
        mock_ai_cls.return_value.review.side_effect = ai_result
    else:
        mock_ai_cls.return_value.review.return_value = ai_result

    ctx = _make_pr_context(**({"diff": diff} if diff is not None else {}))
    payload = {"platform": "bitbucket", "diff": ctx.diff, **asdict(ctx)}
    payload["platform"] = "bitbucket"

    from app.workers.celery_app import process_review

    with patch("app.workers.celery_app._release_review_lock") as mock_release:
        result = process_review.run(payload)
    return result, mock_adapter, mock_release


@patch("app.workers.celery_app.get_adapter")
@patch("app.workers.celery_app.AIReviewer")
@patch("app.workers.celery_app.SemgrepRunner")
@patch("app.workers.celery_app.load_project_config")
def test_ai_budget_exhausted_sets_stopped_status_not_success(*mocks):
    from app.analyzers.ai_reviewer import AIReviewResult

    result, adapter, _ = _run_with(*mocks, AIReviewResult(chunks_total=1, chunks_reviewed=0, stop_reason="budget"))

    assert result["status"] == "stopped"
    state, description = adapter.set_review_status.call_args_list[-1].args[1:]
    assert state == "stopped"
    assert "budget" in description
    adapter.post_summary_comment.assert_not_called()  # the status says why


@patch("app.workers.celery_app.get_adapter")
@patch("app.workers.celery_app.AIReviewer")
@patch("app.workers.celery_app.SemgrepRunner")
@patch("app.workers.celery_app.load_project_config")
def test_ai_exception_sets_stopped_status(*mocks):
    result, adapter, _ = _run_with(*mocks, RuntimeError("api down"))
    assert result["status"] == "stopped"


@patch("app.workers.celery_app.get_adapter")
@patch("app.workers.celery_app.AIReviewer")
@patch("app.workers.celery_app.SemgrepRunner")
@patch("app.workers.celery_app.load_project_config")
def test_partial_ai_review_keeps_status_but_notes_it(*mocks):
    from app.analyzers.ai_reviewer import AIReviewResult

    finding = Finding(severity=Severity.BUG, file="Foo.java", line=1, message="m", suggestion="s", source="ai")
    result, adapter, _ = _run_with(*mocks, AIReviewResult([finding], chunks_total=3, chunks_reviewed=2,
                                                          stop_reason="deadline"))

    assert result["status"] == "success"
    summary = adapter.post_summary_comment.call_args.args[1]
    assert "covered only part of the diff" in summary


@patch("app.workers.celery_app.get_adapter")
@patch("app.workers.celery_app.AIReviewer")
@patch("app.workers.celery_app.SemgrepRunner")
@patch("app.workers.celery_app.load_project_config")
def test_review_lock_released_after_review(*mocks):
    _, _, mock_release = _run_with(*mocks, [])
    mock_release.assert_called_once()
    assert mock_release.call_args.args[0]["pr_id"] == 1


@patch("app.workers.celery_app.get_adapter")
@patch("app.workers.celery_app.AIReviewer")
@patch("app.workers.celery_app.SemgrepRunner")
@patch("app.workers.celery_app.load_project_config")
def test_only_ignored_files_skips_analysis(*mocks):
    mock_config, mock_semgrep_cls, mock_ai_cls, mock_get_adapter = mocks
    diff = "diff --git a/app.properties b/app.properties\n@@ -1 +1 @@\n+a=b\n"
    result, adapter, _ = _run_with(*mocks, [], config=ReviewConfig(ignore_paths=["**/*.properties"]), diff=diff)

    assert result["status"] == "skipped"
    mock_ai_cls.return_value.review.assert_not_called()
    adapter.set_review_status.assert_called_once()
    assert adapter.set_review_status.call_args.args[1] == "success"


@patch("app.workers.celery_app.get_adapter")
@patch("app.workers.celery_app.AIReviewer")
@patch("app.workers.celery_app.SemgrepRunner")
@patch("app.workers.celery_app.load_project_config")
def test_truncation_note_lists_skipped_files(*mocks):
    diff = (
        "diff --git a/Small.java b/Small.java\n@@ -1 +1 @@\n+x\n"
        "diff --git a/Huge.java b/Huge.java\n@@ -1 +1,50 @@\n" + "+y\n" * 50
    )
    _, adapter, _ = _run_with(*mocks, [], config=ReviewConfig(static_analysis=False, max_diff_lines=10), diff=diff)

    adapter.post_summary_comment.assert_not_called()  # no findings: status only
    # only the file that fit was sent to the AI
    sent_ctx = mocks[2].return_value.review.call_args.args[0]
    assert "Huge.java" not in sent_ctx.diff


def test_task_failure_signal_finalizes_status_and_releases_lock():
    from app.workers import celery_app

    payload = {"platform": "bitbucket", "diff": "", **asdict(_make_pr_context(diff=""))}
    with patch("app.workers.celery_app.get_adapter") as mock_get_adapter, \
         patch("app.workers.celery_app._release_review_lock") as mock_release:
        celery_app.on_task_failure(sender=None, task_id="t1", exception=ValueError("x"), args=[payload])

    state = mock_get_adapter.return_value.set_review_status.call_args.args[1]
    assert state == "stopped"
    mock_release.assert_called_once_with(payload)


def test_process_review_retries_on_httpx_transport_errors():
    import httpx

    from app.workers.celery_app import process_review

    assert httpx.TransportError in process_review.autoretry_for


# --- time budget (PR #52) ---

import threading  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from celery.exceptions import SoftTimeLimitExceeded  # noqa: E402

PART_A = "diff --git a/src/A.java b/src/A.java\n--- a/src/A.java\n+++ b/src/A.java\n@@ -1 +1 @@\n-a\n+b\n"
PART_B = "diff --git a/src/B.java b/src/B.java\n--- a/src/B.java\n+++ b/src/B.java\n@@ -1 +1 @@\n-a\n+b\n"


def _agentic_payload():
    ctx = _make_pr_context(diff=PART_A + PART_B, changed_files=["src/A.java", "src/B.java"])
    return {"platform": "bitbucket", "diff": ctx.diff, **asdict(ctx)}


@patch("app.workers.celery_app.metrics")
@patch("app.workers.celery_app._alert")
@patch("app.workers.celery_app.gather_review_context", return_value="")
@patch("app.workers.celery_app._agentic_review")
@patch("app.workers.celery_app.get_adapter")
@patch("app.workers.celery_app.AIReviewer")
@patch("app.workers.celery_app.load_project_config")
def test_agentic_partial_diff_reviews_only_unreviewed_files(mock_config, mock_ai_cls, mock_get_adapter, mock_agentic,
                                                            _gather, mock_alert, mock_metrics):
    from app.analyzers.ai_reviewer import AIReviewResult
    from app.workers.celery_app import process_review

    adapter = MagicMock()
    adapter.get_existing_bot_comments.return_value = []
    mock_get_adapter.return_value = adapter
    mock_config.return_value = ReviewConfig(review_mode="agentic", static_analysis=False)
    agent_res = AIReviewResult([Finding(Severity.BUG, "src/A.java", 1, "a", "s", source="ai")],
                               chunks_total=2, chunks_reviewed=1)
    agent_res.tool_calls = 7
    agent_res.unreviewed_diff, agent_res.failed_parts = PART_B, 1
    agent_res.failed_part_numbers, agent_res.failure_reason = [2], "no time left for this part"
    mock_agentic.return_value = agent_res
    mock_ai_cls.return_value.review.return_value = AIReviewResult(
        [Finding(Severity.BUG, "src/B.java", 1, "b", "s", source="ai")], chunks_total=1, chunks_reviewed=1)

    result = process_review.run(_agentic_payload())

    sub = mock_ai_cls.return_value.review.call_args.args[0]
    assert sub.diff == PART_B  # only the failed part's files, not the whole diff again
    assert result["status"] == "success" and result["findings"] == 2
    summary = adapter.post_summary_comment.call_args.args[1]
    assert "Deep review covered 1 of 2 parts (7 code lookups)" in summary
    assert "no time left for this part" in summary
    kind, title, details = mock_alert.call_args.args[1:]
    assert kind == "agent_partial:no time left for this part" and "partly fell back" in title
    assert details.startswith("Part(s) 2 of 2")
    mock_metrics.record_agentic.assert_called_once_with("partial")


@patch("app.workers.celery_app.get_adapter")
@patch("app.workers.celery_app.AIReviewer")
@patch("app.workers.celery_app.SemgrepRunner")
@patch("app.workers.celery_app.load_project_config")
def test_soft_time_limit_sets_stopped_and_returns(*mocks):
    result, adapter, mock_release = _run_with(*mocks, SoftTimeLimitExceeded())
    # Returned, not raised: on_task_failure would overwrite the status with "Review failed"
    assert result == {"status": "stopped", "reason": "soft_time_limit"}
    state, description = adapter.set_review_status.call_args_list[-1].args[1:]
    assert state == "stopped" and "time limit" in description
    mock_release.assert_called_once()


@patch("app.workers.celery_app.get_adapter")
@patch("app.workers.celery_app.AIReviewer")
@patch("app.workers.celery_app.SemgrepRunner")
@patch("app.workers.celery_app.load_project_config")
def test_semgrep_runs_in_background_and_join_timeout_adds_note(mock_config, mock_semgrep_cls, mock_ai_cls,
                                                                mock_get_adapter):
    from app.workers import celery_app

    release = threading.Event()
    mock_semgrep_cls.return_value.run.side_effect = lambda *a, **k: release.wait(5) and []
    mock_adapter = MagicMock()
    mock_adapter.get_existing_bot_comments.return_value = []
    mock_get_adapter.return_value = mock_adapter
    mock_config.return_value = ReviewConfig()
    mock_ai_cls.return_value.review.return_value = []

    ctx = _make_pr_context()
    celery_app.process_review.request.timelimit = (1005, 885)
    try:
        with patch.object(celery_app, "_FINALIZE_RESERVE", 10_000), \
             patch.object(celery_app, "_STATIC_JOIN_MIN_WAIT", 0.1):
            result = celery_app.process_review.run({"platform": "bitbucket", "diff": ctx.diff, **asdict(ctx)})
    finally:
        celery_app.process_review.request.timelimit = None
        release.set()

    mock_ai_cls.return_value.review.assert_called_once()  # ran while Semgrep was still busy
    assert result["status"] == "success"
    mock_adapter.post_summary_comment.assert_not_called()  # no findings: status only


@patch("app.workers.celery_app.metrics")
@patch("app.workers.celery_app.send_alert")
def test_time_limit_hit_records_metric_without_alert(send, mock_metrics):
    from app.workers import celery_app
    ctx = _make_pr_context()
    notes = celery_app._alert_agent_limits(ctx, ReviewConfig(), SimpleNamespace(limits_hit={"time"}))
    send.assert_not_called()
    mock_metrics.record_agent_limit.assert_called_once_with("time")
    assert notes and "time limit" in notes[0]


@patch("app.workers.celery_app.get_adapter")
@patch("app.workers.celery_app.AIReviewer")
@patch("app.workers.celery_app.SemgrepRunner")
@patch("app.workers.celery_app.load_project_config")
def test_unverified_note_in_summary(*mocks):
    from app.analyzers.ai_reviewer import AIReviewResult

    ai_result = AIReviewResult([_make_finding(Severity.BUG)], chunks_total=1, chunks_reviewed=1)
    ai_result.unverified = 1
    _, adapter, _ = _run_with(*mocks, ai_result)
    assert "1 AI finding(s) were not double-checked (time limit)" in adapter.post_summary_comment.call_args.args[1]


@patch("app.workers.celery_app.get_adapter")
@patch("app.workers.celery_app.AIReviewer")
@patch("app.workers.celery_app.SemgrepRunner")
@patch("app.workers.celery_app.load_project_config")
def test_enqueued_at_is_not_passed_to_pr_context(*mocks):
    import time
    mock_config, mock_semgrep_cls, mock_ai_cls, mock_get_adapter = mocks
    mock_get_adapter.return_value = MagicMock(**{"get_existing_bot_comments.return_value": []})
    mock_config.return_value = ReviewConfig(static_analysis=False)
    mock_ai_cls.return_value.review.return_value = []
    from app.workers.celery_app import process_review
    ctx = _make_pr_context()
    payload = {"platform": "bitbucket", "diff": ctx.diff, **asdict(ctx), "enqueued_at": time.time() - 30}
    assert process_review.run(payload)["status"] == "success"
    assert "enqueued_at" in payload  # the task payload itself is left alone (retries reuse it)



@patch("app.workers.celery_app.get_adapter")
@patch("app.workers.celery_app.AIReviewer")
@patch("app.workers.celery_app.SemgrepRunner")
@patch("app.workers.celery_app.load_project_config")
def test_no_findings_posts_no_comment_only_success_status(*mocks):
    result, adapter, _ = _run_with(*mocks, [])
    adapter.post_summary_comment.assert_not_called()
    adapter.post_inline_comment.assert_not_called()
    assert adapter.set_review_status.call_args_list[-1].args[1:] == ("success", "No issues found")
