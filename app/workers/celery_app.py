import json
import logging
import time
from dataclasses import asdict
from datetime import datetime, timezone
from uuid import uuid4

import redis

from celery import Celery

from app.adapters.factory import get_adapter, get_spm_adapter
from app.analyzers.ai_reviewer import AIReviewer
from app.analyzers.merger import filter_by_config, merge_findings
from app.analyzers.semgrep_runner import SemgrepRunner
from app.analyzers.spm_scanner import SPMScanner
from app.config.project_config import detect_language, load_project_config
from app.config.settings import get_settings
from app.metrics import Metrics
from app.models import PRContext, RepoPostureReport, SPMFinding, SPMScanCategory, Severity

logger = logging.getLogger(__name__)

settings = get_settings()
metrics = Metrics(settings.REDIS_URL)

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
    task_time_limit=300,       # kill task after 5 min — prevents hung workers
    task_soft_time_limit=240,  # give task 4 min to finish gracefully
)


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


@celery_app.task(bind=True, max_retries=2, autoretry_for=(ConnectionError, TimeoutError), retry_backoff=True)
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

    diff_lines = len(pr_context.diff.splitlines())
    truncated = diff_lines > config.max_diff_lines
    if truncated:
        pr_context.diff = "\n".join(pr_context.diff.splitlines()[:config.max_diff_lines])
        logger.info("%s Diff truncated to %d lines (original: %d)", pr_tag, config.max_diff_lines, diff_lines)

    adapter.set_review_status(pr_context, "pending", "Code review in progress...")

    semgrep_results = []
    ai_results = []

    if config.static_analysis:
        logger.info("%s Step 1/2: Static analysis started", pr_tag)
        try:
            semgrep_results = SemgrepRunner(config).run(pr_context, adapter)
            logger.info("%s Step 1/2: Static analysis complete — %d finding(s)", pr_tag, len(semgrep_results))
        except Exception as exc:
            logger.error("%s Step 1/2: Static analysis failed — %s", pr_tag, exc)

    if config.ai_review and not semgrep_results:
        logger.info("%s Step 2/2: AI review started", pr_tag)
        try:
            ai_results = AIReviewer(settings).review(pr_context, config)
            logger.info("%s Step 2/2: AI review complete — %d finding(s)", pr_tag, len(ai_results))
        except Exception as exc:
            logger.error("%s Step 2/2: AI review failed — %s", pr_tag, exc)
    elif semgrep_results:
        logger.info("%s Step 2/2: AI review skipped — static analysis found issues", pr_tag)
    else:
        logger.info("%s Step 2/2: AI review skipped — disabled in config", pr_tag)

    findings = filter_by_config(merge_findings(semgrep_results, ai_results), config)

    # Clean up old bot comments
    for comment in adapter.get_existing_bot_comments(pr_context):
        adapter.delete_comment(pr_context, comment["id"])

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

    if findings or truncated:
        summary = _build_summary(findings, critical_count, bug_count, perf_count, suggest_count)
        if truncated:
            summary += f"\n\n> ⚠️ PR has {diff_lines} lines. Analysis was performed on the first {config.max_diff_lines} lines."
        try:
            adapter.post_summary_comment(pr_context, summary)
            logger.info("%s Summary comment posted — %d inline comment(s)", pr_tag, inline_count)
        except Exception as exc:
            logger.warning("%s Failed to post summary comment: %s", pr_tag, exc)

    final_state = "failure" if any(f.severity.value in config.block_merge_on for f in findings) else "success"
    adapter.set_review_status(pr_context, final_state, f"{len(findings)} issues found")

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
    return {
        "findings": len(findings),
        "critical": critical_count,
        "bugs": bug_count,
        "performance": perf_count,
        "suggestions": suggest_count,
        "status": final_state,
    }


# ---------------------------------------------------------------------------
# AI-SPM tasks
# ---------------------------------------------------------------------------

def _spm_redis() -> redis.Redis:
    return redis.from_url(settings.REDIS_URL)


def _write_report(r: redis.Redis, report: RepoPostureReport, ttl: int) -> None:
    key = f"spm:report:{report.scan_id}"
    payload = {
        "scan_id": report.scan_id,
        "platform": report.platform,
        "repo_full_name": report.repo_full_name,
        "scanned_at": report.scanned_at,
        "status": report.status,
        "error": report.error,
        "files_scanned": report.files_scanned,
        "summary": report.summary,
        "findings": [
            {
                "category": f.category.value,
                "severity": f.severity.value,
                "file": f.file,
                "line": f.line,
                "message": f.message,
                "suggestion": f.suggestion,
                "source": f.source,
                "rule_id": f.rule_id,
            }
            for f in report.findings
        ],
    }
    r.set(key, json.dumps(payload), ex=ttl)


