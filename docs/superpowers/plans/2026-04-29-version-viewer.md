# Version Viewer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add explicit version snapshots to plan review so reviewers can browse past rounds via a header dropdown, see incremental diffs against the previous version, and have comments pinned to the version they were authored on.

**Architecture:** Sealed-snapshot model. A new `Version` dataclass plus an append-only `versions.jsonl` per session store immutable rounds. The existing `Edit` (live draft) is preserved unchanged and conceptually sits on top of the head version. Comments gain a `version` field stamped at creation. Web UI grows a header dropdown listing versions with per-version comment counts; selecting a non-current version replaces the rendered document with its diff (or full content) and disables editing.

**Tech Stack:** Python 3.12+, stdlib-only (`http.server`, `dataclasses`, `difflib`, `argparse`), `pytest` for tests. No new dependencies.

**Spec reference:** `docs/superpowers/specs/2026-04-29-version-viewer-design.md`

**Conventions used in this plan:**
- Commit messages follow the repository's `type: subject` style and include the trailer `Assisted-By: Claude Code` followed by `Pedro` on a separate line (matches user's global preference).
- Tests live in `tests/redliner/`. Run all tests via `uv run pytest tests/redliner/ -v` from the repo root.

---

## File Structure

**Files to modify:**

- `src/redliner/review.py` — add `Version` dataclass, `version` field on `Comment`, `versions` map and methods on `Review`, JSONL persistence for versions.
- `src/redliner/web.py` — add `GET /api/versions`, `GET /api/version`, `POST /api/snapshot`; modify `POST /api/comment` to accept optional `version`; modify `_save_edit_content` to use head-version content as the diff baseline; embed dropdown HTML/CSS/JS in the existing `HTML_TEMPLATE`.
- `src/redliner/cli.py` — add `snapshot`, `versions` subcommands; add `--version` and `--diff` flags to `show`.

**Files to create:**

- `tests/redliner/test_versions.py` — unit tests for the version data model and persistence.
- `tests/redliner/test_web_versions.py` — API tests for the new endpoints and the modified comment endpoint.
- `tests/redliner/test_cli_versions.py` — CLI tests for `snapshot`, `versions`, and `show --version`.

**Storage layout** (under `session_dir(<plan>)`, no path changes):

- `comments.jsonl` — same path; rows gain a `version` integer field.
- `meta.json` — unchanged.
- `edits.jsonl` — unchanged.
- `versions.jsonl` — new file, append-only, one `Version` per row.

---

## Task 1: `Version` dataclass + persistence + v0 synthesis

**Files:**
- Modify: `src/redliner/review.py`
- Create: `tests/redliner/test_versions.py`

- [ ] **Step 1: Write the failing test for the Version dataclass and roundtrip**

Create `tests/redliner/test_versions.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/redliner/test_versions.py -v`
Expected: ImportError or AttributeError — `Version` does not exist; `Review.versions` does not exist.

- [ ] **Step 3: Add the `Version` dataclass and `versions` map**

Edit `src/redliner/review.py`. Add the dataclass after the existing `Edit` dataclass (around line 39):

```python
@dataclass
class Version:
    file: str
    version: int
    content: str
    created: str = ""

    def __post_init__(self) -> None:
        if not self.created:
            self.created = datetime.now(UTC).isoformat(timespec="seconds")
```

Add a field to `Review` (the dataclass already uses `field(default_factory=...)`):

```python
@dataclass
class Review:
    """A review session holding comments across one or more files."""

    comments: list[Comment] = field(default_factory=list)
    files: dict[str, FileState] = field(default_factory=dict)
    edits: dict[str, Edit] = field(default_factory=dict)
    versions: dict[str, list[Version]] = field(default_factory=dict)
```

Add a helper function near the existing `edits_path`:

```python
def versions_path(session: Path) -> Path:
    return session_dir(session) / "versions.jsonl"
```

- [ ] **Step 4: Add JSONL persistence for versions and v0 synthesis**

In `load_review`, after the existing edits-loading block, add:

```python
    vpath = versions_path(session)
    if vpath.exists():
        for raw in vpath.read_text().splitlines():
            line = raw.strip()
            if not line:
                continue
            v = Version(**json.loads(line))
            review.versions.setdefault(v.file, []).append(v)
        for vlist in review.versions.values():
            vlist.sort(key=lambda v: v.version)
    else:
        # Synthesize v0 from the file on disk; fall back to existing edit content
        # if the file is no longer readable. If neither is available, leave versions
        # empty (callers see head_version() == 0 and version_content() raises).
        key = str(session.resolve())
        content: str | None = None
        try:
            if session.is_file():
                content = session.read_text()
        except OSError:
            content = None
        if content is None:
            existing = review.edits.get(key)
            if existing is not None:
                content = existing.content
        if content is not None:
            review.versions[key] = [Version(file=key, version=0, content=content)]
```

In `save_review`, mirror the edits handling:

```python
    vpath = versions_path(session)
    if review.versions:
        version_lines: list[str] = []
        for vlist in review.versions.values():
            for v in vlist:
                version_lines.append(json.dumps(asdict(v)))
        vpath.write_text("\n".join(version_lines) + "\n")
    elif vpath.exists():
        vpath.unlink()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/redliner/test_versions.py -v`
Expected: 4 passed.

- [ ] **Step 6: Run the full test suite to verify no regressions**

Run: `uv run pytest tests/redliner/ -v`
Expected: all existing tests still pass.

- [ ] **Step 7: Commit**

```bash
git add src/redliner/review.py tests/redliner/test_versions.py
git commit -m "$(cat <<'EOF'
feat: add Version dataclass and versions.jsonl persistence

v0 is synthesized from on-disk content on first load when no versions.jsonl exists, preserving compatibility with sessions that predate versioning.

Assisted-By: Claude Code
Pedro
EOF
)"
```

---

## Task 2: `version` field on `Comment` with backwards-compat loading

**Files:**
- Modify: `src/redliner/review.py`
- Modify: `tests/redliner/test_versions.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/redliner/test_versions.py`:

```python
from redliner.review import Comment


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
    from redliner.review import comments_path, session_dir
    sd = session_dir(plan)
    sd.mkdir(parents=True, exist_ok=True)
    legacy_row = (
        '{"id": 1, "file": "' + key + '", "line": 1, '
        '"text": "old", "status": "pending", "created": "2026-01-01T00:00:00+00:00"}'
    )
    comments_path(plan).write_text(legacy_row + "\n")

    loaded = load_review(plan)
    assert loaded.comments[0].version == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/redliner/test_versions.py -v`
Expected: failures around `Comment.version`.

- [ ] **Step 3: Add `version` field to `Comment`**

Edit `src/redliner/review.py`. Update the `Comment` dataclass:

```python
@dataclass
class Comment:
    id: int
    file: str
    line: int
    text: str
    status: str = "pending"
    created: str = ""
    version: int = 0

    def __post_init__(self) -> None:
        if not self.created:
            self.created = datetime.now(UTC).isoformat(timespec="seconds")
```

