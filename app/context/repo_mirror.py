"""Bare git mirrors of reviewed repositories, read without a checkout.

Code is read straight from git objects (`git show <sha>:<path>`, `git grep <sha>`), so
there are no worktrees to create or clean up, and two workers can read the same mirror
at once. Writes (clone/fetch) are serialized per repo with a file lock.

Pre-clone a big repo once so the first agentic review does not spend its time budget
on the clone:

    docker compose exec worker python -m app.context.repo_mirror bitbucket workspace/repo
"""
import base64
import fcntl
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from app.adapters.base import GitCloneSpec

logger = logging.getLogger(__name__)

# Context repos are re-fetched at most this often — they only need to be roughly current
_CONTEXT_REFRESH_SECONDS = 600
# After a clone fails (usually a big repo that does not fit in a review's time budget),
# reviews don't retry it for this long — they fall back right away instead of burning
# their time budget on the same clone again. Pre-clone such repos with the CLI below.
_CLONE_RETRY_AFTER_SECONDS = 3600


class RepoUnavailable(Exception):
    """The mirror could not be created/updated, or does not contain the needed commit."""


class RepoMirror:
    def __init__(self, base_dir: str, platform: str, repo_full_name: str, spec: GitCloneSpec,
                 timeout: float = 900) -> None:
        self.repo_full_name = repo_full_name
        self.path = Path(base_dir) / platform / f"{repo_full_name}.git"
        self._spec = spec
        self._timeout = timeout

    # --- git plumbing ---

    def _env(self) -> dict[str, str]:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        if self._spec.username or self._spec.password:
            # Passed via env, not argv/URL: stays out of `ps`, the mirror's config and git errors
            token = base64.b64encode(f"{self._spec.username}:{self._spec.password}".encode()).decode()
            env.update({
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.extraHeader",
                "GIT_CONFIG_VALUE_0": f"Authorization: Basic {token}",
            })
        return env

    def _git(self, *args: str, timeout: Optional[float] = None, cwd: Optional[Path] = None) -> str:
        try:
            proc = subprocess.run(
                ["git", *args], cwd=cwd or self.path, env=self._env(),
                capture_output=True, text=True, timeout=timeout or self._timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise RepoUnavailable(f"git {args[0]} timed out after {exc.timeout:.0f}s") from exc
        if proc.returncode != 0:
            raise RepoUnavailable(f"git {args[0]} failed: {proc.stderr.strip()[:300]}")
        return proc.stdout

    def _lock(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = open(self.path.parent / f".{self.path.name}.lock", "w")
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        return lock_file

    # --- sync ---

    @property
    def _clone_failed_marker(self) -> Path:
        return self.path.parent / f".{self.path.name}.clone_failed"

    def _clone_recently_failed(self) -> bool:
        marker = self._clone_failed_marker
        return marker.exists() and time.time() - marker.stat().st_mtime < _CLONE_RETRY_AFTER_SECONDS

    def _clone(self, timeout: float) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        started = time.monotonic()
        try:
            self._git("clone", "--bare", "--quiet", self._spec.url, str(tmp), timeout=timeout, cwd=self.path.parent)
            # A bare clone has no fetch refspec; branches are mirrored as-is
            self._git("config", "remote.origin.fetch", "+refs/heads/*:refs/heads/*", cwd=tmp)
        except Exception:
            shutil.rmtree(tmp, ignore_errors=True)
            self._clone_failed_marker.touch()
            raise
        tmp.rename(self.path)
        self._clone_failed_marker.unlink(missing_ok=True)
        logger.info("Cloned mirror %s in %.0fs", self.repo_full_name, time.monotonic() - started)

    def sync(self, timeout: Optional[float] = None, allow_clone: bool = True) -> None:
        """Clone the mirror, or fetch all branches into it."""
        timeout = min(timeout or self._timeout, self._timeout)
        lock = self._lock()
        try:
            if not self.path.exists():
                if not allow_clone:
                    raise RepoUnavailable(f"{self.repo_full_name} is not cloned yet")
                if self._clone_recently_failed():
                    raise RepoUnavailable(f"{self.repo_full_name} is not cloned yet (last clone failed; pre-clone it)")
                self._clone(timeout)
            else:
                self._git("fetch", "--quiet", "--prune", "origin", timeout=timeout)
            (self.path / "last_sync").touch()
        finally:
            lock.close()

    def has_commit(self, sha: str) -> bool:
        if not self.path.exists() or not sha:
            return False
        try:
            self._git("cat-file", "-e", f"{sha}^{{commit}}", timeout=30)
            return True
        except RepoUnavailable:
            return False

    def ensure_commit(self, sha: str, timeout: Optional[float] = None, allow_clone: bool = True) -> None:
        """Make sure the PR's head commit is in the mirror (fetching if needed)."""
        if self.has_commit(sha):
            return
        self.sync(timeout, allow_clone=allow_clone)
        if not self.has_commit(sha):
            raise RepoUnavailable(f"commit {sha[:12]} not found in {self.repo_full_name} (fork PR or force-push?)")

    def ensure_fresh(self, timeout: Optional[float] = None) -> None:
        """For context repos: fetch if the last sync is older than a few minutes. Never clones."""
        marker = self.path / "last_sync"
        if marker.exists() and time.time() - marker.stat().st_mtime < _CONTEXT_REFRESH_SECONDS:
            return
        self.sync(timeout, allow_clone=False)


def _main(argv: list[str]) -> int:
    """python -m app.context.repo_mirror <platform> <workspace/repo> — clone or update one mirror."""
    if len(argv) != 2:
        print(__doc__)
        return 2
    from app.adapters.factory import get_adapter
    from app.config.settings import get_settings

    logging.basicConfig(level=logging.INFO)
    platform, repo_full_name = argv
    settings = get_settings()
    workspace, slug = repo_full_name.split("/", 1)
    spec = get_adapter(platform, workspace, slug, settings).git_clone_spec(repo_full_name)
    mirror = RepoMirror(settings.REPO_MIRRORS_DIR, platform, repo_full_name, spec, timeout=24 * 3600)
    mirror.sync()
    print(f"OK: {mirror.path}")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
