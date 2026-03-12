import hashlib
import hmac
from abc import ABC, abstractmethod
from typing import Optional

from app.models import Finding, PRContext

BOT_MARKER = "<!-- code-review-bot -->"


def hmac_verify(secret: str, body: bytes, signature: str, algorithm: str = "sha256") -> bool:
    mac = hmac.new(secret.encode(), body, algorithm)
    expected = mac.hexdigest()
    return hmac.compare_digest(expected, signature)


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
    def set_review_status(self, pr_context: PRContext, state: str, description: str) -> bool: ...
