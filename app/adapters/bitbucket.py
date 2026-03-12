import logging
from typing import Optional

import httpx

from app.adapters.base import BOT_MARKER, GitPlatform, hmac_verify
from app.models import Finding, PRContext

logger = logging.getLogger(__name__)

BITBUCKET_API = "https://api.bitbucket.org"


class BitbucketAdapter(GitPlatform):
    def __init__(self, username: str, app_password: str, webhook_secret: str) -> None:
        self._secret = webhook_secret
        self._client = httpx.Client(
            auth=(username, app_password),
            timeout=30,
        )

    def validate_webhook(self, body: bytes, headers: dict) -> bool:
        sig_header = headers.get("x-hub-signature", "")
        if not sig_header.startswith("sha256="):
            return False
        signature = sig_header[len("sha256="):]
        return hmac_verify(self._secret, body, signature)

    def parse_webhook(self, payload: dict) -> Optional[PRContext]:
        event = payload.get("event") or payload.get("eventKey", "")
        # Bitbucket Cloud sends event in a separate header, not payload;
        # payload key "pullrequest" indicates PR events.
        pr = payload.get("pullrequest")
        if not pr:
            return None

        actor = payload.get("actor", {})
        repo = pr.get("destination", {}).get("repository", {})
        repo_full = repo.get("full_name", "")
        base_sha = pr.get("destination", {}).get("commit", {}).get("hash", "")
        head_sha = pr.get("source", {}).get("commit", {}).get("hash", "")
        author = actor.get("display_name", actor.get("nickname", "unknown"))
        title = pr.get("title", "")
        pr_id = pr.get("id", 0)

        return PRContext(
            platform="bitbucket",
            repo_full_name=repo_full,
            pr_id=pr_id,
            base_sha=base_sha,
            head_sha=head_sha,
            author=author,
            title=title,
            language="auto",
            diff="",
            changed_files=[],
        )

    def get_diff(self, pr_context: PRContext) -> str:
        workspace, repo = pr_context.repo_full_name.split("/", 1)
        url = f"{BITBUCKET_API}/2.0/repositories/{workspace}/{repo}/pullrequests/{pr_context.pr_id}/diff"
        resp = self._client.get(url)
        resp.raise_for_status()
        return resp.text

    def get_changed_files(self, pr_context: PRContext) -> list[str]:
        workspace, repo = pr_context.repo_full_name.split("/", 1)
        url = f"{BITBUCKET_API}/2.0/repositories/{workspace}/{repo}/pullrequests/{pr_context.pr_id}/diffstat"
        resp = self._client.get(url)
        resp.raise_for_status()
        data = resp.json()
        files = []
        for entry in data.get("values", []):
            new_path = entry.get("new", {})
            if new_path and new_path.get("path"):
                files.append(new_path["path"])
        return files

    def get_file_content(self, pr_context: PRContext, path: str, ref: str) -> Optional[str]:
        workspace, repo = pr_context.repo_full_name.split("/", 1)
        url = f"{BITBUCKET_API}/2.0/repositories/{workspace}/{repo}/src/{ref}/{path}"
        resp = self._client.get(url)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.text

    def post_inline_comment(self, pr_context: PRContext, finding: Finding) -> str:
        workspace, repo = pr_context.repo_full_name.split("/", 1)
        url = f"{BITBUCKET_API}/2.0/repositories/{workspace}/{repo}/pullrequests/{pr_context.pr_id}/comments"
        body = f"**[{finding.severity.value}]** {finding.message}\n\n{finding.suggestion}\n\n{BOT_MARKER}"
        payload = {
            "content": {"raw": body},
            "inline": {"path": finding.file, "to": finding.line},
        }
        resp = self._client.post(url, json=payload)
        resp.raise_for_status()
        return str(resp.json().get("id", ""))

    def post_summary_comment(self, pr_context: PRContext, body: str) -> str:
        workspace, repo = pr_context.repo_full_name.split("/", 1)
        url = f"{BITBUCKET_API}/2.0/repositories/{workspace}/{repo}/pullrequests/{pr_context.pr_id}/comments"
        payload = {"content": {"raw": body + "\n\n" + BOT_MARKER}}
        resp = self._client.post(url, json=payload)
        resp.raise_for_status()
        return str(resp.json().get("id", ""))

    def delete_comment(self, pr_context: PRContext, comment_id: str) -> bool:
        workspace, repo = pr_context.repo_full_name.split("/", 1)
        url = f"{BITBUCKET_API}/2.0/repositories/{workspace}/{repo}/pullrequests/{pr_context.pr_id}/comments/{comment_id}"
        resp = self._client.delete(url)
        return resp.status_code in (200, 204)

    def get_existing_bot_comments(self, pr_context: PRContext) -> list[dict]:
        workspace, repo = pr_context.repo_full_name.split("/", 1)
        url = f"{BITBUCKET_API}/2.0/repositories/{workspace}/{repo}/pullrequests/{pr_context.pr_id}/comments"
        resp = self._client.get(url)
        resp.raise_for_status()
        comments = []
        for c in resp.json().get("values", []):
            raw = c.get("content", {}).get("raw", "")
            if BOT_MARKER in raw:
                comments.append({"id": str(c.get("id", "")), "body": raw})
        return comments

    def set_review_status(self, pr_context: PRContext, state: str, description: str) -> bool:
        state_map = {"pending": "INPROGRESS", "success": "SUCCESSFUL", "failure": "FAILED"}
        bb_state = state_map.get(state, "INPROGRESS")
        workspace, repo = pr_context.repo_full_name.split("/", 1)
        url = f"{BITBUCKET_API}/2.0/repositories/{workspace}/{repo}/commit/{pr_context.head_sha}/statuses/build"
        payload = {
            "state": bb_state,
            "key": "code-review-bot",
            "name": "AI Code Review",
            "description": description,
        }
        resp = self._client.post(url, json=payload)
        return resp.status_code in (200, 201)
