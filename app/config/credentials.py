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

    # Support both auth methods:
    #   api_token  → Bearer token (Bitbucket API tokens, newer)
    #   username + app_password → Basic auth (App Passwords, legacy)
    if ws.get("api_token"):
        api_creds = {"api_token": ws["api_token"]}
    elif ws.get("username") and ws.get("app_password"):
        api_creds = {"username": ws["username"], "app_password": ws["app_password"]}
    else:
        raise ValueError(
            f"Workspace '{workspace}' must have either 'api_token' "
            f"or both 'username' and 'app_password' in config/credentials.yml."
        )

    repos = ws.get("repositories", {})
    if repo_slug not in repos:
        raise ValueError(
            f"No credentials configured for repository '{workspace}/{repo_slug}'. "
            f"Add it under workspaces.{workspace}.repositories in config/credentials.yml."
        )

    repo = repos[repo_slug]
    if not repo.get("webhook_secret"):
        raise ValueError(
            f"Repository '{workspace}/{repo_slug}' in config/credentials.yml is missing 'webhook_secret'."
        )

    return {**api_creds, "webhook_secret": repo["webhook_secret"]}
