import fnmatch
import json
import logging
import subprocess
import tempfile
from pathlib import Path

from app.adapters.base import GitPlatform
from app.config.settings import Settings
from app.models import Finding, PRContext, ReviewConfig, Severity

logger = logging.getLogger(__name__)

SEMGREP_RULE_MAP = {
    "owasp": "p/owasp-top-ten",
    "security-audit": "p/security-audit",
    "p/java": "p/java",
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
            for rel_path in filtered_files:
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
            cmd = ["semgrep", "--json", "--no-rewrite-rule-ids"]
            for rule in rules:
                mapped = SEMGREP_RULE_MAP.get(rule, rule)
                cmd += ["--config", mapped]
            cmd.append(".")

            try:
                result = subprocess.run(
                    cmd, cwd=tmp_dir, capture_output=True, timeout=120, text=True
                )
            except subprocess.TimeoutExpired:
                logger.warning("Semgrep timed out")
                return []

            if result.returncode not in (0, 1):
                logger.warning("Semgrep exited with code %d\nSTDERR:\n%s\nSTDOUT:\n%s",
                               result.returncode, result.stderr, result.stdout[:500])
                return []

            try:
                data = json.loads(result.stdout)
            except json.JSONDecodeError:
                logger.warning("Failed to parse Semgrep output: returncode=%d\nSTDERR:\n%s\nSTDOUT:\n%s",
                               result.returncode, result.stderr, result.stdout[:500])
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
