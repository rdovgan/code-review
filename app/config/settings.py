from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    ANTHROPIC_API_KEY: str = ""
    AI_MODEL: str = "claude-sonnet-4-6"
    AI_MAX_TOKENS: int = 4096
    AI_MAX_DIFF_TOKENS: int = 8000
    REDIS_URL: str = "redis://localhost:6379/0"
    BITBUCKET_WEBHOOK_SECRET: str = ""
    BITBUCKET_USERNAME: str = ""
    BITBUCKET_APP_PASSWORD: str = ""
    MAX_DIFF_LINES: int = 500


@lru_cache
def get_settings() -> Settings:
    return Settings()
