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
        # A 1500-line test file is ~3.9k tokens: each fits alone, two don't — one request per file
        AI_MAX_DIFF_TOKENS=4000,
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
    mock_response.content = [MagicMock(type="text", text=json.dumps([
        {"severity": "CRITICAL", "file": "Foo.java", "line": 10,
         "message": "SQL injection", "suggestion": "Use prepared statements"}
    ]))]
    mock_response.usage = MagicMock(input_tokens=100, output_tokens=50)
    with patch.object(reviewer._client.beta.messages, "create", return_value=mock_response):
        findings = reviewer.review(_make_pr_context(), ReviewConfig())
    assert len(findings) == 1
    assert findings[0].severity == Severity.CRITICAL
    assert findings[0].file == "Foo.java"
    assert findings[0].source == "ai"


def test_ai_reviewer_handles_malformed_json():
    settings = _make_settings()
    reviewer = _make_reviewer(settings)
    mock_response = MagicMock()
    mock_response.content = [MagicMock(type="text", text="This is not JSON at all!!!")]
    mock_response.usage = MagicMock(input_tokens=100, output_tokens=50)
    with patch.object(reviewer._client.beta.messages, "create", return_value=mock_response):
        findings = reviewer.review(_make_pr_context(), ReviewConfig())
    assert findings == []


def test_ai_reviewer_strips_markdown_fences():
    settings = _make_settings()
    reviewer = _make_reviewer(settings)
    fenced = '```json\n[{"severity": "BUG", "file": "Bar.java", "line": 5, "message": "NPE risk", "suggestion": "Add null check"}]\n```'
    mock_response = MagicMock()
    mock_response.content = [MagicMock(type="text", text=fenced)]
    mock_response.usage = MagicMock(input_tokens=100, output_tokens=50)
    with patch.object(reviewer._client.beta.messages, "create", return_value=mock_response):
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
    with patch.object(reviewer._client.beta.messages, "create") as mock_create:
        result = reviewer.verify_semgrep_findings([], _make_pr_context())
    mock_create.assert_not_called()
    assert result == []


def test_verify_semgrep_findings_filters_false_positives():
    settings = _make_settings()
    reviewer = _make_reviewer(settings)
    findings = [_make_semgrep_finding(line=1, message="Real bug"), _make_semgrep_finding(line=2, message="Noise")]
    mock_response = MagicMock()
    mock_response.content = [MagicMock(type="text", text=json.dumps([
        {"index": 0, "verdict": "CONFIRMED", "reason": "genuinely unsafe"},
        {"index": 1, "verdict": "FALSE_POSITIVE", "reason": "input already sanitized above"},
    ]))]
    mock_response.usage = MagicMock(input_tokens=100, output_tokens=50)
    with patch.object(reviewer._client.beta.messages, "create", return_value=mock_response):
        result = reviewer.verify_semgrep_findings(findings, _make_pr_context())
    assert len(result) == 1
    assert result[0].message == "Real bug"


def test_verify_semgrep_findings_fails_open_on_ai_error():
    settings = _make_settings()
    reviewer = _make_reviewer(settings)
    findings = [_make_semgrep_finding()]
    with patch.object(reviewer._client.beta.messages, "create", side_effect=RuntimeError("boom")):
        result = reviewer.verify_semgrep_findings(findings, _make_pr_context())
    assert result == findings


