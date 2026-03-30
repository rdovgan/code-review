import json
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Optional

import redis.asyncio as aioredis
import structlog
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel

from app.adapters.factory import get_adapter
from app.config.settings import get_settings
from app.metrics import Metrics
from app.models import SPMScanCategory
from app.workers.celery_app import process_review, run_spm_scan

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.stdlib.add_log_level,
        structlog.processors.JSONRenderer(),
    ]
)
logger = structlog.get_logger()

settings = get_settings()
metrics = Metrics(settings.REDIS_URL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        r = aioredis.from_url(settings.REDIS_URL)
        await r.ping()
        await r.aclose()
        logger.info("redis_connected", url=settings.REDIS_URL)
    except Exception as exc:
        logger.warning("redis_unavailable", error=str(exc))
    yield


app = FastAPI(title="Code Review Bot", lifespan=lifespan)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
    start = time.time()
    response: Response = await call_next(request)
    duration_ms = round((time.time() - start) * 1000)
    logger.info(
        "http_request",
        method=request.method,
        path=request.url.path,
        status=response.status_code,
        duration_ms=duration_ms,
        request_id=request_id,
    )
    response.headers["x-request-id"] = request_id
    return response


@app.get("/metrics", response_class=PlainTextResponse)
async def prometheus_metrics():
    return PlainTextResponse(metrics.prometheus_text(), media_type="text/plain; version=0.0.4")


@app.get("/health")
async def health():
    redis_status = "error"
    try:
        r = aioredis.from_url(settings.REDIS_URL)
        await r.ping()
        await r.aclose()
        redis_status = "ok"
    except Exception:
        pass
    return {"status": "ok", "redis": redis_status}


def _extract_repo_info(payload: dict) -> tuple[str, str]:
    """Return (workspace, repo_slug) from a Bitbucket webhook payload."""
    full_name = (
        payload.get("pullrequest", {})
        .get("destination", {})
        .get("repository", {})
        .get("full_name", "")
    )
    parts = full_name.split("/", 1)
    return (parts[0], parts[1]) if len(parts) == 2 else ("", "")


@app.post("/webhook/bitbucket")
async def webhook_bitbucket(request: Request):
    body = await request.body()

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON"})

    workspace, repo_slug = _extract_repo_info(payload)
    if not workspace or not repo_slug:
        return JSONResponse(status_code=200, content={"status": "ignored"})

    try:
        adapter = get_adapter("bitbucket", workspace, repo_slug, settings)
    except ValueError as exc:
        logger.warning("no_credentials", workspace=workspace, repo=repo_slug, error=str(exc))
        return JSONResponse(status_code=400, content={"error": str(exc)})

    if not adapter.validate_webhook(body, dict(request.headers)):
        return JSONResponse(status_code=401, content={"error": "Invalid signature"})

    event_key = request.headers.get("x-event-key", "")
    if event_key and event_key not in ("pullrequest:created", "pullrequest:updated", "pullrequest:comment_created"):
        metrics.inc_webhook("ignored")
        return JSONResponse(status_code=200, content={"status": "ignored", "reason": "event_not_reviewable"})

    pr_context = adapter.parse_webhook(payload)
    if pr_context is None:
        metrics.inc_webhook("ignored")
        return JSONResponse(status_code=200, content={"status": "ignored"})

    # Deduplicate: skip if this PR is already queued or running (60s window)
    dedup_key = f"review_lock:{pr_context.repo_full_name}:{pr_context.pr_id}"
    try:
        r = aioredis.from_url(settings.REDIS_URL)
        acquired = await r.set(dedup_key, "1", nx=True, ex=300)  # 5 min = task time limit
        await r.aclose()
    except Exception as exc:
        logger.warning("redis_lock_failed", error=str(exc))
        metrics.inc_webhook("error")
        return JSONResponse(status_code=503, content={"error": "redis_unavailable"})
    if not acquired:
        metrics.inc_webhook("already_queued")
        return JSONResponse(status_code=200, content={"status": "already_queued"})

    task_payload = {"platform": "bitbucket", "diff": "", **asdict(pr_context)}
    task = process_review.delay(task_payload)
    metrics.inc_webhook("queued")
    return JSONResponse(status_code=202, content={"status": "queued", "task_id": task.id})


# ---------------------------------------------------------------------------
# AI-SPM endpoints
# ---------------------------------------------------------------------------

class SPMScanRequest(BaseModel):
    platform: str
    access_key: str
    workspace: Optional[str] = None
    categories: list[SPMScanCategory] = list(SPMScanCategory)
    max_files_per_repo: int = 200


@app.post("/spm/scan")
async def spm_scan(request_body: SPMScanRequest):
    scan_group_id = uuid.uuid4().hex
    lock_key = f"spm:lock:{request_body.platform}:{request_body.access_key[:8]}"

    try:
        r = aioredis.from_url(settings.REDIS_URL)
        acquired = await r.set(lock_key, scan_group_id, nx=True, ex=60)
        await r.aclose()
    except Exception as exc:
        logger.warning("spm_redis_lock_failed", error=str(exc))
        return JSONResponse(status_code=503, content={"error": "redis_unavailable"})

    if not acquired:
        return JSONResponse(status_code=202, content={"status": "already_queued"})

    task_payload = {
        "scan_group_id": scan_group_id,
        "platform": request_body.platform,
        "access_key": request_body.access_key,
        "workspace": request_body.workspace,
        "categories": [c.value for c in request_body.categories],
        "max_files_per_repo": request_body.max_files_per_repo,
    }
    task = run_spm_scan.delay(task_payload)
    logger.info("spm_scan_queued", scan_group_id=scan_group_id, platform=request_body.platform)
    return JSONResponse(status_code=202, content={
        "scan_group_id": scan_group_id,
        "status": "queued",
        "task_id": task.id,
    })


@app.get("/spm/scan/{scan_group_id}")
async def spm_scan_status(scan_group_id: str):
    try:
        r = aioredis.from_url(settings.REDIS_URL)
        group_data = await r.hgetall(f"spm:group:{scan_group_id}")
        if not group_data:
            await r.aclose()
            return JSONResponse(status_code=404, content={"error": "scan_group not found"})

        progress = await r.hgetall(f"spm:group:{scan_group_id}:progress")
        report_ids = await r.smembers(f"spm:group:{scan_group_id}:report_ids")

        reports = []
        if report_ids:
            pipe = r.pipeline()
            for rid in report_ids:
                pipe.get(f"spm:report:{rid.decode() if isinstance(rid, bytes) else rid}")
            raw_reports = await pipe.execute()
            for raw in raw_reports:
                if raw:
                    data = json.loads(raw)
                    # Exclude full findings array from group summary
                    reports.append({k: v for k, v in data.items() if k != "findings"})

        await r.aclose()
    except Exception as exc:
        logger.error("spm_status_error", error=str(exc))
        return JSONResponse(status_code=503, content={"error": "redis_unavailable"})

    def _decode(v):
        return v.decode() if isinstance(v, bytes) else v

    total = int(_decode(progress.get(b"total", progress.get("total", 0))) or 0)
    completed = int(_decode(progress.get(b"completed", progress.get("completed", 0))) or 0)
    errors = int(_decode(progress.get(b"errors", progress.get("errors", 0))) or 0)

    overall_status = "running"
    if total > 0 and (completed + errors) >= total:
        overall_status = "complete" if errors == 0 else "complete_with_errors"

    return JSONResponse(content={
        "scan_group_id": scan_group_id,
        "platform": _decode(group_data.get(b"platform", group_data.get("platform", ""))),
        "submitted_at": _decode(group_data.get(b"submitted_at", group_data.get("submitted_at", ""))),
        "workspace": _decode(group_data.get(b"workspace", group_data.get("workspace", ""))) or None,
        "repos_discovered": int(_decode(group_data.get(b"repos_discovered", group_data.get("repos_discovered", 0))) or 0),
        "progress": {"total": total, "completed": completed, "errors": errors},
        "status": overall_status,
        "reports": reports,
    })


@app.get("/spm/scan/{scan_group_id}/report/{scan_id}")
async def spm_report_detail(scan_group_id: str, scan_id: str):
    try:
        r = aioredis.from_url(settings.REDIS_URL)
        is_member = await r.sismember(f"spm:group:{scan_group_id}:report_ids", scan_id)
        if not is_member:
            await r.aclose()
            return JSONResponse(status_code=404, content={"error": "report not found"})

        raw = await r.get(f"spm:report:{scan_id}")
        await r.aclose()
    except Exception as exc:
        logger.error("spm_report_error", error=str(exc))
        return JSONResponse(status_code=503, content={"error": "redis_unavailable"})

    if not raw:
        return JSONResponse(status_code=404, content={"error": "report not found"})

    return JSONResponse(content=json.loads(raw))