The default of 0 covers the legacy-row case automatically because `Comment(**json.loads(line))` will not pass a `version` kwarg when the field is absent. No changes needed in `load_review`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/redliner/test_versions.py -v`
Expected: all version tests pass (7 total).

- [ ] **Step 5: Run full suite for regressions**

Run: `uv run pytest tests/redliner/ -v`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/redliner/review.py tests/redliner/test_versions.py
git commit -m "$(cat <<'EOF'
feat: add version field to Comment with backwards-compat loading

Comments default version=0 so pre-versioning comments.jsonl rows load cleanly under v0 in the new dropdown.

Assisted-By: Claude Code
Pedro
EOF
)"
```

---

## Task 3: `Review` methods — `head_version`, `snapshot`, `version_content`, `version_diff`, `comments_for_version`

**Files:**
- Modify: `src/redliner/review.py`
- Modify: `tests/redliner/test_versions.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/redliner/test_versions.py`:

```python
import pytest


def test_head_version_returns_zero_for_baseline_only():
    review = Review()
    review.versions["/x"] = [Version(file="/x", version=0, content="a\n")]
    assert review.head_version("/x") == 0


def test_head_version_returns_highest():
    review = Review()
    review.versions["/x"] = [
        Version(file="/x", version=0, content="a\n"),
        Version(file="/x", version=1, content="b\n"),
        Version(file="/x", version=2, content="c\n"),
    ]
    assert review.head_version("/x") == 2


def test_head_version_zero_for_unknown_file():
    review = Review()
    assert review.head_version("/never-seen") == 0


def test_snapshot_appends_with_next_version_number():
    review = Review()
    review.versions["/x"] = [Version(file="/x", version=0, content="a\n")]
    new = review.snapshot("/x", "a\nb\n")
    assert new.version == 1
    assert new.content == "a\nb\n"
    assert review.versions["/x"][-1] is new


def test_snapshot_allows_noop_content():
    review = Review()
    review.versions["/x"] = [Version(file="/x", version=0, content="same\n")]
    new = review.snapshot("/x", "same\n")
    assert new.version == 1


def test_snapshot_initializes_versions_list_if_missing():
    review = Review()
    new = review.snapshot("/x", "first\n")
    assert new.version == 0
    assert review.versions["/x"][0] is new


def test_version_content_returns_content():
    review = Review()
    review.versions["/x"] = [
        Version(file="/x", version=0, content="zero\n"),
        Version(file="/x", version=1, content="one\n"),
    ]
    assert review.version_content("/x", 1) == "one\n"


def test_version_content_raises_for_unknown():
    review = Review()
    review.versions["/x"] = [Version(file="/x", version=0, content="a\n")]
    with pytest.raises(ValueError):
        review.version_content("/x", 99)


def test_version_diff_returns_unified_diff_vs_previous():
    review = Review()
    review.versions["/x"] = [
        Version(file="/x", version=0, content="a\nb\n"),
        Version(file="/x", version=1, content="a\nB\n"),
    ]
    diff = review.version_diff("/x", 1)
    assert "-b" in diff
    assert "+B" in diff


def test_version_diff_raises_for_v0():
    review = Review()
    review.versions["/x"] = [Version(file="/x", version=0, content="a\n")]
    with pytest.raises(ValueError):
        review.version_diff("/x", 0)


def test_comments_for_version_filters_by_file_and_version():
    review = Review()
    review.comments.append(Comment(id=1, file="/x", line=1, text="v0a", version=0))
    review.comments.append(Comment(id=2, file="/x", line=1, text="v1a", version=1))
    review.comments.append(Comment(id=3, file="/y", line=1, text="other", version=1))
    result = review.comments_for_version("/x", 1)
    ids = {c.id for c in result}
    assert ids == {2}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/redliner/test_versions.py -v`
Expected: AttributeError on the new methods.

- [ ] **Step 3: Implement the methods on `Review`**

Edit `src/redliner/review.py`. Add inside the `Review` class:

```python
    def head_version(self, file: str) -> int:
        vlist = self.versions.get(file)
        if not vlist:
            return 0
        return max(v.version for v in vlist)

    def snapshot(self, file: str, content: str) -> Version:
        vlist = self.versions.setdefault(file, [])
        next_num = (max((v.version for v in vlist), default=-1)) + 1
        new = Version(file=file, version=next_num, content=content)
        vlist.append(new)
        return new

    def version_content(self, file: str, n: int) -> str:
        for v in self.versions.get(file, []):
            if v.version == n:
                return v.content
        raise ValueError(f"version {n} not found for {file}")

    def version_diff(self, file: str, n: int) -> str:
        if n <= 0:
            raise ValueError(f"version {n} has no previous version to diff against")
        prev = self.version_content(file, n - 1)
        curr = self.version_content(file, n)
        return "".join(
            difflib.unified_diff(
                prev.splitlines(keepends=True),
                curr.splitlines(keepends=True),
                fromfile=f"{file}@v{n - 1}",
                tofile=f"{file}@v{n}",
            )
        )

    def comments_for_version(self, file: str, n: int) -> list[Comment]:
        return [c for c in self.comments if c.file == file and c.version == n]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/redliner/test_versions.py -v`
Expected: all version tests pass (17 total).

- [ ] **Step 5: Run full suite**

Run: `uv run pytest tests/redliner/ -v`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/redliner/review.py tests/redliner/test_versions.py
git commit -m "$(cat <<'EOF'
feat: add head_version, snapshot, version_content, version_diff helpers

Computes diffs lazily from sealed snapshots; comments_for_version filters by file and pinned version.

Assisted-By: Claude Code
Pedro
EOF
)"
```

---

## Task 4: `add_comment` accepts version; `set_edit` uses head content as baseline

**Files:**
- Modify: `src/redliner/review.py`
- Modify: `tests/redliner/test_versions.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/redliner/test_versions.py`:

```python
def test_add_comment_defaults_version_to_head():
    review = Review()
    review.versions["/x"] = [
        Version(file="/x", version=0, content="a\n"),
        Version(file="/x", version=1, content="b\n"),
    ]
    c = review.add_comment("/x", 1, "ping")
    assert c.version == 1


def test_add_comment_explicit_version_overrides_head():
    review = Review()
    review.versions["/x"] = [
        Version(file="/x", version=0, content="a\n"),
        Version(file="/x", version=1, content="b\n"),
    ]
    c = review.add_comment("/x", 1, "ping", version=0)
    assert c.version == 0


def test_add_comment_no_versions_yields_zero():
    review = Review()
    c = review.add_comment("/x", 1, "ping")
    assert c.version == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/redliner/test_versions.py -v -k add_comment`
Expected: failures (signature does not accept `version`).

- [ ] **Step 3: Update `add_comment`**

Edit `src/redliner/review.py`. Change the existing `add_comment`:

```python
    def add_comment(self, file: str, line: int, text: str, version: int | None = None) -> Comment:
        v = self.head_version(file) if version is None else version
        comment = Comment(id=self.next_id(), file=file, line=line, text=text, version=v)
        self.comments.append(comment)
        self._ensure_file(file).status = "in_review"
        return comment
