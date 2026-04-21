from pathlib import Path

from redliner.review import Review, load_review, save_review


FILE = "/tmp/plan.md"


def test_edit_updates_comment_text():
    review = Review()
    review.add_comment(FILE, 1, "original")
    result = review.edit(1, "updated")
    assert result is not None
    assert result.text == "updated"
    assert review.comments[0].text == "updated"


def test_edit_nonexistent_returns_none():
    review = Review()
    review.add_comment(FILE, 1, "hello")
    assert review.edit(999, "new text") is None


def test_edit_preserves_other_fields():
    review = Review()
    comment = review.add_comment(FILE, 5, "original")
    original_id = comment.id
    original_created = comment.created
    original_status = comment.status
    review.edit(original_id, "edited")
    assert review.comments[0].id == original_id
    assert review.comments[0].line == 5
    assert review.comments[0].file == FILE
    assert review.comments[0].created == original_created
    assert review.comments[0].status == original_status


def test_comment_includes_file_path():
    review = Review()
    comment = review.add_comment("/abs/path/file.py", 10, "check this")
    assert comment.file == "/abs/path/file.py"


def test_save_and_load_roundtrip_jsonl(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))

    plan = tmp_path / "plan.md"
    plan.write_text("a\nb\nc\n")
    key = str(plan.resolve())

    review = Review()
    review.add_comment(key, 1, "first")
    review.add_comment(key, 2, "second")
    review.resolve(1)
    save_review(plan, review)

    loaded = load_review(plan)
    assert len(loaded.comments) == 2
    assert loaded.comments[0].text == "first"
    assert loaded.comments[0].status == "resolved"
    assert loaded.comments[0].file == key
    assert loaded.comments[1].text == "second"


def test_session_stores_multiple_files_in_one_jsonl(tmp_path, monkeypatch):
    from redliner.review import comments_path

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))

    session = tmp_path / "repo"
    session.mkdir()
    file_a = str((session / "a.py").resolve())
    file_b = str((session / "b.py").resolve())

    review = Review()
    review.add_comment(file_a, 1, "on a")
    review.add_comment(file_b, 3, "on b")
    save_review(session, review)

    content = comments_path(session).read_text().strip().splitlines()
    assert len(content) == 2

    loaded = load_review(session)
    assert {c.file for c in loaded.comments} == {file_a, file_b}
    assert len(loaded.comments_for(file_a)) == 1
    assert len(loaded.comments_for(file_b)) == 1


def test_approve_is_per_file(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))

    session = tmp_path / "repo"
    session.mkdir()
    file_a = str((session / "a.py").resolve())
    file_b = str((session / "b.py").resolve())

    review = Review()
    review.add_comment(file_a, 1, "ok")
    review.add_comment(file_b, 1, "blocker")
    review.resolve_all(file=file_a)

    assert review.approve(file_a) is True
    assert review.approve(file_b) is False
    assert review.status_for(file_a) == "approved"
    assert review.status_for(file_b) == "in_review"
