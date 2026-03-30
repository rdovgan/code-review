import json
import logging
import os
import subprocess
import tempfile
import time
from collections import deque
from datetime import date
from pathlib import Path
from typing import Optional

import redis

from app.adapters.base import GitPlatform
from app.config.settings import Settings
from app.models import PRContext, SPMFinding, SPMScanCategory, Severity

logger = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).parent.parent.parent / "prompts"

SPM_SEMGREP_RULES: dict[SPMScanCategory, list[str]] = {
    SPMScanCategory.SECRET: ["p/secrets"],
    SPMScanCategory.MISCONFIGURATION: ["p/owasp-top-ten", "p/security-audit"],
    SPMScanCategory.DEPENDENCY: ["p/python", "p/java", "p/javascript"],
}

SCANNABLE_EXTENSIONS = {
    ".py", ".java", ".js", ".ts", ".jsx", ".tsx", ".php", ".cs", ".go",
    ".rb", ".yml", ".yaml", ".json", ".xml", ".properties", ".env",
    ".conf", ".toml", ".gradle", ".tf",
}
SCANNABLE_BASENAMES = {
    "Dockerfile", "requirements.txt", "package.json", "pom.xml",
    "build.gradle", "Gemfile", ".env",
}

_MAX_FILE_BYTES = 100 * 1024  # skip files larger than 100 KB
_RATE_LIMIT_SLEEP = 0.3        # seconds between API calls


