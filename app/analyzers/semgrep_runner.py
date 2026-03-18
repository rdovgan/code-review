import fnmatch
import json
import logging
import subprocess
import tempfile
import time
from pathlib import Path

from app.adapters.base import GitPlatform
from app.config.settings import Settings
from app.models import Finding, PRContext, ReviewConfig, Severity

logger = logging.getLogger(__name__)

SEMGREP_RULE_MAP = {
    "owasp": "p/owasp-top-ten",
    "security-audit": "p/security-audit",
    "p/java": "p/java",
    "p/secrets": "p/secrets",
    "p/findsecbugs": "p/findsecbugs",
    "p/sql-injection": "p/sql-injection",
    "p/csharp": "p/csharp",
    "p/php": "p/php",
    "p/javascript": "p/javascript",
}

SEVERITY_MAP = {
    "ERROR": Severity.BUG,
    "WARNING": Severity.PERFORMANCE,
    "INFO": Severity.SUGGEST,
}


class SemgrepRunner:
    def __init__(self, config: ReviewConfig) -> None:
        self._config = config

    def run(self, pr_context: PRContext, adapter: GitPlatform) -> list[Finding]:
        filtered_files = [
            f for f in pr_context.changed_files
            if not any(fnmatch.fnmatch(f, pat) for pat in self._config.ignore_paths)
        ]

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            written: list[str] = []
            for i, rel_path in enumerate(filtered_files):
                if i > 0:
                    time.sleep(0.3)  # avoid Bitbucket rate limiting on bulk file fetches
                content = adapter.get_file_content(pr_context, rel_path, pr_context.head_sha)
                if content is None:
                    continue
                dest = tmp_path / rel_path
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(content, encoding="utf-8")
                written.append(rel_path)

            if not written:
                return []

            rules = self._config.semgrep_rules
            cmd = ["semgrep", "--json", "--no-rewrite-rule-ids", "--quiet", "--metrics=off"]
            for rule in rules:
                mapped = SEMGREP_RULE_MAP.get(rule, rule)
                cmd += ["--config", mapped]
            cmd.append(".")

            try:
                result = subprocess.run(
                    cmd, cwd=tmp_dir, capture_output=True, timeout=120, text=True
                )
            except subprocess.TimeoutExpired:
                logger.error("Semgrep timed out after 120s")
                return []

            if result.returncode not in (0, 1):
                logger.error("Semgrep exited with unexpected code %d: %s", result.returncode, result.stderr[:200])
                return []

            try:
                data = json.loads(result.stdout)
            except json.JSONDecodeError:
                logger.error(
                    "Semgrep returned invalid JSON (exit code %d)\nSTDERR: %s\nSTDOUT: %s",
                    result.returncode, result.stderr[:500], result.stdout[:200],
                )
                return []

            findings: list[Finding] = []
            for r in data.get("results", []):
                sev_str = r.get("extra", {}).get("severity", "INFO").upper()
                severity = SEVERITY_MAP.get(sev_str, Severity.SUGGEST)
                path = r.get("path", "")
                # Make path relative to tmp_dir
                if path.startswith(tmp_dir):
                    path = path[len(tmp_dir):].lstrip("/")
                findings.append(
                    Finding(
                        severity=severity,
                        file=path,
                        line=r.get("start", {}).get("line", 0),
                        message=r.get("extra", {}).get("message", ""),
                        suggestion=r.get("extra", {}).get("fix", ""),
                        source="semgrep",
                        rule_id=r.get("check_id"),
                    )
                )
            return findings
