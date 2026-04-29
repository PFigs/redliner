from redliner.review import Comment, Review, Version, save_review

from .conftest import http_get


def test_get_versions_returns_baseline(running_server):
    port = running_server["port"]
    plan = running_server["plan"]
    key = str(plan.resolve())

    data = http_get(port, f"/api/versions?file={key}")

    assert "versions" in data
    assert len(data["versions"]) == 1
    assert data["versions"][0]["version"] == 0
    assert data["versions"][0]["pending"] == 0
    assert data["versions"][0]["resolved"] == 0


def test_get_versions_includes_comment_counts(running_server, tmp_path):
    port = running_server["port"]
    plan = running_server["plan"]
    key = str(plan.resolve())

    review = Review()
    review.versions[key] = [
        Version(file=key, version=0, content="a\nb\nc\n"),
        Version(file=key, version=1, content="a\nB\nc\n"),
    ]
    review.comments.append(
        Comment(id=1, file=key, line=1, text="v0", version=0, status="pending")
    )
    review.comments.append(
        Comment(id=2, file=key, line=2, text="v1a", version=1, status="pending")
    )
    review.comments.append(
        Comment(id=3, file=key, line=2, text="v1b", version=1, status="resolved")
    )
    save_review(plan, review)

    data = http_get(port, f"/api/versions?file={key}")

    by_v = {v["version"]: v for v in data["versions"]}
    assert by_v[0]["pending"] == 1
    assert by_v[0]["resolved"] == 0
    assert by_v[1]["pending"] == 1
    assert by_v[1]["resolved"] == 1
