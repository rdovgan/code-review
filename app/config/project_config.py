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
    "language", "ai_review", "static_analysis", "semgrep_ai_verify", "ai_verify", "ai_context_files",
    "review_mode", "agent_max_steps", "context_repos",
    "block_merge_on",
    "max_diff_lines", "slack_channel", "notify_authors",
    "ignore_paths", "semgrep_rules", "ai_focus", "target_branches",
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


_missing_registry_logged = False


def _load_central_config(repo_full_name: str) -> dict:
    """Return the config dict for repo_full_name from config/projects.yml, or {}."""
    global _missing_registry_logged
    if not _PROJECTS_CONFIG_PATH.exists():
        if not _missing_registry_logged:
            logger.warning("config/projects.yml not found — every repo is reviewed with defaults. "
                           "Create it from config/projects.yml.example")
            _missing_registry_logged = True
        return {}
    try:
        data = yaml.safe_load(_PROJECTS_CONFIG_PATH.read_text())
        if not isinstance(data, dict):
            return {}
        return data.get("projects", {}).get(repo_full_name, {})
    except Exception as exc:
        logger.warning("Failed to load config/projects.yml: %s", exc)
        return {}


# Cost-bearing switches: set only in the central registry. A .codereview.yml in the
# repo must not be able to turn on the (more expensive) agentic review for itself.
CENTRAL_ONLY_FIELDS = frozenset({"review_mode", "agent_max_steps", "context_repos"})

REVIEW_MODES = ("diff", "agentic", "shadow")


def _apply_dict(config: ReviewConfig, data: dict, skip: frozenset = frozenset()) -> None:
    """Overlay non-None values from data onto config in-place."""
    for field_name in _REVIEW_CONFIG_FIELDS:
        if field_name in data and field_name not in skip:
            setattr(config, field_name, data[field_name])


def central_review_mode(repo_full_name: str) -> str:
    """review_mode from config/projects.yml — used at enqueue time to size the task's time limits."""
    mode = _load_central_config(repo_full_name).get("review_mode", "diff")
    return mode if mode in REVIEW_MODES else "diff"


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
                ignored = CENTRAL_ONLY_FIELDS & repo_data.keys()
                if ignored:
                    logger.warning("%s: .codereview.yml cannot set %s — use config/projects.yml",
                                   pr_context.repo_full_name, ", ".join(sorted(ignored)))
                _apply_dict(config, repo_data, skip=CENTRAL_ONLY_FIELDS)
    except Exception as exc:
        logger.warning("Failed to load .codereview.yml for %s: %s", pr_context.repo_full_name, exc)

    if config.review_mode not in REVIEW_MODES:
        logger.warning("%s: unknown review_mode %r, using 'diff'", pr_context.repo_full_name, config.review_mode)
        config.review_mode = "diff"
    return config
