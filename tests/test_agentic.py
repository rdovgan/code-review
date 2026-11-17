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
from app.context.snapshots import BranchSnapshot, SnapshotUnavailable, current_branch, shared_snapshot
from app.models import Finding, PRContext, ReviewConfig, Severity

DIFF = (
    "diff --git a/src/BookingService.java b/src/BookingService.java\n"
    "--- a/src/BookingService.java\n+++ b/src/BookingService.java\n"
    "@@ -1,3 +1,4 @@\n class BookingService {\n+  int total(Booking b) { return b.nights() * b.rate(); }\n }\n"
)


# --- real shallow snapshots ---

def _git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                   capture_output=True)


def _head(cwd):
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def source_repo(tmp_path):
    """The 'remote': master, plus a demo-dev branch that differs."""
    src = tmp_path / "origin" / "repo"
    (src / "src" / "dto").mkdir(parents=True)
    (src / "src" / "BookingService.java").write_text(
        "class BookingService {\n  int total(Booking b) { return b.nights() * b.rate(); }\n}\n")
    (src / "src" / "dto" / "Booking.java").write_text(
        "class Booking {\n  int nights() { return Math.max(1, n); }\n  int rate() { return r; }\n}\n")
    (src / "src" / "Legacy.java").write_text("class Legacy {}\n")
    _git(src, "init", "-q", "-b", "master")
    _git(src, "add", ".")
    _git(src, "commit", "-q", "-m", "init")
    _git(src, "checkout", "-q", "-b", "demo-dev")
    (src / "src" / "DemoOnly.java").write_text("class DemoOnly {}\n")
    _git(src, "add", ".")
    _git(src, "commit", "-q", "-m", "demo")
    _git(src, "checkout", "-q", "master")
    return src


@pytest.fixture
def projects_dir(tmp_path, source_repo):
    """Like the server's /opt/projects: depth-1 clones of master, named by repo slug."""
    projects = tmp_path / "projects"
    projects.mkdir()
    subprocess.run(["git", "clone", "-q", "--depth", "1", "--branch", "master", f"file://{source_repo}",
                    str(projects / "repo")], check=True, capture_output=True)
    return projects


def test_shared_snapshot_is_found_by_repo_slug(projects_dir):
    assert shared_snapshot(str(projects_dir), "ws/repo") == projects_dir / "repo"
    assert shared_snapshot(str(projects_dir), "ws/not-there") is None
    assert shared_snapshot("", "ws/repo") is None
    assert current_branch(projects_dir / "repo") == "master"


def _branch_snapshot(tmp_path, source_repo, branch="demo-dev", max_age=3600, url=None):
    return BranchSnapshot(str(tmp_path / "bot-snapshots"), "ws/repo", branch,
                          GitCloneSpec(url=url or f"file://{source_repo}"), max_age_seconds=max_age, timeout=60)


def test_branch_snapshot_is_a_shallow_clone_of_that_branch(tmp_path, source_repo):
    path = _branch_snapshot(tmp_path, source_repo).ensure()
    assert path.name == "repo@demo-dev"
    assert (path / "src" / "DemoOnly.java").exists()
    assert current_branch(path) == "demo-dev"
    shallow = subprocess.run(["git", "rev-parse", "--is-shallow-repository"], cwd=path, capture_output=True, text=True)
    assert shallow.stdout.strip() == "true"


def test_branch_snapshot_refresh_reclones_and_keeps_no_history(tmp_path, source_repo):
    snap = _branch_snapshot(tmp_path, source_repo, max_age=0)
    path = snap.ensure()
    _git(source_repo, "checkout", "-q", "demo-dev")
    (source_repo / "src" / "Newer.java").write_text("class Newer {}\n")
    _git(source_repo, "add", ".")
    _git(source_repo, "commit", "-q", "-m", "newer")
    snap.ensure()
    assert (path / "src" / "Newer.java").exists()
    count = subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=path, capture_output=True, text=True)
    assert count.stdout.strip() == "1"  # depth 1 every time: history never accumulates
    assert not path.with_name(path.name + ".old").exists() and not path.with_name(path.name + ".new").exists()


def test_fresh_branch_snapshot_is_not_recloned(tmp_path, source_repo):
    snap = _branch_snapshot(tmp_path, source_repo)
    snap.ensure()
    with patch("app.context.snapshots.subprocess.run") as run:
        snap.ensure()
    run.assert_not_called()


def test_failed_branch_clone_is_not_retried_right_away(tmp_path, source_repo):
    snap = _branch_snapshot(tmp_path, source_repo, branch="no-such-branch")
    with pytest.raises(SnapshotUnavailable, match="failed"):
        snap.ensure()
    with pytest.raises(SnapshotUnavailable, match="last clone failed"):
        snap.ensure()


def test_outdated_branch_snapshot_is_used_when_refresh_fails(tmp_path, source_repo):
    path = _branch_snapshot(tmp_path, source_repo).ensure()
    broken = _branch_snapshot(tmp_path, source_repo, max_age=0, url=str(tmp_path / "gone"))
    assert broken.ensure() == path
    assert (path / "src" / "DemoOnly.java").exists()