class SPMScanner:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._redis = redis.from_url(settings.REDIS_URL)

        provider = settings.AI_PROVIDER.lower()
        if provider == "glm":
            from openai import OpenAI
            self._provider = "glm"
            self._ai_client = OpenAI(api_key=settings.ZAI_API_KEY, base_url=settings.ZAI_BASE_URL)
        else:
            import anthropic
            self._provider = "claude"
            self._ai_client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def scan_repo(
        self,
        adapter: GitPlatform,
        repo_full_name: str,
        ref: str,
        categories: list[SPMScanCategory],
        max_files: int,
    ) -> list[SPMFinding]:
        logger.info("[SPM %s] Collecting file list (max %d)", repo_full_name, max_files)
        paths = self._collect_files(adapter, repo_full_name, ref, max_files)
        if not paths:
            logger.info("[SPM %s] No scannable files found", repo_full_name)
            return []

        logger.info("[SPM %s] Downloading %d files", repo_full_name, len(paths))
        with tempfile.TemporaryDirectory() as tmp_dir:
            written, file_contents = self._fetch_files_to_tmpdir(
                adapter, repo_full_name, ref, paths, tmp_dir
            )

            semgrep_findings: list[SPMFinding] = []
            if written:
                logger.info("[SPM %s] Running semgrep on %d files", repo_full_name, len(written))
                semgrep_findings = self._run_semgrep(tmp_dir, categories, repo_full_name)

        ai_findings: list[SPMFinding] = []
        if file_contents:
            logger.info("[SPM %s] Running AI analysis", repo_full_name)
            ai_findings = self._run_ai(file_contents, categories, repo_full_name)

        return self._dedup_and_sort(semgrep_findings, ai_findings)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _is_scannable(self, path: str) -> bool:
        p = Path(path)
        return p.suffix.lower() in SCANNABLE_EXTENSIONS or p.name in SCANNABLE_BASENAMES

    def _collect_files(
        self,
        adapter: GitPlatform,
        repo_full_name: str,
        ref: str,
        max_files: int,
    ) -> list[str]:
        """BFS walk of the repo file tree, returning scannable file paths."""
        collected: list[str] = []
        queue: deque[str] = deque([""])  # start at repo root

        while queue and len(collected) < max_files:
            dir_path = queue.popleft()
            try:
                entries = adapter.list_files(repo_full_name, ref, dir_path)
            except Exception as exc:
                logger.warning("[SPM %s] list_files failed for path=%r: %s", repo_full_name, dir_path, exc)
                continue

            for entry in entries:
                if len(collected) >= max_files:
                    break
                entry_path = entry["path"]
                entry_type = entry.get("type", "")
                if entry_type == "commit_directory":
                    queue.append(entry_path)
                elif self._is_scannable(entry_path):
                    collected.append(entry_path)

        return collected

    def _fetch_files_to_tmpdir(
        self,
        adapter: GitPlatform,
        repo_full_name: str,
        ref: str,
        paths: list[str],
        tmp_dir: str,
    ) -> tuple[list[str], dict[str, str]]:
        """Download files into tmp_dir. Returns (written_paths, {path: content})."""
        written: list[str] = []
        file_contents: dict[str, str] = {}

        # Minimal PRContext stub — only repo_full_name and head_sha are used by get_file_content
        stub_ctx = PRContext(
            platform="bitbucket",
            repo_full_name=repo_full_name,
            pr_id=0,
            base_sha=ref,
            head_sha=ref,
            author="",
            title="",
            language="auto",
            diff="",
        )

        for path in paths:
            try:
                content = adapter.get_file_content(stub_ctx, path, ref)
            except Exception as exc:
                logger.warning("[SPM %s] Failed to fetch %s: %s", repo_full_name, path, exc)
                time.sleep(_RATE_LIMIT_SLEEP)
                continue

            if content is None:
                time.sleep(_RATE_LIMIT_SLEEP)
                continue

            if len(content.encode()) > _MAX_FILE_BYTES:
                logger.debug("[SPM %s] Skipping %s — exceeds 100 KB", repo_full_name, path)
                time.sleep(_RATE_LIMIT_SLEEP)
                continue

            # Write to temp dir preserving subdirectory structure
            dest = os.path.join(tmp_dir, path)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "w", encoding="utf-8", errors="replace") as fh:
                fh.write(content)

            written.append(path)
            file_contents[path] = content
            time.sleep(_RATE_LIMIT_SLEEP)

        return written, file_contents

    def _run_semgrep(
        self,
        tmp_dir: str,
        categories: list[SPMScanCategory],
        repo_full_name: str,
    ) -> list[SPMFinding]:
        rules: list[str] = []
        for cat in categories:
            rules.extend(SPM_SEMGREP_RULES.get(cat, []))
        if not rules:
            return []

        cmd = ["semgrep", "--json", "--no-git-ignore"]
        for rule in rules:
            cmd += ["--config", rule]
        cmd.append(tmp_dir)

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            raw = json.loads(proc.stdout or "{}") if proc.stdout.strip() else {}
        except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception) as exc:
            logger.error("[SPM %s] Semgrep failed: %s", repo_full_name, exc)
            return []

        findings: list[SPMFinding] = []
        severity_map = {"ERROR": Severity.BUG, "WARNING": Severity.PERFORMANCE, "INFO": Severity.SUGGEST}

        for result in raw.get("results", []):
            path = result.get("path", "")
            # Strip the tmp_dir prefix to get the relative repo path
            if path.startswith(tmp_dir):
                path = path[len(tmp_dir):].lstrip("/\\")

            severity = severity_map.get(result.get("extra", {}).get("severity", ""), Severity.SUGGEST)

            # Determine category from rule ID
            rule_id = result.get("check_id", "")
            category = _infer_category(rule_id, categories)

            findings.append(SPMFinding(
                category=category,
                severity=severity,
                file=path,
                line=result.get("start", {}).get("line", 0),
                message=result.get("extra", {}).get("message", "")[:200],
                suggestion="Review and remediate the flagged pattern.",
                source="semgrep",
                rule_id=rule_id,
            ))

        return findings

    def _run_ai(
        self,
        file_contents: dict[str, str],
        categories: list[SPMScanCategory],
        repo_full_name: str,
    ) -> list[SPMFinding]:
        prompt = self._load_spm_prompt()
        category_names = ", ".join(c.value for c in categories)

        # Batch files into chunks of ~8000 tokens (approx 4 chars/token)
        max_chars = self._settings.AI_MAX_DIFF_TOKENS * 4
        chunks: list[str] = []
        current_parts: list[str] = []
        current_len = 0

        for path, content in file_contents.items():
            block = f"### FILE: {path}\n{content}\n"
            if current_len + len(block) > max_chars and current_parts:
                chunks.append("\n".join(current_parts))
                current_parts = []
                current_len = 0
            current_parts.append(block)
            current_len += len(block)

        if current_parts:
            chunks.append("\n".join(current_parts))

        findings: list[SPMFinding] = []
        for chunk in chunks:
            if not self._budget_ok():
                logger.warning("[SPM %s] Daily AI token budget reached, stopping AI scan", repo_full_name)
                break
            user_msg = f"Categories to check: {category_names}\n\n{chunk}"
            try:
                text, input_tokens, output_tokens = self._call_ai(prompt, user_msg)
                self._record_tokens(input_tokens, output_tokens)
                items = self._parse_response(text)
                for item in items:
                    f = self._validate_spm_finding(item, categories)
                    if f:
                        findings.append(f)
            except Exception as exc:
                logger.error("[SPM %s] AI chunk failed: %s", repo_full_name, exc)

        return findings

    def _load_spm_prompt(self) -> str:
        prompt_file = _PROMPTS_DIR / "spm_prompt.md"
        if prompt_file.exists():
            return prompt_file.read_text()
        return _FALLBACK_SPM_PROMPT

    def _call_ai(self, prompt: str, content: str) -> tuple[str, int, int]:
        if self._provider == "glm":
            response = self._ai_client.chat.completions.create(
                model=self._settings.GLM_MODEL,
                max_tokens=self._settings.AI_MAX_TOKENS,
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": content},
                ],
            )
            text = response.choices[0].message.content
            return text, response.usage.prompt_tokens, response.usage.completion_tokens
        else:
            response = self._ai_client.messages.create(
                model=self._settings.AI_MODEL,
                max_tokens=self._settings.AI_MAX_TOKENS,
                system=prompt,
                messages=[{"role": "user", "content": content}],
            )
            text = response.content[0].text
            return text, response.usage.input_tokens, response.usage.output_tokens

    def _budget_ok(self) -> bool:
        budget = self._settings.AI_DAILY_TOKEN_BUDGET
        if budget <= 0:
            return True
        key = f"ai_tokens:{date.today().isoformat()}"
        used = int(self._redis.get(key) or 0)
        return used < budget

    def _record_tokens(self, input_tokens: int, output_tokens: int) -> None:
        key = f"ai_tokens:{date.today().isoformat()}"
        self._redis.incrby(key, input_tokens + output_tokens)
        self._redis.expire(key, 86400 * 2)

    def _parse_response(self, text: str) -> list[dict]:
        text = text.strip()
        if text.startswith("```"):
            lines = text.splitlines()[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            logger.warning("Failed to parse SPM AI response as JSON: %s", text[:200])
            return []

    def _validate_spm_finding(
        self, item: dict, categories: list[SPMScanCategory]
    ) -> Optional[SPMFinding]:
        required = {"severity", "file", "line", "message", "suggestion", "category"}
        if not required.issubset(item.keys()):
            return None
        try:
            severity = Severity(item["severity"])
            category = SPMScanCategory(item["category"])
        except ValueError:
            return None
        if category not in categories:
            return None
        return SPMFinding(
            category=category,
            severity=severity,
            file=str(item["file"]),
            line=int(item["line"]),
            message=str(item["message"]),
            suggestion=str(item["suggestion"]),
            source="ai",
        )

    def _dedup_and_sort(
        self,
        semgrep_findings: list[SPMFinding],
        ai_findings: list[SPMFinding],
    ) -> list[SPMFinding]:
        seen: dict[str, SPMFinding] = {}
        # Semgrep inserted first — wins ties on dedup
        for f in semgrep_findings:
            seen[f.dedup_key] = f
        for f in ai_findings:
            if f.dedup_key not in seen:
                seen[f.dedup_key] = f

        severity_order = {Severity.CRITICAL: 0, Severity.BUG: 1, Severity.PERFORMANCE: 2, Severity.SUGGEST: 3}
        return sorted(seen.values(), key=lambda f: severity_order.get(f.severity, 99))


def _infer_category(rule_id: str, categories: list[SPMScanCategory]) -> SPMScanCategory:
    rule_lower = rule_id.lower()
    if "secret" in rule_lower or "credential" in rule_lower or "password" in rule_lower or "api.key" in rule_lower:
        return SPMScanCategory.SECRET
    if "owasp" in rule_lower or "security" in rule_lower or "auth" in rule_lower:
        return SPMScanCategory.MISCONFIGURATION
    # Fall back to the first category requested, or SECRET
    return categories[0] if categories else SPMScanCategory.SECRET


_FALLBACK_SPM_PROMPT = """You are a security engineer performing a repository-wide security posture assessment.

Analyze the provided file contents and identify security issues. Classify each finding by category:
- SECRET: Hardcoded credentials, API keys, tokens, passwords, private keys
- MISCONFIGURATION: Auth bypass, insecure defaults, missing validation, unsafe configs
- DEPENDENCY: Imports of known-vulnerable libraries or insecure usage patterns

Severity levels:
- CRITICAL: Exploitable immediately (exposed secret, auth bypass, RCE vector)
- BUG: Likely security defect (logic error, unsafe deserialization)
- PERFORMANCE: Not security-critical but notable
- SUGGEST: Best practice recommendation

Rules:
- Only report issues VISIBLE in the provided files.
- Return ONLY a valid JSON array. No markdown fences, no explanation.
- If no issues found, return: []

JSON schema: [{"severity": "...", "file": "...", "line": 42, "message": "...", "suggestion": "...", "category": "..."}]
"""
