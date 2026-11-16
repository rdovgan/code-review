import json
import subprocess
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.adapters.base import GitCloneSpec
from app.analyzers.agent_reviewer import AgentReviewer, AgentReviewFailed
from app.config.settings import Settings
from app.context.code_tools import CodeTools, RepoView
from app.context.repo_mirror import RepoMirror, RepoUnavailable
from app.models import Finding, PRContext, ReviewConfig, Severity

DIFF = (
    "diff --git a/src/BookingService.java b/src/BookingService.java\n"
    "--- a/src/BookingService.java\n+++ b/src/BookingService.java\n"
    "@@ -1,3 +1,4 @@\n class BookingService {\n+  int total(Booking b) { return b.nights() * b.rate(); }\n }\n"
)


# --- a real git repo to mirror ---

def _git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                   capture_output=True)


@pytest.fixture
def source_repo(tmp_path):
    src = tmp_path / "src-repo"
    (src / "src" / "dto").mkdir(parents=True)
    (src / "src" / "BookingService.java").write_text(
        "class BookingService {\n  int total(Booking b) { return b.nights() * b.rate(); }\n}\n")
    (src / "src" / "dto" / "Booking.java").write_text(
        "class Booking {\n  int nights() { return Math.max(1, n); }\n  int rate() { return r; }\n}\n")
    _git(src, "init", "-q", "-b", "master")
    _git(src, "add", ".")
    _git(src, "commit", "-q", "-m", "init")
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=src, capture_output=True, text=True).stdout.strip()
    return src, sha


@pytest.fixture
def mirror(tmp_path, source_repo):
    src, sha = source_repo
    m = RepoMirror(str(tmp_path / "mirrors"), "bitbucket", "ws/repo", GitCloneSpec(url=str(src)), timeout=60)
    m.ensure_commit(sha)
    return m, sha


def test_mirror_clones_and_fetches_new_commits(mirror, source_repo):
    m, sha = mirror
    src, _ = source_repo
    assert m.has_commit(sha)
    (src / "NEW.md").write_text("x")
    _git(src, "add", ".")
    _git(src, "commit", "-q", "-m", "second")
    new_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=src, capture_output=True, text=True).stdout.strip()
    assert not m.has_commit(new_sha)
    m.ensure_commit(new_sha)
    assert m.has_commit(new_sha)


def test_mirror_missing_commit_raises(mirror):
    m, _ = mirror
    with pytest.raises(RepoUnavailable, match="not found"):
        m.ensure_commit("0" * 40)


def test_failed_clone_is_not_retried_right_away(tmp_path):
    m = RepoMirror(str(tmp_path / "mirrors"), "bitbucket", "ws/missing",
                   GitCloneSpec(url=str(tmp_path / "does-not-exist")), timeout=30)
    with pytest.raises(RepoUnavailable, match="clone failed"):
        m.ensure_commit("a" * 40)
    with pytest.raises(RepoUnavailable, match="last clone failed"):
        m.ensure_commit("a" * 40)


def test_context_repos_are_never_cloned_during_a_review(tmp_path, source_repo):
    src, _ = source_repo
    m = RepoMirror(str(tmp_path / "mirrors"), "bitbucket", "ws/other", GitCloneSpec(url=str(src)), timeout=30)
    with pytest.raises(RepoUnavailable, match="not cloned yet"):
        m.ensure_fresh()
    assert not m.path.exists()


def test_clone_credentials_go_to_a_header_not_the_url():
    spec = GitCloneSpec(url="https://bitbucket.org/ws/repo.git", username="x-token-auth", password="s3cret")
    env = RepoMirror("/tmp", "bitbucket", "ws/repo", spec)._env()
    assert env["GIT_CONFIG_KEY_0"] == "http.extraHeader"
    assert env["GIT_CONFIG_VALUE_0"].startswith("Authorization: Basic ")
    assert "s3cret" not in repr(spec)


# --- code tools ---

@pytest.fixture
def tools(mirror):
    m, sha = mirror
    return CodeTools([RepoView("ws/repo", m.path, sha, "the PR's repository")])


def test_read_file_is_line_numbered_and_ranged(tools):
    out = tools.run("read_file", {"path": "src/dto/Booking.java", "start_line": 2, "end_line": 2})
    assert "    2|   int nights() { return Math.max(1, n); }" in out
    assert "int rate()" not in out


