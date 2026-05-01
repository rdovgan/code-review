import logging
import urllib.parse
from typing import Optional

import httpx

from app.adapters.base import GitPlatform
from app.models import Finding, PRContext

logger = logging.getLogger(__name__)

GITLAB_API = "https://gitlab.com/api/v4"


class GitlabAdapter(GitPlatform):
    def __init__(self, webhook_secret: str, token: str, base_url: str = "") -> None:
        self._secret = webhook_secret
        self._base_url = base_url.rstrip("/") if base_url else GITLAB_API
        self._client = httpx.Client(
            headers={"PRIVATE-TOKEN": token},
            timeout=30,
            follow_redirects=True,
        )

    def _encode_project(self, project_path: str) -> str:
        return urllib.parse.quote(project_path, safe="")

    def validate_webhook(self, body: bytes, headers: dict) -> bool:
        token = headers.get("x-gitlab-token", "")
        return token == self._secret

    def parse_webhook(self, payload: dict) -> Optional[PRContext]:
        if payload.get("object_kind") != "merge_request":
            return None

        attrs = payload.get("object_attributes", {})
        action = attrs.get("action", "")
        if action not in ("open", "reopen", "update"):
            return None

        if attrs.get("state", "") != "opened":
            logger.info("Skipping MR: state is %r", attrs.get("state"))
            return None

        project = payload.get("project", {})
        repo_full = project.get("path_with_namespace", "")

        return PRContext(
            platform="gitlab",
            repo_full_name=repo_full,
            pr_id=attrs.get("iid", 0),
            base_sha=attrs.get("target_branch", "main"),
            head_sha=attrs.get("last_commit", {}).get("id", ""),
            author=payload.get("user", {}).get("username", "unknown"),
            title=attrs.get("title", ""),
            target_branch=attrs.get("target_branch", "main"),
            language="auto",
            diff="",
            changed_files=[],
        )

    def get_diff(self, pr_context: PRContext) -> str:
        project = self._encode_project(pr_context.repo_full_name)
        url = f"{self._base_url}/projects/{project}/merge_requests/{pr_context.pr_id}/diffs"
        resp = self._client.get(url)
        resp.raise_for_status()
        diffs = resp.json()
        diff_text = ""
        for d in diffs:
            diff_text += f"diff --git a/{d.get('old_path', '')} b/{d.get('new_path', '')}\n"
            diff_text += d.get("diff", "") + "\n"
        return diff_text

    def get_changed_files(self, pr_context: PRContext) -> list[str]:
        project = self._encode_project(pr_context.repo_full_name)
        url = f"{self._base_url}/projects/{project}/merge_requests/{pr_context.pr_id}/diffs"
        resp = self._client.get(url)
        resp.raise_for_status()
        return [d.get("new_path", d.get("old_path", "")) for d in resp.json()]

    def get_file_content(self, pr_context: PRContext, path: str, ref: str) -> Optional[str]:
        project = self._encode_project(pr_context.repo_full_name)
        url = f"{self._base_url}/projects/{project}/repository/files/{urllib.parse.quote(path, safe='')}"
        resp = self._client.get(url, params={"ref": ref})
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        import base64
        content = resp.json().get("content", "")
        return base64.b64decode(content).decode("utf-8") if content else None

    def post_inline_comment(self, pr_context: PRContext, finding: Finding) -> str:
        project = self._encode_project(pr_context.repo_full_name)
        url = f"{self._base_url}/projects/{project}/merge_requests/{pr_context.pr_id}/discussions"
        body = f"**[{finding.severity.value}]** {finding.message}"
        if finding.suggestion:
            body += f"\n\n💡 {finding.suggestion}"
        payload = {
            "body": body,
            "position": {
                "base_sha": pr_context.base_sha,
                "start_sha": pr_context.base_sha,
                "head_sha": pr_context.head_sha,
                "position_type": "text",
                "new_path": finding.file,
                "new_line": finding.line,
            },
        }
        resp = self._client.post(url, json=payload)
        resp.raise_for_status()
        return str(resp.json().get("id", ""))

    def post_summary_comment(self, pr_context: PRContext, body: str) -> str:
        project = self._encode_project(pr_context.repo_full_name)
        url = f"{self._base_url}/projects/{project}/merge_requests/{pr_context.pr_id}/notes"
        resp = self._client.post(url, json={"body": body})
        resp.raise_for_status()
        return str(resp.json().get("id", ""))

    def delete_comment(self, pr_context: PRContext, comment_id: str) -> bool:
        project = self._encode_project(pr_context.repo_full_name)
        if comment_id.startswith("discussion:"):
            actual_id = comment_id[len("discussion:"):]
            url = f"{self._base_url}/projects/{project}/merge_requests/{pr_context.pr_id}/discussions/{actual_id}"
        elif comment_id.startswith("note:"):
            actual_id = comment_id[len("note:"):]
            url = f"{self._base_url}/projects/{project}/merge_requests/{pr_context.pr_id}/notes/{actual_id}"
        else:
            url = f"{self._base_url}/projects/{project}/merge_requests/{pr_context.pr_id}/notes/{comment_id}"
        resp = self._client.delete(url)
        return resp.status_code in (200, 204)

    def get_existing_bot_comments(self, pr_context: PRContext) -> list[dict]:
        project = self._encode_project(pr_context.repo_full_name)
        comments = []
        url = f"{self._base_url}/projects/{project}/merge_requests/{pr_context.pr_id}/notes"
        resp = self._client.get(url)
        resp.raise_for_status()
        for c in resp.json():
            body = c.get("body", "")
            if any(marker in body for marker in ["## AI Code Review Summary", "**[CRITICAL]**", "**[BUG]**", "**[PERFORMANCE]**", "**[SUGGEST]**"]):
                comments.append({"id": f"note:{c.get('id')}", "body": body})
        url = f"{self._base_url}/projects/{project}/merge_requests/{pr_context.pr_id}/discussions"
        resp = self._client.get(url)
        if resp.status_code == 200:
            for d in resp.json():
                for note in d.get("notes", []):
                    body = note.get("body", "")
                    if any(marker in body for marker in ["**[CRITICAL]**", "**[BUG]**", "**[PERFORMANCE]**", "**[SUGGEST]**"]):
                        comments.append({"id": f"discussion:{d.get('id')}", "body": body})
        return comments

    def set_review_status(self, pr_context: PRContext, state: str, description: str) -> bool:
        state_map = {"pending": "pending", "success": "success", "failure": "failed"}
        gl_state = state_map.get(state, "pending")
        project = self._encode_project(pr_context.repo_full_name)
        url = f"{self._base_url}/projects/{project}/statuses/{pr_context.head_sha}"
        payload = {
            "state": gl_state,
            "name": "code-review-bot",
            "description": description,
            "target_url": f"{self._base_url.replace('/api/v4', '')}/{pr_context.repo_full_name}/-/merge_requests/{pr_context.pr_id}",
        }
        resp = self._client.post(url, json=payload)
        return resp.status_code in (200, 201, 202)
