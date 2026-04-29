import os
import subprocess
import sys

import pytest

from redliner.review import load_review


@pytest.fixture
def plan_file(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    plan = tmp_path / "plan.md"
    plan.write_text("a\nb\nc\n")
    return plan


def _run_cli(*args, env=None):
    return subprocess.run(
        [sys.executable, "-m", "redliner", *args],
        capture_output=True,
        text=True,
        env=env,
    )


def test_snapshot_creates_v1(plan_file, tmp_path):
    env = os.environ.copy()
    env["XDG_DATA_HOME"] = str(tmp_path / "xdg")

    result = _run_cli("snapshot", str(plan_file), env=env)
    assert result.returncode == 0
    assert "v1" in result.stdout

    review = load_review(plan_file)
    nums = [v.version for v in review.versions[str(plan_file.resolve())]]
    assert nums == [0, 1]


def test_versions_lists_versions_with_counts(plan_file, tmp_path):
    env = os.environ.copy()
    env["XDG_DATA_HOME"] = str(tmp_path / "xdg")

    _run_cli("snapshot", str(plan_file), env=env)
    _run_cli("comment", str(plan_file), "1", "hello", env=env)
    _run_cli("snapshot", str(plan_file), env=env)

    result = _run_cli("versions", str(plan_file), env=env)
    assert result.returncode == 0
    out = result.stdout
    assert "v0" in out
    assert "v1" in out
    assert "v2" in out
    # The comment is pinned to v1 (head when 'comment' was run, after first snapshot).
    assert "1 pending" in out or "pending: 1" in out
