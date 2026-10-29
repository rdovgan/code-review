import pytest

from app.utils.diff_parser import parse_diff_for_changed_lines


def test_parse_diff_simple_addition():
    diff = """diff --git a/test.txt b/test.txt
index 123..456 789
--- a/test.txt
+++ b/test.txt
@@ -1,2 +1,3 @@
 line 1
+new line 2
 line 3
"""
    result = parse_diff_for_changed_lines(diff)
    assert result == {"test.txt": {2}}


def test_parse_diff_multiple_additions():
    diff = """diff --git a/test.txt b/test.txt
index 123..456 789
--- a/test.txt
+++ b/test.txt
@@ -1,2 +1,5 @@
 line 1
+new line 2
+new line 3
 line 4
+new line 5
"""
    result = parse_diff_for_changed_lines(diff)
    assert result == {"test.txt": {2, 3, 5}}


def test_parse_diff_with_removals():
    diff = """diff --git a/test.txt b/test.txt
index 123..456 789
--- a/test.txt
+++ b/test.txt
@@ -1,5 +1,4 @@
 line 1
 line 2
-removed line 3
 line 4
+new line 5
"""
    result = parse_diff_for_changed_lines(diff)
    # Only added lines should be tracked, not removed ones
    # After removing line 3, old line 4 becomes new line 3, and the added line becomes line 4
    assert result == {"test.txt": {4}}


def test_parse_diff_multiple_files():
    diff = """diff --git a/file1.txt b/file1.txt
index 123..456 789
--- a/file1.txt
+++ b/file1.txt
@@ -1,2 +1,3 @@
 line 1
+new line 2
 line 3
diff --git a/file2.txt b/file2.txt
index abc..def 123
--- a/file2.txt
+++ b/file2.txt
@@ -1,1 +1,2 @@
 line 1
+new line 2
"""
    result = parse_diff_for_changed_lines(diff)
    assert result == {
        "file1.txt": {2},
        "file2.txt": {2}
    }


def test_parse_diff_empty():
    result = parse_diff_for_changed_lines("")
    assert result == {}


def test_parse_diff_new_file():
    diff = """diff --git a/new_file.txt b/new_file.txt
new file mode 100644
index 0000000..1234567
--- /dev/null
+++ b/new_file.txt
@@ -0,0 +1,3 @@
+line 1
+line 2
+line 3
"""
    result = parse_diff_for_changed_lines(diff)
    assert result == {"new_file.txt": {1, 2, 3}}


def test_parse_diff_deleted_file():
    diff = """diff --git a/deleted.txt b/deleted.txt
deleted file mode 100644
index 1234567..0000000
--- a/deleted.txt
+++ /dev/null
@@ -1,3 +0,0 @@
-line 1
-line 2
-line 3
"""
    result = parse_diff_for_changed_lines(diff)
    # No added lines in deleted file
    assert result == {"deleted.txt": set()}


from app.utils.diff_parser import is_ignored, prepare_diff, split_diff_by_file


def _file_diff(path: str, added: int, hunks: int = 1) -> str:
    out = f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
    for h in range(hunks):
        out += f"@@ -{h * 100 + 1},0 +{h * 100 + 1},{added} @@\n" + "".join(f"+line {i}\n" for i in range(added))
    return out


def test_is_ignored_double_star_matches_repo_root():
    assert is_ignored("app.properties", ["**/*.properties"])
    assert is_ignored("src/main/app.properties", ["**/*.properties"])
    assert not is_ignored("src/Main.java", ["**/*.properties"])


def test_split_diff_detects_deleted_and_binary_files():
    diff = (
        "diff --git a/Gone.java b/Gone.java\ndeleted file mode 100644\n--- a/Gone.java\n+++ /dev/null\n"
        "@@ -1 +0,0 @@\n-class Gone {}\n"
        "diff --git a/logo.png b/logo.png\nBinary files a/logo.png and b/logo.png differ\n"
        + _file_diff("Main.java", 2)
    )
    files = {f.path: f for f in split_diff_by_file(diff)}
    assert files["Gone.java"].deleted
    assert files["logo.png"].binary
    assert not files["Main.java"].deleted and not files["Main.java"].binary


def test_prepare_diff_drops_ignored_files_before_applying_the_limit():
    # The ignored file comes first and alone exceeds the limit — it must not eat the budget
    diff = _file_diff("data/big.csv", 50) + _file_diff("src/Main.java", 5)
    prepared = prepare_diff(diff, ["**/*.csv"], max_lines=20)
    assert prepared.files == ["src/Main.java"]
    assert prepared.ignored_files == ["data/big.csv"]
    assert not prepared.truncated
    assert "big.csv" not in prepared.diff


def test_prepare_diff_cuts_at_file_and_hunk_boundaries():
    diff = _file_diff("A.java", 5) + _file_diff("Big.java", 10, hunks=3) + _file_diff("Huge.java", 100) + _file_diff("C.java", 3)
    prepared = prepare_diff(diff, [], max_lines=42)
    assert prepared.files == ["A.java", "Big.java", "C.java"]
    assert prepared.partial_files == ["Big.java"]
    assert prepared.skipped_files == ["Huge.java"]
    assert prepared.reviewed_lines <= 42
    # every hunk kept is complete
    assert prepared.diff.count("+line 9\n") == 2  # Big.java: 2 of 3 hunks fit
    assert prepared.truncated


def test_prepare_diff_non_git_text_falls_back_to_line_cut():
    prepared = prepare_diff("\n".join(f"line {i}" for i in range(10)), [], max_lines=4)
    assert prepared.diff.splitlines() == ["line 0", "line 1", "line 2", "line 3"]
    assert prepared.truncated