def test_clone_credentials_go_to_a_header_not_the_url(tmp_path):
    spec = GitCloneSpec(url="https://bitbucket.org/ws/repo.git", username="x-token-auth", password="s3cret")
    env = BranchSnapshot(str(tmp_path), "ws/repo", "demo-dev", spec, max_age_seconds=1)._env()
    assert env["GIT_CONFIG_KEY_0"] == "http.extraHeader"
    assert env["GIT_CONFIG_VALUE_0"].startswith("Authorization: Basic ")
    assert "s3cret" not in repr(spec)


# --- code tools over a snapshot with the PR laid on top ---

PR_SERVICE = "class BookingService {\n  int total(Booking b) { return b.nights() * b.rate() + fee(); }\n}\n"


@pytest.fixture
def tools(projects_dir):
    fetched = []

    def fetch(path):
        fetched.append(path)
        return "class Added {\n  int rate() { return 2; }\n}\n" if path == "src/Added.java" else None

    view = RepoView("ws/repo", projects_dir / "repo", "the PR's repository",
                    overlay={"src/BookingService.java": PR_SERVICE, "src/Legacy.java": None},
                    lazy_paths={"src/Added.java"}, fetch=fetch)
    t = CodeTools([view, RepoView("ws/core", projects_dir / "repo", "related repository")])
    t.fetched = fetched
    return t


def test_read_file_returns_the_pr_version_of_changed_files(tools):
    assert "+ fee()" in tools.run("read_file", {"path": "src/BookingService.java"})
    out = tools.run("read_file", {"path": "src/dto/Booking.java", "start_line": 2, "end_line": 2})
    assert "    2|   int nights() { return Math.max(1, n); }" in out and "rate()" not in out


def test_files_removed_by_the_pr_are_hidden(tools):
    assert tools.run("read_file", {"path": "src/Legacy.java"}).startswith("ERROR: file not found")
    assert "Legacy.java" not in tools.run("find_files", {"name_glob": "*.java"})
    assert tools.run("grep", {"pattern": "class Legacy"}) == "no matches"


def test_changed_files_not_prefetched_are_fetched_on_read(tools):
    assert "return 2" in tools.run("read_file", {"path": "src/Added.java"})
    tools.run("read_file", {"path": "src/Added.java"})
    assert tools.fetched == ["src/Added.java"]  # cached after the first read
    assert "src/Added.java" in tools.run("find_files", {"name_glob": "Added*"})


def test_grep_searches_the_pr_version_of_changed_files(tools):
    out = tools.run("grep", {"pattern": "rate\\(", "path_glob": "**/*.java"}).splitlines()
    assert out == ["src/BookingService.java:2:  int total(Booking b) { return b.nights() * b.rate() + fee(); }",
                   "src/dto/Booking.java:3:  int rate() { return r; }"]
    assert tools.run("grep", {"pattern": "fee()", "fixed_string": True}).startswith("src/BookingService.java:2:")
    assert tools.run("grep", {"pattern": "doesNotExist"}) == "no matches"


def test_context_repo_is_read_as_is(tools):
    assert "+ fee()" not in tools.run("read_file", {"path": "src/BookingService.java", "repo": "ws/core"})
    assert "src/Legacy.java" in tools.run("find_files", {"name_glob": "*.java", "repo": "ws/core"})


def test_list_dir(tools):
    assert tools.run("list_dir", {"path": "src"}).splitlines() == ["Added.java", "BookingService.java", "dto/"]
    assert tools.run("list_dir", {}).splitlines() == ["src/"]


def test_tools_cannot_leave_the_repository(tools):
    for path in ("../../etc/passwd", ".git/config", "src/../../outside"):
        assert tools.run("read_file", {"path": path}).startswith("ERROR: file not found"), path


def test_tool_errors_are_returned_as_text(tools):
    assert tools.run("read_file", {"path": "x", "repo": "ws/unknown"}).startswith("ERROR: unknown repo")
    assert tools.run("delete_everything", {}).startswith("ERROR: unknown tool")
    assert tools.run("grep", None).startswith("ERROR")
    assert tools.run("list_dir", {"path": "nope"}).startswith("ERROR: directory not found")
    assert tools.calls == 4


def test_tool_output_is_capped(projects_dir):
    capped = CodeTools([RepoView("ws/repo", projects_dir / "repo", "")], max_result_chars=40)
    assert capped.run("read_file", {"path": "src/dto/Booking.java"}).endswith("[output truncated — narrow the request]")


# --- agent loop ---

def _settings(**kw) -> Settings:
    base = dict(ANTHROPIC_API_KEY="k", REDIS_URL="redis://localhost:6379/0", AI_PROVIDER="claude",
                AI_DAILY_TOKEN_BUDGET=0, AGENT_MIN_TOOL_CALLS=0)  # the investigate-first rule has its own tests
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


def _sent_text(message) -> str:
    """Text of a sent message — a string, or blocks once it carries a cache breakpoint."""
    content = message["content"]
    return content if isinstance(content, str) else "".join(b.get("text", "") for b in content if isinstance(b, dict))


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
    assert "You can read the repository" in first["system"][0]["text"]
    assert _sent_text(first["messages"][0]).startswith("PR_CONTEXT — intent")
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
    assert "tool budget for this review is used up" in _sent_text(last_user)


