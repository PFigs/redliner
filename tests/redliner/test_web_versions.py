import json
from http.client import HTTPConnection
from threading import Thread

import pytest

from redliner.review import Comment, Review, Version, save_review
from redliner.web import ReviewHandler, ReviewServer


@pytest.fixture
def running_server(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))

    plan = tmp_path / "plan.md"
    plan.write_text("a\nb\nc\n")

    server = ReviewServer(("127.0.0.1", 0), ReviewHandler)
    server.plan_file = plan
    server.session = plan
    server.mode = "plan"
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()

    port = server.server_address[1]
    yield {"port": port, "plan": plan}

    server.shutdown()
    thread.join(timeout=2)


def _get(port: int, path: str) -> dict:
    conn = HTTPConnection("127.0.0.1", port)
    conn.request("GET", path)
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    return json.loads(body)


def _post(port: int, path: str, body: dict | None = None) -> tuple[int, dict]:
    conn = HTTPConnection("127.0.0.1", port)
    payload = json.dumps(body or {}).encode()
    conn.request("POST", path, body=payload, headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    raw = resp.read()
    status = resp.status
    conn.close()
    return status, (json.loads(raw) if raw else {})


def test_get_versions_returns_baseline(running_server):
    port = running_server["port"]
    plan = running_server["plan"]
    key = str(plan.resolve())

    data = _get(port, f"/api/versions?file={key}")

    assert "versions" in data
    assert len(data["versions"]) == 1
    assert data["versions"][0]["version"] == 0
    assert data["versions"][0]["pending"] == 0
    assert data["versions"][0]["resolved"] == 0


def test_get_versions_includes_comment_counts(running_server, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
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

    data = _get(port, f"/api/versions?file={key}")

    by_v = {v["version"]: v for v in data["versions"]}
    assert by_v[0]["pending"] == 1
    assert by_v[0]["resolved"] == 0
    assert by_v[1]["pending"] == 1
    assert by_v[1]["resolved"] == 1
