"""Tests for `redliner status` (with edits) and `redliner edits` subcommands."""

from __future__ import annotations

import json
import os
import subprocess
import sys


def _run(tmp_path, env, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "redliner", *args],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
    )


def _env(tmp_path):
    e = os.environ.copy()
    e["XDG_DATA_HOME"] = str(tmp_path / "xdg")
    return e


def _seed_edit(tmp_path, monkeypatch, plan, content, original):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    from redliner.review import Review, save_review
    review = Review()
    review.set_edit(file=str(plan.resolve()), content=content, original=original)
    save_review(plan, review)


def test_status_no_edits(tmp_path):
    plan = tmp_path / "plan.md"
    plan.write_text("a\nb\n")
    result = _run(tmp_path, _env(tmp_path), "status", str(plan))
    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert data["has_edits"] is False
    assert "edits_path" not in data


def test_status_with_edits(tmp_path, monkeypatch):
    plan = tmp_path / "plan.md"
    plan.write_text("a\nb\n")
    _seed_edit(tmp_path, monkeypatch, plan, "A\nB\n", "a\nb\n")

    result = _run(tmp_path, _env(tmp_path), "status", str(plan))
    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert data["has_edits"] is True
    assert data["edits_path"].endswith("edits.jsonl")


def test_edits_subcommand_prints_diff(tmp_path, monkeypatch):
    plan = tmp_path / "plan.md"
    plan.write_text("a\nb\n")
    _seed_edit(tmp_path, monkeypatch, plan, "a\nB\n", "a\nb\n")

    result = _run(tmp_path, _env(tmp_path), "edits", str(plan))
    assert result.returncode == 0
    assert "-b" in result.stdout
    assert "+B" in result.stdout


def test_edits_subcommand_no_edits_exits_one(tmp_path):
    plan = tmp_path / "plan.md"
    plan.write_text("a\n")
    result = _run(tmp_path, _env(tmp_path), "edits", str(plan))
    assert result.returncode == 1
    assert "No edits for" in result.stderr
