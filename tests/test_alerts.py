from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.analyzers.agent_reviewer import AgentReviewer
from app.analyzers.ai_reviewer import AIReviewer
from app.config.settings import Settings
from app.models import PRContext, ReviewConfig
from app.notifiers.alerts import send_alert
from app.workers import celery_app

DIFF = "diff --git a/src/A.java b/src/A.java\n--- a/src/A.java\n+++ b/src/A.java\n@@ -1 +1 @@\n-a\n+b\n"


def _settings(**kw) -> Settings:
    base = dict(ANTHROPIC_API_KEY="k", REDIS_URL="redis://localhost:6379/0", AI_PROVIDER="claude",
                MATTERMOST_WEBHOOK_URL="https://mm.example/hooks/x", AGENT_MIN_TOOL_CALLS=0)
    base.update(kw)
    return Settings(**base)


class _FakeRedis:
    def __init__(self):
        self.keys, self.count = set(), 0

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.keys:
            return None
        self.keys.add(key)
        return True

    def incrby(self, key, n):
        self.count += n
        return self.count

    def get(self, key):
        return self.count

    def __getattr__(self, name):  # expire, hincrby, incr
        return lambda *a, **k: None


@patch("app.notifiers.alerts.mattermost.send_message", return_value=True)
def test_the_same_alert_is_posted_once_per_period(send):
    r = _FakeRedis()
    assert send_alert(_settings(), "agent_limit:steps:ws/repo", "deep review stopped early", "", r)
    assert not send_alert(_settings(), "agent_limit:steps:ws/repo", "deep review stopped early", "", r)
    assert send_alert(_settings(), "agent_limit:steps:ws/other", "deep review stopped early", "", r)
    assert send.call_count == 2


@patch("app.notifiers.alerts.mattermost.send_message")
def test_alerts_off_without_webhook_or_with_zero_period(send):
    assert not send_alert(_settings(MATTERMOST_WEBHOOK_URL=""), "k", "t", "", _FakeRedis())
    assert not send_alert(_settings(ALERT_REPEAT_HOURS=0), "k", "t", "", _FakeRedis())
    send.assert_not_called()


@patch("app.notifiers.alerts.mattermost.send_message", return_value=True)
def test_budget_alerts_at_the_warning_share_and_when_used_up(send):
    reviewer = AIReviewer(_settings(AI_DAILY_TOKEN_BUDGET=1000, AI_BUDGET_ALERT_PERCENT=80))
    reviewer._redis = _FakeRedis()
    reviewer._check_and_record_tokens(700, 0)   # 70%: nothing
    assert send.call_count == 0
    reviewer._check_and_record_tokens(150, 0)   # 85%: warning
    reviewer._check_and_record_tokens(10, 0)    # still under: no repeat
    reviewer._check_and_record_tokens(200, 0)   # over 100%: used up
    texts = [c.kwargs["attachments"][0]["text"] for c in send.call_args_list]
    assert len(texts) == 2 and "85% of the daily AI token budget" in texts[0] and "used up" in texts[1]


def test_agent_reports_the_step_limit_it_hit():
    agent = AgentReviewer(_settings(MATTERMOST_WEBHOOK_URL=""))
    agent._redis = MagicMock(**{"get.return_value": 0, "incrby.return_value": 0})
    usage = SimpleNamespace(input_tokens=1, output_tokens=1)
    tool = SimpleNamespace(type="tool_use", id="a", name="grep", input={"pattern": "x"})
    responses = [SimpleNamespace(content=[tool], stop_reason="tool_use", usage=usage),
                 SimpleNamespace(content=[SimpleNamespace(type="text", text="[]")], stop_reason="end_turn", usage=usage)]
    ctx = PRContext(platform="bitbucket", repo_full_name="ws/repo", pr_id=7, base_sha="m", head_sha="h" * 40,
                    author="dev", title="t", language="java", diff=DIFF, changed_files=["src/A.java"])
    with patch.object(agent._client.beta.messages, "create", side_effect=responses):
        result = agent.review_agentic(ctx, ReviewConfig(agent_max_steps=1), MagicMock(calls=1, **{"run.return_value": "ok"}))
    assert result.limits_hit == {"steps"}


@patch.object(celery_app, "send_alert")
def test_worker_alerts_once_per_repo_and_limit_with_advice(send):
    ctx = PRContext(platform="bitbucket", repo_full_name="ws/admin-portal", pr_id=52, base_sha="m", head_sha="h",
                    author="dev", title="Invoices", language="java", diff=DIFF, changed_files=[])
    results = SimpleNamespace(limits_hit={"steps"})
    celery_app._alert_agent_limits(ctx, ReviewConfig(agent_max_steps=20), results)
    (_, key, title, details), _ = send.call_args
    assert key == "agent_limit:steps:ws/admin-portal" and "admin-portal" in title
    assert "all 20 tool calls" in details and "agent_max_steps" in details and "PR #52" in details
