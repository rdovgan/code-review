import json
import logging
import re
import time
from dataclasses import asdict

import httpx
import redis
from celery import Celery
from celery.exceptions import SoftTimeLimitExceeded
from celery.signals import task_failure, task_retry, task_revoked

from app.adapters.factory import get_adapter
from app.analyzers.agent_reviewer import AgentReviewer, AgentReviewFailed
from app.notifiers.alerts import send_alert
from app.analyzers.ai_reviewer import AIReviewer
from app.analyzers.merger import filter_by_config, merge_findings
from app.analyzers.semgrep_runner import SemgrepRunner
from app.config.project_config import detect_language, load_project_config
from app.config.settings import get_settings
from app.context.code_tools import CodeTools, RepoView
from app.context.snapshots import BranchSnapshot, SnapshotUnavailable, current_branch, shared_snapshot
from app.context.review_context import gather_review_context
from app.metrics import Metrics
from app.models import PRContext, Severity
from app.notifiers import mattermost
from app.utils.diff_parser import prepare_diff, split_diff_by_file

logger = logging.getLogger(__name__)

# httpx logs every request URL at INFO. Some URLs are secrets (the Mattermost webhook URL is
# its own credential), so request lines stay out of the logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

settings = get_settings()
metrics = Metrics(settings.REDIS_URL, ai_daily_token_budget=settings.AI_DAILY_TOKEN_BUDGET)


# Webhook dedup lock (set in main._queue_review). Collapses bursts of webhooks for the
# same PR; released when the review finishes so a follow-up push gets reviewed right away.
REVIEW_LOCK_TTL_SECONDS = 300


def review_lock_key(repo_full_name: str, pr_id) -> str:
    return f"review_lock:{repo_full_name}:{pr_id}"


def _release_review_lock(task_payload: dict) -> None:
    try:
        r = redis.from_url(settings.REDIS_URL)
        r.delete(review_lock_key(task_payload.get("repo_full_name", ""), task_payload.get("pr_id", "")))
    except Exception as exc:
        logger.warning("Failed to release review lock: %s", exc)


def _mark_review_aborted(task_payload: dict, description: str) -> None:
    """Best-effort: replace the "in progress" build status of a task that died."""
    try:
        payload = dict(task_payload)
        platform = payload.pop("platform")
        payload.pop("diff", None)
        pr_context = PRContext(platform=platform, diff="", **payload)
        workspace, repo_slug = pr_context.repo_full_name.split("/", 1)
        get_adapter(platform, workspace, repo_slug, settings).set_review_status(pr_context, "stopped", description)
    except Exception as exc:
        logger.warning("Failed to set build status of an aborted review: %s", exc)


def _task_payload_from(args) -> dict | None:
    return args[0] if args and isinstance(args[0], dict) else None


@task_failure.connect(sender="app.workers.celery_app.process_review")
def on_task_failure(sender, task_id, exception, args=None, **kwargs):
    logger.error("Task %s failed with unhandled exception: %s", task_id, exception)
    metrics.inc_webhook(status="error")
    task_payload = _task_payload_from(args)
    if task_payload:
        _mark_review_aborted(task_payload, f"Review failed: {type(exception).__name__}")
        _release_review_lock(task_payload)


@task_revoked.connect(sender="app.workers.celery_app.process_review")
def on_task_revoked(sender, request, terminated, signum, expired, **kwargs):
    reason = "timeout" if expired or signum else "revoked"
    logger.error("Task %s %s (signal=%s)", request.id, reason, signum)
    metrics.inc_webhook(status="error")
    task_payload = _task_payload_from(getattr(request, "args", None))
    if task_payload:
        _mark_review_aborted(task_payload, f"Review {reason}")
        _release_review_lock(task_payload)


@task_retry.connect(sender="app.workers.celery_app.process_review")
def on_task_retry(sender, request, reason, einfo, **kwargs):
    logger.warning("Task %s retrying: %s", request.id, reason)
    metrics.inc_task_retry()

# Base time limits — dynamically scaled per task in main._queue_review()
_BASE_SOFT_TIME_LIMIT = 300   # 5 min base
_BASE_TIME_LIMIT = 360        # 6 min base
_EXTRA_SECONDS_PER_FILE = 15  # add 15s per file — one AI chunk takes ~30–60s
# Cap on the file count used for limit scaling — keeps the scaled hard limit
# below the global safety net (360 + 60*15 = 1260s < 1740s)
_MAX_FILE_COUNT_FOR_LIMITS = 60
# review_mode: agentic gets this much extra time for the agent (incl. repo fetch); the
# regular budget above stays reserved for the diff-review fallback
_AGENTIC_EXTRA_SECONDS = 600
# Shadow reviews run as their own task after the posted review
_SHADOW_SOFT_TIME_LIMIT = 900
_SHADOW_TIME_LIMIT = 960
_SHADOW_RECORD_TTL_SECONDS = 30 * 86400

