import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict

from celery import Celery

from app.adapters.factory import get_adapter
from app.analyzers.ai_reviewer import AIReviewer
from app.analyzers.merger import filter_by_config, merge_findings
from app.analyzers.semgrep_runner import SemgrepRunner
from app.config.project_config import detect_language, load_project_config
from app.config.settings import get_settings
from app.models import PRContext, Severity

logger = logging.getLogger(__name__)

settings = get_settings()

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
)


def _build_summary(findings, critical_count, bug_count, perf_count, suggest_count) -> str:
    lines = [
        "## AI Code Review Summary",
        "",
        f"**Total findings:** {len(findings)}  ",
        f"🔴 Critical: {critical_count}  |  🟠 Bugs: {bug_count}  |  🟡 Performance: {perf_count}  |  🔵 Suggestions: {suggest_count}",
        "",
        "| Severity | File | Line | Message |",
        "|----------|------|------|---------|",
    ]
    for f in findings:
        lines.append(f"| {f.severity.value} | `{f.file}` | {f.line} | {f.message} |")
    return "\n".join(lines)


@celery_app.task(bind=True, max_retries=3, autoretry_for=(Exception,), retry_backoff=True)
def process_review(self, task_payload: dict) -> dict:
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
    if pr_context.language == "auto":
        pr_context.language = detect_language(pr_context.changed_files)

    logger.info(
        "review_started repo=%s pr=%s language=%s diff_lines=%d changed_files=%s",
        pr_context.repo_full_name, pr_context.pr_id, pr_context.language,
        len(pr_context.diff.splitlines()), pr_context.changed_files,
    )

    diff_lines = len(pr_context.diff.splitlines())
    if diff_lines > config.max_diff_lines:
        adapter.post_summary_comment(
            pr_context,
            f"PR is too large for automated review ({diff_lines} lines, limit {config.max_diff_lines}). "
            "Please split the PR or increase `max_diff_lines` in `.codereview.yml`.",
        )
        return {"status": "skipped", "reason": "diff_too_large", "lines": diff_lines}

    adapter.set_review_status(pr_context, "pending", "Code review in progress...")

    semgrep_results = []
    ai_results = []

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {}
        if config.static_analysis:
            futures["semgrep"] = executor.submit(SemgrepRunner(config).run, pr_context, adapter)
        if config.ai_review:
            futures["ai"] = executor.submit(AIReviewer(settings).review, pr_context, config)

        for name, future in futures.items():
            try:
                result = future.result()
                if name == "semgrep":
                    semgrep_results = result
                else:
                    ai_results = result
            except Exception as exc:
                logger.error("Analyzer %s failed: %s", name, exc)

    findings = filter_by_config(merge_findings(semgrep_results, ai_results), config)

    # Clean up old bot comments
    for comment in adapter.get_existing_bot_comments(pr_context):
        adapter.delete_comment(pr_context, comment["id"])

    # Post inline comments
    for finding in findings:
        try:
            adapter.post_inline_comment(pr_context, finding)
        except Exception as exc:
            logger.warning("Failed to post inline comment: %s", exc)

    # Build and post summary
    critical_count = sum(1 for f in findings if f.severity == Severity.CRITICAL)
    bug_count = sum(1 for f in findings if f.severity == Severity.BUG)
    perf_count = sum(1 for f in findings if f.severity == Severity.PERFORMANCE)
    suggest_count = sum(1 for f in findings if f.severity == Severity.SUGGEST)

    if findings:
        summary = _build_summary(findings, critical_count, bug_count, perf_count, suggest_count)
        try:
            adapter.post_summary_comment(pr_context, summary)
        except Exception as exc:
            logger.warning("Failed to post summary comment: %s", exc)

    final_state = "failure" if any(f.severity.value in config.block_merge_on for f in findings) else "success"
    adapter.set_review_status(pr_context, final_state, f"{len(findings)} issues found")

    result = {
        "findings": len(findings),
        "critical": critical_count,
        "bugs": bug_count,
        "performance": perf_count,
        "suggestions": suggest_count,
        "status": final_state,
    }
    logger.info("Review complete: %s", result)
    return result
