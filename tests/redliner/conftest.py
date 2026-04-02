import subprocess

import pytest


def _git(tmp_path, *args):
    subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)


@pytest.fixture
def git_repo(tmp_path):
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "test@test.com")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "commit.gpgsign", "false")

    hello = tmp_path / "hello.py"
    hello.write_text("def greet():\n    return 'hello'\n")

    _git(tmp_path, "add", "hello.py")
    _git(tmp_path, "commit", "-m", "init")

    return tmp_path
