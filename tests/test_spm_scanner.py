import json
import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.analyzers.spm_scanner import SPMScanner, _infer_category
from app.models import SPMFinding, SPMScanCategory, Severity


def _make_settings(**kwargs):
    s = MagicMock()
    s.AI_PROVIDER = "claude"
    s.ANTHROPIC_API_KEY = "test"
    s.ZAI_API_KEY = ""
    s.ZAI_BASE_URL = ""
    s.GLM_MODEL = "glm-5-turbo"
    s.AI_MODEL = "claude-sonnet-4-6"
    s.AI_MAX_TOKENS = 4096
    s.AI_MAX_DIFF_TOKENS = 8000
    s.AI_DAILY_TOKEN_BUDGET = 0  # unlimited
    s.REDIS_URL = "redis://localhost:6379/0"
    s.SPM_MAX_FILES_PER_REPO = 200
    for k, v in kwargs.items():
        setattr(s, k, v)
    return s


def _make_scanner(settings=None):
    if settings is None:
        settings = _make_settings()
    with patch("redis.from_url"), patch("anthropic.Anthropic"):
        return SPMScanner(settings)


def _make_adapter(files_by_path=None, dirs=None):
    """Build a mock adapter.

    files_by_path: {path: content}  — returned by get_file_content
    dirs: {path: [entries]}         — returned by list_files; entry = {"path": ..., "type": ...}
    """
    adapter = MagicMock()
    files_by_path = files_by_path or {}
    dirs = dirs or {}

    def _list_files(repo, ref, path=""):
        return dirs.get(path, [])

    def _get_file_content(ctx, path, ref):
        return files_by_path.get(path)

    adapter.list_files.side_effect = _list_files
    adapter.get_file_content.side_effect = _get_file_content
    return adapter


# ------------------------------------------------------------------
# _collect_files
# ------------------------------------------------------------------

def test_collect_files_returns_scannable_files():
    scanner = _make_scanner()
    dirs = {
        "": [
            {"path": "src", "type": "commit_directory"},
            {"path": "README.md", "type": "commit_file"},
        ],
        "src": [
            {"path": "src/app.py", "type": "commit_file"},
            {"path": "src/logo.png", "type": "commit_file"},  # not scannable
        ],
    }
    adapter = _make_adapter(dirs=dirs)
    result = scanner._collect_files(adapter, "ws/repo", "main", max_files=50)
    assert "src/app.py" in result
    assert "src/logo.png" not in result
    assert "README.md" not in result  # .md not in SCANNABLE_EXTENSIONS


def test_collect_files_respects_max():
    scanner = _make_scanner()
    entries = [{"path": f"file{i}.py", "type": "commit_file"} for i in range(300)]
    dirs = {"": entries}
    adapter = _make_adapter(dirs=dirs)
    result = scanner._collect_files(adapter, "ws/repo", "main", max_files=10)
    assert len(result) == 10


def test_collect_files_walks_subdirectories():
    scanner = _make_scanner()
    dirs = {
        "": [{"path": "a", "type": "commit_directory"}],
        "a": [{"path": "b", "type": "commit_directory"}],
        "b": [{"path": "b/deep.py", "type": "commit_file"}],
    }
    adapter = _make_adapter(dirs=dirs)
    result = scanner._collect_files(adapter, "ws/repo", "main", max_files=50)
    assert "b/deep.py" in result


def test_collect_files_handles_api_error_gracefully():
    scanner = _make_scanner()
    adapter = MagicMock()
    adapter.list_files.side_effect = Exception("API error")
    result = scanner._collect_files(adapter, "ws/repo", "main", max_files=50)
    assert result == []


# ------------------------------------------------------------------
# _fetch_files_to_tmpdir
# ------------------------------------------------------------------

def test_fetch_skips_large_files():
    scanner = _make_scanner()
    big_content = "x" * (101 * 1024)  # over 100 KB
    adapter = _make_adapter(files_by_path={"big.py": big_content, "small.py": "print('hi')"})
    with tempfile.TemporaryDirectory() as tmp:
        with patch("time.sleep"):  # skip rate-limit sleep
            written, contents = scanner._fetch_files_to_tmpdir(
                adapter, "ws/repo", "main", ["big.py", "small.py"], tmp
            )
    assert "small.py" in written
    assert "big.py" not in written


def test_fetch_skips_none_content():
    scanner = _make_scanner()
    adapter = _make_adapter(files_by_path={"exists.py": "code()"})
    with tempfile.TemporaryDirectory() as tmp:
        with patch("time.sleep"):
            written, contents = scanner._fetch_files_to_tmpdir(
                adapter, "ws/repo", "main", ["exists.py", "missing.py"], tmp
            )
    assert written == ["exists.py"]


# ------------------------------------------------------------------
# _run_semgrep
# ------------------------------------------------------------------

