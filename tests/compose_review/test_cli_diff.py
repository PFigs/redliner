"""Tests for `compose_review diff` CLI subcommand."""

from __future__ import annotations

import json
import subprocess
import sys


def _run_diff(git_repo, *extra_args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "compose_review", "diff", *extra_args],
        cwd=git_repo,
        capture_output=True,
        text=True,
    )


def test_diff_no_changes_exits_zero(git_repo):
    result = _run_diff(git_repo, "--no-open")

    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert data["files"] == 0


def test_diff_with_changes_no_open(git_repo):
    hello = git_repo / "hello.py"
    hello.write_text("def greet():\n    return 'hi'\n")

    result = _run_diff(git_repo, "--no-open")

    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert data["files"] == 1


def test_diff_path_filter(git_repo):
    hello = git_repo / "hello.py"
    hello.write_text("def greet():\n    return 'hi'\n")

    other = git_repo / "other.py"
    other.write_text("x = 1\n")
    subprocess.run(["git", "add", "other.py"], cwd=git_repo, check=True, capture_output=True)

    result = _run_diff(git_repo, "--path", "hello.py", "--no-open")

    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert data["files"] == 1
