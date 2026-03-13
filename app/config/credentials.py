import logging
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

_CREDENTIALS_PATH = Path(__file__).parent.parent.parent / "config" / "credentials.yml"


def _load() -> dict:
    if not _CREDENTIALS_PATH.exists():
        raise ValueError(
            "config/credentials.yml not found. "
            "Copy config/credentials.yml.example and fill in your credentials."
        )
    try:
        return yaml.safe_load(_CREDENTIALS_PATH.read_text()) or {}
    except Exception as exc:
        raise ValueError(f"Failed to parse config/credentials.yml: {exc}") from exc


def get_credentials(platform: str, workspace: str, repo_slug: str) -> dict:
    """
    Return {username, app_password, webhook_secret} for the given repo.
    username and app_password come from the workspace block.
    webhook_secret comes from the repository block within that workspace.
    Raises ValueError with a descriptive message if anything is missing.
    """
    data = _load()
    workspaces = data.get(platform, {}).get("workspaces", {})

    if workspace not in workspaces:
        raise ValueError(
            f"No credentials configured for {platform} workspace '{workspace}'. "
            f"Add it to config/credentials.yml."
        )

    ws = workspaces[workspace]

    repos = ws.get("repositories", {})
    if repo_slug not in repos:
        raise ValueError(
            f"No credentials configured for repository '{workspace}/{repo_slug}'. "
            f"Add it under workspaces.{workspace}.repositories in config/credentials.yml."
        )

    repo = repos[repo_slug]

    if not repo.get("webhook_secret"):
        raise ValueError(
            f"Repository '{workspace}/{repo_slug}' is missing 'webhook_secret' in config/credentials.yml."
        )

    # Auth priority: repo-level api_token → workspace-level api_token → workspace username+app_password
    if repo.get("api_token"):
        api_creds = {"api_token": repo["api_token"]}
    elif ws.get("api_token"):
        api_creds = {"api_token": ws["api_token"]}
    elif ws.get("username") and ws.get("app_password"):
        api_creds = {"username": ws["username"], "app_password": ws["app_password"]}
    else:
        raise ValueError(
            f"No API credentials found for '{workspace}/{repo_slug}'. "
            f"Add 'api_token' to the repository, 'api_token' to the workspace, "
            f"or 'username'+'app_password' to the workspace in config/credentials.yml."
        )

    return {**api_creds, "webhook_secret": repo["webhook_secret"]}