@pytest.mark.parametrize("responses, reason", [
    ([_claude(_text("not json")), _claude(_text("still prose"))], "not valid JSON"),
    ([_claude(stop="refusal")], "declined"),
    (RuntimeError("api down"), "AI request failed"),
])
def test_agent_failures_raise_for_fallback(responses, reason):
    agent = _agent()
    kwargs = {"side_effect": responses} if isinstance(responses, Exception) else {"side_effect": responses}
    with patch.object(agent._client.beta.messages, "create", **kwargs):
        with pytest.raises(AgentReviewFailed, match=reason):
            agent.review_agentic(_ctx(), ReviewConfig(), MagicMock(calls=0))


def test_agent_answering_in_prose_is_asked_once_for_json():
    agent = _agent()
    tools = MagicMock(calls=1)
    tools.run.return_value = "ok"
    responses = [_claude(_tool_use("a", "grep", {"pattern": "x"})),
                 _claude(_text("I checked the callers; the rate can be zero for comp bookings.")),
                 _claude(_text(FINDING))]
    with patch.object(agent._client.beta.messages, "create", side_effect=responses) as create:
        result = agent.review_agentic(_ctx(), ReviewConfig(), tools)
    assert [f.line for f in result] == [2]
    last = create.call_args_list[2].kwargs["messages"]
    assert last[-2]["role"] == "assistant" and "not a JSON array" in _sent_text(last[-1])


def test_agent_asked_for_json_may_not_call_tools_again():
    agent = _agent()
    responses = [_claude(_text("prose")), _claude(_tool_use("a", "grep", {"pattern": "x"}))]
    with patch.object(agent._client.beta.messages, "create", side_effect=responses):
        with pytest.raises(AgentReviewFailed, match="kept calling tools"):
            agent.review_agentic(_ctx(), ReviewConfig(), MagicMock(calls=0))


@pytest.mark.parametrize("text", [
    "Summary of what I checked.\n\n```json\n" + FINDING + "\n```\nDone.",
    json.dumps({"findings": json.loads(FINDING)}),
])
def test_findings_are_read_from_wrapped_answers(text):
    assert _agent()._parse_response(text) == json.loads(FINDING)


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


def test_agent_end_to_end_on_a_real_snapshot(tools):
    """Real snapshot and tools; only the model is mocked. The tool result the model gets is real code."""
    agent = _agent()
    responses = [_claude(_tool_use("t1", "grep", {"pattern": "int rate\\(", "path_glob": "src/dto/**"})),
                 _claude(_text(FINDING))]
    with patch.object(agent._client.beta.messages, "create", side_effect=responses) as create:
        result = agent.review_agentic(_ctx(), ReviewConfig(), tools)
    tool_result = create.call_args_list[1].kwargs["messages"][-1]["content"][0]["content"]
    assert tool_result == "src/dto/Booking.java:3:  int rate() { return r; }"
    assert len(result) == 1 and result.tool_calls == 1
    assert "ws/repo: the PR's repository" in _sent_text(create.call_args_list[0].kwargs["messages"][0])


# --- worker: building the agent's view ---

def test_agentic_needs_the_repo_in_the_projects_folder(projects_dir):
    from app.workers import celery_app
    with patch.object(celery_app.settings, "PROJECTS_SNAPSHOT_DIR", str(projects_dir)):
        with pytest.raises(AgentReviewFailed, match="not in"):
            celery_app._build_code_tools(MagicMock(), _ctx(repo_full_name="ws/elsewhere"), ReviewConfig(),
                                         deadline=10**9, file_contents={}, raw_diff=DIFF)


def test_pr_into_the_snapshot_branch_uses_the_shared_snapshot(projects_dir):
    from app.workers import celery_app
    adapter = MagicMock()
    config = ReviewConfig(context_repos=["ws/repo", "ws/missing"])
    with patch.object(celery_app.settings, "PROJECTS_SNAPSHOT_DIR", str(projects_dir)):
        tools = celery_app._build_code_tools(adapter, _ctx(target_branch="master"), config, deadline=10**9,
                                             file_contents={"src/BookingService.java": PR_SERVICE}, raw_diff=DIFF)
    adapter.git_clone_spec.assert_not_called()  # nothing cloned
    assert "+ fee()" in tools.run("read_file", {"path": "src/BookingService.java"})
    assert "master (shared snapshot" in tools.describe()
    assert "ws/missing" not in tools.describe()


def test_pr_into_another_branch_gets_a_bot_snapshot_of_it(tmp_path, projects_dir, source_repo):
    from app.workers import celery_app
    adapter = MagicMock()
    adapter.git_clone_spec.return_value = GitCloneSpec(url=f"file://{source_repo}")
    with patch.object(celery_app.settings, "PROJECTS_SNAPSHOT_DIR", str(projects_dir)), \
         patch.object(celery_app.settings, "BRANCH_SNAPSHOT_DIR", str(tmp_path / "bot")):
        tools = celery_app._build_code_tools(adapter, _ctx(target_branch="demo-dev"), ReviewConfig(),
                                             deadline=10**9, file_contents={}, raw_diff=DIFF)
    assert "src/DemoOnly.java" in tools.run("find_files", {"name_glob": "DemoOnly*"})
    assert not (projects_dir / "repo" / "src" / "DemoOnly.java").exists()  # shared snapshot untouched
    assert current_branch(projects_dir / "repo") == "master"