celery_app = Celery(
    "code_review",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
)
celery_app.conf.update(
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    # Global safety net — individual tasks override via apply_async
    task_time_limit=1800,
    task_soft_time_limit=1740,
)


def _pr_url(pr_context: PRContext) -> str:
    if pr_context.platform == "bitbucket":
        return f"https://bitbucket.org/{pr_context.repo_full_name}/pull-requests/{pr_context.pr_id}"
    if pr_context.platform == "github":
        return f"https://github.com/{pr_context.repo_full_name}/pull/{pr_context.pr_id}"
    if pr_context.platform == "gitlab":
        return f"https://gitlab.com/{pr_context.repo_full_name}/-/merge_requests/{pr_context.pr_id}"
    return ""


# Mattermost message-attachment colours (left border): green on pass, red on fail
_MM_COLOR_PASS = "#3EB489"
_MM_COLOR_FAIL = "#D24B4A"
_MM_MAX_COMMITS = 3
_MM_MAX_COMMIT_LEN = 72


def _notify_mattermost(adapter, pr_context: PRContext, config, final_state: str, critical_count: int,
                       bug_count: int, summary_comment_id: str = "", commits: list[str] | None = None) -> None:
    if not settings.MATTERMOST_WEBHOOK_URL or not config.notify_authors:
        return
    if not pr_context.is_new_pr:
        return
    if pr_context.author not in config.notify_authors:
        return
    status_emoji = {"failure": "❌", "stopped": "⚠️"}.get(final_state, "✅")
    repo_name = pr_context.repo_full_name.split("/")[-1]
    pr_url = _pr_url(pr_context)
    if pr_context.source_branch:
        branch_line = f"`{pr_context.source_branch}` → `{pr_context.target_branch}`"
    else:
        branch_line = f"`{pr_context.target_branch}`"
    lines = [
        f"{status_emoji} **[{repo_name} / {pr_context.title}]({pr_url})**",
        f"{branch_line} · {pr_context.author}",
    ]
    if commits is None:
        commits = _fetch_commits(adapter, pr_context)
    if commits:
        lines.append("")
        for c in commits[:_MM_MAX_COMMITS]:
            c = c.strip()
            if len(c) > _MM_MAX_COMMIT_LEN:
                c = c[:_MM_MAX_COMMIT_LEN - 1].rstrip() + "…"
            lines.append(f"- {c}")
        if len(commits) > _MM_MAX_COMMITS:
            lines.append(f"- …and {len(commits) - _MM_MAX_COMMITS} more")

    lines.append("")
    lines.append("---")
    stats = []
    if critical_count > 0:
        stats.append(f"🔴 Critical: **{critical_count}**")
    if bug_count > 0:
        stats.append(f"🟠 Bugs: **{bug_count}**")
    lines.append("   |   ".join(stats) if stats else "✅ No critical issues or bugs found")
    if summary_comment_id and pr_url:
        lines.append(f"[View review comment →]({pr_url}#comment-{summary_comment_id})")

    attachment = {
        "color": _MM_COLOR_PASS if final_state == "success" else _MM_COLOR_FAIL,
        "fallback": f"{status_emoji} {repo_name} / {pr_context.title}",
        "text": "\n".join(lines),
    }
    mattermost.send_message(settings.MATTERMOST_WEBHOOK_URL, attachments=[attachment])


def _build_summary(findings, critical_count, bug_count, perf_count, suggest_count) -> str:
    lines = [
        "## AI Code Review Summary",
        "",
        f"**Total findings:** {len(findings)}  ",
        f"🔴 Critical: {critical_count}  |  🟠 Bugs: {bug_count}  |  🟡 Performance: {perf_count}  |  🔵 Suggestions: {suggest_count}",
    ]
    if findings:
        lines += [
            "",
            "| Severity | File | Line | Message |",
            "|----------|------|------|---------|",
        ]
        for f in findings:
            lines.append(f"| {f.severity.value} | `{f.file}` | {f.line} | {f.message} |")
    return "\n".join(lines)


_AI_STOP_REASONS = {
    "budget": "daily AI token budget exhausted",
    "deadline": "task time limit reached",
    "chunk_cap": "diff split into too many chunks",
    "parse_failures": "AI responses could not be parsed",
    "errors": "AI requests failed",
}
_MAX_LISTED_FILES = 10