def test_run_semgrep_empty_dir_returns_empty():
    scanner = _make_scanner()
    with tempfile.TemporaryDirectory() as tmp:
        result = scanner._run_semgrep(tmp, [SPMScanCategory.SECRET], "ws/repo")
    assert result == []


def test_run_semgrep_parses_results():
    scanner = _make_scanner()
    fake_output = json.dumps({
        "results": [{
            "path": "/tmp/fakedir/config.py",
            "check_id": "python.secrets.hardcoded-password",
            "start": {"line": 5},
            "extra": {
                "severity": "ERROR",
                "message": "Hardcoded password found",
            },
        }]
    })
    with tempfile.TemporaryDirectory() as tmp:
        # Write a dummy file so semgrep has something to scan
        Path(os.path.join(tmp, "config.py")).write_text("password = 'secret123'")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout=fake_output, returncode=0)
            result = scanner._run_semgrep(tmp, [SPMScanCategory.SECRET], "ws/repo")

    assert len(result) == 1
    assert result[0].severity == Severity.BUG
    assert result[0].source == "semgrep"
    assert result[0].line == 5


def test_run_semgrep_strips_tmpdir_prefix():
    scanner = _make_scanner()
    with tempfile.TemporaryDirectory() as tmp:
        fake_output = json.dumps({
            "results": [{
                "path": f"{tmp}/src/app.py",
                "check_id": "test.rule",
                "start": {"line": 1},
                "extra": {"severity": "WARNING", "message": "test"},
            }]
        })
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout=fake_output, returncode=0)
            result = scanner._run_semgrep(tmp, [SPMScanCategory.MISCONFIGURATION], "ws/repo")

    assert result[0].file == "src/app.py"


# ------------------------------------------------------------------
# _dedup_and_sort
# ------------------------------------------------------------------

def test_dedup_semgrep_wins_ties():
    scanner = _make_scanner()

    def _finding(source, severity=Severity.BUG):
        return SPMFinding(
            category=SPMScanCategory.SECRET,
            severity=severity,
            file="f.py",
            line=1,
            message="same message here for dedup",
            suggestion="fix it",
            source=source,
        )

    semgrep_f = _finding("semgrep")
    ai_f = _finding("ai")
    result = scanner._dedup_and_sort([semgrep_f], [ai_f])
    assert len(result) == 1
    assert result[0].source == "semgrep"


def test_dedup_sort_by_severity():
    scanner = _make_scanner()

    def _finding(sev, file):
        return SPMFinding(
            category=SPMScanCategory.SECRET,
            severity=sev,
            file=file,
            line=1,
            message=f"issue in {file}",
            suggestion="fix",
            source="semgrep",
        )

    findings = [
        _finding(Severity.SUGGEST, "d.py"),
        _finding(Severity.CRITICAL, "a.py"),
        _finding(Severity.PERFORMANCE, "c.py"),
        _finding(Severity.BUG, "b.py"),
    ]
    result = scanner._dedup_and_sort(findings, [])
    severities = [f.severity for f in result]
    assert severities == [Severity.CRITICAL, Severity.BUG, Severity.PERFORMANCE, Severity.SUGGEST]


# ------------------------------------------------------------------
# _infer_category
# ------------------------------------------------------------------

def test_infer_category_secret():
    assert _infer_category("python.secrets.hardcoded-api-key", list(SPMScanCategory)) == SPMScanCategory.SECRET


def test_infer_category_misconfiguration():
    assert _infer_category("owasp.injection.sqli", list(SPMScanCategory)) == SPMScanCategory.MISCONFIGURATION


def test_infer_category_fallback():
    result = _infer_category("random.rule.xyz", [SPMScanCategory.DEPENDENCY])
    assert result == SPMScanCategory.DEPENDENCY


# ------------------------------------------------------------------
# _validate_spm_finding
# ------------------------------------------------------------------

def test_validate_spm_finding_valid():
    scanner = _make_scanner()
    item = {
        "severity": "CRITICAL",
        "file": "app.py",
        "line": 10,
        "message": "Hardcoded key",
        "suggestion": "Use env var",
        "category": "SECRET",
    }
    f = scanner._validate_spm_finding(item, list(SPMScanCategory))
    assert f is not None
    assert f.category == SPMScanCategory.SECRET
    assert f.severity == Severity.CRITICAL


def test_validate_spm_finding_missing_field():
    scanner = _make_scanner()
    item = {"severity": "CRITICAL", "file": "app.py", "line": 10, "message": "x"}
    assert scanner._validate_spm_finding(item, list(SPMScanCategory)) is None


def test_validate_spm_finding_wrong_category_filtered():
    scanner = _make_scanner()
    item = {
        "severity": "CRITICAL",
        "file": "app.py",
        "line": 1,
        "message": "issue",
        "suggestion": "fix",
        "category": "SECRET",
    }
    # Only DEPENDENCY requested — SECRET should be filtered out
    assert scanner._validate_spm_finding(item, [SPMScanCategory.DEPENDENCY]) is None
