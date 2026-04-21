import json
import threading
from http.client import HTTPConnection

import pytest

from redliner.web import ReviewHandler, ReviewServer


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))

    plan_file = tmp_path / "plan.md"
    plan_file.write_text("line 1\nline 2\nline 3\n")

    srv = ReviewServer(("127.0.0.1", 0), ReviewHandler)
    srv.plan_file = plan_file
    srv.session = plan_file
    srv.mode = "plan"
    srv.diff_data = []
    srv.active_file = ""
    srv.repo_root = tmp_path

    thread = threading.Thread(target=srv.serve_forever)
    thread.daemon = True
    thread.start()

    yield srv

    srv.shutdown()


def _conn(server):
    host, port = server.server_address
    return HTTPConnection(host, port)


def _post_json(conn, path, body=None):
    conn.request(
        "POST",
        path,
        body=json.dumps(body).encode() if body else None,
        headers={"Content-Type": "application/json"},
    )
    return conn.getresponse()


def _get_json(conn, path):
    conn.request("GET", path)
    return conn.getresponse()


def test_edit_comment(server):
    conn = _conn(server)
    _post_json(conn, "/api/comment", {"line": 1, "text": "original"})
    conn.close()

    conn = _conn(server)
    resp = _post_json(conn, "/api/edit/1", {"text": "edited"})
    assert resp.status == 200
    data = json.loads(resp.read())
    assert data["comments"][0]["text"] == "edited"
    conn.close()


def test_edit_comment_not_found(server):
    conn = _conn(server)
    resp = _post_json(conn, "/api/edit/999", {"text": "nope"})
    assert resp.status == 404
    conn.close()


def test_edit_comment_no_text(server):
    conn = _conn(server)
    _post_json(conn, "/api/comment", {"line": 1, "text": "original"})
    conn.close()

    conn = _conn(server)
    resp = _post_json(conn, "/api/edit/1", {"text": ""})
    assert resp.status == 400
    conn.close()


def test_review_endpoint_exposes_storage_path(server):
    conn = _conn(server)
    resp = _get_json(conn, "/api/review")
    assert resp.status == 200
    data = json.loads(resp.read())
    assert "storage" in data
    assert data["storage"]
    assert "file_path" in data
    conn.close()


def test_comment_includes_file_path_in_response(server):
    conn = _conn(server)
    resp = _post_json(conn, "/api/comment", {"line": 1, "text": "hello"})
    assert resp.status == 200
    data = json.loads(resp.read())
    assert data["comments"][0]["file"] == str(server.plan_file.resolve())
    conn.close()