def test_pr_overlay_hides_deleted_and_renamed_away_files():
    from app.workers import celery_app
    raw = ("diff --git a/src/Old.java b/src/New.java\nsimilarity index 90%\nrename from src/Old.java\n"
           "rename to src/New.java\n@@ -1 +1 @@\n-a\n+b\n"
           "diff --git a/src/Gone.java b/src/Gone.java\ndeleted file mode 100644\n--- a/src/Gone.java\n"
           "+++ /dev/null\n@@ -1 +0,0 @@\n-x\n")
    ctx = _ctx(changed_files=["src/New.java", "src/Big.java"])
    overlay, lazy = celery_app._pr_overlay(ctx, raw, {"src/New.java": "b\n"})
    assert overlay == {"src/Old.java": None, "src/New.java": "b\n", "src/Gone.java": None}
    assert lazy == {"src/Big.java"}


# --- depth: the agent must look at the code ---

def test_agent_that_answers_without_reading_code_is_sent_back_once():
    agent = _agent(_settings(AGENT_MIN_TOOL_CALLS=2))
    tools = MagicMock(calls=2)
    tools.run.return_value = "src/X.java:3: caller"
    responses = [_claude(_text("[]")),                                   # answers from the diff alone
                 _claude(_tool_use("a", "grep", {"pattern": "total\\("})),
                 _claude(_tool_use("b", "read_file", {"path": "src/dto/Booking.java"})),
                 _claude(_text(FINDING))]
    with patch.object(agent._client.beta.messages, "create", side_effect=responses) as create:
        result = agent.review_agentic(_ctx(), ReviewConfig(), tools)
    assert len(result) == 1 and tools.run.call_count == 2
    second = create.call_args_list[1].kwargs["messages"]
    assert second[-2]["role"] == "assistant"
    assert "answered after 0 code lookup(s)" in second[-1]["content"][0]["text"]


def test_investigate_first_is_asked_only_once():
    agent = _agent(_settings(AGENT_MIN_TOOL_CALLS=2))
    with patch.object(agent._client.beta.messages, "create",
                      side_effect=[_claude(_text("[]")), _claude(_text("[]"))]) as create:
        result = agent.review_agentic(_ctx(), ReviewConfig(), MagicMock(calls=0))
    assert list(result) == [] and create.call_count == 2


# --- cost: prompt caching and how cached tokens count ---

def test_claude_agent_marks_system_and_newest_message_for_caching():
    agent = _agent()
    tools = MagicMock(calls=1)
    tools.run.return_value = "ok"
    responses = [_claude(_tool_use("a", "grep", {"pattern": "x"})), _claude(_text("[]"))]
    with patch.object(agent._client.beta.messages, "create", side_effect=responses) as create:
        agent.review_agentic(_ctx(), ReviewConfig(), tools)
    second = create.call_args_list[1].kwargs
    assert second["system"][0]["cache_control"] == {"type": "ephemeral"}
    marked = [m for m in second["messages"] if isinstance(m["content"], list)
              and any(isinstance(b, dict) and "cache_control" in b for b in m["content"])]
    assert marked == [second["messages"][-1]]  # only the newest message, never accumulating
    assert second["messages"][-1]["content"][-1]["type"] == "tool_result"


def test_cached_tokens_count_at_their_weight():
    from types import SimpleNamespace
    agent = _agent(_settings(AI_CACHED_TOKEN_WEIGHT=0.1))
    usage = SimpleNamespace(input_tokens=200, cache_creation_input_tokens=1000, cache_read_input_tokens=30000,
                            output_tokens=50)
    response = SimpleNamespace(content=[_text("[]")], stop_reason="end_turn", usage=usage)
    with patch.object(agent._client.beta.messages, "create", return_value=response):
        agent.review_agentic(_ctx(), ReviewConfig(), MagicMock(calls=0))
    agent._redis.incrby.assert_called_once_with(agent._budget_key(), 1200 + 50 + 3000)
    agent._redis.hincrby.assert_any_call("metrics:ai_tokens_by_provider", "claude:cached", 30000)


def test_glm_cached_prompt_tokens_are_split_out():
    from types import SimpleNamespace
    agent = _agent(_settings(AI_PROVIDER="glm", ZAI_API_KEY="z", AI_CACHED_TOKEN_WEIGHT=0.1))
    usage = SimpleNamespace(prompt_tokens=40000, completion_tokens=100,
                            prompt_tokens_details=SimpleNamespace(cached_tokens=36000))
    final = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="[]", tool_calls=None))], usage=usage)
    with patch.object(agent._client.chat.completions, "create", return_value=final):
        agent.review_agentic(_ctx(), ReviewConfig(), MagicMock(calls=0))
    agent._redis.incrby.assert_called_once_with(agent._budget_key(), 4000 + 100 + 3600)


# --- bounded time: parts in parallel, kept on failure, sized for GLM (PR #52) ---

import threading  # noqa: E402
import time  # noqa: E402

from celery.exceptions import SoftTimeLimitExceeded  # noqa: E402

from app.analyzers import agent_reviewer as agent_mod  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_glm_thinking_state():
    agent_mod._glm_thinking_param_rejected, agent_mod._glm_slowest_answer = False, 0.0
    yield
    agent_mod._glm_thinking_param_rejected, agent_mod._glm_slowest_answer = False, 0.0


class APITimeoutError(Exception):
    """Named like the SDKs' timeout error: classify_error goes by the class name."""


class APIConnectionError(Exception):
    pass


class BadRequestError(Exception):
    status_code = 400


