import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.analyzers.ai_reviewer import AIReviewer
from app.analyzers.semgrep_runner import SemgrepRunner
from app.config.settings import Settings
from app.models import Finding, PRContext, ReviewConfig, Severity

FIXTURES = Path(__file__).parent / "fixtures"


def _make_settings() -> Settings:
    return Settings(
        ANTHROPIC_API_KEY="test-key",
        REDIS_URL="redis://localhost:6379/0",
        BITBUCKET_WEBHOOK_SECRET="secret",
        BITBUCKET_USERNAME="user",
        BITBUCKET_APP_PASSWORD="pass",
        AI_PROVIDER="claude",  # pin — host .env may set glm, tests mock the Anthropic client
        AI_MAX_DIFF_TOKENS=8000,
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
        language="java",
        diff="diff --git a/Foo.java b/Foo.java\n+some code",
        changed_files=["Foo.java"],
    )
    defaults.update(kwargs)
    return PRContext(**defaults)


def _make_reviewer(settings: Settings) -> AIReviewer:
    reviewer = AIReviewer(settings)
    reviewer._redis = MagicMock()
    reviewer._redis.get.return_value = 0
    reviewer._redis.incrby.return_value = 0
    return reviewer


def test_ai_reviewer_parses_valid_json():
    settings = _make_settings()
    reviewer = _make_reviewer(settings)
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text=json.dumps([
        {"severity": "CRITICAL", "file": "Foo.java", "line": 10,
         "message": "SQL injection", "suggestion": "Use prepared statements"}
    ]))]
    mock_response.usage = MagicMock(input_tokens=100, output_tokens=50)
    with patch.object(reviewer._client.messages, "create", return_value=mock_response):
        findings = reviewer.review(_make_pr_context(), ReviewConfig())
    assert len(findings) == 1
    assert findings[0].severity == Severity.CRITICAL
    assert findings[0].file == "Foo.java"
    assert findings[0].source == "ai"


def test_ai_reviewer_handles_malformed_json():
    settings = _make_settings()
    reviewer = _make_reviewer(settings)
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text="This is not JSON at all!!!")]
    mock_response.usage = MagicMock(input_tokens=100, output_tokens=50)
    with patch.object(reviewer._client.messages, "create", return_value=mock_response):
        findings = reviewer.review(_make_pr_context(), ReviewConfig())
    assert findings == []


def test_ai_reviewer_strips_markdown_fences():
    settings = _make_settings()
    reviewer = _make_reviewer(settings)
    fenced = '```json\n[{"severity": "BUG", "file": "Bar.java", "line": 5, "message": "NPE risk", "suggestion": "Add null check"}]\n```'
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text=fenced)]
    mock_response.usage = MagicMock(input_tokens=100, output_tokens=50)
    with patch.object(reviewer._client.messages, "create", return_value=mock_response):
        findings = reviewer.review(_make_pr_context(), ReviewConfig())
    assert len(findings) == 1
    assert findings[0].severity == Severity.BUG
    assert findings[0].file == "Bar.java"


def _make_semgrep_finding(file: str = "Foo.java", line: int = 1, message: str = "Possible issue") -> Finding:
    return Finding(
        severity=Severity.BUG,
        file=file,
        line=line,
        message=message,
        suggestion="Fix it",
        source="semgrep",
        rule_id="java.lang.security.some-rule",
    )


def test_verify_semgrep_findings_empty_list_skips_ai_call():
    settings = _make_settings()
    reviewer = _make_reviewer(settings)
    with patch.object(reviewer._client.messages, "create") as mock_create:
        result = reviewer.verify_semgrep_findings([], _make_pr_context())
    mock_create.assert_not_called()
    assert result == []


def test_verify_semgrep_findings_filters_false_positives():
    settings = _make_settings()
    reviewer = _make_reviewer(settings)
    findings = [_make_semgrep_finding(line=1, message="Real bug"), _make_semgrep_finding(line=2, message="Noise")]
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text=json.dumps([
        {"index": 0, "verdict": "CONFIRMED", "reason": "genuinely unsafe"},
        {"index": 1, "verdict": "FALSE_POSITIVE", "reason": "input already sanitized above"},
    ]))]
    mock_response.usage = MagicMock(input_tokens=100, output_tokens=50)
    with patch.object(reviewer._client.messages, "create", return_value=mock_response):
        result = reviewer.verify_semgrep_findings(findings, _make_pr_context())
    assert len(result) == 1
    assert result[0].message == "Real bug"


def test_verify_semgrep_findings_fails_open_on_ai_error():
    settings = _make_settings()
    reviewer = _make_reviewer(settings)
    findings = [_make_semgrep_finding()]
    with patch.object(reviewer._client.messages, "create", side_effect=RuntimeError("boom")):
        result = reviewer.verify_semgrep_findings(findings, _make_pr_context())
    assert result == findings


def test_verify_semgrep_findings_fails_open_on_unparseable_response():
    settings = _make_settings()
    reviewer = _make_reviewer(settings)
    findings = [_make_semgrep_finding()]
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text="not json")]
    mock_response.usage = MagicMock(input_tokens=100, output_tokens=50)
    with patch.object(reviewer._client.messages, "create", return_value=mock_response):
        result = reviewer.verify_semgrep_findings(findings, _make_pr_context())
    assert result == findings


