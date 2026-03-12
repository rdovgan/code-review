import json
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict

import redis.asyncio as aioredis
import structlog
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from app.adapters.factory import get_adapter
from app.config.settings import get_settings
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

    pr_context = adapter.parse_webhook(payload)
    if pr_context is None:
        return JSONResponse(status_code=200, content={"status": "ignored"})

    task_payload = {"platform": "bitbucket", "diff": "", **asdict(pr_context)}
    task = process_review.delay(task_payload)
    return JSONResponse(status_code=202, content={"status": "queued", "task_id": task.id})