def _file_diff(name: str, lines: int = 40) -> str:
    return (f"diff --git a/src/{name}.java b/src/{name}.java\n--- a/src/{name}.java\n+++ b/src/{name}.java\n"
            f"@@ -1,1 +1,{lines + 1} @@\n class {name} {{\n" + "".join(f"+  int v{j};\n" for j in range(lines)))


TWO_PARTS = _file_diff("A") + _file_diff("B")


def _finding_on(name: str, line: int = 2, severity: str = "BUG") -> str:
    return json.dumps([{"severity": severity, "file": f"src/{name}.java", "line": line, "message": "m",
                        "suggestion": "s"}])


def _two_part_agent(**kw):
    # Each file fits a part on its own, both together do not
    agent = _agent(_settings(AGENT_MAX_DIFF_TOKENS=200, **kw))
    assert len(agent._pack(TWO_PARTS, 800)) == 2
    return agent


def _part_of(kwargs) -> str:
    first = _sent_text(kwargs["messages"][0])
    return "B" if "(part 2 of 2)" in first else "A"


def test_failed_part_keeps_finished_parts_findings():
    agent = _two_part_agent()

    def create(**kw):
        if _part_of(kw) == "B":
            raise RuntimeError("api down")
        return _claude(_text(_finding_on("A")))

    with patch.object(agent._client.beta.messages, "create", side_effect=create):
        result = agent.review_agentic(_ctx(diff=TWO_PARTS), ReviewConfig(), MagicMock(calls=0))
    assert [f.file for f in result] == ["src/A.java"]
    assert result.chunks_reviewed == 1 and result.failed_parts == 1 and result.failed_part_numbers == [2]
    assert "src/B.java" in result.unreviewed_diff and "src/A.java" not in result.unreviewed_diff
    assert result.failure_reason == "AI request failed: RuntimeError"


def test_all_parts_failed_raises_for_fallback():
    agent = _two_part_agent()
    with patch.object(agent._client.beta.messages, "create", side_effect=RuntimeError("api down")):
        with pytest.raises(AgentReviewFailed, match="AI request failed"):
            agent.review_agentic(_ctx(diff=TWO_PARTS), ReviewConfig(), MagicMock(calls=0))


def test_parts_run_concurrently_with_the_same_deadline():
    agent = _two_part_agent()
    barrier = threading.Barrier(2, timeout=5)
    timeouts = []

    def create(**kw):
        barrier.wait()  # breaks (and the part fails) unless both parts are in flight at once
        timeouts.append(kw["timeout"])
        return _claude(_text(_finding_on(_part_of(kw))))

    deadline = time.monotonic() + 1000
    with patch.object(agent._client.beta.messages, "create", side_effect=create):
        result = agent.review_agentic(_ctx(diff=TWO_PARTS), ReviewConfig(), MagicMock(calls=0), deadline=deadline)
    assert result.chunks_reviewed == 2 and not result.unreviewed_diff
    assert [f.file for f in result] == ["src/A.java", "src/B.java"]  # submission order, not finish order
    assert len(timeouts) == 2 and all(t <= deadline - time.monotonic() for t in timeouts)


def test_queued_part_gets_share_of_remaining_time():
    agent = _two_part_agent(AGENT_PARALLEL_PARTS=1, AGENT_STEP_TIMEOUT_SECONDS=1000)
    timeouts = {}

    def create(**kw):
        timeouts[_part_of(kw)] = kw["timeout"]
        return _claude(_text("[]"))

    with patch.object(agent._client.beta.messages, "create", side_effect=create):
        agent.review_agentic(_ctx(diff=TWO_PARTS), ReviewConfig(), MagicMock(calls=0),
                             deadline=time.monotonic() + 1000)
    assert 400 < timeouts["A"] <= 410   # half of the window, minus the 90 s answer reserve
    assert timeouts["B"] > 800          # the last part gets all that is left


def test_part_without_viable_window_goes_to_unreviewed_without_calls():
    agent = _two_part_agent(AGENT_PARALLEL_PARTS=1)
    clock = {"now": 1000.0}

    def create(**kw):
        clock["now"] += 300  # part 1 eats into part 2's share
        return _claude(_text("[]"))

    with patch.object(agent._client.beta.messages, "create", side_effect=create) as mock_create, \
         patch("app.analyzers.agent_reviewer.time.monotonic", side_effect=lambda: clock["now"]):
        result = agent.review_agentic(_ctx(diff=TWO_PARTS), ReviewConfig(), MagicMock(calls=0), deadline=1500.0)
    assert mock_create.call_count == 1
    assert result.chunks_reviewed == 1 and result.failure_reason == "no time left for this part"
    assert "src/B.java" in result.unreviewed_diff


def test_no_viable_window_at_start_raises_not_enough_time():
    agent = _agent()
    with patch.object(agent._client.beta.messages, "create") as mock_create:
        with pytest.raises(AgentReviewFailed, match="not enough time left"):
            agent.review_agentic(_ctx(), ReviewConfig(), MagicMock(calls=0), deadline=time.monotonic() + 100)
    mock_create.assert_not_called()


def _sized_file(name: str, size: int) -> str:
    head = f"diff --git a/{name} b/{name}\n"
    return head + "+" + "x" * (size - len(head) - 2) + "\n"


