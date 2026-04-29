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