def _file_list(paths: list[str]) -> str:
    listed = ", ".join(f"`{p}`" for p in paths[:_MAX_LISTED_FILES])
    if len(paths) > _MAX_LISTED_FILES:
        listed += f" and {len(paths) - _MAX_LISTED_FILES} more"
    return listed


def _fetch_commits(adapter, pr_context: PRContext) -> list[str]:
    """Best-effort: a fetch failure must not block the review or the notification."""
    try:
        return adapter.get_pr_commits(pr_context)
    except Exception as exc:
        logger.warning("[PR #%s %s] Failed to fetch commits: %s", pr_context.pr_id, pr_context.repo_full_name, exc)
        return []


def _fetch_file_contents(adapter, pr_context: PRContext, paths: list[str]) -> dict[str, str | None]:
    """New-version content of each file (None if missing), shared by Semgrep and the AI context."""
    contents: dict[str, str | None] = {}
    for i, path in enumerate(paths):
        if i > 0:
            time.sleep(0.3)  # avoid Bitbucket rate limiting on bulk file fetches
        try:
            contents[path] = adapter.get_file_content(pr_context, path, pr_context.head_sha)
        except Exception as exc:
            logger.warning("Failed to fetch %s for review context: %s", path, exc)
            contents[path] = None
    return contents


# httpx errors are not builtin ConnectionError/TimeoutError subclasses — list them explicitly
@celery_app.task(bind=True, max_retries=2, retry_backoff=True,
                 autoretry_for=(ConnectionError, TimeoutError, httpx.TransportError))
def process_review(self, task_payload: dict) -> dict:
    try:
        return _run_review(self, task_payload)
    finally:
        _release_review_lock(task_payload)


