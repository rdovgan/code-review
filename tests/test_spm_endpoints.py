import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


# Patch Redis and Celery before importing the app
@pytest.fixture(autouse=True)
def _patch_redis_celery():
    with (
        patch("redis.from_url"),
        patch("redis.asyncio.from_url"),
        patch("app.metrics.Metrics.__init__", return_value=None),
        patch("app.metrics.Metrics.inc_webhook"),
        patch("app.metrics.Metrics.prometheus_text", return_value=""),
    ):
        yield


@pytest.fixture()
def client():
    from app.main import app
    return TestClient(app)


# ------------------------------------------------------------------
# POST /spm/scan
# ------------------------------------------------------------------

def test_post_spm_scan_returns_202(client):
    mock_task = MagicMock()
    mock_task.id = "task-abc"

    async def fake_set(*args, **kwargs):
        return True  # lock acquired

    async def fake_aclose():
        pass

    mock_redis = MagicMock()
    mock_redis.set = AsyncMock(return_value=True)
    mock_redis.aclose = AsyncMock()

    with (
        patch("redis.asyncio.from_url", return_value=mock_redis),
        patch("app.workers.celery_app.run_spm_scan.delay", return_value=mock_task),
    ):
        resp = client.post("/spm/scan", json={
            "platform": "bitbucket",
            "access_key": "ATBBtest12345",
            "workspace": "myworkspace",
            "categories": ["SECRET"],
        })

    assert resp.status_code == 202
    body = resp.json()
    assert "scan_group_id" in body
    assert body["status"] == "queued"
    assert body["task_id"] == "task-abc"


def test_post_spm_scan_already_queued(client):
    mock_redis = MagicMock()
    mock_redis.set = AsyncMock(return_value=None)  # lock NOT acquired
    mock_redis.aclose = AsyncMock()

    with patch("redis.asyncio.from_url", return_value=mock_redis):
        resp = client.post("/spm/scan", json={
            "platform": "bitbucket",
            "access_key": "ATBBtest12345",
        })

    assert resp.status_code == 202
    assert resp.json()["status"] == "already_queued"


def test_post_spm_scan_redis_unavailable(client):
    mock_redis = MagicMock()
    mock_redis.set = AsyncMock(side_effect=Exception("connection refused"))
    mock_redis.aclose = AsyncMock()

    with patch("redis.asyncio.from_url", return_value=mock_redis):
        resp = client.post("/spm/scan", json={
            "platform": "bitbucket",
            "access_key": "ATBBtest12345",
        })

    assert resp.status_code == 503


# ------------------------------------------------------------------
# GET /spm/scan/{scan_group_id}
# ------------------------------------------------------------------

def test_get_spm_scan_not_found(client):
    mock_redis = MagicMock()
    mock_redis.hgetall = AsyncMock(return_value={})
    mock_redis.smembers = AsyncMock(return_value=set())
    mock_redis.aclose = AsyncMock()

    with patch("redis.asyncio.from_url", return_value=mock_redis):
        resp = client.get("/spm/scan/nonexistent-group")

    assert resp.status_code == 404


def test_get_spm_scan_returns_progress(client):
    group_data = {
        b"platform": b"bitbucket",
        b"submitted_at": b"2026-03-30T10:00:00+00:00",
        b"workspace": b"myws",
        b"repos_discovered": b"3",
    }
    progress_data = {
        b"total": b"3",
        b"completed": b"2",
        b"errors": b"0",
    }
    report_stub = json.dumps({
        "scan_id": "scan1",
        "repo_full_name": "myws/repo1",
        "status": "complete",
        "files_scanned": 10,
        "summary": {"CRITICAL": 1, "BUG": 0, "PERFORMANCE": 0, "SUGGEST": 0},
        "scanned_at": "2026-03-30T10:01:00+00:00",
    })

    mock_pipeline = MagicMock()
    mock_pipeline.get = MagicMock()
    mock_pipeline.execute = AsyncMock(return_value=[report_stub.encode()])

    mock_redis = MagicMock()
    mock_redis.hgetall = AsyncMock(side_effect=[group_data, progress_data])
    mock_redis.smembers = AsyncMock(return_value={b"scan1"})
    mock_redis.pipeline = MagicMock(return_value=mock_pipeline)
    mock_redis.aclose = AsyncMock()

    with patch("redis.asyncio.from_url", return_value=mock_redis):
        resp = client.get("/spm/scan/group123")

    assert resp.status_code == 200
    body = resp.json()
    assert body["scan_group_id"] == "group123"
    assert body["progress"]["total"] == 3
    assert body["progress"]["completed"] == 2
    assert len(body["reports"]) == 1
    assert body["reports"][0]["scan_id"] == "scan1"


# ------------------------------------------------------------------
# GET /spm/scan/{scan_group_id}/report/{scan_id}
# ------------------------------------------------------------------

def test_get_spm_report_not_in_group(client):
    mock_redis = MagicMock()
    mock_redis.sismember = AsyncMock(return_value=False)
    mock_redis.aclose = AsyncMock()

    with patch("redis.asyncio.from_url", return_value=mock_redis):
        resp = client.get("/spm/scan/group123/report/scan999")

    assert resp.status_code == 404


def test_get_spm_report_returns_findings(client):
    report_data = {
        "scan_id": "scan1",
        "platform": "bitbucket",
        "repo_full_name": "myws/repo1",
        "scanned_at": "2026-03-30T10:01:00+00:00",
        "status": "complete",
        "error": None,
        "files_scanned": 5,
        "summary": {"CRITICAL": 1, "BUG": 0, "PERFORMANCE": 0, "SUGGEST": 0},
        "findings": [{
            "category": "SECRET",
            "severity": "CRITICAL",
            "file": "config.py",
            "line": 10,
            "message": "Hardcoded AWS key",
            "suggestion": "Use environment variable",
            "source": "semgrep",
            "rule_id": "python.aws.hardcoded-access-key",
        }],
    }

    mock_redis = MagicMock()
    mock_redis.sismember = AsyncMock(return_value=True)
    mock_redis.get = AsyncMock(return_value=json.dumps(report_data).encode())
    mock_redis.aclose = AsyncMock()

    with patch("redis.asyncio.from_url", return_value=mock_redis):
        resp = client.get("/spm/scan/group123/report/scan1")

    assert resp.status_code == 200
    body = resp.json()
    assert body["scan_id"] == "scan1"
    assert len(body["findings"]) == 1
    assert body["findings"][0]["category"] == "SECRET"
    assert body["findings"][0]["severity"] == "CRITICAL"