def test_verify_semgrep_findings_fails_open_on_unparseable_response():
    settings = _make_settings()
    reviewer = _make_reviewer(settings)
    findings = [_make_semgrep_finding()]
    mock_response = MagicMock()
    mock_response.content = [MagicMock(type="text", text="not json")]
    mock_response.usage = MagicMock(input_tokens=100, output_tokens=50)
    with patch.object(reviewer._client.beta.messages, "create", return_value=mock_response):
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
        diff="".join(
            f"diff --git a/{p} b/{p}\n--- a/{p}\n+++ b/{p}\n@@ -0,0 +1 @@\n+class X {{}}\n"
            for p in ("migrations/001.java", "src/Main.java", "Auto.generated.java")
        ),
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
    good_response.content = [MagicMock(type="text", text=json.dumps([
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

    with patch.object(reviewer._client.beta.messages, "create", side_effect=fake_create), \
         patch("app.analyzers.ai_reviewer.time.monotonic", side_effect=lambda: clock["now"]):
        # Reserve is 60s (stop if now > 1500-60=1440): chunks at clock 1000 and
        # 1300 run, the third iteration (clock 1600) crosses and breaks
        findings = reviewer.review(ctx, ReviewConfig(ai_verify=False), deadline=1500.0)

    assert calls["n"] == 2, "deadline guard must stop mid-loop before the hard timeout"
    assert len(findings) == 2


def test_ai_review_no_deadline_processes_all_chunks():
    settings = _make_settings()
    reviewer = _make_reviewer(settings)
    reviewer._settings.AI_DAILY_TOKEN_BUDGET = 0

    empty_response = MagicMock()
    empty_response.content = [MagicMock(type="text", text=json.dumps([
        {"severity": "BUG", "file": "Foo.java", "line": 1, "message": "bug", "suggestion": "fix"}
    ]))]
    empty_response.usage = MagicMock(input_tokens=10, output_tokens=5)

    # Multi-file diff (one file per chunk): 4 files × ~3.9k tokens, limit 4000
    diff = "".join(
        f"diff --git a/File{i}.java b/File{i}.java\n" + "".join(f"+line {j}\n" for j in range(1500))
        for i in range(4)
    )
    ctx = _make_pr_context(diff=diff)

    with patch.object(reviewer._client.beta.messages, "create", return_value=empty_response) as mock_create:
        findings = reviewer.review(ctx, ReviewConfig(ai_verify=False), deadline=None)

    assert mock_create.call_count == 4  # one chunk per file
    assert len(findings) == 4


def _response(text: str, *blocks) -> MagicMock:
    resp = MagicMock()
    resp.content = list(blocks) + [MagicMock(type="text", text=text)]
    resp.stop_reason = "end_turn"
    resp.usage = MagicMock(input_tokens=10, output_tokens=5)
    return resp


def _multi_file_diff(n: int, lines_per_file: int = 1500) -> str:
    return "".join(
        f"diff --git a/File{i}.java b/File{i}.java\n" + "".join(f"+line {j}\n" for j in range(lines_per_file))
        for i in range(n)
    )


def test_ai_review_clean_chunks_do_not_trip_circuit_breaker():
    """[] means "no issues" — it used to count as a parse failure and abort after 2 clean chunks."""
    reviewer = _make_reviewer(_make_settings())
    ctx = _make_pr_context(diff=_multi_file_diff(4))
    with patch.object(reviewer._client.beta.messages, "create", return_value=_response("[]")) as mock_create:
        result = reviewer.review(ctx, ReviewConfig())
    assert mock_create.call_count == 4
    assert result.complete and result.chunks_reviewed == 4


def test_ai_review_reports_budget_stop_when_nothing_reviewed():
    settings = _make_settings()
    settings.AI_DAILY_TOKEN_BUDGET = 1000
    reviewer = _make_reviewer(settings)
    reviewer._redis.get.return_value = 5000
    with patch.object(reviewer._client.beta.messages, "create") as mock_create:
        result = reviewer.review(_make_pr_context(), ReviewConfig())
    mock_create.assert_not_called()
    assert result.stop_reason == "budget"
    assert result.chunks_reviewed == 0


def test_ai_review_reports_errors_when_every_chunk_fails():
    reviewer = _make_reviewer(_make_settings())
    with patch.object(reviewer._client.beta.messages, "create", side_effect=RuntimeError("boom")):
        result = reviewer.review(_make_pr_context(), ReviewConfig())
    assert result.stop_reason == "errors"
    assert result.chunks_reviewed == 0


def test_ai_review_reads_text_blocks_after_thinking():
    reviewer = _make_reviewer(_make_settings())
    thinking = MagicMock(type="thinking", thinking="", text="")
    answer = json.dumps([{"severity": "BUG", "file": "Foo.java", "line": 3, "message": "m", "suggestion": "s"}])
    with patch.object(reviewer._client.beta.messages, "create", return_value=_response(answer, thinking)):
        result = reviewer.review(_make_pr_context(), ReviewConfig())
    assert [f.line for f in result] == [3]


def test_ai_review_refusal_counts_as_failed_chunk():
    reviewer = _make_reviewer(_make_settings())
    refused = _response("")
    refused.stop_reason = "refusal"
    with patch.object(reviewer._client.beta.messages, "create", return_value=refused):
        result = reviewer.review(_make_pr_context(), ReviewConfig())
    assert result.stop_reason == "errors"


def test_ai_review_sends_numbered_file_context_for_changed_files():
    reviewer = _make_reviewer(_make_settings())
    diff = "diff --git a/Foo.java b/Foo.java\n--- a/Foo.java\n+++ b/Foo.java\n@@ -1 +1,2 @@\n class Foo {\n+  int x;\n"
    contents = {"Foo.java": "class Foo {\n  int x;\n}", "Other.java": "class Other {}"}
    with patch.object(reviewer._client.beta.messages, "create", return_value=_response("[]")) as mock_create:
        reviewer.review(_make_pr_context(diff=diff), ReviewConfig(), file_contents=contents)
    sent = mock_create.call_args.kwargs["messages"][0]["content"]
    assert "FULL_FILE_CONTEXT" in sent
    assert "    2|   int x;" in sent
    assert "Other.java" not in sent  # only files present in the chunk
    assert sent.endswith(diff)


def test_ai_review_file_context_respects_char_budget():
    settings = _make_settings()
    settings.AI_FILE_CONTEXT_MAX_CHARS = 50
    reviewer = _make_reviewer(settings)
    diff = "diff --git a/Foo.java b/Foo.java\n@@ -1 +1 @@\n+x\n"
    with patch.object(reviewer._client.beta.messages, "create", return_value=_response("[]")) as mock_create:
        reviewer.review(_make_pr_context(diff=diff), ReviewConfig(), file_contents={"Foo.java": "y\n" * 100})
    assert mock_create.call_args.kwargs["messages"][0]["content"] == diff


def test_ai_review_requests_server_side_fallback():
    reviewer = _make_reviewer(_make_settings())
    with patch.object(reviewer._client.beta.messages, "create", return_value=_response("[]")) as mock_create:
        reviewer.review(_make_pr_context(), ReviewConfig())
    kwargs = mock_create.call_args.kwargs
    assert kwargs["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in kwargs["betas"]


def test_ai_review_packs_small_files_into_one_request():
    """Files of one PR are reviewed together, not one blind request per file."""
    settings = _make_settings()
    settings.AI_MAX_DIFF_TOKENS = 9000  # two ~3.9k-token files fit, the third doesn't
    reviewer = _make_reviewer(settings)
    with patch.object(reviewer._client.beta.messages, "create", return_value=_response("[]")) as mock_create:
        result = reviewer.review(_make_pr_context(diff=_multi_file_diff(3)), ReviewConfig())
    assert mock_create.call_count == 2
    first = mock_create.call_args_list[0].kwargs["messages"][0]["content"]
    assert "File0.java" in first and "File1.java" in first and "File2.java" not in first
    assert result.complete and result.chunks_reviewed == 2


def test_ai_review_sends_pr_context_before_the_diff():
    reviewer = _make_reviewer(_make_settings())
    ctx = _make_pr_context()
    with patch.object(reviewer._client.beta.messages, "create", return_value=_response("[]")) as mock_create:
        reviewer.review(ctx, ReviewConfig(), context="PR_CONTEXT — intent")
    sent = mock_create.call_args.kwargs["messages"][0]["content"]
    assert sent.startswith("PR_CONTEXT — intent")
    assert sent.endswith(ctx.diff)


def test_ai_review_prompt_combines_base_language_and_focus():
    reviewer = _make_reviewer(_make_settings())
    with patch.object(reviewer._client.beta.messages, "create", return_value=_response("[]")) as mock_create:
        reviewer.review(_make_pr_context(language="java"), ReviewConfig(ai_focus=["bugs", "billing rules"]))
    system = mock_create.call_args.kwargs["system"]
    assert "Evidence rule" in system            # review_base.md
    assert "@Transactional" in system           # java_prompt.md
    assert "broken business logic" in system    # known focus hint
    assert "billing rules" in system            # free-form focus passed through


def _finding_json(*lines: int) -> str:
    return json.dumps([
        {"severity": "BUG", "file": "Foo.java", "line": n, "message": f"m{n}", "suggestion": "s"} for n in lines
    ])


def test_ai_verification_drops_refuted_findings():
    reviewer = _make_reviewer(_make_settings())
    verdicts = json.dumps([{"index": 0, "verdict": "CONFIRMED"}, {"index": 1, "verdict": "FALSE_POSITIVE"}])
    with patch.object(reviewer._client.beta.messages, "create",
                      side_effect=[_response(_finding_json(1, 2)), _response(verdicts)]) as mock_create:
        result = reviewer.review(_make_pr_context(), ReviewConfig())
    assert [f.line for f in result] == [1]
    assert result.dropped_by_verification == 1
    verify_call = mock_create.call_args_list[1].kwargs
    assert "skeptical" in verify_call["system"]
    assert "REVIEW_FINDINGS_JSON" in verify_call["messages"][0]["content"]


def test_ai_verification_fails_open_on_error():
    reviewer = _make_reviewer(_make_settings())
    with patch.object(reviewer._client.beta.messages, "create",
                      side_effect=[_response(_finding_json(1, 2)), RuntimeError("boom")]):
        result = reviewer.review(_make_pr_context(), ReviewConfig())
    assert [f.line for f in result] == [1, 2]
    assert result.complete  # a failed verification does not make the review incomplete


def test_ai_verification_fails_open_without_usable_verdicts():
    reviewer = _make_reviewer(_make_settings())
    with patch.object(reviewer._client.beta.messages, "create",
                      side_effect=[_response(_finding_json(1)), _response('[{"index": 7, "verdict": "CONFIRMED"}]')]):
        result = reviewer.review(_make_pr_context(), ReviewConfig())
    assert [f.line for f in result] == [1]


def test_ai_verification_skipped_when_disabled_or_nothing_found():
    reviewer = _make_reviewer(_make_settings())
    with patch.object(reviewer._client.beta.messages, "create", return_value=_response(_finding_json(1))) as mock_create:
        reviewer.review(_make_pr_context(), ReviewConfig(ai_verify=False))
    assert mock_create.call_count == 1
    with patch.object(reviewer._client.beta.messages, "create", return_value=_response("[]")) as mock_create:
        reviewer.review(_make_pr_context(), ReviewConfig())
    assert mock_create.call_count == 1


def _glm_reviewer():
    settings = _make_settings()
    settings.AI_PROVIDER = "glm"
    settings.ZAI_API_KEY = "z"
    settings.AI_MAX_DIFF_TOKENS = 100_000  # two files in one request
    return _make_reviewer(settings)


def _glm_response(content, finish_reason="stop"):
    from types import SimpleNamespace
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason=finish_reason)],
                           usage=SimpleNamespace(prompt_tokens=100, completion_tokens=50))


def test_cut_off_answer_splits_the_part_and_retries():
    """glm-5.3-flash spent all of max_tokens thinking on a big PR and answered nothing (PR #52)."""
    reviewer = _glm_reviewer()
    found = json.dumps([{"severity": "BUG", "file": "File1.java", "line": 3, "message": "m", "suggestion": "s"}])
    responses = [_glm_response(None, "length"), _glm_response("[]"), _glm_response(found)]
    with patch.object(reviewer._client.chat.completions, "create", side_effect=responses) as create:
        result = reviewer.review(_make_pr_context(diff=_multi_file_diff(2)), ReviewConfig(ai_verify=False))
    assert create.call_count == 3
    halves = [c.kwargs["messages"][1]["content"] for c in create.call_args_list[1:]]
    assert "File0.java" in halves[0] and "File1.java" not in halves[0] and "File1.java" in halves[1]
    assert result.complete and result.chunks_total == 2 and result.chunks_reviewed == 2
    assert [f.line for f in result] == [3]


def test_cut_off_answer_on_a_single_file_is_a_failed_part():
    reviewer = _glm_reviewer()
    with patch.object(reviewer._client.chat.completions, "create", return_value=_glm_response(None, "length")) as create:
        result = reviewer.review(_make_pr_context(), ReviewConfig())
    assert create.call_count == 1
    assert result.stop_reason == "errors" and result.chunks_reviewed == 0


def test_mattermost_failure_never_logs_the_webhook_url(caplog):
    import httpx
    import respx
    from app.notifiers import mattermost
    url = "https://chat.example.com/hooks/s3cr3t-hook-id"
    with respx.mock:
        respx.post(url).mock(return_value=httpx.Response(404))
        assert mattermost.send_message(url, text="hi") is False
    assert "s3cr3t-hook-id" not in caplog.text
    assert "HTTP 404" in caplog.text


def test_http_client_request_lines_are_not_logged():
    import logging
    import app.workers.celery_app  # noqa: F401  (configures the loggers on import)
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING


# --- bounded requests (PR #52: one diff request waited 7.5+ minutes with no timeout) ---

import time  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from celery.exceptions import SoftTimeLimitExceeded  # noqa: E402

from app.analyzers.ai_calls import RequestTimedOut, call_bounded, classify_error  # noqa: E402


class APITimeoutError(Exception):
    """Named like the SDKs' timeout error: classify_error goes by the class name."""


class APIConnectionError(Exception):
    pass


def _status_error(code: int, name: str = "APIStatusError") -> Exception:
    exc = type(name, (Exception,), {})(f"HTTP {code}")
    exc.status_code = code
    return exc


def _wrapped_soft_limit() -> Exception:
    try:
        try:
            raise SoftTimeLimitExceeded()
        except SoftTimeLimitExceeded as inner:
            raise APIConnectionError("Connection error.") from inner
    except APIConnectionError as exc:
        return exc


def test_clients_have_timeout_and_no_sdk_retries():
    claude = AIReviewer(_make_settings())
    glm_settings = _make_settings()
    glm_settings.AI_PROVIDER, glm_settings.ZAI_API_KEY = "glm", "z"
    glm = AIReviewer(glm_settings)
    for reviewer in (claude, glm):
        assert reviewer._client.max_retries == 0
        assert reviewer._client.timeout.read == 420 and reviewer._client.timeout.connect == 10


def test_claude_create_gets_timeout_kwarg_with_32k_max_tokens():
    """Without an explicit timeout the Anthropic SDK refuses a non-streaming 32k request."""
    import anthropic
    settings = _make_settings()
    settings.AI_MAX_TOKENS = 32_000
    reviewer = _make_reviewer(settings)
    with patch.object(reviewer._client.beta.messages, "create", return_value=_response("[]")) as mock_create:
        reviewer.review(_make_pr_context(), ReviewConfig(ai_verify=False))
    assert mock_create.call_args.kwargs["timeout"] == 420
    # The real SDK call gets past its own checks and fails only on the (closed) port
    reviewer._client = reviewer._client.with_options(base_url="http://127.0.0.1:9")
    with pytest.raises(anthropic.APIConnectionError):
        reviewer._call_claude("system", "diff", 5)


def test_classify_error():
    import anthropic
    import openai
    assert classify_error(_status_error(400, "BadRequestError")) == "bad_request"
    for code in (408, 409, 429, 500, 529):
        assert classify_error(_status_error(code)) == "transient", code
    assert classify_error(_status_error(401, "AuthenticationError")) == "fatal"
    assert classify_error(APITimeoutError()) == "timeout"
    assert classify_error(APIConnectionError()) == "transient"
    assert classify_error(_wrapped_soft_limit()) == "soft_limit"
    assert classify_error(SoftTimeLimitExceeded()) == "soft_limit"
    assert classify_error(ValueError("x")) == "fatal"
    # The real SDK classes, both providers
    assert classify_error(openai.APITimeoutError(request=None)) == "timeout"
    assert classify_error(anthropic.APITimeoutError(request=None)) == "timeout"
    assert classify_error(anthropic.APIConnectionError(request=None)) == "transient"


@patch("app.analyzers.ai_calls.time.sleep")
def test_transient_retried_once_timeout_and_soft_limit_never(_sleep):
    def run(errors):
        calls = []

        def fn(timeout):
            calls.append(timeout)
            if errors:
                raise errors.pop(0)
            return "ok"

        try:
            return call_bounded(fn, deadline=None, reserve=0, cap=420, floor=60, kind="chunk"), len(calls)
        except BaseException as exc:
            return exc, len(calls)

    assert run([_status_error(529, "OverloadedError")]) == ("ok", 2)            # Claude overloaded
    result, calls = run([_status_error(429, "RateLimitError"), _status_error(429, "RateLimitError")])
    assert calls == 2 and result.status_code == 429                             # GLM: only one retry
    result, calls = run([APITimeoutError()])
    assert calls == 1 and isinstance(result, RequestTimedOut) and not result.by_deadline
    result, calls = run([_wrapped_soft_limit()])
    assert calls == 1 and isinstance(result, SoftTimeLimitExceeded)


@patch("app.analyzers.ai_calls.time.sleep")
def test_transient_not_retried_without_time_for_it(_sleep):
    calls = []

    def fn(timeout):
        calls.append(timeout)
        raise _status_error(503)

    with pytest.raises(Exception):
        call_bounded(fn, deadline=time.monotonic() + 150, reserve=90, cap=420, floor=50, kind="chunk")
    assert len(calls) == 1  # 60 s left, the retry needs 60 + backoff


def test_soft_limit_wrapped_in_connection_error_on_semgrep_verify_not_retried():
    reviewer = _make_reviewer(_make_settings())
    finding = Finding(severity=Severity.BUG, file="Foo.java", line=1, message="m", suggestion="s", source="semgrep")
    with patch.object(reviewer._client.beta.messages, "create", side_effect=_wrapped_soft_limit()) as mock_create:
        with pytest.raises(SoftTimeLimitExceeded):
            reviewer.verify_semgrep_findings([finding], _make_pr_context())
    assert mock_create.call_count == 1


def test_chunk_timeout_capped_by_deadline():
    reviewer = _make_reviewer(_make_settings())
    with patch.object(reviewer._client.beta.messages, "create", return_value=_response("[]")) as mock_create:
        reviewer.review(_make_pr_context(), ReviewConfig(), deadline=time.monotonic() + 200)
    assert 100 < mock_create.call_args.kwargs["timeout"] <= 110  # 200 - the 90 s reserve


def test_chunk_timeout_at_deadline_stops_with_deadline_and_keeps_partial():
    reviewer = _make_reviewer(_make_settings())
    responses = [_response(_finding_json(1)), APITimeoutError("read timeout")]
    with patch.object(reviewer._client.beta.messages, "create", side_effect=responses) as mock_create:
        result = reviewer.review(_make_pr_context(diff=_multi_file_diff(3)), ReviewConfig(ai_verify=False),
                                 deadline=time.monotonic() + 300)
    assert mock_create.call_count == 2  # the timed-out chunk is not split: the deadline cut it short
    assert result.stop_reason == "deadline" and result.chunks_reviewed == 1 and [f.line for f in result] == [1]


def test_non_deadline_timeout_halves_multi_file_chunk_without_breaker():
    reviewer = _glm_reviewer()
    responses = [APITimeoutError("read timeout"), _glm_response("[]"), _glm_response("[]")]
    with patch.object(reviewer._client.chat.completions, "create", side_effect=responses) as create:
        result = reviewer.review(_make_pr_context(diff=_multi_file_diff(2)), ReviewConfig(ai_verify=False))
    assert create.call_count == 3
    assert result.complete and result.chunks_total == 2 and result.chunks_reviewed == 2


def test_soft_limit_inside_chunk_returns_partial_with_deadline():
    reviewer = _make_reviewer(_make_settings())
    responses = [_response(_finding_json(1)), SoftTimeLimitExceeded()]
    with patch.object(reviewer._client.beta.messages, "create", side_effect=responses) as mock_create:
        result = reviewer.review(_make_pr_context(diff=_multi_file_diff(3)), ReviewConfig())
    assert mock_create.call_count == 2  # no verification: the task is at its soft limit
    assert result.stop_reason == "deadline" and [f.line for f in result] == [1] and result.unverified == 1


def _per_file_finding(severity: str, file: str) -> str:
    return json.dumps([{"severity": severity, "file": file, "line": 1, "message": "m", "suggestion": "s"}])


def test_verify_runs_after_all_chunks_critical_first():
    reviewer = _make_reviewer(_make_settings())
    order = []

    def create(**kw):
        content = kw["messages"][0]["content"]
        if "REVIEW_FINDINGS_JSON" in content:
            order.append("verify:" + json.loads(content.rsplit("REVIEW_FINDINGS_JSON:\n", 1)[1])[0]["file"])
            return _response('[{"index": 0, "verdict": "CONFIRMED"}]')
        file = next(f"File{i}.java" for i in range(3) if f"File{i}.java" in content)
        order.append("review:" + file)
        answers = {"File0.java": _per_file_finding("BUG", "File0.java"), "File1.java": "[]",
                   "File2.java": _per_file_finding("CRITICAL", "File2.java")}
        return _response(answers[file])

    with patch.object(reviewer._client.beta.messages, "create", side_effect=create):
        result = reviewer.review(_make_pr_context(diff=_multi_file_diff(3)), ReviewConfig())
    assert order == ["review:File0.java", "review:File1.java", "review:File2.java",
                     "verify:File2.java", "verify:File0.java"]
    assert [f.file for f in result] == ["File0.java", "File2.java"]  # posted in diff order
    assert result.unverified == 0


def test_verify_without_time_fails_open_and_counts_unverified():
    reviewer = _make_reviewer(_make_settings())
    clock = {"now": 1000.0}

    def create(**kw):
        clock["now"] += 250  # the review of this chunk took 250 s
        return _response(_finding_json(1, 2))

    with patch.object(reviewer._client.beta.messages, "create", side_effect=create) as mock_create, \
         patch("app.analyzers.ai_reviewer.time.monotonic", side_effect=lambda: clock["now"]):
        result = reviewer.review(_make_pr_context(), ReviewConfig(), deadline=1400.0)
    # 60 s left before the reserve; a verification as slow as the review would not fit
    assert mock_create.call_count == 1
    assert [f.line for f in result] == [1, 2] and result.unverified == 2 and result.complete


def test_semgrep_verify_gets_timeout_and_fails_open():
    reviewer = _make_reviewer(_make_settings())
    finding = Finding(severity=Severity.BUG, file="Foo.java", line=1, message="m", suggestion="s", source="semgrep")
    with patch.object(reviewer._client.beta.messages, "create", side_effect=APITimeoutError()) as mock_create:
        assert reviewer.verify_semgrep_findings([finding], _make_pr_context()) == [finding]
    assert mock_create.call_args.kwargs["timeout"] == 120
    with patch.object(reviewer._client.beta.messages, "create") as mock_create:
        kept = reviewer.verify_semgrep_findings([finding], _make_pr_context(), deadline=time.monotonic() + 10)
    assert kept == [finding]
    mock_create.assert_not_called()


def test_glm_call_reports_reasoning_tokens_and_finish_reason():
    reviewer = _glm_reviewer()
    response = _glm_response("[]")
    response.usage.completion_tokens_details = SimpleNamespace(reasoning_tokens=900)
    with patch.object(reviewer._client.chat.completions, "create", return_value=response):
        res = reviewer._call("system", "diff", kind="chunk")
    assert res.reasoning_tokens == 900 and res.finish_reason == "stop" and not res.truncated