def _run_review(self, task_payload: dict) -> dict:
    # From the task's start: time spent fetching, Semgrep and the agent all count against it
    deadline = _task_deadline(self)
    payload = dict(task_payload)  # copy — pop() would corrupt dict on retry
    platform = payload.pop("platform")
    diff = payload.pop("diff", "")
    pr_context = PRContext(platform=platform, diff=diff, **payload)

    workspace, repo_slug = pr_context.repo_full_name.split("/", 1)
    adapter = get_adapter(platform, workspace, repo_slug, settings)

    if not pr_context.diff:
        pr_context.diff = adapter.get_diff(pr_context)

    if not pr_context.changed_files:
        pr_context.changed_files = adapter.get_changed_files(pr_context)

    config = load_project_config(adapter, pr_context)

    if config.target_branches and pr_context.target_branch not in config.target_branches:
        logger.info("[PR #%s %s] Skipped — branch '%s' not in target list", pr_context.pr_id, pr_context.repo_full_name, pr_context.target_branch)
        metrics.record_review(status="skipped", duration_ms=0, critical=0, bugs=0, perf=0, suggestions=0, semgrep_count=0, ai_count=0,
                              language=pr_context.language, project=pr_context.repo_full_name, author=pr_context.author)
        return {"status": "skipped", "reason": "branch_not_targeted", "branch": pr_context.target_branch}

    if pr_context.language == "auto":
        pr_context.language = config.language if config.language != "auto" else detect_language(pr_context.changed_files)

    pr_tag = f"[PR #{pr_context.pr_id} {pr_context.repo_full_name}]"
    review_start = time.monotonic()
    logger.info("%s Review started — %d files, language: %s", pr_tag, len(pr_context.changed_files), pr_context.language)

    # Ignored/deleted/binary files are dropped before the line limit is applied, and the
    # limit cuts at file/hunk boundaries — see prepare_diff()
    raw_diff = pr_context.diff  # the agent's view of the repo also needs the files this drops (deleted ones)
    prepared = prepare_diff(pr_context.diff, config.ignore_paths, config.max_diff_lines)
    pr_context.diff = prepared.diff
    if prepared.ignored_files:
        logger.info("%s Excluded %d ignored/deleted/binary file(s) from review", pr_tag, len(prepared.ignored_files))
    if prepared.truncated:
        logger.info(
            "%s Diff truncated to %d/%d lines — %d file(s) skipped, %d partial",
            pr_tag, prepared.reviewed_lines, prepared.original_lines,
            len(prepared.skipped_files), len(prepared.partial_files),
        )

    if not pr_context.diff.strip():
        logger.info("%s Nothing to review — every changed file is ignored, deleted or binary", pr_tag)
        adapter.set_review_status(pr_context, "success", "No reviewable changes")
        metrics.record_review(status="skipped", duration_ms=0, critical=0, bugs=0, perf=0, suggestions=0, semgrep_count=0, ai_count=0,
                              language=pr_context.language, project=pr_context.repo_full_name, author=pr_context.author)
        return {"status": "skipped", "reason": "no_reviewable_changes"}

    adapter.set_review_status(pr_context, "pending", "Code review in progress...")

    # Fetched once: Semgrep scans these files, the AI gets them as context around the diff
    file_contents: dict[str, str | None] = {}
    if config.static_analysis or (config.ai_review and settings.AI_FILE_CONTEXT_MAX_CHARS > 0):
        review_files = [fd.path for fd in split_diff_by_file(pr_context.diff)]
        file_contents = _fetch_file_contents(adapter, pr_context, review_files)

    # What the change is meant to do (PR description, commits, Jira ticket) and the repo's
    # own guide — fetched once, sent with every AI request
    commits = _fetch_commits(adapter, pr_context)
    review_context = ""
    if config.ai_review:
        try:
            review_context = gather_review_context(adapter, pr_context, config, settings, commits)
        except Exception as exc:
            logger.warning("%s Failed to gather PR context, reviewing without it: %s", pr_tag, exc)

    semgrep_results = []
    ai_results = []
    ai_reviewer = None
    ai_incomplete_reason = None  # set when the AI review did not cover the whole diff
    ai_reviewed_nothing = False  # ...and when it covered none of it
    review_mode_note = None      # agentic review: how it went, shown in the summary

    try:
        if config.static_analysis:
            logger.info("%s Step 1/2: Static analysis started", pr_tag)
            try:
                semgrep_results = SemgrepRunner(config).run(pr_context, adapter, file_contents=file_contents)
                logger.info("%s Step 1/2: Static analysis complete — %d finding(s)", pr_tag, len(semgrep_results))
            except Exception as exc:
                logger.error("%s Step 1/2: Static analysis failed — %s", pr_tag, exc)

            if semgrep_results and config.semgrep_ai_verify:
                logger.info("%s Step 1/2: Verifying %d semgrep finding(s) via AI", pr_tag, len(semgrep_results))
                try:
                    ai_reviewer = ai_reviewer or AIReviewer(settings)
                    verified = ai_reviewer.verify_semgrep_findings(semgrep_results, pr_context)
                    logger.info(
                        "%s Step 1/2: Semgrep AI verification kept %d/%d finding(s)",
                        pr_tag, len(verified), len(semgrep_results),
                    )
                    metrics.record_semgrep_verification(kept=len(verified), total=len(semgrep_results))
                    semgrep_results = verified
                except Exception as exc:
                    logger.error("%s Step 1/2: Semgrep AI verification failed, keeping unverified results — %s", pr_tag, exc)

        if config.ai_review:
            # deadline = the task's soft time limit. The reviewer stops before it and
            # returns partial findings, so the build status always gets finalized
            # instead of the worker being SIGKILLed mid-review.
            agentic_done = False
            if config.review_mode == "agentic":
                logger.info("%s Step 2/2: Agentic review started", pr_tag)
                try:
                    ai_results = _agentic_review(adapter, pr_context, config, review_context,
                                                 _agent_deadline(deadline, _soft_time_limit(self)), file_contents,
                                                 raw_diff)
                    agentic_done = True
                    metrics.record_agentic("ok")
                    review_mode_note = (f"> 🔎 Deep review: the AI checked the change against the repository "
                                        f"({getattr(ai_results, 'tool_calls', 0)} code lookups).")
                    logger.info("%s Step 2/2: Agentic review complete — %d finding(s), %d code lookup(s)",
                                pr_tag, len(ai_results), getattr(ai_results, "tool_calls", 0))
                    _alert_agent_limits(pr_context, config, ai_results)
                except SoftTimeLimitExceeded:
                    raise
                except Exception as exc:
                    reason = exc.reason if isinstance(exc, AgentReviewFailed) else f"error: {type(exc).__name__}"
                    logger.warning("%s Step 2/2: Agentic review failed (%s) — falling back to diff review", pr_tag, reason)
                    metrics.record_agentic("fallback")
                    review_mode_note = f"> ℹ️ Deep review unavailable ({reason}); this is the standard diff review."
                    _alert(pr_context, f"agent_failed:{re.sub(r'[0-9]+', 'N', reason)}",
                           "deep review fell back to the diff review",
                           f"Reason: {reason}. The PR was reviewed the standard way.")
                    ai_results = []

            if not agentic_done:
                logger.info("%s Step 2/2: AI review started", pr_tag)
                ai_results, ai_incomplete_reason, ai_reviewed_nothing = _diff_ai_review(
                    ai_reviewer, pr_context, config, deadline, file_contents, review_context, pr_tag)
        else:
            logger.info("%s Step 2/2: AI review skipped — disabled in config", pr_tag)

    except SoftTimeLimitExceeded:
        duration_ms = round((time.monotonic() - review_start) * 1000)
        logger.error("%s Soft time limit exceeded after %dms — recording failure", pr_tag, duration_ms)
        metrics.record_review(
            status="failure",
            duration_ms=duration_ms,
            critical=0, bugs=0, perf=0, suggestions=0,
            semgrep_count=len(semgrep_results),
            ai_count=len(ai_results),
            language=pr_context.language,
            project=pr_context.repo_full_name,
            author=pr_context.author,
        )
        try:
            adapter.set_review_status(pr_context, "failure", "Review timed out")
        except Exception:
            pass
        raise

    findings = filter_by_config(merge_findings(semgrep_results, ai_results), config)

    # Clean up old bot comments (best-effort — don't crash if permissions are missing)
    try:
        for comment in adapter.get_existing_bot_comments(pr_context):
            adapter.delete_comment(pr_context, comment["id"])
    except Exception as exc:
        logger.warning("%s Failed to clean up old bot comments: %s", pr_tag, exc)

    # Post inline comments — SUGGEST is summary-only (line attribution is often imprecise)
    inline_count = 0
    for finding in findings:
        if finding.severity == Severity.SUGGEST:
            continue
        try:
            adapter.post_inline_comment(pr_context, finding)
            inline_count += 1
        except Exception as exc:
            logger.warning("%s Failed to post inline comment: %s", pr_tag, exc)

    # Build and post summary
    critical_count = sum(1 for f in findings if f.severity == Severity.CRITICAL)
    bug_count = sum(1 for f in findings if f.severity == Severity.BUG)
    perf_count = sum(1 for f in findings if f.severity == Severity.PERFORMANCE)
    suggest_count = sum(1 for f in findings if f.severity == Severity.SUGGEST)

    notes = []
    if prepared.truncated:
        note = (f"> ⚠️ PR diff has {prepared.original_lines} lines; {prepared.reviewed_lines} were analyzed "
                f"(limit {config.max_diff_lines}, ignored files excluded).")
        if prepared.skipped_files:
            note += f"  \n> Not reviewed: {_file_list(prepared.skipped_files)}."
        if prepared.partial_files:
            note += f"  \n> Partially reviewed: {_file_list(prepared.partial_files)}."
        notes.append(note)
    if ai_incomplete_reason:
        scope = "did not run" if ai_reviewed_nothing else "covered only part of the diff"
        notes.append(f"> ⚠️ AI review {scope}: {ai_incomplete_reason}.")
    if review_mode_note:
        notes.append(review_mode_note)

    summary_comment_id = ""
    if findings or notes:
        summary = _build_summary(findings, critical_count, bug_count, perf_count, suggest_count)
        if notes:
            summary += "\n\n" + "\n\n".join(notes)
        try:
            summary_comment_id = adapter.post_summary_comment(pr_context, summary) or ""
            logger.info("%s Summary comment posted — %d inline comment(s)", pr_tag, inline_count)
        except Exception as exc:
            logger.warning("%s Failed to post summary comment: %s", pr_tag, exc)

    if any(f.severity.value in config.block_merge_on for f in findings):
        final_state, status_description = "failure", f"{len(findings)} issues found"
    elif ai_reviewed_nothing:
        # The AI review is the main check — a green status without it would be misleading
        final_state, status_description = "stopped", f"Not reviewed: {ai_incomplete_reason}"
    else:
        final_state, status_description = "success", f"{len(findings)} issues found"
    adapter.set_review_status(pr_context, final_state, status_description)

    try:
        _notify_mattermost(adapter, pr_context, config, final_state, critical_count, bug_count, summary_comment_id,
                           commits=commits)
    except Exception as exc:
        logger.warning("%s Failed to send Mattermost notification: %s", pr_tag, exc)

    duration_ms = round((time.monotonic() - review_start) * 1000)
    metrics.record_review(
        status=final_state,
        duration_ms=duration_ms,
        critical=critical_count,
        bugs=bug_count,
        perf=perf_count,
        suggestions=suggest_count,
        semgrep_count=len(semgrep_results),
        ai_count=len(ai_results),
        language=pr_context.language,
        project=pr_context.repo_full_name,
        author=pr_context.author,
    )

    logger.info(
        "%s Done — total: %d (critical: %d, bugs: %d, perf: %d, suggestions: %d) status: %s",
        pr_tag, len(findings), critical_count, bug_count, perf_count, suggest_count, final_state,
    )

    if config.review_mode == "shadow" and config.ai_review:
        _enqueue_shadow_review(task_payload, findings, pr_tag)
    return {
        "findings": len(findings),
        "critical": critical_count,
        "bugs": bug_count,
        "performance": perf_count,
        "suggestions": suggest_count,
        "status": final_state,
    }