def test_grep_finds_usages_without_rev_prefix(tools):
    out = tools.run("grep", {"pattern": "nights\\(", "path_glob": "**/*.java"})
    assert "src/BookingService.java:2:" in out
    assert "src/dto/Booking.java:2:" in out
    assert tools.run("grep", {"pattern": "doesNotExist"}) == "no matches"


def test_find_files_and_list_dir(tools):
    assert tools.run("find_files", {"name_glob": "Booking*.java"}).splitlines() == [
        "src/BookingService.java", "src/dto/Booking.java"]
    assert tools.run("list_dir", {"path": "src"}).splitlines() == ["BookingService.java", "dto/"]


def test_tool_errors_are_returned_as_text(tools):
    assert tools.run("read_file", {"path": "nope.java"}).startswith("ERROR: file not found")
    assert tools.run("read_file", {"path": "x", "repo": "ws/unknown"}).startswith("ERROR: unknown repo")
    assert tools.run("delete_everything", {}).startswith("ERROR: unknown tool")
    assert tools.run("grep", None).startswith("ERROR")
    assert tools.calls == 4


def test_tool_output_is_capped(mirror):
    m, sha = mirror
    capped = CodeTools([RepoView("ws/repo", m.path, sha, "")], max_result_chars=40)
    assert capped.run("read_file", {"path": "src/dto/Booking.java"}).endswith("[output truncated — narrow the request]")


# --- agent loop ---

def _settings(**kw) -> Settings:
    base = dict(ANTHROPIC_API_KEY="k", REDIS_URL="redis://localhost:6379/0", AI_PROVIDER="claude",
                AI_DAILY_TOKEN_BUDGET=0)
    base.update(kw)
    return Settings(**base)


def _ctx(**kw) -> PRContext:
    base = dict(platform="bitbucket", repo_full_name="ws/repo", pr_id=7, base_sha="master", head_sha="h" * 40,
                author="dev", title="Fix totals", language="java", diff=DIFF, changed_files=["src/BookingService.java"])
    base.update(kw)
    return PRContext(**base)


def _agent(settings=None) -> AgentReviewer:
    reviewer = AgentReviewer(settings or _settings())
    reviewer._redis = MagicMock()
    reviewer._redis.get.return_value = 0
    reviewer._redis.incrby.return_value = 0
    return reviewer


def _claude(*blocks, stop="end_turn"):
    return SimpleNamespace(content=list(blocks), stop_reason=stop,
                           usage=SimpleNamespace(input_tokens=100, output_tokens=10))


def _tool_use(call_id, name, args):
    return SimpleNamespace(type="tool_use", id=call_id, name=name, input=args)


def _text(text):
    return SimpleNamespace(type="text", text=text)


FINDING = json.dumps([{"severity": "BUG", "file": "src/BookingService.java", "line": 2,
                       "message": "rate() can be 0 for comp bookings", "suggestion": "Guard it"}])


def test_agent_reads_code_with_tools_then_answers():
    agent = _agent()
    tools = MagicMock()
    tools.describe.return_value = "AVAILABLE_REPOSITORIES: ws/repo"
    tools.run.return_value = "src/dto/Booking.java:3:  int rate() { return r; }"
    tools.calls = 1
    responses = [_claude(_tool_use("t1", "grep", {"pattern": "rate\\("})), _claude(_text(FINDING))]
    with patch.object(agent._client.beta.messages, "create", side_effect=responses) as create:
        result = agent.review_agentic(_ctx(), ReviewConfig(), tools, context="PR_CONTEXT — intent")

    assert [f.line for f in result] == [2] and result[0].source == "ai"
    tools.run.assert_called_once_with("grep", {"pattern": "rate\\("})
    first = create.call_args_list[0].kwargs
    assert {t["name"] for t in first["tools"]} == {"read_file", "grep", "find_files", "list_dir"}
    assert "You can read the repository" in first["system"]
    assert first["messages"][0]["content"].startswith("PR_CONTEXT — intent")
    tool_result = create.call_args_list[1].kwargs["messages"][-1]["content"][0]
    assert tool_result["type"] == "tool_result" and tool_result["tool_use_id"] == "t1"


def test_agent_drops_findings_outside_the_diff():
    agent = _agent()
    other = json.dumps([{"severity": "BUG", "file": "src/dto/Booking.java", "line": 3, "message": "m", "suggestion": "s"}])
    tools = MagicMock(calls=0)
    with patch.object(agent._client.beta.messages, "create", return_value=_claude(_text(other))):
        assert list(agent.review_agentic(_ctx(), ReviewConfig(), tools)) == []


