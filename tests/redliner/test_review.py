from redliner.review import Edit, Review, load_review, save_review

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


def test_edit_dataclass_sets_saved_timestamp():
    e = Edit(file="/tmp/p.md", content="hello\n", diff="")
    assert e.saved  # ISO timestamp set in __post_init__


def test_edit_dataclass_preserves_explicit_saved():
    e = Edit(file="/tmp/p.md", content="x", diff="", saved="2026-04-29T00:00:00+00:00")
    assert e.saved == "2026-04-29T00:00:00+00:00"


def test_set_edit_stores_content_and_computes_diff():
    review = Review()
    edit = review.set_edit(file=FILE, content="line1\nline2-edited\n", original="line1\nline2\n")
    assert edit.file == FILE
    assert edit.content == "line1\nline2-edited\n"
    assert "-line2" in edit.diff
    assert "+line2-edited" in edit.diff
    assert review.edits[FILE] is edit


def test_set_edit_overwrites_previous_edit_for_same_file():
    review = Review()
    review.set_edit(file=FILE, content="v1\n", original="orig\n")
    review.set_edit(file=FILE, content="v2\n", original="orig\n")
    assert review.edits[FILE].content == "v2\n"
    assert len(review.edits) == 1


def test_get_edit_returns_none_when_missing():
    review = Review()
    assert review.get_edit(FILE) is None


def test_get_edit_returns_stored_edit():
    review = Review()
    review.set_edit(file=FILE, content="x\n", original="y\n")
    assert review.get_edit(FILE) is not None
    assert review.get_edit(FILE).content == "x\n"


def test_clear_edit_removes_and_returns_edit():
    review = Review()
    review.set_edit(file=FILE, content="x\n", original="y\n")
    removed = review.clear_edit(FILE)
    assert removed is not None
    assert FILE not in review.edits


def test_clear_edit_returns_none_when_missing():
    review = Review()
    assert review.clear_edit(FILE) is None


def test_save_and_load_roundtrip_edits(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))

    plan = tmp_path / "plan.md"
    plan.write_text("a\nb\nc\n")
    key = str(plan.resolve())

    review = Review()
    review.set_edit(file=key, content="a\nB\nc\n", original="a\nb\nc\n")
    save_review(plan, review)

    loaded = load_review(plan)
    assert key in loaded.edits
    assert loaded.edits[key].content == "a\nB\nc\n"
    assert "-b" in loaded.edits[key].diff
    assert "+B" in loaded.edits[key].diff


def test_save_with_no_edits_does_not_create_jsonl(tmp_path, monkeypatch):
    from redliner.review import edits_path
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    plan = tmp_path / "plan.md"
    plan.write_text("a\n")
    review = Review()
    review.add_comment(str(plan.resolve()), 1, "c")
    save_review(plan, review)
    assert not edits_path(plan).exists()


def test_save_after_clear_edit_removes_jsonl(tmp_path, monkeypatch):
    from redliner.review import edits_path
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    plan = tmp_path / "plan.md"
    plan.write_text("a\n")
    key = str(plan.resolve())
    review = Review()
    review.set_edit(file=key, content="b\n", original="a\n")
    save_review(plan, review)
    assert edits_path(plan).exists()

    review.clear_edit(key)
    save_review(plan, review)
    assert not edits_path(plan).exists()


def test_load_with_no_edits_jsonl_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    plan = tmp_path / "plan.md"
    plan.write_text("a\n")
    loaded = load_review(plan)
    assert loaded.edits == {}
