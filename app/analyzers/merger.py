import fnmatch

from app.models import Finding, ReviewConfig, _severity_order


def merge_findings(semgrep: list[Finding], ai: list[Finding]) -> list[Finding]:
    seen: set[str] = set()
    merged: list[Finding] = []
    for f in semgrep + ai:
        key = f.dedup_key
        if key not in seen:
            seen.add(key)
            merged.append(f)
    merged.sort(key=lambda f: _severity_order.get(f.severity.value, 99))
    return merged


def filter_by_config(findings: list[Finding], config: ReviewConfig) -> list[Finding]:
    if not config.ignore_paths:
        return findings
    return [
        f for f in findings
        if not any(fnmatch.fnmatch(f.file, pat) for pat in config.ignore_paths)
    ]
