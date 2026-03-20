import logging
from collections import Counter
from pathlib import Path

import yaml

from app.adapters.base import GitPlatform
from app.models import PRContext, ReviewConfig

logger = logging.getLogger(__name__)

_EXT_TO_LANG = {
    ".java": "java",
    ".cs": "dotnet",
    ".php": "php",
    ".js": "js",
    ".ts": "js",
    ".tsx": "js",
    ".jsx": "js",
    ".py": "python",
}

_PROJECTS_CONFIG_PATH = Path(__file__).parent.parent.parent / "config" / "projects.yml"

_REVIEW_CONFIG_FIELDS = (
    "language", "ai_review", "static_analysis", "block_merge_on",
    "max_diff_lines", "slack_channel", "ignore_paths", "semgrep_rules", "ai_focus",
    "target_branches",
)


def detect_language(changed_files: list[str]) -> str:
    counts: Counter = Counter()
    for f in changed_files:
        for ext, lang in _EXT_TO_LANG.items():
            if f.endswith(ext):
                counts[lang] += 1
                break
    if not counts:
        return "java"
    return counts.most_common(1)[0][0]


def _load_central_config(repo_full_name: str) -> dict:
    """Return the config dict for repo_full_name from config/projects.yml, or {}."""
    if not _PROJECTS_CONFIG_PATH.exists():
        return {}
    try:
        data = yaml.safe_load(_PROJECTS_CONFIG_PATH.read_text())
        if not isinstance(data, dict):
            return {}
        return data.get("projects", {}).get(repo_full_name, {})
    except Exception as exc:
        logger.warning("Failed to load config/projects.yml: %s", exc)
        return {}


def _apply_dict(config: ReviewConfig, data: dict) -> None:
    """Overlay non-None values from data onto config in-place."""
    for field_name in _REVIEW_CONFIG_FIELDS:
        if field_name in data:
            setattr(config, field_name, data[field_name])


def load_project_config(adapter: GitPlatform, pr_context: PRContext) -> ReviewConfig:
    """
    Build ReviewConfig using a two-layer merge:
      1. config/projects.yml  (central, bot-managed)
      2. .codereview.yml      (in the repo — overrides central)
    Falls back to ReviewConfig defaults if neither source provides a value.
    """
    config = ReviewConfig()

    # Layer 1: central registry
    central = _load_central_config(pr_context.repo_full_name)
    _apply_dict(config, central)

    # Layer 2: per-repo override
    try:
        content = adapter.get_file_content(pr_context, ".codereview.yml", pr_context.base_sha)
        if content:
            repo_data = yaml.safe_load(content)
            if isinstance(repo_data, dict):
                _apply_dict(config, repo_data)
    except Exception as exc:
        logger.warning("Failed to load .codereview.yml for %s: %s", pr_context.repo_full_name, exc)

    return config
