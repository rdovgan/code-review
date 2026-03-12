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

    for field in ("username", "app_password"):
        if not ws.get(field):
            raise ValueError(
                f"Workspace '{workspace}' in config/credentials.yml is missing '{field}'."
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

    return {
        "username": ws["username"],
        "app_password": ws["app_password"],
        "webhook_secret": repo["webhook_secret"],
    }
