import logging
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

_CREDENTIALS_PATH = Path(__file__).parent.parent.parent / "config" / "credentials.yml"


def get_workspace_credentials(platform: str, workspace: str) -> dict:
    """
    Return credentials dict for the given platform workspace.
    Raises ValueError if the workspace is not configured.
    """
    if not _CREDENTIALS_PATH.exists():
        raise ValueError(
            f"config/credentials.yml not found. "
            f"Copy config/credentials.yml.example and fill in your credentials."
        )
    try:
        data = yaml.safe_load(_CREDENTIALS_PATH.read_text()) or {}
    except Exception as exc:
        raise ValueError(f"Failed to parse config/credentials.yml: {exc}") from exc

    workspaces = data.get(platform, {}).get("workspaces", {})
    if workspace not in workspaces:
        raise ValueError(
            f"No credentials configured for {platform} workspace '{workspace}'. "
            f"Add it to config/credentials.yml."
        )
    creds = workspaces[workspace]
    for field in ("username", "app_password", "webhook_secret"):
        if not creds.get(field):
            raise ValueError(
                f"Credentials for {platform}/{workspace} are missing field '{field}'."
            )
    return creds
