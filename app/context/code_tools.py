"""Read-only code tools the review agent calls: read files, grep, find files, list dirs.

Each repository is a working-tree snapshot (app/context/snapshots.py). For the PR's own
repository the files changed in the PR are laid over the snapshot at their PR version
(and files the PR deletes or renames away are hidden), so the agent sees the code as it
is with this PR applied — without anyone checking out the PR branch.

Tool errors come back as text for the model — they never raise.
"""
import fnmatch
import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from app.context.snapshots import git

logger = logging.getLogger(__name__)

_MAX_READ_LINES = 400
_MAX_GREP_MATCHES = 80
_MAX_LINE_CHARS = 300
_MAX_FOUND_FILES = 100


@dataclass
class RepoView:
    """One repository as the agent sees it."""
    name: str
    root: Path
    description: str
    # PR overlay: path → content at the PR's head, or None for a file the PR removes
    overlay: dict[str, Optional[str]] = field(default_factory=dict)
    # Changed files not prefetched: read_file fetches them on demand through `fetch`
    lazy_paths: set[str] = field(default_factory=set)
    fetch: Optional[Callable[[str], Optional[str]]] = None


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
        "description": "Search file contents (extended regex). Returns path:line:text, at most 80 matches. "
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
            result = handler(self._repo(args.get("repo")), args)
        except _ToolError as exc:
            result = f"ERROR: {exc}"
        except subprocess.TimeoutExpired:
            result = "ERROR: search took too long — narrow it down"
        except Exception as exc:  # a tool must never break the agent loop
            logger.warning("Tool %s failed: %s", name, exc)
            result = f"ERROR: {type(exc).__name__}"
        return self._cap(result)

    # --- tools ---

    def _read_file(self, repo: RepoView, args: dict) -> str:
        path = _clean_path(args.get("path"))
        if not path:
            raise _ToolError("path is required")
        content = self._content(repo, path)
        if content is None:
            raise _ToolError(f"file not found: {path}")
        lines = content.splitlines()
        start = max(1, _int(args.get("start_line"), 1))
        if start > len(lines):
            return f"{path} has {len(lines)} lines"
        end = _int(args.get("end_line"), start + _MAX_READ_LINES - 1)
        end = max(start, min(end, start + _MAX_READ_LINES - 1, len(lines)))
        body = "\n".join(f"{n:>5}| {lines[n - 1]}" for n in range(start, end + 1))
        more = f"\n… {len(lines) - end} more lines (file has {len(lines)})" if end < len(lines) else ""
        return f"{repo.name}:{path} lines {start}-{end}\n{body}{more}"

    def _grep(self, repo: RepoView, args: dict) -> str:
        pattern = args.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            raise _ToolError("pattern is required")
        fixed = bool(args.get("fixed_string"))
        glob = _clean_path(args.get("path_glob")) if isinstance(args.get("path_glob"), str) else ""

        cmd = ["grep", "-n", "-I", "--no-color", "-F" if fixed else "-E", "-e", pattern, "--"]
        if glob:
            cmd.append(f":(glob){glob}")
        proc = git(repo.root, *cmd)
        if proc.returncode not in (0, 1) or (proc.returncode == 1 and proc.stderr.strip()):
            raise _ToolError(proc.stderr.strip()[:300] or "grep failed")
        # Snapshot matches, except in files the PR changed — those are searched in their PR version
        matches = [m for m in proc.stdout.splitlines() if m.split(":", 1)[0] not in repo.overlay]
        matches += self._grep_overlay(repo, pattern, fixed, glob)
        if not matches:
            return "no matches"
        matches.sort()
        shown = [m if len(m) <= _MAX_LINE_CHARS else m[:_MAX_LINE_CHARS] + "…" for m in matches[:_MAX_GREP_MATCHES]]
        tail = (f"\n… {len(matches) - len(shown)} more matches — narrow the pattern or path_glob"
                if len(matches) > len(shown) else "")
        return "\n".join(shown) + tail

    def _grep_overlay(self, repo: RepoView, pattern: str, fixed: bool, glob: str) -> list[str]:
        if not repo.overlay:
            return []
        try:
            regex = re.compile(re.escape(pattern) if fixed else pattern)
        except re.error:
            return []  # git accepted the ERE, Python can't: the PR's versions just aren't searched
        out = []
        for path, content in repo.overlay.items():
            if content is None or (glob and not _glob_match(path, glob)):
                continue
            for n, line in enumerate(content.splitlines(), 1):
                if regex.search(line):
                    out.append(f"{path}:{n}:{line}")
        return out

    def _find_files(self, repo: RepoView, args: dict) -> str:
        glob = args.get("name_glob")
        if not isinstance(glob, str) or not glob.strip():
            raise _ToolError("name_glob is required")
        found = [p for p in self._files(repo) if _glob_match(p, glob) or fnmatch.fnmatch(p.rsplit("/", 1)[-1], glob)]
        if not found:
            return "no files found"
        tail = f"\n… {len(found) - _MAX_FOUND_FILES} more" if len(found) > _MAX_FOUND_FILES else ""
        return "\n".join(found[:_MAX_FOUND_FILES]) + tail

    def _list_dir(self, repo: RepoView, args: dict) -> str:
        path = _clean_path(args.get("path") or "").rstrip("/")
        prefix = f"{path}/" if path else ""
        entries = set()
        for file in self._files(repo):
            if file.startswith(prefix):
                head, sep, _ = file[len(prefix):].partition("/")
                entries.add(head + "/" if sep else head)
        if not entries:
            raise _ToolError(f"directory not found: {path}")
        return "\n".join(sorted(entries))

    # --- helpers ---

    def _repo(self, name: Optional[str]) -> RepoView:
        if not name:
            return self._repos[self._primary]
        repo = self._repos.get(name)
        if repo is None:
            raise _ToolError(f"unknown repo {name!r}; available: {', '.join(self._repos)}")
        return repo

    def _files(self, repo: RepoView) -> list[str]:
        """Tracked files of the snapshot with the PR's added/removed files applied."""
        if repo.name not in self._file_lists:
            proc = git(repo.root, "ls-files")
            if proc.returncode != 0:
                raise _ToolError(proc.stderr.strip()[:300] or "cannot list files")
            files = set(proc.stdout.splitlines())
            for path, content in repo.overlay.items():
                (files.discard if content is None else files.add)(path)
            files |= repo.lazy_paths
            self._file_lists[repo.name] = sorted(files)
        return self._file_lists[repo.name]

    def _content(self, repo: RepoView, path: str) -> Optional[str]:
        if path in repo.overlay:
            return repo.overlay[path]
        if path in repo.lazy_paths and repo.fetch is not None:
            content = repo.fetch(path)
            repo.overlay[path] = content
            return content
        root = repo.root.resolve()
        target = (root / path).resolve()
        if root not in target.parents or ".git" in target.relative_to(root).parts or not target.is_file():
            return None
        return target.read_text(errors="replace")

    def _cap(self, text: str) -> str:
        if len(text) <= self._max_chars:
            return text
        return text[:self._max_chars] + "\n… [output truncated — narrow the request]"


class _ToolError(Exception):
    pass


def _clean_path(path: Any) -> str:
    return str(path or "").strip().lstrip("/").removeprefix("./")


def _glob_match(path: str, glob: str) -> bool:
    """Glob with ** — fnmatch's * already crosses '/', so '**/' also has to match nothing."""
    return fnmatch.fnmatch(path, glob) or (glob.startswith("**/") and fnmatch.fnmatch(path, glob[3:]))


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
