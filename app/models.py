import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal, Optional

_severity_order = {
    "CRITICAL": 0,
    "BUG": 1,
    "PERFORMANCE": 2,
    "SUGGEST": 3,
}


class Severity(str, Enum):
    CRITICAL = "CRITICAL"
    BUG = "BUG"
    PERFORMANCE = "PERFORMANCE"
    SUGGEST = "SUGGEST"


@dataclass
class Finding:
    severity: Severity
    file: str
    line: int
    message: str
    suggestion: str
    source: Literal["semgrep", "ai"]
    rule_id: Optional[str] = None

    @property
    def dedup_key(self) -> str:
        raw = f"{self.file}:{self.line}:{self.message[:50]}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


@dataclass
class PRContext:
    platform: str
    repo_full_name: str
    pr_id: int
    base_sha: str
    head_sha: str
    author: str
    title: str
    language: str
    diff: str
    changed_files: list[str] = field(default_factory=list)


@dataclass
class ReviewConfig:
    language: str = "auto"
    ai_review: bool = True
    static_analysis: bool = True
    block_merge_on: list[str] = field(default_factory=lambda: ["CRITICAL"])
    max_diff_lines: int = 500
    slack_channel: str = ""
    ignore_paths: list[str] = field(default_factory=list)
    semgrep_rules: list[str] = field(default_factory=lambda: ["owasp", "security-audit"])
    ai_focus: list[str] = field(default_factory=lambda: ["security", "bugs"])
