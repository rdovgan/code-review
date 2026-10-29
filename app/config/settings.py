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
    AI_MAX_DIFF_TOKENS: int = 8000
    # Full new-version content of changed files sent alongside the diff, per AI request
    # (chars, ~4 chars per token). Files that don't fit are skipped; 0 disables.
    AI_FILE_CONTEXT_MAX_CHARS: int = 120_000
    REDIS_URL: str = "redis://localhost:6379/0"
    MAX_DIFF_LINES: int = 500
    AI_DAILY_TOKEN_BUDGET: int = 10_000_000  # input+output tokens per day; 0 = unlimited

    # Mattermost Incoming Webhook URL for author-based PR notifications
    MATTERMOST_WEBHOOK_URL: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
