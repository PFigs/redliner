import pytest

from redliner.review import (
    Comment,
    Review,
    Version,
    comments_path,
    load_review,
    save_review,
    session_dir,
)


def test_version_dataclass_autofills_created():
    v = Version(file="/tmp/x.md", version=1, content="hello\n")
    assert v.created != ""


def test_save_load_roundtrips_versions(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))

    plan = tmp_path / "plan.md"
    plan.write_text("a\nb\n")
    key = str(plan.resolve())

    review = Review()
    review.versions[key] = [
        Version(file=key, version=0, content="a\nb\n"),
        Version(file=key, version=1, content="a\nB\n"),
    ]
    save_review(plan, review)

    loaded = load_review(plan)
    assert len(loaded.versions[key]) == 2
    assert loaded.versions[key][0].version == 0
    assert loaded.versions[key][1].content == "a\nB\n"


def test_load_synthesizes_v0_from_disk_when_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))

    plan = tmp_path / "plan.md"
    plan.write_text("hello\nworld\n")
    key = str(plan.resolve())

    loaded = load_review(plan)
    assert key in loaded.versions
    assert loaded.versions[key][0].version == 0
    assert loaded.versions[key][0].content == "hello\nworld\n"


def test_load_skips_v0_synthesis_when_versions_already_exist(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))

    plan = tmp_path / "plan.md"
    plan.write_text("on disk\n")
    key = str(plan.resolve())

    review = Review()
    review.versions[key] = [Version(file=key, version=0, content="frozen\n")]
    save_review(plan, review)

    loaded = load_review(plan)
    assert loaded.versions[key][0].content == "frozen\n"


def test_comment_defaults_version_to_zero():
    c = Comment(id=1, file="/x", line=1, text="hi")
    assert c.version == 0


def test_comment_roundtrips_version_field(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))

    plan = tmp_path / "plan.md"
    plan.write_text("x\n")
    key = str(plan.resolve())

    review = Review()
    review.comments.append(Comment(id=1, file=key, line=1, text="r1", version=1))
    review.comments.append(Comment(id=2, file=key, line=1, text="r2", version=2))
    save_review(plan, review)

    loaded = load_review(plan)
    versions = {c.id: c.version for c in loaded.comments}
    assert versions == {1: 1, 2: 2}


def test_loads_legacy_comments_without_version_field(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))

    plan = tmp_path / "plan.md"
    plan.write_text("x\n")
    key = str(plan.resolve())

    # Simulate a pre-versioning comments.jsonl: no `version` field.
    sd = session_dir(plan)
    sd.mkdir(parents=True, exist_ok=True)
    legacy_row = (
        '{"id": 1, "file": "' + key + '", "line": 1, '
        '"text": "old", "status": "pending", "created": "2026-01-01T00:00:00+00:00"}'
    )
    comments_path(plan).write_text(legacy_row + "\n")

    loaded = load_review(plan)
    assert loaded.comments[0].version == 0


def test_head_version_returns_zero_for_baseline_only():
    review = Review()
    review.versions["/x"] = [Version(file="/x", version=0, content="a\n")]
    assert review.head_version("/x") == 0


def test_head_version_returns_highest():
    review = Review()
    review.versions["/x"] = [
        Version(file="/x", version=0, content="a\n"),
        Version(file="/x", version=1, content="b\n"),
        Version(file="/x", version=2, content="c\n"),
    ]
    assert review.head_version("/x") == 2


def test_head_version_zero_for_unknown_file():
    review = Review()
    assert review.head_version("/never-seen") == 0


def test_snapshot_appends_with_next_version_number():
    review = Review()
    review.versions["/x"] = [Version(file="/x", version=0, content="a\n")]
    new = review.snapshot("/x", "a\nb\n")
    assert new.version == 1
    assert new.content == "a\nb\n"
    assert review.versions["/x"][-1] is new


def test_snapshot_allows_noop_content():
    review = Review()
    review.versions["/x"] = [Version(file="/x", version=0, content="same\n")]
    new = review.snapshot("/x", "same\n")
    assert new.version == 1


def test_snapshot_initializes_versions_list_if_missing():
    review = Review()
    new = review.snapshot("/x", "first\n")
    assert new.version == 0
    assert review.versions["/x"][0] is new


def test_version_content_returns_content():
    review = Review()
    review.versions["/x"] = [
        Version(file="/x", version=0, content="zero\n"),
        Version(file="/x", version=1, content="one\n"),
    ]
    assert review.version_content("/x", 1) == "one\n"


def test_version_content_raises_for_unknown():
    review = Review()
    review.versions["/x"] = [Version(file="/x", version=0, content="a\n")]
    with pytest.raises(ValueError):
        review.version_content("/x", 99)


def test_version_diff_returns_unified_diff_vs_previous():
    review = Review()
    review.versions["/x"] = [
        Version(file="/x", version=0, content="a\nb\n"),
        Version(file="/x", version=1, content="a\nB\n"),
    ]
    diff = review.version_diff("/x", 1)
    assert "-b" in diff
    assert "+B" in diff


def test_version_diff_raises_for_v0():
    review = Review()
    review.versions["/x"] = [Version(file="/x", version=0, content="a\n")]
    with pytest.raises(ValueError):
        review.version_diff("/x", 0)


def test_comments_for_version_filters_by_file_and_version():
    review = Review()
    review.comments.append(Comment(id=1, file="/x", line=1, text="v0a", version=0))
    review.comments.append(Comment(id=2, file="/x", line=1, text="v1a", version=1))
    review.comments.append(Comment(id=3, file="/y", line=1, text="other", version=1))
    result = review.comments_for_version("/x", 1)
    ids = {c.id for c in result}
    assert ids == {2}
