import fnmatch
import logging
from dataclasses import dataclass, field
from typing import Dict, Set

logger = logging.getLogger(__name__)


def is_ignored(path: str, patterns: list[str]) -> bool:
    """fnmatch with one fix: a leading '**/' also matches files in the repo root."""
    for pat in patterns:
        if fnmatch.fnmatch(path, pat):
            return True
        if pat.startswith("**/") and fnmatch.fnmatch(path, pat[3:]):
            return True
    return False


@dataclass
class FileDiff:
    path: str
    text: str
    deleted: bool = False
    binary: bool = False

    @property
    def line_count(self) -> int:
        return len(self.text.splitlines())


def split_diff_by_file(diff: str) -> list[FileDiff]:
    """Split a unified diff into per-file sections (path = new path, old path for deletions)."""
    sections: list[list[str]] = []
    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git ") or not sections:
            sections.append([])
        sections[-1].append(line)

    files: list[FileDiff] = []
    for lines in sections:
        text = "".join(lines)
        header = lines[0]
        if not header.startswith("diff --git "):
            continue  # preamble before the first file header
        parts = header.split()
        a_path = parts[2][2:] if len(parts) >= 3 else ""
        b_path = parts[3][2:] if len(parts) >= 4 else a_path
        deleted = any(l.startswith("+++ /dev/null") or l.startswith("deleted file mode") for l in lines[:6])
        binary = not any(l.startswith("@@ ") for l in lines) and any(
            l.startswith("Binary files") or l.startswith("GIT binary patch") for l in lines
        )
        files.append(FileDiff(path=a_path if deleted else b_path, text=text, deleted=deleted, binary=binary))
    return files


def _take_hunks(file_diff: FileDiff, budget: int) -> str:
    """Return the file header plus as many leading hunks as fit into `budget` lines ('' if none fit)."""
    lines = file_diff.text.splitlines(keepends=True)
    first_hunk = next((i for i, l in enumerate(lines) if l.startswith("@@ ")), len(lines))
    taken = lines[:first_hunk]
    hunk: list[str] = []
    hunks_taken = 0
    for line in lines[first_hunk:] + ["@@ end"]:
        if line.startswith("@@ ") and hunk:
            if len(taken) + len(hunk) > budget:
                break
            taken += hunk
            hunks_taken += 1
            hunk = []
        hunk.append(line)
    return "".join(taken) if hunks_taken else ""


@dataclass
class PreparedDiff:
    diff: str
    original_lines: int
    reviewed_lines: int
    files: list[str] = field(default_factory=list)          # reviewed (fully or partially)
    partial_files: list[str] = field(default_factory=list)  # cut at a hunk boundary
    skipped_files: list[str] = field(default_factory=list)  # did not fit into max_lines
    ignored_files: list[str] = field(default_factory=list)  # ignore_paths / deleted / binary

    @property
    def truncated(self) -> bool:
        return bool(self.partial_files or self.skipped_files)


def prepare_diff(diff: str, ignore_paths: list[str], max_lines: int) -> PreparedDiff:
    """Drop ignored, deleted and binary files, then fit the rest into `max_lines`.

    Truncation happens at file and hunk boundaries, never mid-hunk: a file that does not
    fit as a whole contributes its leading hunks that do, and smaller files after it still
    get a chance to use the remaining budget.
    """
    file_diffs = split_diff_by_file(diff)
    result = PreparedDiff(diff="", original_lines=len(diff.splitlines()), reviewed_lines=0)
    if not file_diffs:
        # Not a git-style diff — nothing to split, fall back to a plain line cut
        lines = diff.splitlines()
        result.diff = "\n".join(lines[:max_lines])
        result.reviewed_lines = min(len(lines), max_lines)
        if len(lines) > max_lines:
            result.partial_files.append("<diff>")
        return result

    parts: list[str] = []
    remaining = max_lines
    for fd in file_diffs:
        if fd.deleted or fd.binary or is_ignored(fd.path, ignore_paths):
            result.ignored_files.append(fd.path)
            continue
        if fd.line_count <= remaining:
            parts.append(fd.text)
            remaining -= fd.line_count
            result.files.append(fd.path)
            continue
        partial = _take_hunks(fd, remaining)
        if partial:
            parts.append(partial)
            remaining -= len(partial.splitlines())
            result.files.append(fd.path)
            result.partial_files.append(fd.path)
        else:
            result.skipped_files.append(fd.path)

    result.diff = "".join(parts)
    result.reviewed_lines = max_lines - remaining
    return result


def parse_diff_for_changed_lines(diff: str) -> Dict[str, Set[int]]:
    """
    Parse git diff and extract line numbers that were added/changed for each file.
    
    Args:
        diff: Git diff output in unified format
        
    Returns:
        Dictionary mapping file paths to sets of line numbers that were added
    """
    changed_lines: Dict[str, Set[int]] = {}
    current_file = None
    current_line_start = 0
    
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4:
                a_path = parts[2][2:]  # Remove 'a/' prefix
                b_path = parts[3][2:]  # Remove 'b/' prefix
                current_file = b_path if b_path != "/dev/null" else a_path
                if current_file not in changed_lines:
                    changed_lines[current_file] = set()
        elif line.startswith("@@ "):
            # Parse hunk header: @@ -old_start,old_count +new_start,new_count @@
            try:
                header = line.split("@@")[1].strip()
                new_part = [p for p in header.split() if p.startswith("+")]
                if new_part:
                    new_info = new_part[0][1:].split(",")
                    current_line_start = int(new_info[0])
            except (ValueError, IndexError):
                current_line_start = 0
        elif line.startswith("+") and not line.startswith("+++") and current_file:
            # This is an added line
            changed_lines[current_file].add(current_line_start)
            current_line_start += 1
        elif line.startswith("-") and not line.startswith("---"):
            # This is a removed line - don't increment line number for removed lines
            pass
        elif line.startswith(" ") or line.startswith("\\"):
            # Context line or newline marker
            current_line_start += 1
        else:
            # Other lines - increment line number
            current_line_start += 1
    
    return changed_lines
