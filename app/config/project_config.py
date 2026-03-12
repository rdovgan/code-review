import logging
from collections import Counter
from typing import Optional

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
}


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


def load_project_config(adapter: GitPlatform, pr_context: PRContext) -> ReviewConfig:
    config = ReviewConfig()
    try:
        content = adapter.get_file_content(pr_context, ".codereview.yml", pr_context.base_sha)
        if not content:
            return config
        data = yaml.safe_load(content)
        if not isinstance(data, dict):
            return config
        for field_name in (
            "language", "ai_review", "static_analysis", "block_merge_on",
            "max_diff_lines", "slack_channel", "ignore_paths", "semgrep_rules", "ai_focus",
        ):
            if field_name in data:
                setattr(config, field_name, data[field_name])
    except Exception as exc:
        logger.warning("Failed to load .codereview.yml: %s", exc)
    return config
