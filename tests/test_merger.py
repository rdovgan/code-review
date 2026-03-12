from app.analyzers.merger import filter_by_config, merge_findings
from app.models import Finding, ReviewConfig, Severity


def _f(severity: Severity, file: str = "Foo.java", line: int = 1, source: str = "ai") -> Finding:
    return Finding(
        severity=severity,
        file=file,
        line=line,
        message=f"Issue at {file}:{line}",
        suggestion="Fix it",
        source=source,  # type: ignore[arg-type]
    )


def test_dedup_same_key():
    f1 = _f(Severity.CRITICAL, "Foo.java", 10, "semgrep")
    f2 = _f(Severity.CRITICAL, "Foo.java", 10, "ai")
    result = merge_findings([f1], [f2])
    assert len(result) == 1
    # semgrep finding wins (comes first)
    assert result[0].source == "semgrep"


def test_sort_by_severity():
    findings = [
        _f(Severity.SUGGEST),
        _f(Severity.CRITICAL, line=2),
        _f(Severity.PERFORMANCE, line=3),
        _f(Severity.BUG, line=4),
    ]
    result = merge_findings([], findings)
    assert result[0].severity == Severity.CRITICAL
    assert result[1].severity == Severity.BUG
    assert result[2].severity == Severity.PERFORMANCE
    assert result[3].severity == Severity.SUGGEST


def test_filter_ignore_paths():
    findings = [
        _f(Severity.CRITICAL, "src/Main.java"),
        _f(Severity.BUG, "migrations/001.java"),
        _f(Severity.SUGGEST, "src/util/Helper.java"),
    ]
    config = ReviewConfig(ignore_paths=["migrations/*"])
    result = filter_by_config(findings, config)
    files = [f.file for f in result]
    assert "migrations/001.java" not in files
    assert "src/Main.java" in files