```

- [ ] **Step 4: Update `_save_edit_content` in `web.py` to diff against head version**

Edit `src/redliner/web.py`. In `_save_edit_content` (around line 205), replace the `original = ...` line with head-version content:

```python
    def _save_edit_content(self) -> None:
        if self.server.mode != "plan":
            self._json_response({"error": "editing only available in plan mode"}, 400)
            return
        body = self._read_body()
        content = body.get("content")
        if not isinstance(content, str):
            self._json_response({"error": "content (string) required"}, 400)
            return
        plan_file = self._active_plan_file()
        key = self._active_key()
        review = load_review(self.server.session)
        head = review.head_version(key)
        try:
            original = review.version_content(key, head)
        except ValueError:
            original = plan_file.read_text() if plan_file.exists() else ""
        review.set_edit(file=key, content=content, original=original)
        save_review(self.server.session, review)
        self._get_review()
```

The `try/except` covers the rare case where versions were not synthesized (e.g., file deleted between sessions); fall back to current behavior.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/redliner/ -v`
Expected: all pass — existing web tests still pass because v0 synthesis now produces head content equal to disk content for new sessions.

- [ ] **Step 6: Commit**

```bash
git add src/redliner/review.py src/redliner/web.py
git commit -m "$(cat <<'EOF'
feat: pin new comments to head version; diff edits vs head

add_comment now stamps the head version (or an explicit one) on the comment. set_edit's baseline becomes head-version content so the edit indicator reflects diff vs the latest sealed round.

Assisted-By: Claude Code
Pedro
EOF
)"
```

---

## Task 5: `GET /api/versions` endpoint

**Files:**
- Modify: `src/redliner/web.py`
- Create: `tests/redliner/test_web_versions.py`

- [ ] **Step 1: Write failing API test**

Create `tests/redliner/test_web_versions.py`:

```python
import json
from http.client import HTTPConnection
from threading import Thread
from pathlib import Path

import pytest

from redliner.review import Comment, Review, Version, save_review
from redliner.web import HTML_TEMPLATE, ReviewHandler, ReviewServer


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
    review.comments.append(Comment(id=1, file=key, line=1, text="v0", version=0, status="pending"))
    review.comments.append(Comment(id=2, file=key, line=2, text="v1a", version=1, status="pending"))
    review.comments.append(Comment(id=3, file=key, line=2, text="v1b", version=1, status="resolved"))
    save_review(plan, review)

    data = _get(port, f"/api/versions?file={key}")

    by_v = {v["version"]: v for v in data["versions"]}
    assert by_v[0]["pending"] == 1
    assert by_v[0]["resolved"] == 0
    assert by_v[1]["pending"] == 1
    assert by_v[1]["resolved"] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/redliner/test_web_versions.py -v`
Expected: 404 from the server (endpoint not implemented).

- [ ] **Step 3: Implement `GET /api/versions`**

Edit `src/redliner/web.py`. In `do_GET`, add a new branch before the final `else`:

```python
    def do_GET(self) -> None:
        if self.path == "/":
            self._serve_html()
        elif self.path == "/api/review":
            self._get_review()
        elif self.path == "/api/diff" and self.server.mode == "diff":
            self._get_diff()
        elif self.path.startswith("/api/versions"):
            self._get_versions()
        elif self.path.startswith("/api/version"):
            self._get_version()
        else:
            self._not_found()
```

Add the handler method (place near `_get_review`):

```python
    def _parse_query(self) -> dict[str, str]:
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(self.path).query)
        return {k: v[0] for k, v in qs.items() if v}

    def _get_versions(self) -> None:
        params = self._parse_query()
        file_key = params.get("file") or self._active_key()
        review = load_review(self.server.session)
        vlist = review.versions.get(file_key, [])
        result = []
        for v in vlist:
            comments = review.comments_for_version(file_key, v.version)
            pending = sum(1 for c in comments if c.status == "pending")
            resolved = sum(1 for c in comments if c.status == "resolved")
            result.append({
                "version": v.version,
                "created": v.created,
                "pending": pending,
                "resolved": resolved,
            })
        self._json_response({"versions": result})
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/redliner/test_web_versions.py -v -k get_versions`
Expected: 2 passed.

- [ ] **Step 5: Run full suite**

Run: `uv run pytest tests/redliner/ -v`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/redliner/web.py tests/redliner/test_web_versions.py
git commit -m "$(cat <<'EOF'
feat: add GET /api/versions endpoint with per-version comment counts

Returns the version list for the dropdown including pending and resolved counts pinned to each version.

