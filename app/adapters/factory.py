from app.adapters.base import GitPlatform
from app.adapters.bitbucket import BitbucketAdapter
from app.adapters.github import GithubAdapter
from app.adapters.gitlab import GitlabAdapter
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
    if platform == "github":
        creds = get_credentials("github", workspace, repo_slug)
        return GithubAdapter(
            webhook_secret=creds["webhook_secret"],
            token=creds.get("api_token", ""),
        )
    if platform == "gitlab":
        creds = get_credentials("gitlab", workspace, repo_slug)
        return GitlabAdapter(
            webhook_secret=creds["webhook_secret"],
            token=creds.get("api_token", ""),
            base_url=creds.get("base_url", ""),
        )
    raise ValueError(f"Unknown platform: {platform!r}")
