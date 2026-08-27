import base64
import logging
from typing import Optional

import httpx

from app.adapters.base import GitPlatform, hmac_verify
from app.models import Finding, PRContext

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"


class GithubAdapter(GitPlatform):
    def __init__(self, webhook_secret: str, token: str) -> None:
        self._secret = webhook_secret
        self._bot_username: str | None = None
        self._client = httpx.Client(
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github.v3+json",
            },
            timeout=30,
            follow_redirects=True,
        )

    def validate_webhook(self, body: bytes, headers: dict) -> bool:
        sig_header = headers.get("x-hub-signature-256", "")
        if not sig_header.startswith("sha256="):
            return False
        signature = sig_header[len("sha256="):]
        return hmac_verify(self._secret, body, signature)

    def parse_webhook(self, payload: dict) -> Optional[PRContext]:
        is_new_pr = False
        if "pull_request" in payload:
            action = payload.get("action", "")
            if action not in ("opened", "synchronize", "reopened"):
                return None
            pr = payload["pull_request"]
            is_new_pr = action == "opened"
        elif "comment" in payload and "issue" in payload:
            comment_body = payload["comment"].get("body", "").strip().lower()
            if comment_body != "review":
                return None
            issue = payload["issue"]
            if "pull_request" not in issue:
                return None
            pr_url = issue["pull_request"]["url"]
            resp = self._client.get(pr_url)
            resp.raise_for_status()
            pr = resp.json()
        else:
            return None

        if pr.get("state", "") != "open":
            logger.info("Skipping PR: state is %r", pr.get("state"))
            return None

        repo = payload.get("repository", {})
        repo_full = repo.get("full_name", "")

        return PRContext(
            platform="github",
            repo_full_name=repo_full,
            pr_id=pr.get("number", 0),
            base_sha=pr.get("base", {}).get("sha", ""),
            head_sha=pr.get("head", {}).get("sha", ""),
            author=pr.get("user", {}).get("login", "unknown"),
            title=pr.get("title", ""),
            target_branch=pr.get("base", {}).get("ref", "main"),
            source_branch=pr.get("head", {}).get("ref", ""),
            language="auto",
            diff="",
            changed_files=[],
            is_new_pr=is_new_pr,
        )

    def get_diff(self, pr_context: PRContext) -> str:
        owner, repo = pr_context.repo_full_name.split("/", 1)
        url = f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_context.pr_id}"
        headers = {"Accept": "application/vnd.github.v3.diff"}
        resp = self._client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.text

    def get_changed_files(self, pr_context: PRContext) -> list[str]:
        owner, repo = pr_context.repo_full_name.split("/", 1)
        url = f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_context.pr_id}/files"
        resp = self._client.get(url)
        resp.raise_for_status()
        return [f["filename"] for f in resp.json()]

    def get_pr_commits(self, pr_context: PRContext) -> list[str]:
        owner, repo = pr_context.repo_full_name.split("/", 1)
        url = f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_context.pr_id}/commits"
        resp = self._client.get(url)
        resp.raise_for_status()
        return [
            c.get("commit", {}).get("message", "").strip().splitlines()[0]
            for c in resp.json()
            if c.get("commit", {}).get("message", "").strip()
        ]

    def get_file_content(self, pr_context: PRContext, path: str, ref: str) -> Optional[str]:
        owner, repo = pr_context.repo_full_name.split("/", 1)
        url = f"{GITHUB_API}/repos/{owner}/{repo}/contents/{path}"
        resp = self._client.get(url, params={"ref": ref})
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        content = resp.json().get("content", "")
        return base64.b64decode(content).decode("utf-8") if content else None

    def post_inline_comment(self, pr_context: PRContext, finding: Finding) -> str:
        owner, repo = pr_context.repo_full_name.split("/", 1)
        url = f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_context.pr_id}/comments"
        body = f"**[{finding.severity.value}]** {finding.message}"
        if finding.suggestion:
            body += f"\n\n💡 {finding.suggestion}"
        payload = {
            "body": body,
            "commit_id": pr_context.head_sha,
            "path": finding.file,
            "line": finding.line,
            "side": "RIGHT",
        }
        resp = self._client.post(url, json=payload)
        resp.raise_for_status()
        return str(resp.json().get("id", ""))

    def post_summary_comment(self, pr_context: PRContext, body: str) -> str:
        owner, repo = pr_context.repo_full_name.split("/", 1)
        url = f"{GITHUB_API}/repos/{owner}/{repo}/issues/{pr_context.pr_id}/comments"
        resp = self._client.post(url, json={"body": body})
        resp.raise_for_status()
        return str(resp.json().get("id", ""))

    def delete_comment(self, pr_context: PRContext, comment_id: str) -> bool:
        owner, repo = pr_context.repo_full_name.split("/", 1)
        if comment_id.startswith("review:"):
            actual_id = comment_id[len("review:"):]
            url = f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_context.pr_id}/comments/{actual_id}"
        elif comment_id.startswith("issue:"):
            actual_id = comment_id[len("issue:"):]
            url = f"{GITHUB_API}/repos/{owner}/{repo}/issues/comments/{actual_id}"
        else:
            url = f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_context.pr_id}/comments/{comment_id}"
        resp = self._client.delete(url)
        return resp.status_code in (200, 204)

    def _resolve_bot_identity(self) -> str:
        """Lazy-fetch the bot's GitHub login (cached after first call)."""
        if self._bot_username is None:
            resp = self._client.get(f"{GITHUB_API}/user")
            resp.raise_for_status()
            self._bot_username = resp.json()["login"]
        return self._bot_username

    def get_existing_bot_comments(self, pr_context: PRContext) -> list[dict]:
        bot_login = self._resolve_bot_identity()
        owner, repo = pr_context.repo_full_name.split("/", 1)
        comments = []
        url = f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_context.pr_id}/comments"
        resp = self._client.get(url)
        resp.raise_for_status()
        for c in resp.json():
            if c.get("user", {}).get("login") == bot_login:
                comments.append({"id": f"review:{c.get('id')}", "body": c.get("body", "")})
        url = f"{GITHUB_API}/repos/{owner}/{repo}/issues/{pr_context.pr_id}/comments"
        resp = self._client.get(url)
        resp.raise_for_status()
        for c in resp.json():
            if c.get("user", {}).get("login") == bot_login:
                comments.append({"id": f"issue:{c.get('id')}", "body": c.get("body", "")})
        return comments

    def set_review_status(self, pr_context: PRContext, state: str, description: str) -> bool:
        state_map = {"pending": "pending", "success": "success", "failure": "failure"}
        gh_state = state_map.get(state, "pending")
        owner, repo = pr_context.repo_full_name.split("/", 1)
        url = f"{GITHUB_API}/repos/{owner}/{repo}/statuses/{pr_context.head_sha}"
        payload = {
            "state": gh_state,
            "context": "code-review-bot",
            "description": description,
            "target_url": f"https://github.com/{owner}/{repo}/pull/{pr_context.pr_id}",
        }
        resp = self._client.post(url, json=payload)
        return resp.status_code in (200, 201, 202)