def test_agent_is_asked_for_an_answer_when_steps_run_out():
    agent = _agent()
    tools = MagicMock(calls=2)
    tools.run.return_value = "ok"
    responses = [_claude(_tool_use("a", "grep", {"pattern": "x"})),
                 _claude(_tool_use("b", "grep", {"pattern": "y"})),
                 _claude(_text("[]"))]
    with patch.object(agent._client.beta.messages, "create", side_effect=responses) as create:
        result = agent.review_agentic(_ctx(), ReviewConfig(agent_max_steps=2), tools)
    assert list(result) == []
    last_user = create.call_args_list[2].kwargs["messages"][-1]
    assert "tool budget for this review is used up" in last_user["content"]


@pytest.mark.parametrize("responses, reason", [
    ([_claude(_text("not json"))], "not valid JSON"),
    ([_claude(stop="refusal")], "declined"),
    (RuntimeError("api down"), "AI request failed"),
])
def test_agent_failures_raise_for_fallback(responses, reason):
    agent = _agent()
    kwargs = {"side_effect": responses} if isinstance(responses, Exception) else {"side_effect": responses}
    with patch.object(agent._client.beta.messages, "create", **kwargs):
        with pytest.raises(AgentReviewFailed, match=reason):
            agent.review_agentic(_ctx(), ReviewConfig(), MagicMock(calls=0))


def test_agent_refuses_too_large_diffs_and_exhausted_budget():
    agent = _agent(_settings(AGENT_MAX_DIFF_TOKENS=10))
    big = DIFF + DIFF.replace("BookingService", "A") + DIFF.replace("BookingService", "B") + DIFF.replace("BookingService", "C")
    with pytest.raises(AgentReviewFailed, match="too large"):
        agent.review_agentic(_ctx(diff=big), ReviewConfig(), MagicMock())
    agent = _agent(_settings(AI_DAILY_TOKEN_BUDGET=100))
    agent._redis.get.return_value = 500
    with pytest.raises(AgentReviewFailed, match="budget"):
        agent.review_agentic(_ctx(), ReviewConfig(), MagicMock())


def test_agent_works_with_openai_compatible_tool_calls():
    agent = _agent(_settings(AI_PROVIDER="glm", ZAI_API_KEY="z"))
    call = SimpleNamespace(id="c1", function=SimpleNamespace(name="read_file", arguments='{"path": "src/dto/Booking.java"}'))
    usage = SimpleNamespace(prompt_tokens=50, completion_tokens=5)
    first = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=None, tool_calls=[call]))], usage=usage)
    final = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=FINDING, tool_calls=None))], usage=usage)
    tools = MagicMock(calls=1)
    tools.run.return_value = "file text"
    with patch.object(agent._client.chat.completions, "create", side_effect=[first, final]) as create:
        result = agent.review_agentic(_ctx(), ReviewConfig(), tools)
    assert len(result) == 1
    tools.run.assert_called_once_with("read_file", {"path": "src/dto/Booking.java"})
    messages = create.call_args_list[1].kwargs["messages"]
    assert messages[-2]["tool_calls"][0]["id"] == "c1"
    assert messages[-1] == {"role": "tool", "tool_call_id": "c1", "content": "file text"}


# --- worker: modes and fallback ---

def _payload(ctx=None):
    ctx = ctx or _ctx()
    return {"platform": "bitbucket", "diff": ctx.diff, **asdict(ctx)}


def _adapter():
    adapter = MagicMock()
    adapter.get_existing_bot_comments.return_value = []
    adapter.get_pr_commits.return_value = []
    return adapter


def _agent_result(findings, tool_calls):
    from app.analyzers.ai_reviewer import AIReviewResult
    result = AIReviewResult(findings, chunks_total=1, chunks_reviewed=1)
    result.tool_calls = tool_calls
    return result


def _finding(line=2):
    return Finding(severity=Severity.BUG, file="src/BookingService.java", line=line, message="m",
                   suggestion="s", source="ai")


@patch("app.workers.celery_app.gather_review_context", return_value="")
@patch("app.workers.celery_app._agentic_review")
@patch("app.workers.celery_app.get_adapter")
@patch("app.workers.celery_app.AIReviewer")
@patch("app.workers.celery_app.load_project_config")
def test_agentic_mode_posts_agent_findings(mock_config, mock_ai, mock_get_adapter, mock_agentic, _gather):
    from app.workers.celery_app import process_review
    adapter = _adapter()
    mock_get_adapter.return_value = adapter
    mock_config.return_value = ReviewConfig(review_mode="agentic", static_analysis=False)
    mock_agentic.return_value = _agent_result([_finding()], tool_calls=5)
    process_review.run(_payload())
    mock_ai.return_value.review.assert_not_called()
    adapter.post_inline_comment.assert_called_once()
    summary = adapter.post_summary_comment.call_args.args[1]
    assert "Deep review" in summary and "5 code lookups" in summary


