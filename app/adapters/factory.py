from app.adapters.base import GitPlatform
from app.adapters.bitbucket import BitbucketAdapter
from app.config.credentials import get_credentials
from app.config.settings import Settings


def get_adapter(platform: str, workspace: str, repo_slug: str, settings: Settings) -> GitPlatform:
    if platform == "bitbucket":
        creds = get_credentials("bitbucket", workspace, repo_slug)
        return BitbucketAdapter(
            webhook_secret=creds["webhook_secret"],
            username=creds.get("username", ""),
            app_password=creds.get("app_password", ""),
            api_token=creds.get("api_token", ""),
        )
    raise ValueError(f"Unknown platform: {platform!r}")


def get_spm_adapter(platform: str, access_key: str) -> GitPlatform:
    if platform == "bitbucket":
        return BitbucketAdapter(webhook_secret="", api_token=access_key)
    raise ValueError(f"Unknown platform: {platform!r}")