def _soft_time_limit(task) -> float | None:
    """Celery delivers limits as request.timelimit = (hard, soft)."""
    timelimit = getattr(task.request, "timelimit", None)
    if timelimit and len(timelimit) == 2 and timelimit[1]:
        return float(timelimit[1])
    return None


def _task_deadline(task) -> float | None:
    """time.monotonic() value of the task's soft time limit; call it when the task starts."""
    soft = _soft_time_limit(task)
    return time.monotonic() + soft if soft else None


def _agent_deadline(task_deadline: float | None, soft_limit: float | None) -> float:
    """The agent gets _AGENTIC_EXTRA_SECONDS. The rest of the task's time — what a diff-mode
    review of this PR gets — stays reserved for the fallback."""
    agent_deadline = time.monotonic() + _AGENTIC_EXTRA_SECONDS
    if task_deadline is not None and soft_limit:
        fallback_reserve = max(soft_limit - _AGENTIC_EXTRA_SECONDS, _BASE_SOFT_TIME_LIMIT)
        agent_deadline = min(agent_deadline, task_deadline - fallback_reserve)
    return agent_deadline


def _diff_ai_review(ai_reviewer, pr_context: PRContext, config, deadline, file_contents, review_context, pr_tag):
    """The standard AI review. Returns (results, incomplete_reason, reviewed_nothing)."""
    try:
        ai_reviewer = ai_reviewer or AIReviewer(settings)
        ai_results = ai_reviewer.review(pr_context, config, deadline=deadline, file_contents=file_contents,
                                        context=review_context)
    except SoftTimeLimitExceeded:
        raise
    except Exception as exc:
        logger.error("%s Step 2/2: AI review failed — %s", pr_tag, exc)
        return [], f"AI review failed: {type(exc).__name__}", True
    incomplete_reason, reviewed_nothing = None, False
    stop_reason = getattr(ai_results, "stop_reason", None)
    if stop_reason:
        incomplete_reason = _AI_STOP_REASONS.get(stop_reason, stop_reason)
        reviewed_nothing = getattr(ai_results, "chunks_reviewed", 0) == 0
        logger.warning("%s Step 2/2: AI review incomplete (%s) — %d/%d chunk(s) reviewed",
                       pr_tag, incomplete_reason, ai_results.chunks_reviewed, ai_results.chunks_total)
        if stop_reason != "budget":  # the budget has its own alert
            _alert(pr_context, f"ai_incomplete:{stop_reason}", "AI review incomplete",
                   f"{incomplete_reason}: {ai_results.chunks_reviewed} of {ai_results.chunks_total} part(s) "
                   "of the diff reviewed.")
    logger.info("%s Step 2/2: AI review complete — %d finding(s), %d dropped by verification",
                pr_tag, len(ai_results), getattr(ai_results, "dropped_by_verification", 0))
    return ai_results, incomplete_reason, reviewed_nothing