@patch("app.workers.celery_app.gather_review_context", return_value="")
@patch("app.workers.celery_app._agentic_review", side_effect=AgentReviewFailed("repository unavailable: no clone"))
@patch("app.workers.celery_app.get_adapter")
@patch("app.workers.celery_app.AIReviewer")
@patch("app.workers.celery_app.load_project_config")
def test_agentic_failure_falls_back_to_diff_review(mock_config, mock_ai, mock_get_adapter, _agentic, _gather):
    from app.workers.celery_app import process_review
    adapter = _adapter()
    mock_get_adapter.return_value = adapter
    mock_config.return_value = ReviewConfig(review_mode="agentic", static_analysis=False)
    mock_ai.return_value.review.return_value = [_finding()]
    result = process_review.run(_payload())
    mock_ai.return_value.review.assert_called_once()
    assert result["status"] == "success" and result["findings"] == 1
    summary = adapter.post_summary_comment.call_args.args[1]
    assert "Deep review unavailable (repository unavailable: no clone)" in summary


@patch("app.workers.celery_app.gather_review_context", return_value="")
@patch("app.workers.celery_app._agentic_review", side_effect=ValueError("unexpected"))
@patch("app.workers.celery_app.get_adapter")
@patch("app.workers.celery_app.AIReviewer")
@patch("app.workers.celery_app.load_project_config")
def test_any_agentic_error_falls_back(mock_config, mock_ai, mock_get_adapter, _agentic, _gather):
    from app.workers.celery_app import process_review
    mock_get_adapter.return_value = _adapter()
    mock_config.return_value = ReviewConfig(review_mode="agentic", static_analysis=False)
    mock_ai.return_value.review.return_value = []
    assert process_review.run(_payload())["status"] == "success"
    mock_ai.return_value.review.assert_called_once()


@patch("app.workers.celery_app.gather_review_context", return_value="")
@patch("app.workers.celery_app.process_shadow_review")
@patch("app.workers.celery_app._agentic_review")
@patch("app.workers.celery_app.get_adapter")
@patch("app.workers.celery_app.AIReviewer")
@patch("app.workers.celery_app.load_project_config")
def test_shadow_mode_posts_diff_review_and_queues_agent(mock_config, mock_ai, mock_get_adapter, mock_agentic,
                                                        mock_shadow, _gather):
    from app.workers.celery_app import process_review
    mock_get_adapter.return_value = _adapter()
    mock_config.return_value = ReviewConfig(review_mode="shadow", static_analysis=False)
    mock_ai.return_value.review.return_value = [_finding()]
    process_review.run(_payload())
    mock_agentic.assert_not_called()  # not in the posted review
    args = mock_shadow.apply_async.call_args.kwargs["args"]
    assert args[1] == [{"severity": "BUG", "file": "src/BookingService.java", "line": 2, "message": "m", "source": "ai"}]


@patch("app.workers.celery_app.gather_review_context", return_value="")
@patch("app.workers.celery_app.process_shadow_review")
@patch("app.workers.celery_app.get_adapter")
@patch("app.workers.celery_app.AIReviewer")
@patch("app.workers.celery_app.load_project_config")
def test_diff_mode_queues_no_shadow(mock_config, mock_ai, mock_get_adapter, mock_shadow, _gather):
    from app.workers.celery_app import process_review
    mock_get_adapter.return_value = _adapter()
    mock_config.return_value = ReviewConfig(static_analysis=False)
    mock_ai.return_value.review.return_value = []
    process_review.run(_payload())
    mock_shadow.apply_async.assert_not_called()


