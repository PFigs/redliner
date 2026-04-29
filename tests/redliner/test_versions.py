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
