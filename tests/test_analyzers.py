import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.analyzers.ai_reviewer import AIReviewer
from app.analyzers.semgrep_runner import SemgrepRunner
from app.config.settings import Settings
from app.models import PRContext, ReviewConfig, Severity

FIXTURES = Path(__file__).parent / "fixtures"


def _make_settings() -> Settings:
    return Settings(
        ANTHROPIC_API_KEY="test-key",
        REDIS_URL="redis://localhost:6379/0",
        BITBUCKET_WEBHOOK_SECRET="secret",
        BITBUCKET_USERNAME="user",
        BITBUCKET_APP_PASSWORD="pass",
        AI_MAX_DIFF_TOKENS=8000,
    )


def _make_pr_context() -> PRContext:
    return PRContext(
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
