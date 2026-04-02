import subprocess
from pathlib import Path

import pytest

from compose_review.diff import FileDiff, DiffLine, parse_git_diff


def test_parse_no_changes(git_repo):
    result = parse_git_diff(cwd=git_repo)
    assert result == []


def test_parse_modified_file(git_repo):
    hello = git_repo / "hello.py"
    hello.write_text("def greet():\n    return 'hi'\n")

    result = parse_git_diff(cwd=git_repo)

    assert len(result) == 1
    file_diff = result[0]
    assert file_diff.path == "hello.py"

    kinds = [line.kind for line in file_diff.lines]
    assert "context" in kinds
    assert "removed" in kinds
    assert "added" in kinds

    context_lines = [l for l in file_diff.lines if l.kind == "context"]
    removed_lines = [l for l in file_diff.lines if l.kind == "removed"]
    added_lines = [l for l in file_diff.lines if l.kind == "added"]

    assert context_lines[0].text.strip() == "def greet():"
    assert context_lines[0].old_num == 1
    assert context_lines[0].new_num == 1

    assert removed_lines[0].text.strip() == "return 'hello'"
    assert removed_lines[0].old_num == 2
    assert removed_lines[0].new_num is None

    assert added_lines[0].text.strip() == "return 'hi'"
    assert added_lines[0].old_num is None
    assert added_lines[0].new_num == 2


def test_parse_new_file(git_repo):
    new_file = git_repo / "new.py"
    new_file.write_text("x = 1\ny = 2\n")
    subprocess.run(["git", "add", "new.py"], cwd=git_repo, check=True, capture_output=True)

    result = parse_git_diff(cwd=git_repo)

    assert len(result) == 1
    file_diff = result[0]
    assert file_diff.path == "new.py"

    assert all(l.kind == "added" for l in file_diff.lines)
    assert all(l.old_num is None for l in file_diff.lines)
    new_nums = [l.new_num for l in file_diff.lines]
    assert new_nums == [1, 2]


def test_parse_deleted_file(git_repo):
    hello = git_repo / "hello.py"
    hello.unlink()

    result = parse_git_diff(cwd=git_repo)

    assert len(result) == 1
    file_diff = result[0]
    assert file_diff.path == "hello.py"

    assert all(l.kind == "removed" for l in file_diff.lines)
    assert all(l.new_num is None for l in file_diff.lines)
    old_nums = [l.old_num for l in file_diff.lines]
    assert old_nums == [1, 2]


def test_parse_with_path_filter(git_repo):
    (git_repo / "a.py").write_text("a = 1\n")
    (git_repo / "b.py").write_text("b = 2\n")
    subprocess.run(["git", "add", "a.py", "b.py"], cwd=git_repo, check=True, capture_output=True)

    result = parse_git_diff(path="a.py", cwd=git_repo)

    assert len(result) == 1
    assert result[0].path == "a.py"


def test_parse_multiple_hunks(git_repo):
    lines = ["line1\n", "line2\n", "line3\n", "line4\n", "line5\n",
             "line6\n", "line7\n", "line8\n", "line9\n", "line10\n"]
    hello = git_repo / "hello.py"
    hello.write_text("".join(lines))
    subprocess.run(["git", "add", "hello.py"], cwd=git_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add lines"], cwd=git_repo, check=True, capture_output=True)

    modified = lines[:]
    modified[0] = "LINE1\n"
    modified[9] = "LINE10\n"
    hello.write_text("".join(modified))

    result = parse_git_diff(cwd=git_repo)

    assert len(result) == 1
    file_diff = result[0]

    hunk_starts = []
    prev_kind = None
    in_hunk = False
    for line in file_diff.lines:
        if line.kind in ("added", "removed") and not in_hunk:
            hunk_starts.append(line)
            in_hunk = True
        elif line.kind == "context":
            in_hunk = False

    assert len(hunk_starts) >= 2