_LIMIT_ADVICE = {
    "steps": ("used all {steps} tool calls (`agent_max_steps`)",
              "Raise `agent_max_steps` for this repository in config/projects.yml if it keeps happening."),
    "tokens": ("used its token allowance for one review (`AGENT_MAX_TOKENS_PER_REVIEW`)",
               "Raise `AGENT_MAX_TOKENS_PER_REVIEW` in .env, or lower `agent_max_steps`."),
    "time": ("ran out of time",
             "Big PRs in this repository may need fewer `agent_max_steps` or `review_mode: diff`."),
}


def _alert(pr_context: PRContext, kind: str, title: str, details: str) -> None:
    """One alert per repository and kind per ALERT_REPEAT_HOURS, with a link to the PR that hit it."""
    repo = pr_context.repo_full_name
    pr_url = _pr_url(pr_context)
    pr_ref = f"[PR #{pr_context.pr_id}]({pr_url})" if pr_url else f"PR #{pr_context.pr_id}"
    send_alert(settings, f"{kind}:{repo}", f"{title} — {repo.split('/')[-1]}",
               f"{pr_ref} {pr_context.title}\n{details}")


def _alert_agent_limits(pr_context: PRContext, config, ai_results) -> None:
    for limit in sorted(getattr(ai_results, "limits_hit", ()) or ()):
        what, advice = _LIMIT_ADVICE[limit]
        _alert(pr_context, f"agent_limit:{limit}", "deep review stopped early",
               f"The agent {what.format(steps=config.agent_max_steps)} before it finished checking the change. "
               f"Its findings so far were posted. {advice}")


