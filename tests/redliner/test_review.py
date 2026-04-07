from redliner.review import Review


def test_edit_updates_comment_text():
    review = Review()
    review.add_comment(1, "original")
    result = review.edit(1, "updated")
    assert result is not None
    assert result.text == "updated"
    assert review.comments[0].text == "updated"


def test_edit_nonexistent_returns_none():
    review = Review()
    review.add_comment(1, "hello")
    assert review.edit(999, "new text") is None


def test_edit_preserves_other_fields():
    review = Review()
    comment = review.add_comment(5, "original")
    original_id = comment.id
    original_created = comment.created
    original_status = comment.status
    review.edit(original_id, "edited")
    assert review.comments[0].id == original_id
    assert review.comments[0].line == 5
    assert review.comments[0].created == original_created
    assert review.comments[0].status == original_status