@celery_app.task(
    bind=True,
    max_retries=2,
    autoretry_for=(ConnectionError, TimeoutError),
    retry_backoff=True,
    task_time_limit=600,
    task_soft_time_limit=540,
)
def scan_repository(self, task_payload: dict) -> dict:
    scan_id = task_payload["scan_id"]
    scan_group_id = task_payload["scan_group_id"]
    platform = task_payload["platform"]
    access_key = task_payload["access_key"]
    repo_full_name = task_payload["repo_full_name"]
    ref = task_payload["ref"]
    categories = [SPMScanCategory(c) for c in task_payload["categories"]]
    max_files = task_payload.get("max_files", settings.SPM_MAX_FILES_PER_REPO)
    ttl = settings.SPM_RESULT_TTL_SECONDS

    r = _spm_redis()

    now = datetime.now(timezone.utc).isoformat()
    report = RepoPostureReport(
        scan_id=scan_id,
        platform=platform,
        repo_full_name=repo_full_name,
        scanned_at=now,
        status="running",
    )
    _write_report(r, report, ttl)

    try:
        adapter = get_spm_adapter(platform, access_key)
        scanner = SPMScanner(settings)
        findings = scanner.scan_repo(adapter, repo_full_name, ref, categories, max_files)

        report.status = "complete"
        report.findings = findings
        report.files_scanned = max_files  # upper bound; actual count tracked internally
        report.scanned_at = datetime.now(timezone.utc).isoformat()
        _write_report(r, report, ttl)
        r.hincrby(f"spm:group:{scan_group_id}:progress", "completed", 1)

        logger.info("[SPM %s] scan_id=%s complete — %d findings", repo_full_name, scan_id, len(findings))
        return {"scan_id": scan_id, "repo": repo_full_name, "findings": len(findings)}

    except Exception as exc:
        report.status = "error"
        report.error = str(exc)
        report.scanned_at = datetime.now(timezone.utc).isoformat()
        _write_report(r, report, ttl)
        r.hincrby(f"spm:group:{scan_group_id}:progress", "errors", 1)
        logger.error("[SPM %s] scan_id=%s failed: %s", repo_full_name, scan_id, exc)
        raise


@celery_app.task(bind=True, task_time_limit=120)
def run_spm_scan(self, task_payload: dict) -> dict:
    scan_group_id = task_payload["scan_group_id"]
    platform = task_payload["platform"]
    access_key = task_payload["access_key"]
    workspace_filter = task_payload.get("workspace")
    categories = task_payload.get("categories", [c.value for c in SPMScanCategory])
    max_files = task_payload.get("max_files_per_repo", settings.SPM_MAX_FILES_PER_REPO)
    ttl = settings.SPM_RESULT_TTL_SECONDS

    r = _spm_redis()
    submitted_at = datetime.now(timezone.utc).isoformat()

    r.hset(f"spm:group:{scan_group_id}", mapping={
        "platform": platform,
        "submitted_at": submitted_at,
        "workspace": workspace_filter or "",
        "repos_discovered": 0,
    })
    r.expire(f"spm:group:{scan_group_id}", ttl)

    adapter = get_spm_adapter(platform, access_key)

    workspaces = [workspace_filter] if workspace_filter else adapter.list_workspaces()

    repo_count = 0
    for ws in workspaces:
        try:
            repos = adapter.list_repositories(ws)
        except Exception as exc:
            logger.warning("[SPM] list_repositories(%s) failed: %s", ws, exc)
            continue

        for repo in repos:
            scan_id = uuid4().hex
            ref = repo.get("mainbranch", "main")

            r.sadd(f"spm:group:{scan_group_id}:report_ids", scan_id)
            r.expire(f"spm:group:{scan_group_id}:report_ids", ttl)

            scan_repository.delay({
                "scan_id": scan_id,
                "scan_group_id": scan_group_id,
                "platform": platform,
                "access_key": access_key,
                "repo_full_name": repo["full_name"],
                "ref": ref,
                "categories": categories,
                "max_files": max_files,
            })
            repo_count += 1

    r.hset(f"spm:group:{scan_group_id}", "repos_discovered", repo_count)
    r.hset(f"spm:group:{scan_group_id}:progress", mapping={
        "total": repo_count,
        "completed": 0,
        "errors": 0,
    })
    r.expire(f"spm:group:{scan_group_id}:progress", ttl)

    logger.info("[SPM] scan_group=%s dispatched %d repo tasks", scan_group_id, repo_count)
    return {"scan_group_id": scan_group_id, "repos_discovered": repo_count}