def _remaining(deadline: float) -> float:
    return max(30.0, deadline - time.monotonic())


def _base_snapshot(adapter, pr_context: PRContext, deadline: float):
    """Snapshot of the PR's target branch: the shared one, or the bot's own for other branches."""
    shared = shared_snapshot(settings.PROJECTS_SNAPSHOT_DIR, pr_context.repo_full_name)
    if shared is None:
        raise AgentReviewFailed(f"repository is not in {settings.PROJECTS_SNAPSHOT_DIR or 'the projects folder'}")
    branch = current_branch(shared)
    if branch == pr_context.target_branch:
        return shared, f"{branch} (shared snapshot, refreshed nightly)"
    try:
        spec = adapter.git_clone_spec(pr_context.repo_full_name)
    except NotImplementedError:
        raise AgentReviewFailed(f"no snapshot of {pr_context.target_branch} and {pr_context.platform} cannot be cloned")
    snapshot = BranchSnapshot(settings.BRANCH_SNAPSHOT_DIR, pr_context.repo_full_name, pr_context.target_branch,
                              spec, max_age_seconds=settings.BRANCH_SNAPSHOT_MAX_AGE_HOURS * 3600,
                              timeout=settings.BRANCH_SNAPSHOT_CLONE_TIMEOUT)
    try:
        return snapshot.ensure(timeout=_remaining(deadline)), f"{pr_context.target_branch} (bot snapshot)"
    except SnapshotUnavailable as exc:
        raise AgentReviewFailed(str(exc))


def _pr_overlay(pr_context: PRContext, raw_diff: str, file_contents: dict) -> tuple[dict, set]:
    """Files the PR changes, at its head: (prefetched path → content or None if removed, paths to fetch lazily).

    raw_diff is the diff before prepare_diff(), which drops deleted files.
    """
    overlay: dict[str, str | None] = {}
    for fd in split_diff_by_file(raw_diff or pr_context.diff):
        header = fd.text.split("\n", 1)[0].split()
        old_path = header[2][2:] if len(header) >= 3 else ""
        if fd.deleted:
            overlay[fd.path] = None
            continue
        if old_path and old_path != fd.path:  # rename: the old path is gone
            overlay[old_path] = None
        content = file_contents.get(fd.path)
        if content is not None:
            overlay[fd.path] = content
    lazy = {p for p in pr_context.changed_files if p not in overlay}
    return overlay, lazy


def _build_code_tools(adapter, pr_context: PRContext, config, deadline: float, file_contents: dict,
                      raw_diff: str) -> CodeTools:
    """The PR's repo (target-branch snapshot + the PR's files on top), plus context repos."""
    base, base_label = _base_snapshot(adapter, pr_context, deadline)
    overlay, lazy = _pr_overlay(pr_context, raw_diff, file_contents)

    def fetch(path: str) -> str | None:
        try:
            return adapter.get_file_content(pr_context, path, pr_context.head_sha)
        except Exception as exc:
            logger.warning("Failed to fetch %s for the agent: %s", path, exc)
            return None

    views = [RepoView(pr_context.repo_full_name, base,
                      f"the PR's repository — files changed in this PR at the PR's head commit, "
                      f"all other files at {base_label}",
                      overlay=overlay, lazy_paths=lazy, fetch=fetch)]
    for name in config.context_repos or []:
        if name == pr_context.repo_full_name:
            continue
        path = shared_snapshot(settings.PROJECTS_SNAPSHOT_DIR, str(name))
        if path is None:
            logger.warning("[PR #%s %s] Context repo %s skipped: not in %s", pr_context.pr_id,
                           pr_context.repo_full_name, name, settings.PROJECTS_SNAPSHOT_DIR)
            continue
        views.append(RepoView(str(name), path,
                              f"related repository, at {current_branch(path) or 'its main branch'} "
                              f"(shared snapshot, refreshed nightly)"))
    return CodeTools(views, max_result_chars=settings.AGENT_TOOL_RESULT_MAX_CHARS)


def _agentic_review(adapter, pr_context: PRContext, config, review_context: str, deadline: float,
                    file_contents: dict | None = None, raw_diff: str = ""):
    """Raises on any failure — the caller falls back to the diff review."""
    tools = _build_code_tools(adapter, pr_context, config, deadline, file_contents or {}, raw_diff)
    return AgentReviewer(settings).review_agentic(pr_context, config, tools, context=review_context, deadline=deadline)


