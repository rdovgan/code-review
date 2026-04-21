import logging
from typing import Dict, Set

logger = logging.getLogger(__name__)


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
