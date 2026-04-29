import json
import subprocess
from http.client import HTTPConnection
from threading import Thread

import pytest

from redliner.web import ReviewHandler, ReviewServer


def _git(tmp_path, *args):
    subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)


@pytest.fixture
def git_repo(tmp_path):
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "test@test.com")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "commit.gpgsign", "false")

    hello = tmp_path / "hello.py"
    hello.write_text("def greet():\n    return 'hello'\n")

    _git(tmp_path, "add", "hello.py")
    _git(tmp_path, "commit", "-m", "init")

    return tmp_path


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
    server.server_close()


def http_get(port: int, path: str) -> dict:
    conn = HTTPConnection("127.0.0.1", port)
    conn.request("GET", path)
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    return json.loads(body)


def http_post(port: int, path: str, body: dict | None = None) -> tuple[int, dict]:
    conn = HTTPConnection("127.0.0.1", port)
    payload = json.dumps(body or {}).encode()
    conn.request("POST", path, body=payload, headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    raw = resp.read()
    status = resp.status
    conn.close()
    return status, (json.loads(raw) if raw else {})
