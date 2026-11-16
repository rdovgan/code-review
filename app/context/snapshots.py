"""Code the review agent reads: shallow working-tree snapshots, never modified in place.

Two sources:

* Shared snapshots — PROJECTS_SNAPSHOT_DIR/<repo-slug> (the server's /opt/projects,
  mounted read-only). Shallow clones of each repo's main branch, kept current by a cron
  job outside the bot. The bot only reads them: it never fetches, checks out or cleans
  there, so other users of the folder (docs generation, the cron) are unaffected.

* Branch snapshots — BRANCH_SNAPSHOT_DIR/<repo-slug>@<branch>, owned by the bot. When a
  PR targets a branch the shared snapshot is not on (demo-dev, demo-stable, ...), the
  bot keeps its own depth-1 single-branch clone of that branch. A refresh clones anew
  and swaps the folder, so no history accumulates. A repo that is not in
  PROJECTS_SNAPSHOT_DIR gets no branch snapshot either — it is not reviewed agentically.

Pre-create a branch snapshot (e.g. before the first agentic review of a big repo):

    docker compose exec worker python -m app.context.snapshots bitbucket workspace/repo demo-dev
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

# After a branch clone fails, reviews don't retry it for this long (they fall back at once)
_CLONE_RETRY_AFTER_SECONDS = 3600


class SnapshotUnavailable(Exception):
    """No code snapshot for this repo/branch — the agentic review cannot run."""


def git(path: Path, *args: str, timeout: float = 30, env: Optional[dict] = None) -> subprocess.CompletedProcess:
    """git on a snapshot. safe.directory: the shared snapshots belong to another OS user."""
    return subprocess.run(["git", "-c", "safe.directory=*", "-C", str(path), *args],
                          capture_output=True, text=True, timeout=timeout, env=env)


def repo_slug(repo_full_name: str) -> str:
    return repo_full_name.split("/", 1)[-1]


def shared_snapshot(base_dir: str, repo_full_name: str) -> Optional[Path]:
    """The repo's folder in PROJECTS_SNAPSHOT_DIR, or None when it is not there."""
    if not base_dir:
        return None
    path = Path(base_dir) / repo_slug(repo_full_name)
    return path if (path / ".git").exists() else None


def current_branch(path: Path) -> str:
    proc = git(path, "rev-parse", "--abbrev-ref", "HEAD")
    return proc.stdout.strip() if proc.returncode == 0 else ""


class BranchSnapshot:
    """The bot's own depth-1 clone of one branch of one repo."""

    def __init__(self, base_dir: str, repo_full_name: str, branch: str, spec: GitCloneSpec,
                 max_age_seconds: float, timeout: float = 900) -> None:
        safe_branch = branch.replace("/", "_")
        self.path = Path(base_dir) / f"{repo_slug(repo_full_name)}@{safe_branch}"
        self.repo_full_name, self.branch = repo_full_name, branch
        self._spec, self._max_age, self._timeout = spec, max_age_seconds, timeout

    @property
    def _marker(self) -> Path:
        return self.path / ".git" / "snapshot_at"

    @property
    def _failed_marker(self) -> Path:
        return self.path.parent / f".{self.path.name}.clone_failed"

    def _age(self) -> Optional[float]:
        return time.time() - self._marker.stat().st_mtime if self._marker.exists() else None

    def _env(self) -> dict[str, str]:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        if self._spec.username or self._spec.password:
            # Via env, not the URL/argv: stays out of `ps`, .git/config and git's error messages
            token = base64.b64encode(f"{self._spec.username}:{self._spec.password}".encode()).decode()
            env.update({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraHeader",
                        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {token}"})
        return env

    def ensure(self, timeout: Optional[float] = None) -> Path:
        """A snapshot no older than max_age; cloned anew (and swapped in) when it is."""
        age = self._age()
        if age is not None and age < self._max_age:
            return self.path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path.parent / f".{self.path.name}.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            age = self._age()  # another worker may have refreshed it while we waited
            if age is not None and age < self._max_age:
                return self.path
            try:
                self._refresh(min(timeout or self._timeout, self._timeout))
            except SnapshotUnavailable:
                if age is not None:  # an outdated snapshot beats none
                    logger.warning("Using outdated snapshot %s (refresh failed)", self.path.name)
                    return self.path
                raise
        return self.path

    def _refresh(self, timeout: float) -> None:
        failed = self._failed_marker
        if failed.exists() and time.time() - failed.stat().st_mtime < _CLONE_RETRY_AFTER_SECONDS:
            raise SnapshotUnavailable(f"{self.path.name} is not available (last clone failed; pre-create it)")
        tmp = self.path.with_name(self.path.name + ".new")
        shutil.rmtree(tmp, ignore_errors=True)
        started = time.monotonic()
        try:
            proc = subprocess.run(
                ["git", "clone", "--quiet", "--depth", "1", "--single-branch", "--branch", self.branch,
                 self._spec.url, str(tmp)],
                capture_output=True, text=True, timeout=timeout, env=self._env())
            if proc.returncode != 0:
                raise SnapshotUnavailable(f"clone of {self.path.name} failed: {proc.stderr.strip()[:300]}")
        except subprocess.TimeoutExpired:
            shutil.rmtree(tmp, ignore_errors=True)
            failed.touch()
            raise SnapshotUnavailable(f"clone of {self.path.name} timed out after {timeout:.0f}s")
        except SnapshotUnavailable:
            shutil.rmtree(tmp, ignore_errors=True)
            failed.touch()
            raise
        (tmp / ".git" / "snapshot_at").touch()
        # Swap: readers see either the old or the new folder, never a half-cloned one
        old = self.path.with_name(self.path.name + ".old")
        shutil.rmtree(old, ignore_errors=True)
        if self.path.exists():
            self.path.rename(old)
        tmp.rename(self.path)
        shutil.rmtree(old, ignore_errors=True)
        failed.unlink(missing_ok=True)
        logger.info("Branch snapshot %s refreshed in %.0fs", self.path.name, time.monotonic() - started)


def _main(argv: list[str]) -> int:
    """python -m app.context.snapshots <platform> <workspace/repo> <branch>"""
    if len(argv) != 3:
        print(__doc__)
        return 2
    from app.adapters.factory import get_adapter
    from app.config.settings import get_settings

    logging.basicConfig(level=logging.INFO)
    platform, repo_full_name, branch = argv
    settings = get_settings()
    if not shared_snapshot(settings.PROJECTS_SNAPSHOT_DIR, repo_full_name):
        print(f"{repo_slug(repo_full_name)} is not in {settings.PROJECTS_SNAPSHOT_DIR} — "
              "it will not be reviewed agentically, no branch snapshot needed")
        return 1
    workspace, slug = repo_full_name.split("/", 1)
    spec = get_adapter(platform, workspace, slug, settings).git_clone_spec(repo_full_name)
    snapshot = BranchSnapshot(settings.BRANCH_SNAPSHOT_DIR, repo_full_name, branch, spec, max_age_seconds=0,
                              timeout=24 * 3600)
    snapshot._failed_marker.unlink(missing_ok=True)
    print(f"OK: {snapshot.ensure()}")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