def test_pack_balances_parts():
    files = [_sized_file(f"F{i}.java", 30_000) for i in range(4)]
    parts = _agent()._pack("".join(files), 100_000)
    assert [len(p) for p in parts] == [60_000, 60_000]  # greedy would give 90k + 30k


def test_pack_falls_back_to_greedy_when_balanced_makes_more_parts():
    diff = _sized_file("A.java", 60_000) + _sized_file("B.java", 10_000) + _sized_file("C.java", 55_000)
    parts = _agent()._pack(diff, 70_000)
    assert [len(p) for p in parts] == [70_000, 55_000]


def test_final_reserve_by_provider_and_thinking_state():
    glm = _settings(AI_PROVIDER="glm", ZAI_API_KEY="z")
    assert agent_mod._final_reserve(_settings(), "claude") == 90
    assert agent_mod._final_reserve(glm, "glm") == 120
    assert agent_mod._final_reserve(_settings(AI_PROVIDER="glm", GLM_ANSWER_THINKING="enabled"), "glm") == 270
    agent_mod._glm_slowest_answer = 100.0  # reserve covers the slowest answer seen + 30% + 15 s
    assert agent_mod._final_reserve(glm, "glm") == 145
    assert agent_mod._answer_floor(145) == pytest.approx(145 * 2 / 3)
    agent_mod._glm_slowest_answer = 400.0
    assert agent_mod._final_reserve(glm, "glm") == 270
    agent_mod._glm_thinking_param_rejected = True  # Z.AI rejected the parameter
    agent_mod._glm_slowest_answer = 0.0
    assert agent_mod._final_reserve(glm, "glm") == 270
    assert agent_mod._final_reserve(_settings(AGENT_FINAL_ANSWER_RESERVE_SECONDS=45), "glm") == 45


def test_tool_turn_timeout_goes_to_final_answer():
    agent = _agent()
    with patch.object(agent._client.beta.messages, "create",
                      side_effect=[APITimeoutError("read timeout"), _claude(_text(FINDING))]) as create:
        result = agent.review_agentic(_ctx(), ReviewConfig(), MagicMock(calls=0))
    assert len(result) == 1 and result.limits_hit == {"time"}
    answer = create.call_args_list[1].kwargs
    assert "tool budget for this review is used up" in _sent_text(answer["messages"][-1])
    assert answer["tool_choice"] == {"type": "none"}


def test_tool_turn_below_floor_is_not_sent():
    """A turn as slow as the slowest one so far would not fit before the answer reserve: answer instead."""
    agent = _agent()
    clock = {"now": 1000.0}
    responses = iter([_claude(_tool_use("a", "grep", {"pattern": "x"})), _claude(_text(FINDING))])

    def create(**kw):
        clock["now"] += 200
        return next(responses)

    tools = MagicMock(calls=1)
    tools.run.return_value = "ok"
    with patch.object(agent._client.beta.messages, "create", side_effect=create) as mock_create, \
         patch("app.analyzers.agent_reviewer.time.monotonic", side_effect=lambda: clock["now"]):
        result = agent.review_agentic(_ctx(), ReviewConfig(), tools, deadline=1400.0)
    # 110 s were left for tools (1400 - 90 - 1200), less than the 200 s the first turn took
    assert mock_create.call_count == 2 and len(result) == 1 and result.limits_hit == {"time"}
    assert mock_create.call_args_list[1].kwargs["tool_choice"] == {"type": "none"}


def test_final_answer_timeout_fails_only_that_part():
    agent = _two_part_agent()

    def create(**kw):
        if _part_of(kw) == "A":
            return _claude(_text(_finding_on("A")))
        if kw.get("tool_choice"):
            raise APITimeoutError("read timeout")
        return _claude(_tool_use("b", "grep", {"pattern": "x"}))

    tools = MagicMock(calls=1)
    tools.run.return_value = "ok"
    with patch.object(agent._client.beta.messages, "create", side_effect=create):
        result = agent.review_agentic(_ctx(diff=TWO_PARTS), ReviewConfig(agent_max_steps=1), tools)
    assert [f.file for f in result] == ["src/A.java"]
    assert result.failure_reason == "agent's final answer did not arrive in time"


def _glm_turn(content=None, tool_calls=None, reasoning=0, finish="stop"):
    usage = SimpleNamespace(prompt_tokens=50, completion_tokens=5,
                            completion_tokens_details=SimpleNamespace(reasoning_tokens=reasoning))
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=finish)], usage=usage)


def _glm_tool_call():
    return [SimpleNamespace(id="c1", function=SimpleNamespace(name="grep", arguments='{"pattern": "x"}'))]


def _glm_agent():
    return _agent(_settings(AI_PROVIDER="glm", ZAI_API_KEY="z"))


def test_glm_answer_disables_thinking_only_on_forced_calls():
    agent = _glm_agent()
    tools = MagicMock(calls=1)
    tools.run.return_value = "ok"
    with patch.object(agent._client.chat.completions, "create",
                      side_effect=[_glm_turn(tool_calls=_glm_tool_call()), _glm_turn(FINDING)]) as create:
        result = agent.review_agentic(_ctx(), ReviewConfig(agent_max_steps=1), tools)
    assert len(result) == 1
    assert "extra_body" not in create.call_args_list[0].kwargs  # tool turns think as before
    assert create.call_args_list[1].kwargs["extra_body"] == {"thinking": {"type": "disabled"}}
    assert not agent_mod._glm_thinking_param_rejected and agent._final_reserve() == 120


