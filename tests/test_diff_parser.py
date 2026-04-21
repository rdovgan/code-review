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