def test_semgrep_runner_filters_ignore_paths():
    config = ReviewConfig(ignore_paths=["migrations/*", "*.generated.java"])
    runner = SemgrepRunner(config)
    mock_adapter = MagicMock()
    pr_context = PRContext(
        platform="bitbucket",
        repo_full_name="workspace/repo",
        pr_id=1,
        base_sha="base",
        head_sha="head",
        author="dev",
        title="Test",
        language="java",
        diff="",
        changed_files=["migrations/001.java", "src/Main.java", "Auto.generated.java"],
    )
    # Patch subprocess to avoid running real semgrep
    with patch("app.analyzers.semgrep_runner.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout='{"results": []}', stderr="")
        mock_adapter.get_file_content.return_value = "public class Main {}"
        runner.run(pr_context, mock_adapter)

    # Only src/Main.java should have been fetched (migrations/ and *.generated.java filtered)
    called_paths = [call[0][1] for call in mock_adapter.get_file_content.call_args_list]
    assert "migrations/001.java" not in called_paths
    assert "Auto.generated.java" not in called_paths
    assert "src/Main.java" in called_paths


VULNERABLE_JAVA = """
import java.sql.*;

public class UserService {
    private Connection conn;

    public void getUserByName(String username) throws SQLException {
        Statement stmt = conn.createStatement();
        stmt.execute("SELECT * FROM users WHERE name = '" + username + "'");
    }
}
"""


@pytest.mark.integration
def test_semgrep_on_fixture():
    """Requires semgrep installed locally."""
    import subprocess
    result = subprocess.run(["semgrep", "--version"], capture_output=True)
    if result.returncode != 0:
        pytest.skip("semgrep not installed")

    config = ReviewConfig(semgrep_rules=["p/java"])
    runner = SemgrepRunner(config)
    mock_adapter = MagicMock()
    mock_adapter.get_file_content.return_value = VULNERABLE_JAVA
    pr_context = PRContext(
        platform="bitbucket",
        repo_full_name="workspace/repo",
        pr_id=1,
        base_sha="base",
        head_sha="head",
        author="dev",
        title="Test",
        language="java",
        diff="""diff --git a/UserService.java b/UserService.java
index 123..456 789
--- a/UserService.java
+++ b/UserService.java
@@ -1,9 +1,9 @@

 import java.sql.*;

 public class UserService {
     private Connection conn;

     public void getUserByName(String username) throws SQLException {
         Statement stmt = conn.createStatement();
-        stmt.execute("SELECT * FROM users WHERE name = '" + username + "'");
+        stmt.execute("SELECT * FROM users WHERE name = '" + username + "'"); // SQL injection
     }
 }
""",
        changed_files=["UserService.java"],
    )
    findings = runner.run(pr_context, mock_adapter)

    assert len(findings) > 0, "Semgrep should detect SQL injection in vulnerable Java code"
    sql_findings = [f for f in findings if "sql" in f.message.lower() or "injection" in f.message.lower()]
    assert len(sql_findings) > 0, "Semgrep should detect SQL injection specifically"
    assert all(f.source == "semgrep" for f in findings)
    assert all(f.file == "UserService.java" for f in findings)


def test_ai_review_stops_at_deadline_returns_partial_findings():
    """Deadline guard: the chunk loop must break before a Celery hard-timeout
    SIGKILLs the worker — partial findings are returned so the build status
    can still be finalized (bug: PR #3565 stuck on 'in progress' forever)."""
    settings = _make_settings()
    reviewer = _make_reviewer(settings)
    reviewer._settings.AI_DAILY_TOKEN_BUDGET = 0  # disable budget check

    good_response = MagicMock()
    good_response.content = [MagicMock(text=json.dumps([
        {"severity": "BUG", "file": "Foo.java", "line": 1, "message": "bug", "suggestion": "fix"}
    ]))]
    good_response.usage = MagicMock(input_tokens=10, output_tokens=5)

    # Multi-file diff — 4 chunks, so the guard can stop mid-loop
    diff = "".join(
        f"diff --git a/File{i}.java b/File{i}.java\n" + "".join(f"+line {j}\n" for j in range(1500))
        for i in range(4)
    )
    ctx = _make_pr_context(diff=diff)

    clock = {"now": 1000.0}
    calls = {"n": 0}

    def fake_create(**kwargs):
        calls["n"] += 1
        # Simulate each AI call consuming 300s of wall time
        clock["now"] += 300.0
        return good_response

    with patch.object(reviewer._client.messages, "create", side_effect=fake_create), \
         patch("app.analyzers.ai_reviewer.time.monotonic", side_effect=lambda: clock["now"]):
        # Reserve is 60s (stop if now > 1500-60=1440): chunks at clock 1000 and
        # 1300 run, the third iteration (clock 1600) crosses and breaks
        findings = reviewer.review(ctx, ReviewConfig(), deadline=1500.0)

    assert calls["n"] == 2, "deadline guard must stop mid-loop before the hard timeout"
    assert len(findings) == 2


def test_ai_review_no_deadline_processes_all_chunks():
    settings = _make_settings()
    reviewer = _make_reviewer(settings)
    reviewer._settings.AI_DAILY_TOKEN_BUDGET = 0

    empty_response = MagicMock()
    empty_response.content = [MagicMock(text=json.dumps([
        {"severity": "BUG", "file": "Foo.java", "line": 1, "message": "bug", "suggestion": "fix"}
    ]))]
    empty_response.usage = MagicMock(input_tokens=10, output_tokens=5)

    # Multi-file diff (one file per chunk): 4 files × ~10k tokens each > 8000
    diff = "".join(
        f"diff --git a/File{i}.java b/File{i}.java\n" + "".join(f"+line {j}\n" for j in range(1500))
        for i in range(4)
    )
    ctx = _make_pr_context(diff=diff)

    with patch.object(reviewer._client.messages, "create", return_value=empty_response) as mock_create:
        findings = reviewer.review(ctx, ReviewConfig(), deadline=None)

    assert mock_create.call_count == 4  # one chunk per file
    assert len(findings) == 4