@patch("app.analyzers.agent_reviewer.send_alert")
def test_glm_thinking_param_400_retries_without_and_remembers(send):
    agent = _glm_agent()
    tools = MagicMock(calls=1)
    tools.run.return_value = "ok"
    responses = [_glm_turn(tool_calls=_glm_tool_call()), BadRequestError("unknown field thinking"), _glm_turn(FINDING)]
    with patch.object(agent._client.chat.completions, "create", side_effect=responses) as create:
        result = agent.review_agentic(_ctx(), ReviewConfig(agent_max_steps=1), tools)
    assert len(result) == 1 and create.call_count == 3
    assert "extra_body" not in create.call_args_list[2].kwargs
    assert agent_mod._glm_thinking_param_rejected and agent._final_reserve() == 270
    assert send.call_args.args[1].startswith("glm_thinking_toggle_unsupported:")


@patch("app.analyzers.agent_reviewer.send_alert")
def test_glm_reasoning_with_thinking_off_keeps_the_switch_and_no_alert(send):
    """Z.AI honours thinking-off yet reasons ~1k tokens on long conversations: not a failure."""
    agent = _glm_agent()
    tools = MagicMock(calls=1)
    tools.run.return_value = "ok"
    responses = [_glm_turn(tool_calls=_glm_tool_call()), _glm_turn(FINDING, reasoning=1019),
                 _glm_turn(tool_calls=_glm_tool_call()), _glm_turn(FINDING)]
    with patch.object(agent._client.chat.completions, "create", side_effect=responses) as create:
        agent.review_agentic(_ctx(), ReviewConfig(agent_max_steps=1), tools)
        agent.review_agentic(_ctx(), ReviewConfig(agent_max_steps=1), tools)
    assert create.call_args_list[3].kwargs["extra_body"] == {"thinking": {"type": "disabled"}}
    assert agent._final_reserve() == 120
    send.assert_not_called()


def test_glm_slow_answer_raises_the_reserve_to_cover_it():
    agent = _glm_agent()
    tools = MagicMock(calls=1)
    tools.run.return_value = "ok"
    clock = {"now": 1000.0}
    responses = iter([_glm_turn(tool_calls=_glm_tool_call()), _glm_turn(FINDING)])

    def create(**kw):
        clock["now"] += 100 if "extra_body" in kw else 5
        return next(responses)

    with patch.object(agent._client.chat.completions, "create", side_effect=create), \
         patch("app.analyzers.agent_reviewer.time.monotonic", side_effect=lambda: clock["now"]):
        agent.review_agentic(_ctx(), ReviewConfig(agent_max_steps=1), tools)
    assert agent._final_reserve() == pytest.approx(100 * 1.3 + 15)


def test_stray_tool_calls_in_forced_mode_get_error_and_one_more_turn():
    agent = _agent()
    tools = MagicMock(calls=1)
    tools.run.return_value = "ok"
    responses = [_claude(_tool_use("a", "grep", {"pattern": "x"})),
                 _claude(_tool_use("b", "grep", {"pattern": "y"})),   # asked to answer, looks up instead
                 _claude(_text(FINDING))]
    with patch.object(agent._client.beta.messages, "create", side_effect=responses) as create:
        result = agent.review_agentic(_ctx(), ReviewConfig(agent_max_steps=1), tools)
    assert len(result) == 1 and tools.run.call_count == 1
    refused = create.call_args_list[2].kwargs["messages"][-1]["content"][0]
    assert refused["tool_use_id"] == "b" and refused["content"] == "ERROR: no more lookups, answer now"


def test_excess_lookups_in_one_turn_are_refused():
    agent = _agent()
    tools = MagicMock(calls=2)
    tools.run.return_value = "ok"
    three = _claude(*[_tool_use(f"t{k}", "grep", {"pattern": str(k)}) for k in range(3)])
    with patch.object(agent._client.beta.messages, "create", side_effect=[three, _claude(_text("[]"))]) as create:
        result = agent.review_agentic(_ctx(), ReviewConfig(agent_max_steps=2), tools)
    assert tools.run.call_count == 2 and result.tool_calls == 2
    results = create.call_args_list[1].kwargs["messages"][-2]["content"]
    assert results[2]["content"] == "ERROR: lookup budget used, answer now"


def test_length_with_empty_answer_reported_as_cut_off():
    agent = _agent()
    cut = _claude(stop="max_tokens")
    with patch.object(agent._client.beta.messages, "create", side_effect=[cut, cut]) as create:
        with pytest.raises(AgentReviewFailed, match=r"cut off \(AGENT_MAX_TOKENS\)"):
            agent.review_agentic(_ctx(), ReviewConfig(), MagicMock(calls=0))
    assert "cut off" in _sent_text(create.call_args_list[1].kwargs["messages"][-1])


def test_shared_token_budget_across_parts():
    agent = _two_part_agent(AGENT_PARALLEL_PARTS=1, AGENT_MAX_TOKENS_PER_REVIEW=150)  # each turn costs 110
    tools = MagicMock(calls=1)
    tools.run.return_value = "ok"
    first_messages = {}

    def create(**kw):
        part = _part_of(kw)
        first_messages.setdefault(part, _sent_text(kw["messages"][-1]))
        if part == "A" and not kw.get("tool_choice"):
            return _claude(_tool_use("a", "grep", {"pattern": "x"}))
        return _claude(_text("[]"))

    with patch.object(agent._client.beta.messages, "create", side_effect=create):
        result = agent.review_agentic(_ctx(diff=TWO_PARTS), ReviewConfig(), tools)
    assert result.chunks_reviewed == 2 and result.limits_hit == {"tokens"}
    # Part 1 spent the review's allowance: part 2 is asked for its answer right away
    assert "tool budget for this review is used up" in first_messages["B"]


