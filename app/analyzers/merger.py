from app.models import Finding, ReviewConfig, _severity_order
from app.utils.diff_parser import is_ignored


def merge_findings(semgrep: list[Finding], ai: list[Finding]) -> list[Finding]:
    """One finding per file:line. On a collision the higher severity wins; on equal
    severity the earlier one does (Semgrep before AI)."""
    by_key: dict[str, Finding] = {}
    for f in semgrep + ai:
        existing = by_key.get(f.dedup_key)
        if existing is None or _rank(f) < _rank(existing):
            by_key[f.dedup_key] = f
    merged = list(by_key.values())
    merged.sort(key=_rank)
    return merged


def _rank(f: Finding) -> int:
    return _severity_order.get(f.severity.value, 99)


def filter_by_config(findings: list[Finding], config: ReviewConfig) -> list[Finding]:
    if not config.ignore_paths:
        return findings
    return [f for f in findings if not is_ignored(f.file, config.ignore_paths)]
