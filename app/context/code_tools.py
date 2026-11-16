"""Read-only code tools the review agent calls: read files, grep, find files, list dirs.

Every tool reads a fixed git revision of a mirror (the PR's head commit, or a context
repo's default branch). Tool errors come back as text for the model — they never raise.
"""
import fnmatch
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_GIT_TIMEOUT = 30
_MAX_READ_LINES = 400
_MAX_GREP_MATCHES = 80
_MAX_LINE_CHARS = 300
_MAX_FOUND_FILES = 100


@dataclass(frozen=True)
class RepoView:
    """One repository at one revision, as the agent sees it."""
    name: str
    git_dir: Path
    rev: str
    description: str  # e.g. "this PR, at its head commit"


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "read_file",
        "description": "Read a file, line-numbered. Reads at most 400 lines per call; use start_line/end_line for long files.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path from the repository root"},
                "repo": {"type": "string", "description": "Repository name; defaults to the PR's repository"},
                "start_line": {"type": "integer", "description": "First line, 1-based (default 1)"},
                "end_line": {"type": "integer", "description": "Last line, inclusive"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "grep",
        "description": "Search file contents (git grep, extended regex). Returns path:line:text, at most 80 matches. "
                       "Use it to find callers, definitions and usages.",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Extended regular expression, or a literal if fixed_string"},
                "repo": {"type": "string", "description": "Repository name; defaults to the PR's repository"},
                "path_glob": {"type": "string", "description": "Limit to paths matching this glob, e.g. '**/*.java' or 'src/main/**'"},
                "fixed_string": {"type": "boolean", "description": "Treat pattern as a literal string"},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "find_files",
        "description": "Find files whose path matches a glob, e.g. '*BookingService*.java' or 'src/**/dto/*.java'. At most 100 results.",
        "parameters": {
            "type": "object",
            "properties": {
                "name_glob": {"type": "string", "description": "Glob matched against the file name and the full path"},
                "repo": {"type": "string", "description": "Repository name; defaults to the PR's repository"},
            },
            "required": ["name_glob"],
        },
    },
    {
        "name": "list_dir",
        "description": "List a directory's entries (directories end with '/').",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path from the repository root; empty for the root"},
                "repo": {"type": "string", "description": "Repository name; defaults to the PR's repository"},
            },
        },
    },
]