def _raise_wrapped_soft_limit(**kw):
    try:
        raise SoftTimeLimitExceeded()
    except SoftTimeLimitExceeded as exc:
        raise APIConnectionError("Connection error.") from exc


@pytest.mark.parametrize("side_effect", [SoftTimeLimitExceeded(), _raise_wrapped_soft_limit])
def test_soft_time_limit_is_not_wrapped_as_agent_failure(side_effect):
    agent = _agent()
    with patch.object(agent._client.beta.messages, "create", side_effect=side_effect) as create:
        with pytest.raises(SoftTimeLimitExceeded):
            agent.review_agentic(_ctx(), ReviewConfig(), MagicMock(calls=0))
    assert create.call_count == 1  # a connection error caused by the soft limit is not retried


def test_tools_reraise_soft_time_limit(projects_dir):
    def fetch(path):
        raise SoftTimeLimitExceeded()

    view = RepoView("ws/repo", projects_dir / "repo", "", lazy_paths={"src/Late.java"}, fetch=fetch)
    with pytest.raises(SoftTimeLimitExceeded):
        CodeTools([view]).run("read_file", {"path": "src/Late.java"})


def test_code_tools_overlay_grep_during_lazy_fetch_is_safe(projects_dir):
    lazy = {f"src/Lazy{i}.java" for i in range(40)}

    def fetch(path):
        time.sleep(0.001)
        return f"class {path} {{ int rate() {{ return 1; }} }}\n"

    tools = CodeTools([RepoView("ws/repo", projects_dir / "repo", "", overlay={"src/BookingService.java": PR_SERVICE},
                                lazy_paths=lazy, fetch=fetch)])
    errors = []

    def reader():
        for path in sorted(lazy):
            tools.run("read_file", {"path": path})

    def grepper():
        for _ in range(40):
            out = tools.run("grep", {"pattern": "rate\\("})
            if out.startswith("ERROR"):
                errors.append(out)

    threads = [threading.Thread(target=reader), threading.Thread(target=grepper), threading.Thread(target=grepper)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and tools.calls == 40 + 80


@patch("app.workers.celery_app.metrics")
@patch("app.workers.celery_app.redis")
@patch("app.workers.celery_app.gather_review_context", return_value="")
@patch("app.workers.celery_app._agentic_review")
@patch("app.workers.celery_app.get_adapter")
@patch("app.workers.celery_app.load_project_config")
def test_shadow_partial_recorded_with_partial_error(mock_config, mock_get_adapter, mock_agentic, _gather, _redis,
                                                    mock_metrics):
    from app.workers.celery_app import process_shadow_review
    adapter = _adapter()
    adapter.get_diff.return_value = DIFF
    mock_get_adapter.return_value = adapter
    mock_config.return_value = ReviewConfig(review_mode="shadow")
    result = _agent_result([_finding(2)], tool_calls=4)
    result.unreviewed_diff, result.failed_parts, result.failure_reason = "diff --git a/x b/x\n", 1, "no time left for this part"
    mock_agentic.return_value = result
    summary = process_shadow_review.run(_payload(), [])
    assert summary["error"] == "partial: 1 part(s): no time left for this part"
    assert summary["tool_calls"] == 4
    mock_metrics.record_agentic.assert_called_once_with("shadow_partial")


@patch("app.analyzers.agent_reviewer.send_alert")
def test_glm_answer_timeout_raises_reserve_and_keeps_thinking_off(send):
    agent = _glm_agent()
    tools = MagicMock(calls=1)
    tools.run.return_value = "ok"
    responses = [_glm_turn(tool_calls=_glm_tool_call()), APITimeoutError("read timeout")]
    with patch.object(agent._client.chat.completions, "create", side_effect=responses):
        with pytest.raises(agent_mod.AgentReviewFailed):
            agent.review_agentic(_ctx(), ReviewConfig(agent_max_steps=1), tools)
    assert not agent_mod._glm_thinking_param_rejected and agent._final_reserve() == 270
    assert send.call_args.args[1].startswith("glm_slow_final_answer:")


@patch("app.analyzers.agent_reviewer.send_alert")
def test_glm_400_also_without_thinking_param_keeps_thinking_state(send):
    agent = _glm_agent()
    tools = MagicMock(calls=1)
    tools.run.return_value = "ok"
    responses = [_glm_turn(tool_calls=_glm_tool_call()), BadRequestError("sensitive content"),
                 BadRequestError("sensitive content")]
    with patch.object(agent._client.chat.completions, "create", side_effect=responses):
        with pytest.raises(agent_mod.AgentReviewFailed):
            agent.review_agentic(_ctx(), ReviewConfig(agent_max_steps=1), tools)
    # The 400 was not about the switch: thinking-off stays on, no alert
    assert not agent_mod._glm_thinking_param_rejected and agent._final_reserve() == 120
    send.assert_not_called()