def _finding_dicts(findings) -> list[dict]:
    return [{"severity": f.severity.value, "file": f.file, "line": f.line, "message": f.message,
             "source": f.source} for f in findings]


def _enqueue_shadow_review(task_payload: dict, posted_findings, pr_tag: str) -> None:
    try:
        process_shadow_review.apply_async(
            args=[task_payload, _finding_dicts(posted_findings)],
            soft_time_limit=_SHADOW_SOFT_TIME_LIMIT,
            time_limit=_SHADOW_TIME_LIMIT,
        )
        logger.info("%s Shadow agentic review queued", pr_tag)
    except Exception as exc:
        logger.warning("%s Failed to queue shadow review: %s", pr_tag, exc)


def shadow_record_key(repo_full_name: str, pr_id, head_sha: str) -> str:
    return f"shadow_review:{repo_full_name}:{pr_id}:{head_sha[:12]}"


@celery_app.task(bind=True)
def process_shadow_review(self, task_payload: dict, posted: list[dict]) -> dict:
    """Agentic review that is only recorded, never posted — compares it with what was posted.

    Not wired to the failure signals of process_review: whatever happens here, the PR's
    build status and comments stay as the posted review left them.
    """
    started = time.monotonic()
    record = {"posted": posted, "agent": [], "tool_calls": 0, "error": None}
    payload = dict(task_payload)
    platform = payload.pop("platform")
    payload.pop("diff", None)
    pr_context = PRContext(platform=platform, diff="", **payload)
    pr_tag = f"[PR #{pr_context.pr_id} {pr_context.repo_full_name}] [shadow]"
    try:
        deadline = (_task_deadline(self) or time.monotonic() + _SHADOW_SOFT_TIME_LIMIT) - 30
        workspace, repo_slug = pr_context.repo_full_name.split("/", 1)
        adapter = get_adapter(platform, workspace, repo_slug, settings)
        pr_context.diff = adapter.get_diff(pr_context)
        if not pr_context.changed_files:
            pr_context.changed_files = adapter.get_changed_files(pr_context)
        config = load_project_config(adapter, pr_context)
        if pr_context.language == "auto":
            pr_context.language = config.language if config.language != "auto" else detect_language(pr_context.changed_files)
        raw_diff = pr_context.diff
        pr_context.diff = prepare_diff(pr_context.diff, config.ignore_paths, config.max_diff_lines).diff
        review_context = gather_review_context(adapter, pr_context, config, settings,
                                               _fetch_commits(adapter, pr_context))
        file_contents = _fetch_file_contents(adapter, pr_context, [fd.path for fd in split_diff_by_file(pr_context.diff)])
        result = _agentic_review(adapter, pr_context, config, review_context, deadline, file_contents, raw_diff)
        agent_findings = filter_by_config(merge_findings([], list(result)), config)
        record.update(agent=_finding_dicts(agent_findings), tool_calls=getattr(result, "tool_calls", 0))
        metrics.record_agentic("shadow_ok")
    except Exception as exc:  # SoftTimeLimitExceeded included: a shadow run never escalates
        record["error"] = exc.reason if isinstance(exc, AgentReviewFailed) else f"{type(exc).__name__}: {exc}"
        metrics.record_agentic("shadow_failed")
        logger.warning("%s Shadow review failed: %s", pr_tag, record["error"])

    posted_keys = {(f["file"], f["line"]) for f in posted}
    agent_keys = {(f["file"], f["line"]) for f in record["agent"]}
    record.update(
        both=len(posted_keys & agent_keys),
        only_posted=len(posted_keys - agent_keys),
        only_agent=len(agent_keys - posted_keys),
        duration_ms=round((time.monotonic() - started) * 1000),
        head_sha=pr_context.head_sha,
        recorded_at=int(time.time()),
    )
    logger.info("%s Shadow review: posted=%d agent=%d both=%d only_agent=%d only_posted=%d lookups=%d",
                pr_tag, len(posted), len(record["agent"]), record["both"], record["only_agent"],
                record["only_posted"], record["tool_calls"])
    try:
        redis.from_url(settings.REDIS_URL).set(
            shadow_record_key(pr_context.repo_full_name, pr_context.pr_id, pr_context.head_sha),
            json.dumps(record), ex=_SHADOW_RECORD_TTL_SECONDS)
    except Exception as exc:
        logger.warning("%s Failed to store shadow record: %s", pr_tag, exc)
    return {k: record[k] for k in ("both", "only_posted", "only_agent", "tool_calls", "error")}