class CodeTools:
    def __init__(self, repos: list[RepoView], max_result_chars: int = 12_000) -> None:
        if not repos:
            raise ValueError("CodeTools needs at least the PR's repository")
        self._repos = {r.name: r for r in repos}
        self._primary = repos[0].name
        self._max_chars = max_result_chars
        self._file_lists: dict[str, list[str]] = {}
        self.calls = 0

    def describe(self) -> str:
        lines = [f"- {r.name}: {r.description}" for r in self._repos.values()]
        return "AVAILABLE_REPOSITORIES (pass the name as `repo`; the first one is the default):\n" + "\n".join(lines)

    @property
    def repo_names(self) -> list[str]:
        return list(self._repos)

    # --- dispatch ---

    def run(self, name: str, args: Any) -> str:
        """Execute one tool call; the result is always text for the model."""
        self.calls += 1
        if not isinstance(args, dict):
            return "ERROR: arguments must be a JSON object"
        handler = {
            "read_file": self._read_file,
            "grep": self._grep,
            "find_files": self._find_files,
            "list_dir": self._list_dir,
        }.get(name)
        if handler is None:
            return f"ERROR: unknown tool {name!r}"
        try:
            repo = self._repo(args.get("repo"))
            result = handler(repo, args)
        except _ToolError as exc:
            result = f"ERROR: {exc}"
        except Exception as exc:  # a tool must never break the agent loop
            logger.warning("Tool %s failed: %s", name, exc)
            result = f"ERROR: {type(exc).__name__}"
        return self._cap(result)

    # --- tools ---

    def _read_file(self, repo: RepoView, args: dict) -> str:
        path = _clean_path(args.get("path"))
        if not path:
            raise _ToolError("path is required")
        content = self._git(repo, "show", f"{repo.rev}:{path}", missing=f"file not found: {path}")
        lines = content.splitlines()
        start = max(1, _int(args.get("start_line"), 1))
        end = _int(args.get("end_line"), start + _MAX_READ_LINES - 1)
        end = min(end, start + _MAX_READ_LINES - 1, len(lines))
        if start > len(lines):
            return f"{path} has {len(lines)} lines"
        body = "\n".join(f"{n:>5}| {lines[n - 1]}" for n in range(start, end + 1))
        more = f"\n… {len(lines) - end} more lines (file has {len(lines)})" if end < len(lines) else ""
        return f"{repo.name}:{path} lines {start}-{end}\n{body}{more}"

    def _grep(self, repo: RepoView, args: dict) -> str:
        pattern = args.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            raise _ToolError("pattern is required")
        cmd = ["grep", "-n", "-I", "--no-color", "-F" if args.get("fixed_string") else "-E", "-e", pattern, repo.rev, "--"]
        glob = args.get("path_glob")
        if isinstance(glob, str) and glob.strip():
            cmd.append(f":(glob){_clean_path(glob)}")
        out = self._git(repo, *cmd, no_match_ok=True)
        if not out.strip():
            return "no matches"
        prefix = f"{repo.rev}:"
        matches = []
        for line in out.splitlines():
            line = line[len(prefix):] if line.startswith(prefix) else line
            matches.append(line if len(line) <= _MAX_LINE_CHARS else line[:_MAX_LINE_CHARS] + "…")
        shown = matches[:_MAX_GREP_MATCHES]
        tail = f"\n… {len(matches) - len(shown)} more matches — narrow the pattern or path_glob" if len(matches) > len(shown) else ""
        return "\n".join(shown) + tail

    def _find_files(self, repo: RepoView, args: dict) -> str:
        glob = args.get("name_glob")
        if not isinstance(glob, str) or not glob.strip():
            raise _ToolError("name_glob is required")
        found = [p for p in self._all_files(repo)
                 if fnmatch.fnmatch(p, glob) or fnmatch.fnmatch(p.rsplit("/", 1)[-1], glob)]
        if not found:
            return "no files found"
        tail = f"\n… {len(found) - _MAX_FOUND_FILES} more" if len(found) > _MAX_FOUND_FILES else ""
        return "\n".join(found[:_MAX_FOUND_FILES]) + tail

    def _list_dir(self, repo: RepoView, args: dict) -> str:
        path = _clean_path(args.get("path") or "")
        treeish = f"{repo.rev}:{path}" if path else repo.rev
        out = self._git(repo, "ls-tree", treeish, missing=f"directory not found: {path}")
        entries = []
        for line in out.splitlines():
            meta, _, name = line.partition("\t")
            entries.append(name + "/" if " tree " in f" {meta} " else name)
        return "\n".join(entries) or "empty directory"

    # --- helpers ---

    def _repo(self, name: Optional[str]) -> RepoView:
        if not name:
            return self._repos[self._primary]
        repo = self._repos.get(name)
        if repo is None:
            raise _ToolError(f"unknown repo {name!r}; available: {', '.join(self._repos)}")
        return repo

    def _all_files(self, repo: RepoView) -> list[str]:
        if repo.name not in self._file_lists:
            out = self._git(repo, "ls-tree", "-r", "--name-only", repo.rev)
            self._file_lists[repo.name] = out.splitlines()
        return self._file_lists[repo.name]

    def _git(self, repo: RepoView, *args: str, missing: str = "", no_match_ok: bool = False) -> str:
        try:
            proc = subprocess.run(["git", f"--git-dir={repo.git_dir}", *args],
                                  capture_output=True, text=True, timeout=_GIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise _ToolError("search took too long — narrow it down")
        if proc.returncode == 0:
            return proc.stdout
        if no_match_ok and proc.returncode == 1 and not proc.stderr.strip():
            return ""
        if missing:
            raise _ToolError(missing)
        raise _ToolError(proc.stderr.strip()[:300] or f"git {args[0]} failed")

    def _cap(self, text: str) -> str:
        if len(text) <= self._max_chars:
            return text
        return text[:self._max_chars] + "\n… [output truncated — narrow the request]"


class _ToolError(Exception):
    pass


def _clean_path(path: Any) -> str:
    return str(path or "").strip().lstrip("/").removeprefix("./")


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
