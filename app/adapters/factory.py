from app.adapters.base import GitPlatform
from app.adapters.bitbucket import BitbucketAdapter
from app.config.credentials import get_credentials
from app.config.settings import Settings


def get_adapter(platform: str, workspace: str, repo_slug: str, settings: Settings) -> GitPlatform:
    if platform == "bitbucket":
        creds = get_credentials("bitbucket", workspace, repo_slug)
        return BitbucketAdapter(
            username=creds["username"],
            app_password=creds["app_password"],
            webhook_secret=creds["webhook_secret"],
        )
    raise ValueError(f"Unknown platform: {platform!r}")
