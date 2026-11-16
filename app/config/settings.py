from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # AI provider: "claude" or "glm"
    AI_PROVIDER: str = "claude"

    # Claude (Anthropic)
    ANTHROPIC_API_KEY: str = ""
    AI_MODEL: str = "claude-sonnet-5-5"

    # GLM (Z.AI)
    ZAI_API_KEY: str = ""
    ZAI_BASE_URL: str = "https://api.z.ai/api/coding/paas/v4"
    GLM_MODEL: str = "glm-5-turbo"

    # Covers thinking + the JSON answer — current Claude models think adaptively,
    # and a cut-off answer is unparseable JSON
    AI_MAX_TOKENS: int = 16000
    # Diff tokens per AI request. Bigger diffs are split, packing whole files into each
    # request, so related files of one PR are reviewed together.
    AI_MAX_DIFF_TOKENS: int = 60_000
    # Full new-version content of changed files sent alongside the diff, per AI request
    # (chars, ~4 chars per token). Files that don't fit are skipped; 0 disables.
    AI_FILE_CONTEXT_MAX_CHARS: int = 120_000
    REDIS_URL: str = "redis://localhost:6379/0"
    MAX_DIFF_LINES: int = 500
    # Per-request cap on the PR intent context: description, commits, Jira ticket and
    # repo guide files (ai_context_files). Chars, ~4 chars per token; 0 disables.
    AI_PR_CONTEXT_MAX_CHARS: int = 40_000
    AI_DAILY_TOKEN_BUDGET: int = 10_000_000  # input+output tokens per day; 0 = unlimited
    # Input tokens served from the provider's prompt cache cost a fraction of normal ones
    # (Claude ~0.1×). They count toward the budget at this weight.
    AI_CACHED_TOKEN_WEIGHT: float = 0.1

    # Agentic review (review_mode: agentic | shadow)
    # Shared shallow clones of each repo's main branch (the server's /opt/projects, mounted
    # read-only), kept current by a cron job outside the bot. A repo that is not here is
    # never reviewed agentically. Empty disables agentic review.
    PROJECTS_SNAPSHOT_DIR: str = "/projects"
    # The bot's own depth-1 clones of other target branches (demo-dev, ...)
    BRANCH_SNAPSHOT_DIR: str = "/repos"
    BRANCH_SNAPSHOT_MAX_AGE_HOURS: float = 6
    # Max seconds for one branch-snapshot clone
    BRANCH_SNAPSHOT_CLONE_TIMEOUT: int = 900
    # Diff tokens per agent conversation. Smaller than AI_MAX_DIFF_TOKENS: the diff is
    # re-sent on every agent step, and the agent reads surrounding code with tools.
    AGENT_MAX_DIFF_TOKENS: int = 30_000
    # Input+output tokens one agentic review may spend before it must give its answer
    AGENT_MAX_TOKENS_PER_REVIEW: int = 800_000
    # Chars of one tool result sent back to the model
    AGENT_TOOL_RESULT_MAX_CHARS: int = 12_000
    # An agent that answers with fewer code lookups than this is sent back once to check
    # callers and definitions first; 0 disables
    AGENT_MIN_TOOL_CALLS: int = 2

    # Jira (optional) — the ticket whose key is in the source branch or PR title is
    # sent to the AI as the change's intent. Empty JIRA_BASE_URL disables it.
    JIRA_BASE_URL: str = ""
    JIRA_EMAIL: str = ""
    JIRA_API_TOKEN: str = ""

    # Mattermost Incoming Webhook URL for author-based PR notifications
    MATTERMOST_WEBHOOK_URL: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
