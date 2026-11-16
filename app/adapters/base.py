import hashlib
import hmac
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from app.models import Finding, PRContext


def hmac_verify(secret: str, body: bytes, signature: str, algorithm: str = "sha256") -> bool:
    mac = hmac.new(secret.encode(), body, algorithm)
    expected = mac.hexdigest()
    return hmac.compare_digest(expected, signature)


@dataclass(frozen=True)
class GitCloneSpec:
    """How to clone a repo over HTTPS. Credentials go into an auth header, never into the URL."""
    url: str
    username: str = ""
    password: str = ""

    def __repr__(self) -> str:  # keep secrets out of logs and tracebacks
        return f"GitCloneSpec(url={self.url!r})"


class GitPlatform(ABC):
    @abstractmethod
    def validate_webhook(self, body: bytes, headers: dict) -> bool: ...

    @abstractmethod
    def parse_webhook(self, payload: dict) -> Optional[PRContext]: ...

    @abstractmethod
    def get_diff(self, pr_context: PRContext) -> str: ...

    @abstractmethod
    def get_changed_files(self, pr_context: PRContext) -> list[str]: ...

    @abstractmethod
    def get_pr_commits(self, pr_context: PRContext) -> list[str]:
        """Return commit subjects (first line of message) of the PR, oldest first."""
        ...

    @abstractmethod
    def get_file_content(self, pr_context: PRContext, path: str, ref: str) -> Optional[str]: ...

    @abstractmethod
    def post_inline_comment(self, pr_context: PRContext, finding: Finding) -> str: ...

    @abstractmethod
    def post_summary_comment(self, pr_context: PRContext, body: str) -> str: ...

    @abstractmethod
    def delete_comment(self, pr_context: PRContext, comment_id: str) -> bool: ...

    @abstractmethod
    def get_existing_bot_comments(self, pr_context: PRContext) -> list[dict]: ...

    @abstractmethod
    def set_review_status(self, pr_context: PRContext, state: str, description: str) -> bool:
        """state: "pending" | "success" | "failure" | "stopped" (review could not run)."""
        ...

    def git_clone_spec(self, repo_full_name: str) -> GitCloneSpec:
        """Clone URL and credentials for agentic review. Platforms without it fall back to diff review."""
        raise NotImplementedError(f"{type(self).__name__} does not support cloning")
