"""Tests for webhook queueing — dynamic time limits must reflect the real PR
size (bug: Bitbucket webhooks carry no changed files, so every PR got the
10-file default limits and large PRs were SIGKILLed mid-review, leaving the
build status stuck on "in progress")."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from app.models import PRContext
from app.workers.celery_app import (
    _BASE_SOFT_TIME_LIMIT,
    _BASE_TIME_LIMIT,
    _EXTRA_SECONDS_PER_FILE,
    _MAX_FILE_COUNT_FOR_LIMITS,
)


def _make_pr_context(**kwargs) -> PRContext:
    defaults = dict(
        platform="bitbucket",
        repo_full_name="workspace/repo",
        pr_id=1,
        base_sha="base",
        head_sha="head",
        author="dev",
        title="Test PR",
        language="js",
        diff="",
        changed_files=[],
    )
    defaults.update(kwargs)
    return PRContext(**defaults)


def _run_queue_review(pr_context, adapter):
    from app.main import _queue_review

    mock_redis = MagicMock()
    mock_redis.set = AsyncMock(return_value=True)
    mock_redis.aclose = AsyncMock()

    with patch("app.main.aioredis.from_url", return_value=mock_redis), \
         patch("app.main.process_review.apply_async") as mock_apply:
        mock_apply.return_value.id = "task-123"
        response = asyncio.run(_queue_review(pr_context, "bitbucket", adapter))

    return response, mock_apply


def test_queue_review_fetches_changed_files_and_scales_limits():
    """39-file PR (wizard-ui #3565 case): limits scale with the real file count."""
    adapter = MagicMock()
    adapter.get_changed_files.return_value = [f"src/File{i}.tsx" for i in range(39)]
    ctx = _make_pr_context()

    response, mock_apply = _run_queue_review(ctx, adapter)

    adapter.get_changed_files.assert_called_once()
    kwargs = mock_apply.call_args.kwargs
    assert kwargs["soft_time_limit"] == _BASE_SOFT_TIME_LIMIT + 39 * _EXTRA_SECONDS_PER_FILE
    assert kwargs["time_limit"] == _BASE_TIME_LIMIT + 39 * _EXTRA_SECONDS_PER_FILE
    # Fetched files are passed to the task so the worker skips a second call
    assert len(mock_apply.call_args.kwargs["args"][0]["changed_files"]) == 39
    assert response.status_code == 202


def test_queue_review_falls_back_to_default_on_fetch_failure():
    """Diffstat fetch failure must not break the webhook — conservative default."""
    adapter = MagicMock()
    adapter.get_changed_files.side_effect = RuntimeError("bitbucket down")
    ctx = _make_pr_context()

    response, mock_apply = _run_queue_review(ctx, adapter)

    kwargs = mock_apply.call_args.kwargs
    assert kwargs["soft_time_limit"] == _BASE_SOFT_TIME_LIMIT + 10 * _EXTRA_SECONDS_PER_FILE
    assert response.status_code == 202


def test_queue_review_caps_file_count():
    """Extreme PRs stay under the global time-limit safety net."""
    adapter = MagicMock()
    adapter.get_changed_files.return_value = [f"f{i}" for i in range(500)]
    ctx = _make_pr_context()

    response, mock_apply = _run_queue_review(ctx, adapter)

    kwargs = mock_apply.call_args.kwargs
    assert kwargs["time_limit"] == _BASE_TIME_LIMIT + _MAX_FILE_COUNT_FOR_LIMITS * _EXTRA_SECONDS_PER_FILE
    assert response.status_code == 202


def test_queue_review_uses_existing_changed_files_without_fetch():
    """When the payload already lists files (e.g. GitHub), no extra API call."""
    adapter = MagicMock()
    ctx = _make_pr_context(changed_files=["a.java", "b.java"])

    response, mock_apply = _run_queue_review(ctx, adapter)

    adapter.get_changed_files.assert_not_called()
    kwargs = mock_apply.call_args.kwargs
    assert kwargs["soft_time_limit"] == _BASE_SOFT_TIME_LIMIT + 2 * _EXTRA_SECONDS_PER_FILE
    assert response.status_code == 202


def test_queue_review_dedup_skips_when_lock_held():
    """Second webhook for the same PR while queued is a no-op."""
    from app.main import _queue_review

    adapter = MagicMock()
    ctx = _make_pr_context()

    mock_redis = MagicMock()
    mock_redis.set = AsyncMock(return_value=False)  # lock already held
    mock_redis.aclose = AsyncMock()

    with patch("app.main.aioredis.from_url", return_value=mock_redis), \
         patch("app.main.process_review.apply_async") as mock_apply:
        response = asyncio.run(_queue_review(ctx, "bitbucket", adapter))

    mock_apply.assert_not_called()
    assert response.status_code == 200
