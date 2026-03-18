import json
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict

import redis.asyncio as aioredis
import structlog
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from app.adapters.factory import get_adapter
from app.config.settings import get_settings
from app.metrics import Metrics
from app.workers.celery_app import process_review

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
