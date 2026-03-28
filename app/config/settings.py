from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # AI provider: "claude" or "glm"
    AI_PROVIDER: str = "claude"

    # Claude (Anthropic)
    ANTHROPIC_API_KEY: str = ""
    AI_MODEL: str = "claude-sonnet-4-6"

    # GLM (Z.AI)
    ZAI_API_KEY: str = ""
    ZAI_BASE_URL: str = "https://api.z.ai/api/paas/v4"
    GLM_MODEL: str = "glm-5-turbo"

    AI_MAX_TOKENS: int = 4096
    AI_MAX_DIFF_TOKENS: int = 8000
    REDIS_URL: str = "redis://localhost:6379/0"
    MAX_DIFF_LINES: int = 500
    AI_DAILY_TOKEN_BUDGET: int = 100_000  # input+output tokens per day; 0 = unlimited


@lru_cache
def get_settings() -> Settings:
    return Settings()
