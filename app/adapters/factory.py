from app.adapters.base import GitPlatform
from app.adapters.bitbucket import BitbucketAdapter
from app.config.settings import Settings


def get_adapter(platform: str, settings: Settings) -> GitPlatform:
    if platform == "bitbucket":
        return BitbucketAdapter(settings)
    raise ValueError(f"Unknown platform: {platform!r}")
