from concurrent.futures import ThreadPoolExecutor

from redliner.review import Comment, Review, Version, load_review, save_review

from .conftest import http_get, http_post


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


def test_get_version_full_returns_content(running_server, tmp_path):
    port = running_server["port"]
    plan = running_server["plan"]
    key = str(plan.resolve())

    review = Review()
    review.versions[key] = [
        Version(file=key, version=0, content="a\nb\n"),
        Version(file=key, version=1, content="a\nB\n"),
    ]
    save_review(plan, review)

    data = http_get(port, f"/api/version?file={key}&n=1&view=full")

    assert data["content"] == "a\nB\n"
    assert data["diff"] == ""
    assert data["comments"] == []


def test_get_version_diff_returns_unified_diff(running_server, tmp_path):
    port = running_server["port"]
    plan = running_server["plan"]
    key = str(plan.resolve())

    review = Review()
    review.versions[key] = [
        Version(file=key, version=0, content="a\nb\n"),
        Version(file=key, version=1, content="a\nB\n"),
    ]
    save_review(plan, review)

    data = http_get(port, f"/api/version?file={key}&n=1&view=diff")

    assert "-b" in data["diff"]
    assert "+B" in data["diff"]
    assert data["content"] == "a\nB\n"


def test_get_version_diff_for_v0_returns_full(running_server):
    port = running_server["port"]
    plan = running_server["plan"]
    key = str(plan.resolve())

    data = http_get(port, f"/api/version?file={key}&n=0&view=diff")

    assert data["diff"] == ""
    assert data["content"] == "a\nb\nc\n"


def test_get_version_returns_pinned_comments(running_server, tmp_path):
    port = running_server["port"]
    plan = running_server["plan"]
    key = str(plan.resolve())

    review = Review()
    review.versions[key] = [
        Version(file=key, version=0, content="a\nb\n"),
        Version(file=key, version=1, content="a\nB\n"),
    ]
    review.comments.append(Comment(id=10, file=key, line=2, text="on v1", version=1))
    review.comments.append(Comment(id=11, file=key, line=1, text="on v0", version=0))
    save_review(plan, review)

    data = http_get(port, f"/api/version?file={key}&n=1&view=full")

    ids = {c["id"] for c in data["comments"]}
    assert ids == {10}


def test_get_version_unknown_returns_404(running_server):
    from http.client import HTTPConnection
    port = running_server["port"]
    plan = running_server["plan"]
    key = str(plan.resolve())

    conn = HTTPConnection("127.0.0.1", port)
    conn.request("GET", f"/api/version?file={key}&n=99&view=full")
    resp = conn.getresponse()
    status = resp.status
    conn.close()
    assert status == 404


def test_snapshot_creates_new_version(running_server, tmp_path):
    port = running_server["port"]
    plan = running_server["plan"]
    key = str(plan.resolve())

    # Save an edit so the snapshot has new content.
    status, _ = http_post(port, "/api/edit-content", {"content": "a\nB\nc\n"})
    assert status == 200

    status, body = http_post(port, "/api/snapshot", {"file": key})
    assert status == 200
    assert body["version"] == 1
    assert body["content"] == "a\nB\nc\n"

    listing = http_get(port, f"/api/versions?file={key}")
    nums = [v["version"] for v in listing["versions"]]
    assert nums == [0, 1]


def test_snapshot_falls_back_to_head_when_no_edit(running_server, tmp_path):
    port = running_server["port"]
    plan = running_server["plan"]
    key = str(plan.resolve())

    status, body = http_post(port, "/api/snapshot", {"file": key})
    assert status == 200
    assert body["version"] == 1
    assert body["content"] == "a\nb\nc\n"  # head (v0) content


def test_snapshot_serializes_concurrent_writes(running_threaded_server, tmp_path):
    port = running_threaded_server["port"]
    plan = running_threaded_server["plan"]
    key = str(plan.resolve())

    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = [ex.submit(http_post, port, "/api/snapshot", {"file": key}) for _ in range(4)]
        results = [f.result() for f in futures]

    versions = sorted(body["version"] for status, body in results)
    assert versions == [1, 2, 3, 4]


def test_comment_post_uses_head_version_by_default(running_server, tmp_path):
    port = running_server["port"]
    plan = running_server["plan"]
    key = str(plan.resolve())

    # Take a snapshot so head is v1.
    http_post(port, "/api/snapshot", {"file": key})

    status, _ = http_post(port, "/api/comment", {"line": 1, "text": "ping"})
    assert status == 200

    listing = http_get(port, f"/api/versions?file={key}")
    by_v = {v["version"]: v for v in listing["versions"]}
    assert by_v[1]["pending"] == 1
    assert by_v[0]["pending"] == 0


def test_comment_post_explicit_version_pins_to_it(running_server, tmp_path):
    port = running_server["port"]
    plan = running_server["plan"]
    key = str(plan.resolve())

    http_post(port, "/api/snapshot", {"file": key})  # v1 exists

    status, _ = http_post(port, "/api/comment", {"line": 1, "text": "back to v0", "version": 0})
    assert status == 200

    listing = http_get(port, f"/api/versions?file={key}")
    by_v = {v["version"]: v for v in listing["versions"]}
    assert by_v[0]["pending"] == 1
    assert by_v[1]["pending"] == 0


def test_comment_post_invalid_version_400(running_server, tmp_path):
    port = running_server["port"]

    status, body = http_post(port, "/api/comment", {"line": 1, "text": "ping", "version": 99})
    assert status == 400
    assert "version" in body.get("error", "").lower()


def test_save_edit_content_diffs_vs_head_version(running_server, tmp_path):
    port = running_server["port"]
    plan = running_server["plan"]
    key = str(plan.resolve())

    # Seed two versions, with v1 deliberately different from disk content.
    review = Review()
    review.versions[key] = [
        Version(file=key, version=0, content="a\nb\n"),
        Version(file=key, version=1, content="X\nY\n"),
    ]
    save_review(plan, review)

    # Save an edit on top of v1.
    status, _ = http_post(port, "/api/edit-content", {"content": "X\nY\nZ\n"})
    assert status == 200

    # Reload and verify the diff is computed vs v1, not vs disk.
    reloaded = load_review(plan)
    edit = reloaded.get_edit(key)
    assert edit is not None
    assert edit.content == "X\nY\nZ\n"
    # The added line is "Z" relative to v1. Disk has "c\n" — if it diffed against
    # disk we'd see "-c" in the diff.
    assert "+Z" in edit.diff
    assert "-c" not in edit.diff