@patch("app.workers.celery_app.redis")
@patch("app.workers.celery_app.gather_review_context", return_value="")
@patch("app.workers.celery_app._agentic_review")
@patch("app.workers.celery_app.get_adapter")
@patch("app.workers.celery_app.load_project_config")
def test_shadow_task_records_comparison_and_never_touches_the_pr(mock_config, mock_get_adapter, mock_agentic,
                                                                 _gather, mock_redis):
    from app.workers.celery_app import process_shadow_review
    adapter = _adapter()
    adapter.get_diff.return_value = DIFF
    mock_get_adapter.return_value = adapter
    mock_config.return_value = ReviewConfig(review_mode="shadow")
    mock_agentic.return_value = _agent_result([_finding(2), _finding(9)], tool_calls=3)
    posted = [{"severity": "BUG", "file": "src/BookingService.java", "line": 2, "message": "m", "source": "ai"},
              {"severity": "BUG", "file": "src/BookingService.java", "line": 5, "message": "m", "source": "ai"}]

    summary = process_shadow_review.run(_payload(), posted)

    assert summary == {"both": 1, "only_posted": 1, "only_agent": 1, "tool_calls": 3, "error": None}
    key, value = mock_redis.from_url.return_value.set.call_args.args
    assert key == f"shadow_review:ws/repo:7:{'h' * 12}"
    assert json.loads(value)["agent"][1]["line"] == 9
    adapter.post_inline_comment.assert_not_called()
    adapter.post_summary_comment.assert_not_called()
    adapter.set_review_status.assert_not_called()


@patch("app.workers.celery_app.redis")
@patch("app.workers.celery_app.gather_review_context", return_value="")
@patch("app.workers.celery_app._agentic_review", side_effect=AgentReviewFailed("agent ran out of time"))
@patch("app.workers.celery_app.get_adapter")
@patch("app.workers.celery_app.load_project_config")
def test_shadow_task_failure_is_only_recorded(mock_config, mock_get_adapter, _agentic, _gather, _redis):
    from app.workers.celery_app import process_shadow_review
    adapter = _adapter()
    mock_get_adapter.return_value = adapter
    mock_config.return_value = ReviewConfig(review_mode="shadow")
    summary = process_shadow_review.run(_payload(), [])
    assert summary["error"] == "agent ran out of time"
    adapter.set_review_status.assert_not_called()


def test_agent_gets_its_own_time_and_leaves_the_regular_budget_for_fallback():
    from app.workers import celery_app
    with patch("app.workers.celery_app.time.monotonic", return_value=1000.0):
        # soft limit 1050 = 300 base + 10 files x 15 + 600 agentic extra; task started at 1000
        assert celery_app._agent_deadline(2050.0, 1050.0) == 1600.0
        # agent started late (after Semgrep): it still must leave 450s for the fallback
        assert celery_app._agent_deadline(1500.0, 1050.0) == 1050.0


# --- config ---

def test_review_mode_cannot_be_set_from_the_repo(tmp_path):
    from app.config import project_config
    registry = tmp_path / "projects.yml"
    registry.write_text("projects:\n  ws/repo:\n    review_mode: diff\n")
    adapter = MagicMock()
    adapter.get_file_content.return_value = "review_mode: agentic\nagent_max_steps: 500\nai_verify: false\n"
    with patch.object(project_config, "_PROJECTS_CONFIG_PATH", registry):
        config = project_config.load_project_config(adapter, _ctx())
        assert project_config.central_review_mode("ws/repo") == "diff"
    assert config.review_mode == "diff"
    assert config.agent_max_steps == 20
    assert config.ai_verify is False  # ordinary fields still overridable


def test_unknown_review_mode_falls_back_to_diff(tmp_path):
    from app.config import project_config
    registry = tmp_path / "projects.yml"
    registry.write_text("projects:\n  ws/repo:\n    review_mode: deep\n")
    adapter = MagicMock()
    adapter.get_file_content.return_value = None
    with patch.object(project_config, "_PROJECTS_CONFIG_PATH", registry):
        assert project_config.load_project_config(adapter, _ctx()).review_mode == "diff"
        assert project_config.central_review_mode("ws/repo") == "diff"


def test_agent_end_to_end_on_a_real_mirror(tools):
    """Real mirror and tools; only the model is mocked. The tool result the model gets is real code."""
    agent = _agent()
    responses = [_claude(_tool_use("t1", "grep", {"pattern": "int rate\\(", "path_glob": "**/*.java"})),
                 _claude(_text(FINDING))]
    with patch.object(agent._client.beta.messages, "create", side_effect=responses) as create:
        result = agent.review_agentic(_ctx(), ReviewConfig(), tools)
    tool_result = create.call_args_list[1].kwargs["messages"][-1]["content"][0]["content"]
    assert tool_result == "src/dto/Booking.java:3:  int rate() { return r; }"
    assert len(result) == 1 and result.tool_calls == 1
    assert "ws/repo: the PR's repository" in create.call_args_list[0].kwargs["messages"][0]["content"]