Assisted-By: Claude Code
Pedro
EOF
)"
```

---

## Task 6: `GET /api/version?n=N&view=diff|full` endpoint

**Files:**
- Modify: `src/redliner/web.py`
- Modify: `tests/redliner/test_web_versions.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/redliner/test_web_versions.py`:

```python
def test_get_version_full_returns_content(running_server, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    port = running_server["port"]
    plan = running_server["plan"]
    key = str(plan.resolve())

    review = Review()
    review.versions[key] = [
        Version(file=key, version=0, content="a\nb\n"),
        Version(file=key, version=1, content="a\nB\n"),
    ]
    save_review(plan, review)

    data = _get(port, f"/api/version?file={key}&n=1&view=full")

    assert data["content"] == "a\nB\n"
    assert data["diff"] == ""
    assert data["comments"] == []


def test_get_version_diff_returns_unified_diff(running_server, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    port = running_server["port"]
    plan = running_server["plan"]
    key = str(plan.resolve())

    review = Review()
    review.versions[key] = [
        Version(file=key, version=0, content="a\nb\n"),
        Version(file=key, version=1, content="a\nB\n"),
    ]
    save_review(plan, review)

    data = _get(port, f"/api/version?file={key}&n=1&view=diff")

    assert "-b" in data["diff"]
    assert "+B" in data["diff"]
    assert data["content"] == "a\nB\n"


def test_get_version_diff_for_v0_returns_full(running_server, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    port = running_server["port"]
    plan = running_server["plan"]
    key = str(plan.resolve())

    data = _get(port, f"/api/version?file={key}&n=0&view=diff")

    assert data["diff"] == ""
    assert data["content"] == "a\nb\nc\n"


def test_get_version_returns_pinned_comments(running_server, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
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

    data = _get(port, f"/api/version?file={key}&n=1&view=full")

    ids = {c["id"] for c in data["comments"]}
    assert ids == {10}


def test_get_version_unknown_returns_404(running_server):
    port = running_server["port"]
    plan = running_server["plan"]
    key = str(plan.resolve())

    conn = HTTPConnection("127.0.0.1", port)
    conn.request("GET", f"/api/version?file={key}&n=99&view=full")
    resp = conn.getresponse()
    status = resp.status
    conn.close()
    assert status == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/redliner/test_web_versions.py -v -k get_version`
Expected: failures (404 or KeyError, depending on what gets routed).

- [ ] **Step 3: Implement `_get_version`**

Edit `src/redliner/web.py`. Add the handler:

```python
    def _get_version(self) -> None:
        params = self._parse_query()
        file_key = params.get("file") or self._active_key()
        try:
            n = int(params.get("n", "0"))
        except ValueError:
            self._json_response({"error": "n must be an integer"}, 400)
            return
        view = params.get("view", "full")
        if view not in ("diff", "full"):
            self._json_response({"error": "view must be 'diff' or 'full'"}, 400)
            return
        review = load_review(self.server.session)
        try:
            content = review.version_content(file_key, n)
        except ValueError:
            self._json_response({"error": f"version {n} not found"}, 404)
            return
        diff = ""
        if view == "diff" and n > 0:
            diff = review.version_diff(file_key, n)
        comments = [
            {
                "id": c.id,
                "file": c.file,
                "line": c.line,
                "text": c.text,
                "status": c.status,
                "created": c.created,
                "version": c.version,
            }
            for c in review.comments_for_version(file_key, n)
        ]
        self._json_response({"content": content, "diff": diff, "comments": comments})
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/redliner/test_web_versions.py -v -k get_version`
Expected: 5 passed.

- [ ] **Step 5: Run full suite**

Run: `uv run pytest tests/redliner/ -v`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/redliner/web.py tests/redliner/test_web_versions.py
git commit -m "$(cat <<'EOF'
feat: add GET /api/version with diff and full views

Returns content at v_n plus optional unified diff vs v_(n-1) and the comments pinned to that version.

Assisted-By: Claude Code
Pedro
EOF
)"
```

---

## Task 7: `POST /api/snapshot` endpoint with serialized writes

**Files:**
- Modify: `src/redliner/web.py`
- Modify: `tests/redliner/test_web_versions.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/redliner/test_web_versions.py`:

```python
def test_snapshot_creates_new_version(running_server, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    port = running_server["port"]
    plan = running_server["plan"]
    key = str(plan.resolve())

    # Save an edit so the snapshot has new content.
    status, _ = _post(port, "/api/edit-content", {"content": "a\nB\nc\n"})
    assert status == 200

    status, body = _post(port, "/api/snapshot", {"file": key})
    assert status == 200
    assert body["version"] == 1
    assert body["content"] == "a\nB\nc\n"

    listing = _get(port, f"/api/versions?file={key}")
    nums = [v["version"] for v in listing["versions"]]
    assert nums == [0, 1]


def test_snapshot_falls_back_to_head_when_no_edit(running_server, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    port = running_server["port"]
    plan = running_server["plan"]
    key = str(plan.resolve())

    status, body = _post(port, "/api/snapshot", {"file": key})
    assert status == 200
    assert body["version"] == 1
    assert body["content"] == "a\nb\nc\n"  # head (v0) content


def test_snapshot_serializes_concurrent_writes(running_server, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    port = running_server["port"]
    plan = running_server["plan"]
    key = str(plan.resolve())

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = [ex.submit(_post, port, "/api/snapshot", {"file": key}) for _ in range(4)]
        results = [f.result() for f in futures]

    versions = sorted(body["version"] for status, body in results)
    assert versions == [1, 2, 3, 4]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/redliner/test_web_versions.py -v -k snapshot`
Expected: 404 from the server.

- [ ] **Step 3: Add a per-server lock and the snapshot handler**

Edit `src/redliner/web.py`. Add a lock attribute on the server:

```python
import threading

class ReviewServer(HTTPServer):
    plan_file: Path
    done: bool = False
    mode: str = "plan"
    diff_data: list[FileDiff]
    active_file: str = ""
    repo_root: Path = Path(".")
    session: Path = Path(".")
    snapshot_lock: threading.Lock = threading.Lock()
```

Note: Python class-level mutable attributes are shared across instances; that is the intended scope here since each test spawns a fresh server. If the codebase later needs per-instance locks, move the assignment into `__init__`.

In `do_POST`, add a route:

```python
        elif self.path == "/api/snapshot":
            self._snapshot()
```

Add the handler:

```python
    def _snapshot(self) -> None:
        if self.server.mode != "plan":
            self._json_response({"error": "snapshots only available in plan mode"}, 400)
            return
        body = self._read_body()
        file_key = body.get("file") or self._active_key()
        with self.server.snapshot_lock:
            review = load_review(self.server.session)
            edit = review.get_edit(file_key)
            if edit is not None:
                content = edit.content
            else:
                head = review.head_version(file_key)
                try:
                    content = review.version_content(file_key, head)
                except ValueError:
                    self._json_response({"error": "no baseline content available"}, 400)
                    return
            new_version = review.snapshot(file_key, content)
            save_review(self.server.session, review)
        self._json_response({
            "version": new_version.version,
            "created": new_version.created,
            "content": new_version.content,
        })
```

The lock guards the read-modify-write so concurrent calls cannot land at the same `next_num`.

If the existing `HTTPServer` is single-threaded (which it is by default — `HTTPServer` is not `ThreadingHTTPServer`), the concurrency test still validates behavior because requests serialize on the server side; the lock makes the invariant explicit and safe if the server type ever changes.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/redliner/test_web_versions.py -v -k snapshot`
Expected: 3 passed.

- [ ] **Step 5: Run full suite**

Run: `uv run pytest tests/redliner/ -v`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/redliner/web.py tests/redliner/test_web_versions.py
git commit -m "$(cat <<'EOF'
feat: add POST /api/snapshot to seal a new version

Snapshots the live draft (or head content if no draft) into a new monotonic version. A per-server lock serializes writes so concurrent tabs cannot collide.

Assisted-By: Claude Code
Pedro
EOF
)"
```

---

## Task 8: `POST /api/comment` accepts optional `version`

**Files:**
- Modify: `src/redliner/web.py`
- Modify: `tests/redliner/test_web_versions.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/redliner/test_web_versions.py`:

```python
def test_comment_post_uses_head_version_by_default(running_server, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    port = running_server["port"]
    plan = running_server["plan"]
    key = str(plan.resolve())

    # Take a snapshot so head is v1.
    _post(port, "/api/snapshot", {"file": key})

    status, _ = _post(port, "/api/comment", {"line": 1, "text": "ping"})
    assert status == 200

    listing = _get(port, f"/api/versions?file={key}")
    by_v = {v["version"]: v for v in listing["versions"]}
    assert by_v[1]["pending"] == 1
    assert by_v[0]["pending"] == 0


def test_comment_post_explicit_version_pins_to_it(running_server, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    port = running_server["port"]
    plan = running_server["plan"]
    key = str(plan.resolve())

    _post(port, "/api/snapshot", {"file": key})  # v1 exists

    status, _ = _post(port, "/api/comment", {"line": 1, "text": "back to v0", "version": 0})
    assert status == 200

    listing = _get(port, f"/api/versions?file={key}")
    by_v = {v["version"]: v for v in listing["versions"]}
    assert by_v[0]["pending"] == 1
    assert by_v[1]["pending"] == 0


def test_comment_post_invalid_version_400(running_server, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    port = running_server["port"]

    status, body = _post(port, "/api/comment", {"line": 1, "text": "ping", "version": 99})
    assert status == 400
    assert "version" in body.get("error", "").lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/redliner/test_web_versions.py -v -k comment_post`
Expected: failures (server does not validate `version`).

- [ ] **Step 3: Update `_add_comment`**

Edit `src/redliner/web.py`. Replace `_add_comment`:

```python
    def _add_comment(self) -> None:
        body = self._read_body()
        line = body.get("line")
        text = body.get("text", "").strip()
        if not isinstance(line, int) or not text:
            self._json_response({"error": "line (int) and text required"}, 400)
            return
        key = self._active_key()
        review = load_review(self.server.session)
        version = body.get("version")
        if version is not None:
            if not isinstance(version, int):
                self._json_response({"error": "version must be an integer"}, 400)
                return
            valid = {v.version for v in review.versions.get(key, [])}
            if version not in valid:
                self._json_response({"error": f"version {version} not found"}, 400)
                return
        review.add_comment(key, line, text, version=version)
        save_review(self.server.session, review)
        if self.server.mode == "diff":
            self._get_diff()
        else:
            self._json_response(self._file_review_dict(key))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/redliner/test_web_versions.py -v -k comment_post`
Expected: 3 passed.

- [ ] **Step 5: Run full suite**

Run: `uv run pytest tests/redliner/ -v`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/redliner/web.py tests/redliner/test_web_versions.py
git commit -m "$(cat <<'EOF'
feat: accept optional version on POST /api/comment

Defaults to head when omitted; rejects unknown versions with 400.

Assisted-By: Claude Code
Pedro
EOF
)"
```

---

## Task 9: CLI `redliner snapshot <file>`

**Files:**
- Modify: `src/redliner/cli.py`
- Create: `tests/redliner/test_cli_versions.py`

- [ ] **Step 1: Write failing test**

Create `tests/redliner/test_cli_versions.py`:

```python
import json
import subprocess
import sys
from pathlib import Path

import pytest

from redliner.review import load_review


@pytest.fixture
def plan_file(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    plan = tmp_path / "plan.md"
    plan.write_text("a\nb\nc\n")
    return plan


def _run_cli(*args, env=None):
    return subprocess.run(
        [sys.executable, "-m", "redliner", *args],
        capture_output=True,
        text=True,
        env=env,
    )


def test_snapshot_creates_v1(plan_file, tmp_path, monkeypatch):
    import os
    env = os.environ.copy()
    env["XDG_DATA_HOME"] = str(tmp_path / "xdg")

    result = _run_cli("snapshot", str(plan_file), env=env)
    assert result.returncode == 0
    assert "v1" in result.stdout

    review = load_review(plan_file)
    nums = [v.version for v in review.versions[str(plan_file.resolve())]]
    assert nums == [0, 1]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/redliner/test_cli_versions.py::test_snapshot_creates_v1 -v`
Expected: failure (subcommand does not exist).

- [ ] **Step 3: Add `cmd_snapshot` and wire into argparse**

Edit `src/redliner/cli.py`. Add the imports near the top (replace the existing review import line):

```python
from redliner.review import edits_path, load_review, save_review, session_dir
```

Add the command function (place after `cmd_resolve` or wherever fits the existing layout):

```python
def cmd_snapshot(args: argparse.Namespace) -> None:
    plan_file = Path(args.file).resolve()
    if not plan_file.exists():
        print(f"File not found: {plan_file}", file=sys.stderr)
        sys.exit(1)
    review = load_review(plan_file)
    key = str(plan_file)
    edit = review.get_edit(key)
    if edit is not None:
        content = edit.content
    else:
        head = review.head_version(key)
        try:
            content = review.version_content(key, head)
        except ValueError:
            content = plan_file.read_text()
    new_version = review.snapshot(key, content)
    save_review(plan_file, review)
    print(f"Created v{new_version.version} at {new_version.created}")
```

Find the argparse setup (likely a `build_parser` or `main` function with subparsers) and add:

```python
    p_snap = sub.add_parser("snapshot", help="Create a new sealed version of a plan")
    p_snap.add_argument("file")
    p_snap.set_defaults(func=cmd_snapshot)
```

(If you do not see `sub` in scope, look for `subparsers = parser.add_subparsers(...)` and use that name.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/redliner/test_cli_versions.py::test_snapshot_creates_v1 -v`
Expected: pass.

- [ ] **Step 5: Run full suite**

Run: `uv run pytest tests/redliner/ -v`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/redliner/cli.py tests/redliner/test_cli_versions.py
git commit -m "$(cat <<'EOF'
feat: add 'redliner snapshot' subcommand

Seals current draft (or head content) as a new version.

Assisted-By: Claude Code
Pedro
EOF
)"
```

---

## Task 10: CLI `redliner versions <file>`

**Files:**
- Modify: `src/redliner/cli.py`
- Modify: `tests/redliner/test_cli_versions.py`

- [ ] **Step 1: Write failing test**

Append to `tests/redliner/test_cli_versions.py`:

```python
def test_versions_lists_versions_with_counts(plan_file, tmp_path):
    import os
    env = os.environ.copy()
    env["XDG_DATA_HOME"] = str(tmp_path / "xdg")

    _run_cli("snapshot", str(plan_file), env=env)
    _run_cli("comment", str(plan_file), "1", "hello", env=env)
    _run_cli("snapshot", str(plan_file), env=env)

    result = _run_cli("versions", str(plan_file), env=env)
    assert result.returncode == 0
    out = result.stdout
    assert "v0" in out
    assert "v1" in out
    assert "v2" in out
    # The comment is pinned to v1 (head when 'comment' was run, after first snapshot).
    assert "1 pending" in out or "pending: 1" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/redliner/test_cli_versions.py::test_versions_lists_versions_with_counts -v`
Expected: failure (subcommand does not exist).

- [ ] **Step 3: Add `cmd_versions` and wire into argparse**

Edit `src/redliner/cli.py`. Add:

```python
def cmd_versions(args: argparse.Namespace) -> None:
    plan_file = Path(args.file).resolve()
    review = load_review(plan_file)
    key = str(plan_file)
    vlist = review.versions.get(key, [])
    if not vlist:
        print("No versions.")
        return
    for v in vlist:
        comments = review.comments_for_version(key, v.version)
        pending = sum(1 for c in comments if c.status == "pending")
        resolved = sum(1 for c in comments if c.status == "resolved")
        print(f"v{v.version}  {v.created}  {pending} pending  {resolved} resolved")
```

Wire it up in the parser block:

```python
    p_versions = sub.add_parser("versions", help="List versions of a plan")
    p_versions.add_argument("file")
    p_versions.set_defaults(func=cmd_versions)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/redliner/test_cli_versions.py::test_versions_lists_versions_with_counts -v`
Expected: pass.

- [ ] **Step 5: Run full suite**

Run: `uv run pytest tests/redliner/ -v`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/redliner/cli.py tests/redliner/test_cli_versions.py
git commit -m "$(cat <<'EOF'
feat: add 'redliner versions' subcommand

Lists versions with timestamp and per-version pending/resolved comment counts.

Assisted-By: Claude Code
Pedro
EOF
)"
```

---

## Task 11: CLI `redliner show --version N [--diff]`

**Files:**
- Modify: `src/redliner/cli.py`
- Modify: `tests/redliner/test_cli_versions.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/redliner/test_cli_versions.py`:

```python
def test_show_at_version_prints_version_content(plan_file, tmp_path):
    import os
    env = os.environ.copy()
    env["XDG_DATA_HOME"] = str(tmp_path / "xdg")

    # Edit and snapshot so v1 has different content.
    review = load_review(plan_file)
    review.set_edit(file=str(plan_file.resolve()), content="a\nNEW\nc\n", original="a\nb\nc\n")
    from redliner.review import save_review
    save_review(plan_file, review)
    _run_cli("snapshot", str(plan_file), env=env)

    out_v0 = _run_cli("show", str(plan_file), "--version", "0", env=env).stdout
    out_v1 = _run_cli("show", str(plan_file), "--version", "1", env=env).stdout

    assert "b" in out_v0 and "NEW" not in out_v0
    assert "NEW" in out_v1


def test_show_at_version_with_diff_prints_unified_diff(plan_file, tmp_path):
    import os
    env = os.environ.copy()
    env["XDG_DATA_HOME"] = str(tmp_path / "xdg")

    review = load_review(plan_file)
    review.set_edit(file=str(plan_file.resolve()), content="a\nNEW\nc\n", original="a\nb\nc\n")
    from redliner.review import save_review
    save_review(plan_file, review)
    _run_cli("snapshot", str(plan_file), env=env)

    out = _run_cli("show", str(plan_file), "--version", "1", "--diff", env=env).stdout
    assert "-b" in out
    assert "+NEW" in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/redliner/test_cli_versions.py -v -k show_at_version`
Expected: failure (`--version` flag does not exist).

- [ ] **Step 3: Add `--version` and `--diff` flags to `show`**

Edit `src/redliner/cli.py`. Modify the `show` parser:

```python
    p_show = sub.add_parser("show", help="Print plan with line numbers and comments")
    p_show.add_argument("file")
    p_show.add_argument("--version", type=int, default=None, help="Show document at this version")
    p_show.add_argument("--diff", action="store_true", help="With --version, show diff vs previous version")
    p_show.set_defaults(func=cmd_show)
```

Update `cmd_show` to handle the new flags. Modify the existing function body:

```python
def cmd_show(args: argparse.Namespace) -> None:
    plan_file = Path(args.file).resolve()
    if not plan_file.exists() and args.version is None:
        print(f"File not found: {plan_file}", file=sys.stderr)
        sys.exit(1)

    review = load_review(plan_file)
    key = str(plan_file)

    if args.version is not None:
        try:
            content = review.version_content(key, args.version)
        except ValueError:
            print(f"Version {args.version} not found", file=sys.stderr)
            sys.exit(1)
        if args.diff:
            if args.version == 0:
                print(content, end="")
                return
            print(review.version_diff(key, args.version), end="")
            return
        comments_by_line: dict[int, list[str]] = {}
        for c in review.comments_for_version(key, args.version):
            tag = ">>>" if c.status == "pending" else "~~~"
            comments_by_line.setdefault(c.line, []).append(f"     {tag} [#{c.id}] {c.text}")
        for i, line in enumerate(content.splitlines(), 1):
            print(f"{i:4d} | {line}")
            for cl in comments_by_line.get(i, []):
                print(cl)
        return

    # Existing path: show file from disk with all comments.
    lines = plan_file.read_text().splitlines()
    comments_by_line = {}
    for c in review.comments_for(key):
        tag = ">>>" if c.status == "pending" else "~~~"
        comments_by_line.setdefault(c.line, []).append(f"     {tag} [#{c.id}] {c.text}")
    for i, line in enumerate(lines, 1):
        print(f"{i:4d} | {line}")
        for cl in comments_by_line.get(i, []):
            print(cl)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/redliner/test_cli_versions.py -v -k show_at_version`
Expected: 2 passed.

- [ ] **Step 5: Run full suite**

Run: `uv run pytest tests/redliner/ -v`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/redliner/cli.py tests/redliner/test_cli_versions.py
git commit -m "$(cat <<'EOF'
feat: add --version and --diff flags to 'redliner show'

Renders document at a specific version, optionally as a unified diff vs the previous version.

Assisted-By: Claude Code
Pedro
EOF
)"
```

---

## Task 12: UI — header dropdown HTML/CSS, populate from `/api/versions`

**Files:**
- Modify: `src/redliner/web.py` (HTML_TEMPLATE only)

This task adds the dropdown UI element and wires it to fetch the version list. Selecting a version is a no-op for now (Task 13 wires the rendering).

- [ ] **Step 1: Add dropdown HTML to the header**

Edit `src/redliner/web.py`. Find the `<header>` block (around line 635) inside `HTML_TEMPLATE` and modify it to:

```html
<header id="header">
  <div class="title" id="header-title">redliner</div>
  <div class="version-picker">
    <button id="version-btn" class="version-btn" onclick="toggleVersionMenu()">current ▾</button>
    <div id="version-menu" class="version-menu hidden"></div>
    <button id="snapshot-btn" class="version-action" onclick="takeSnapshot()">Snapshot</button>
    <span class="view-toggle hidden" id="view-toggle">
      <button id="toggle-diff" class="active" onclick="setView('diff')">Diff</button>
      <button id="toggle-full" onclick="setView('full')">Full</button>
    </span>
  </div>
  <div class="stats" id="stats"></div>
  <div class="actions" id="header-actions"></div>
</header>
```

- [ ] **Step 2: Add CSS for the dropdown**

Inside the existing `<style>` block in `HTML_TEMPLATE` (find `.actions` or `.stats` styles near line 390), append:

```css
.version-picker { display: flex; align-items: center; gap: 8px; position: relative; }
.version-btn {
  background: #21262d; color: #e6edf3; border: 1px solid #30363d;
  padding: 4px 10px; border-radius: 4px; font: inherit; cursor: pointer;
}
.version-btn:hover { background: #30363d; }
.version-menu {
  position: absolute; top: 100%; left: 0; margin-top: 4px;
  background: #161b22; border: 1px solid #30363d; border-radius: 6px;
  min-width: 280px; max-height: 400px; overflow: auto;
  box-shadow: 0 8px 24px rgba(0,0,0,0.4); z-index: 10;
}
.version-menu.hidden { display: none; }
.version-menu .item {
  padding: 8px 12px; cursor: pointer; display: flex; gap: 12px; align-items: center;
  border-bottom: 1px solid #21262d;
}
.version-menu .item:hover { background: #21262d; }
.version-menu .item .num { font-weight: 600; min-width: 70px; }
.version-menu .item .ts { color: #8b949e; font-size: 0.85em; flex: 1; }
.version-menu .item .counts { color: #8b949e; font-size: 0.85em; }
.version-action {
  background: #21262d; color: #e6edf3; border: 1px solid #30363d;
  padding: 4px 10px; border-radius: 4px; font: inherit; cursor: pointer;
}
.version-action.hidden { display: none; }
.view-toggle { display: inline-flex; border: 1px solid #30363d; border-radius: 4px; overflow: hidden; }
.view-toggle.hidden { display: none; }
.view-toggle button {
  background: #161b22; color: #e6edf3; border: 0; padding: 4px 10px;
  font: inherit; cursor: pointer;
}
.view-toggle button.active { background: #30363d; }
```

- [ ] **Step 3: Add JS to fetch and render the menu**

Inside the existing `<script>` block in `HTML_TEMPLATE`, add module-level state and fetch helpers near the top (after existing `let` declarations around line 700):

```js
let selectedVersion = null;  // null = "current"; integer = past version
let availableVersions = [];
let currentView = 'diff';

async function loadVersions() {
  const filePath = (window.__filePath || '');
  const url = '/api/versions' + (filePath ? `?file=${encodeURIComponent(filePath)}` : '');
  const resp = await fetch(url);
  const data = await resp.json();
  availableVersions = data.versions || [];
  renderVersionMenu();
  updateVersionButton();
}

function renderVersionMenu() {
  const menu = document.getElementById('version-menu');
  if (!menu) return;
  const items = [];
  items.push(itemHTML({ kind: 'current', label: 'current', ts: '', pending: liveCounts().pending, resolved: liveCounts().resolved }));
  // Versions newest-first.
  const sorted = [...availableVersions].sort((a, b) => b.version - a.version);
  for (const v of sorted) {
    const label = v.version === 0 ? 'v0 baseline' : `v${v.version}`;
    items.push(itemHTML({ kind: 'past', version: v.version, label, ts: v.created, pending: v.pending, resolved: v.resolved }));
  }
  menu.innerHTML = items.join('');
}

function liveCounts() {
  // Stats element holds current pending; count from existing review state if available.
  const head = availableVersions.length ? Math.max(...availableVersions.map(v => v.version)) : 0;
  const v = availableVersions.find(x => x.version === head);
  return v ? { pending: v.pending, resolved: v.resolved } : { pending: 0, resolved: 0 };
}

function itemHTML(o) {
  const onclick = o.kind === 'current'
    ? `selectVersion(null)`
    : `selectVersion(${o.version})`;
  const ts = o.ts ? new Date(o.ts).toLocaleString() : '';
  return `<div class="item" onclick="${onclick}"><span class="num">${o.label}</span><span class="ts">${ts}</span><span class="counts">${o.pending} pending, ${o.resolved} resolved</span></div>`;
}

function updateVersionButton() {
  const btn = document.getElementById('version-btn');
  if (!btn) return;
  btn.textContent = (selectedVersion === null ? 'current' : `v${selectedVersion}`) + ' ▾';
}

function toggleVersionMenu() {
  const menu = document.getElementById('version-menu');
  menu.classList.toggle('hidden');
}

function selectVersion(n) {
  selectedVersion = n;
  document.getElementById('version-menu').classList.add('hidden');
  updateVersionButton();
  // Task 13 will wire this to actually re-render the document.
}

document.addEventListener('click', (e) => {
  const picker = document.querySelector('.version-picker');
  if (picker && !picker.contains(e.target)) {
    document.getElementById('version-menu')?.classList.add('hidden');
  }
});
```

In the existing review-loading code, capture `data.file_path` so subsequent requests know which file we are looking at:

```js
// Inside the function that fetches /api/review (search for `/api/review`):
window.__filePath = data.file_path;
```

Call `loadVersions()` once after the initial review load.

- [ ] **Step 4: Manual smoke check**

Run: `uv run redliner open <some-plan.md>` and open the browser. Expect to see the dropdown showing `current ▾` between the title and stats. Clicking the button opens a menu listing `current` and `v0 baseline`. Selecting items closes the menu and updates the button label.

(No automated UI test — this codebase does not run a JS test harness. Verification is manual.)

- [ ] **Step 5: Run full suite for backend regressions**

Run: `uv run pytest tests/redliner/ -v`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/redliner/web.py
git commit -m "$(cat <<'EOF'
feat: add version dropdown UI in web header

Populates from /api/versions; clicking does not yet re-render the document (wired in next task).

Assisted-By: Claude Code
Pedro
EOF
)"
```

---

## Task 13: UI — render selected version (diff/full) and its pinned comments

**Files:**
- Modify: `src/redliner/web.py` (HTML_TEMPLATE only)

- [ ] **Step 1: Wire `selectVersion` to fetch and re-render**

Edit `src/redliner/web.py`. Replace the placeholder `selectVersion` and add the rendering logic:

```js
async function selectVersion(n) {
  selectedVersion = n;
  document.getElementById('version-menu').classList.add('hidden');
  updateVersionButton();
  if (n === null) {
    document.getElementById('view-toggle').classList.add('hidden');
    document.getElementById('snapshot-btn').classList.remove('hidden');
    await reloadCurrentView();
  } else {
    document.getElementById('view-toggle').classList.remove('hidden');
    document.getElementById('snapshot-btn').classList.add('hidden');
    await renderHistoricalVersion();
  }
}

async function reloadCurrentView() {
  // Re-fetch /api/review to render the current state — same code path as initial load.
  await loadReview();  // existing function in this template; rename if different
}

async function renderHistoricalVersion() {
  const filePath = window.__filePath || '';
  const view = (selectedVersion === 0) ? 'full' : currentView;
  const url = `/api/version?file=${encodeURIComponent(filePath)}&n=${selectedVersion}&view=${view}`;
  const resp = await fetch(url);
  const data = await resp.json();
  if (view === 'diff') {
    renderDiffPane(data.diff, data.comments, selectedVersion);
  } else {
    renderFullPane(data.content, data.comments, selectedVersion);
  }
  updateStatsForHistorical(data.comments);
}

function setView(view) {
  currentView = view;
  document.getElementById('toggle-diff').classList.toggle('active', view === 'diff');
  document.getElementById('toggle-full').classList.toggle('active', view === 'full');
  if (selectedVersion !== null) {
    renderHistoricalVersion();
  }
}

function renderFullPane(content, comments, version) {
  const main = document.getElementById('main-area') || document.querySelector('.main-area');
  const lines = content.split('\\n');
  const byLine = new Map();
  for (const c of comments) {
    if (!byLine.has(c.line)) byLine.set(c.line, []);
    byLine.get(c.line).push(c);
  }
  let html = '<div class="historical-pane">';
  lines.forEach((line, idx) => {
    const lineNum = idx + 1;
    html += `<div class="line"><span class="ln">${lineNum}</span><span class="content">${escapeHtml(line)}</span></div>`;
    for (const c of (byLine.get(lineNum) || [])) {
      html += renderCommentBlock(c);
    }
  });
  html += '</div>';
  main.innerHTML = html;
}

function renderDiffPane(diffText, comments, version) {
  const main = document.getElementById('main-area') || document.querySelector('.main-area');
  const lines = diffText.split('\\n');
  const visibleLineNums = new Set();
  let newLineCounter = 0;
  // Track which `new` line numbers appear in the hunk so we know which comments are visible.
  for (const raw of lines) {
    if (raw.startsWith('@@')) {
      const m = raw.match(/\\+(\\d+)/);
      if (m) newLineCounter = parseInt(m[1]) - 1;
    } else if (raw.startsWith('+') && !raw.startsWith('+++')) {
      newLineCounter += 1;
      visibleLineNums.add(newLineCounter);
    } else if (raw.startsWith(' ')) {
      newLineCounter += 1;
      visibleLineNums.add(newLineCounter);
    }
  }
  const visibleComments = comments.filter(c => visibleLineNums.has(c.line));
  const hidden = comments.length - visibleComments.length;

  let html = '<div class="historical-pane diff-pane">';
  if (hidden > 0) {
    html += `<div class="diff-notice">(${hidden} comment${hidden === 1 ? '' : 's'} hidden — switch to Full)</div>`;
  }
  html += '<pre class="diff-text">' + escapeHtml(diffText) + '</pre>';
  // Inline comment blocks under their anchored lines.
  if (visibleComments.length) {
    html += '<div class="diff-comments">';
    for (const c of visibleComments) {
      html += `<div class="diff-comment-anchor">Line ${c.line}: ${renderCommentBlock(c)}</div>`;
    }
    html += '</div>';
  }
  html += '</div>';
  main.innerHTML = html;
}

function escapeHtml(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function updateStatsForHistorical(comments) {
  const stats = document.getElementById('stats');
  if (!stats) return;
  const pending = comments.filter(c => c.status === 'pending').length;
  const resolved = comments.filter(c => c.status === 'resolved').length;
  stats.innerHTML = `<span class="badge pending-badge">${pending} pending</span><span class="badge">${resolved} resolved</span>`;
}
```

`renderCommentBlock(c)` is a small helper that returns the same HTML the existing review pane already produces for a comment. The current template likely builds this inline inside its main render loop; before this task, extract that snippet into a top-level function `renderCommentBlock(c)` so both the live view and the historical view use the same markup. Required fields: `c.id`, `c.text`, `c.status`, plus the existing buttons for resolve/unresolve/edit/delete. The wired buttons keep their current `onclick` handlers — those endpoints work identically on historical-version comments.

Add CSS for the historical pane:

```css
.historical-pane { padding: 16px; font-family: ui-monospace, monospace; }
.historical-pane .line { display: flex; gap: 8px; align-items: baseline; }
.historical-pane .ln { color: #6e7681; min-width: 4ch; text-align: right; }
.historical-pane .content { white-space: pre; }
.diff-notice { background: #21262d; padding: 6px 10px; margin-bottom: 12px; border-left: 3px solid #d29922; color: #d29922; }
.diff-text { white-space: pre; background: #0d1117; padding: 12px; border-radius: 4px; overflow: auto; }
.diff-comments { margin-top: 12px; }
.diff-comment-anchor { margin-top: 8px; }
```

- [ ] **Step 2: Manual smoke check**

Run: `uv run redliner open <plan.md>` with at least one snapshot taken and a comment pinned to v0. Expectations:

- Selecting `v0 baseline` shows the full document (diff is hidden because v0 has no previous).
- Taking another snapshot, then selecting `v1` defaults to a diff view.
- Diff/Full toggle switches between rendering.
- Pinned comments render under their lines in Full view.
- Comments anchored outside any hunk show the "(N comments hidden)" banner in Diff view.
- Selecting `current` restores the normal review pane.

- [ ] **Step 3: Run full suite for backend regressions**

Run: `uv run pytest tests/redliner/ -v`
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add src/redliner/web.py
git commit -m "$(cat <<'EOF'
feat: render historical versions in the web UI

Selecting a past version replaces the main pane with a diff (default) or full document, with pinned comments inline. Diff hides comments that fall outside hunks and shows a switch-to-Full hint.

Assisted-By: Claude Code
Pedro
EOF
)"
```

---

## Task 14: UI — disable editing/new-comments on past versions; keep status mutation enabled

**Files:**
- Modify: `src/redliner/web.py` (HTML_TEMPLATE only)

- [ ] **Step 1: Gate edit-mode and new-comment affordances**

Edit `src/redliner/web.py`. In the JS, add a helper:

```js
function isHistoricalView() {
  return selectedVersion !== null;
}

function applyHistoricalDisable() {
  const editToggle = document.getElementById('mode-toggle');  // adjust selector to match template
  if (editToggle) editToggle.disabled = isHistoricalView();
  document.querySelectorAll('.new-comment-input, .add-comment-btn').forEach(el => {
    el.style.display = isHistoricalView() ? 'none' : '';
  });
}
```

Call `applyHistoricalDisable()` at the end of `selectVersion`.

In the existing comment-form rendering, add a guard so click handlers that open a "new comment" modal are no-ops when `isHistoricalView()` is true. Resolve, unresolve, edit-text, and delete buttons remain enabled because `comment.status` is mutable on any version.

- [ ] **Step 2: Manual smoke check**

- Selecting v0/v1: edit-mode toggle is disabled; line-click "add comment" affordance does not appear.
- Resolve/unresolve buttons on existing comments still work and persist.
- Returning to `current`: all editing and commenting works again.

- [ ] **Step 3: Run full suite for backend regressions**

Run: `uv run pytest tests/redliner/ -v`
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add src/redliner/web.py
git commit -m "$(cat <<'EOF'
feat: disable editing and new comments on historical versions

Resolve/edit-text/delete on existing comments stays enabled because comment status is mutable across versions.

Assisted-By: Claude Code
Pedro
EOF
)"
```

---

## Task 15: UI — Snapshot button + dropdown auto-refresh

**Files:**
- Modify: `src/redliner/web.py` (HTML_TEMPLATE only)

The Snapshot button HTML already exists from Task 12. This task wires it and refreshes the dropdown after relevant mutations.

- [ ] **Step 1: Implement `takeSnapshot` and refresh hooks**

Edit `src/redliner/web.py`. Add JS:

```js
async function takeSnapshot() {
  const filePath = window.__filePath || '';
  const resp = await fetch('/api/snapshot', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ file: filePath }),
  });
  if (!resp.ok) {
    alert('Snapshot failed: ' + (await resp.text()));
    return;
  }
  await loadVersions();
}
```

Make `loadVersions()` callable from the existing comment-mutation handlers so counts in the menu stay live. Find the success branches of the resolve/unresolve/delete/edit-comment fetches and add:

```js
loadVersions();
```

after the existing UI refresh.

- [ ] **Step 2: Manual smoke check**

- Click Snapshot on `current`. Open the dropdown; a new `vN` entry appears with timestamp and counts. The button label still reads `current ▾` (selection unchanged).
- Resolve a comment. Open the dropdown; the resolved/pending counts on the affected version reflect the change without a page reload.

- [ ] **Step 3: Run full suite for backend regressions**

Run: `uv run pytest tests/redliner/ -v`
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add src/redliner/web.py
git commit -m "$(cat <<'EOF'
feat: wire Snapshot button and live-refresh dropdown counts

Snapshot button POSTs /api/snapshot then reloads the version list. Comment-status mutations also trigger a version-list refresh so per-version counts stay current.

Assisted-By: Claude Code
Pedro
EOF
)"
```

---

## Final verification

- [ ] **Step 1: Run the entire test suite**

Run: `uv run pytest tests/redliner/ -v`
Expected: all tests pass (existing + new).

- [ ] **Step 2: Manual end-to-end walkthrough**

In a scratch directory:

```bash
echo -e "line one\nline two\nline three\n" > scratch.md
uv run redliner open scratch.md
```

In the browser:
1. Confirm header shows `current ▾` dropdown and Snapshot button.
2. Add a comment on line 2.
3. Open dropdown — verify `current` shows `1 pending, 0 resolved` and `v0 baseline` shows `0 pending, 0 resolved` (because the comment is pinned to v0, the head when no snapshot has been taken).
4. Click Snapshot. Dropdown now lists `v1`. Verify `v0 baseline` shows `1 pending`.
5. Switch to Edit mode, change line 2's text, click Snapshot. `v2` appears.
6. Select `v2` — diff view shows the line change vs v1; toggle Full to see whole document.
7. Select `v0 baseline` — full document shows; the original line 2 comment is visible.
8. Select `current` — back to normal review.
9. Resolve the v0 comment. Dropdown count for `v0 baseline` updates to `0 pending, 1 resolved`.
10. Approve the file (Approve button); confirms across all versions.

- [ ] **Step 3: CLI sanity**

```bash
uv run redliner versions scratch.md          # lists v0, v1, v2 with counts
uv run redliner show scratch.md --version 1  # prints v1 content
uv run redliner show scratch.md --version 2 --diff  # prints diff vs v1
uv run redliner snapshot scratch.md          # creates v3
```

If any of the above misbehaves, fix and re-run the relevant task's tests before moving on.
