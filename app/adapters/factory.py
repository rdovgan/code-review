from app.adapters.base import GitPlatform
from app.adapters.bitbucket import BitbucketAdapter
from app.config.credentials import get_workspace_credentials
from app.config.settings import Settings


def get_adapter(platform: str, workspace: str, settings: Settings) -> GitPlatform:
    if platform == "bitbucket":
        creds = get_workspace_credentials("bitbucket", workspace)
        return BitbucketAdapter(
            username=creds["username"],
            app_password=creds["app_password"],
            webhook_secret=creds["webhook_secret"],
        )
    raise ValueError(f"Unknown platform: {platform!r}")
