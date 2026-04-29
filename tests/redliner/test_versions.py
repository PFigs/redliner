from pathlib import Path

from redliner.review import Review, Version, load_review, save_review


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
