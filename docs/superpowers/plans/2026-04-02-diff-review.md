# Git Diff Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `compose-review diff` command that opens a side-by-side git diff review UI with collapsible columns and per-file commenting.

**Architecture:** New `diff.py` module parses `git diff HEAD` output into structured data. `web.py` gets a diff mode with a second HTML template for two-column layout. CLI gets a `diff` subcommand. Existing `Comment`/`Review` model and per-file `.review.json` sidecars are reused unchanged.

**Tech Stack:** Python 3.12+, stdlib only (`subprocess`, `http.server`, `dataclasses`, `json`)

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `src/compose_review/diff.py` | Create | `DiffLine`/`FileDiff` dataclasses, `parse_git_diff()` function |
| `src/compose_review/web.py` | Modify | Add diff mode to server, new endpoints, `DIFF_HTML_TEMPLATE` |
| `src/compose_review/cli.py` | Modify | Add `diff` subcommand |
| `tests/compose_review/test_diff.py` | Create | Tests for diff parsing |
| `tests/compose_review/test_cli_diff.py` | Create | Tests for diff CLI subcommand |
| `tests/compose_review/conftest.py` | Create | Shared fixtures (temp git repos) |

---

### Task 1: Diff Parsing — Data Model and Parser

**Files:**
- Create: `src/compose_review/diff.py`
- Create: `tests/compose_review/test_diff.py`
- Create: `tests/compose_review/conftest.py`

- [ ] **Step 1: Create test directory and conftest with git repo fixture**

```python
# tests/compose_review/conftest.py
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    """Create a temporary git repo with an initial commit."""
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    # Initial commit with a file
    hello = tmp_path / "hello.py"
    hello.write_text("def greet():\n    return 'hello'\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init", "--no-gpg-sign"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    return tmp_path
```

- [ ] **Step 2: Write failing tests for parse_git_diff**

```python
# tests/compose_review/test_diff.py
from __future__ import annotations

import subprocess
from pathlib import Path

from compose_review.diff import DiffLine, FileDiff, parse_git_diff


def test_parse_no_changes(git_repo: Path) -> None:
    result = parse_git_diff(cwd=git_repo)
    assert result == []


def test_parse_modified_file(git_repo: Path) -> None:
    hello = git_repo / "hello.py"
    hello.write_text("def greet():\n    return 'hi'\n")

    result = parse_git_diff(cwd=git_repo)
    assert len(result) == 1
    assert result[0].path == "hello.py"

    lines = result[0].lines
    # Should have context, removed, and added lines
    kinds = [line.kind for line in lines]
    assert "context" in kinds
    assert "removed" in kinds
    assert "added" in kinds

    # Removed line should have old_num but no new_num
    removed = [l for l in lines if l.kind == "removed"]
    assert len(removed) == 1
    assert removed[0].old_num is not None
    assert removed[0].new_num is None
    assert removed[0].text == "    return 'hello'"

    # Added line should have new_num but no old_num
    added = [l for l in lines if l.kind == "added"]
    assert len(added) == 1
    assert added[0].old_num is None
    assert added[0].new_num is not None
    assert added[0].text == "    return 'hi'"


def test_parse_new_file(git_repo: Path) -> None:
    new_file = git_repo / "new.py"
    new_file.write_text("x = 1\n")

    result = parse_git_diff(cwd=git_repo)
    assert len(result) == 1
    assert result[0].path == "new.py"
    # All lines should be added
    assert all(l.kind == "added" for l in result[0].lines)


def test_parse_deleted_file(git_repo: Path) -> None:
    hello = git_repo / "hello.py"
    hello.unlink()

    result = parse_git_diff(cwd=git_repo)
    assert len(result) == 1
    assert result[0].path == "hello.py"
    # All lines should be removed
    assert all(l.kind == "removed" for l in result[0].lines)


def test_parse_with_path_filter(git_repo: Path) -> None:
    # Modify two files
    (git_repo / "hello.py").write_text("def greet():\n    return 'hi'\n")
    (git_repo / "other.py").write_text("y = 2\n")

    result = parse_git_diff(path="hello.py", cwd=git_repo)
    assert len(result) == 1
    assert result[0].path == "hello.py"


def test_parse_multiple_hunks(git_repo: Path) -> None:
    # Create a file with many lines, then change two distant lines
    lines = [f"line{i}" for i in range(30)]
    hello = git_repo / "hello.py"
    hello.write_text("\n".join(lines) + "\n")
    subprocess.run(["git", "add", "."], cwd=git_repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "many lines", "--no-gpg-sign"],
        cwd=git_repo, check=True, capture_output=True,
    )
    lines[2] = "CHANGED2"
    lines[27] = "CHANGED27"
    hello.write_text("\n".join(lines) + "\n")

    result = parse_git_diff(cwd=git_repo)
    assert len(result) == 1
    # Should have multiple hunks worth of lines
    removed = [l for l in result[0].lines if l.kind == "removed"]
    added = [l for l in result[0].lines if l.kind == "added"]
    assert len(removed) == 2
    assert len(added) == 2
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd /home/silva/workspace/plan-review-tool && python -m pytest tests/compose_review/test_diff.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'compose_review.diff'`

- [ ] **Step 4: Implement diff.py**

```python
# src/compose_review/diff.py
"""Parse git diff output into structured data."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class DiffLine:
    old_num: int | None  # None for added lines
    new_num: int | None  # None for removed lines
    kind: str  # "context" | "added" | "removed"
    text: str


@dataclass
class FileDiff:
    path: str
    lines: list[DiffLine] = field(default_factory=list)


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_DIFF_HEADER_RE = re.compile(r"^diff --git a/.+ b/(.+)$")


def parse_git_diff(
    path: str | None = None,
    cwd: Path | None = None,
) -> list[FileDiff]:
    """Parse `git diff HEAD` output into FileDiff objects."""
    cmd = ["git", "diff", "HEAD"]
    if path:
        cmd += ["--", path]

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=cwd,
    )
    if result.returncode != 0:
        return []

    output = result.stdout
    if not output.strip():
        return []

    files: list[FileDiff] = []
    current: FileDiff | None = None
    old_num = 0
    new_num = 0

    for raw_line in output.splitlines():
        # New file header
        m = _DIFF_HEADER_RE.match(raw_line)
        if m:
            current = FileDiff(path=m.group(1))
            files.append(current)
            continue

        if current is None:
            continue

        # Skip diff metadata lines
        if raw_line.startswith("---") or raw_line.startswith("+++"):
            continue
        if raw_line.startswith("index ") or raw_line.startswith("new file") or raw_line.startswith("deleted file"):
            continue

        # Hunk header
        hm = _HUNK_RE.match(raw_line)
        if hm:
            old_num = int(hm.group(1))
            new_num = int(hm.group(2))
            continue

        # Diff content lines
        if raw_line.startswith("-"):
            current.lines.append(DiffLine(
                old_num=old_num,
                new_num=None,
                kind="removed",
                text=raw_line[1:],
            ))
            old_num += 1
        elif raw_line.startswith("+"):
            current.lines.append(DiffLine(
                old_num=None,
                new_num=new_num,
                kind="added",
                text=raw_line[1:],
            ))
            new_num += 1
        elif raw_line.startswith(" "):
            current.lines.append(DiffLine(
                old_num=old_num,
                new_num=new_num,
                kind="context",
                text=raw_line[1:],
            ))
            old_num += 1
            new_num += 1
        # Skip \ No newline at end of file and other noise

    return files
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /home/silva/workspace/plan-review-tool && python -m pytest tests/compose_review/test_diff.py -v`
Expected: All 6 tests PASS

- [ ] **Step 6: Commit**

```bash
git add src/compose_review/diff.py tests/compose_review/test_diff.py tests/compose_review/conftest.py
git commit -m "feat: add git diff parser with DiffLine/FileDiff data model"
```

---

### Task 2: CLI Diff Subcommand

**Files:**
- Modify: `src/compose_review/cli.py`
- Create: `tests/compose_review/test_cli_diff.py`

- [ ] **Step 1: Write failing test for diff subcommand argument parsing**

```python
# tests/compose_review/test_cli_diff.py
from __future__ import annotations

import json
import subprocess
from pathlib import Path


def test_diff_no_changes_exits_zero(git_repo: Path) -> None:
    """When there are no changes, diff should exit 0 with empty status."""
    result = subprocess.run(
        ["python", "-m", "compose_review", "diff", "--no-open"],
        cwd=git_repo,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert data["files"] == 0


def test_diff_with_changes_no_open(git_repo: Path) -> None:
    """With --no-open, diff should print status and exit without launching browser."""
    (git_repo / "hello.py").write_text("def greet():\n    return 'hi'\n")
    result = subprocess.run(
        ["python", "-m", "compose_review", "diff", "--no-open"],
        cwd=git_repo,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert data["files"] == 1


def test_diff_path_filter(git_repo: Path) -> None:
    (git_repo / "hello.py").write_text("def greet():\n    return 'hi'\n")
    (git_repo / "other.py").write_text("y = 2\n")
    result = subprocess.run(
        ["python", "-m", "compose_review", "diff", "--path", "hello.py", "--no-open"],
        cwd=git_repo,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert data["files"] == 1
```

Note: `--no-open` is a convenience flag that skips launching the browser and just prints the diff summary as JSON. This is useful for testing and for agents that just want to check status.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/silva/workspace/plan-review-tool && python -m pytest tests/compose_review/test_cli_diff.py -v`
Expected: FAIL — `diff` subcommand not recognized

- [ ] **Step 3: Implement cmd_diff and add subcommand to CLI**

Add to `src/compose_review/cli.py`, after the existing `cmd_open` function:

```python
def cmd_diff(args: argparse.Namespace) -> None:
    from compose_review.diff import parse_git_diff

    file_diffs = parse_git_diff(path=args.path)

    if not file_diffs:
        print(json.dumps({"files": 0, "status": "no_changes"}))
        sys.exit(0)

    if args.no_open:
        print(json.dumps({"files": len(file_diffs), "status": "pending"}))
        sys.exit(0)

    from compose_review.web import run_diff_web

    result = run_diff_web(file_diffs)
    print(json.dumps(result))
    sys.exit(0 if result["status"] == "approved" else 1)
```

Add the subparser in `main()`, after the `open` subparser block:

```python
    # diff
    p = sub.add_parser("diff", help="Review git diff in side-by-side web UI")
    p.add_argument("--path", default=None, help="Filter to a single file path")
    p.add_argument("--no-open", action="store_true", help="Print status without launching browser")
    p.set_defaults(func=cmd_diff)
```

- [ ] **Step 4: Run CLI tests to verify they pass**

Run: `cd /home/silva/workspace/plan-review-tool && python -m pytest tests/compose_review/test_cli_diff.py -v`
Expected: All 3 tests PASS

- [ ] **Step 5: Run all tests to check no regressions**

Run: `cd /home/silva/workspace/plan-review-tool && python -m pytest tests/ -v`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add src/compose_review/cli.py tests/compose_review/test_cli_diff.py
git commit -m "feat: add diff subcommand to CLI"
```

---

### Task 3: Web Server Diff Mode — Backend

**Files:**
- Modify: `src/compose_review/web.py`

This task adds the diff-mode server attributes, new API endpoints, and route dispatch. The HTML template is added in Task 4.

- [ ] **Step 1: Add diff mode attributes to ReviewServer**

At the top of `web.py`, add the import:

```python
from compose_review.diff import FileDiff
```

Modify the `ReviewServer` class:

```python
class ReviewServer(HTTPServer):
    plan_file: Path
    done: bool = False
    mode: str = "plan"  # "plan" | "diff"
    diff_data: list[FileDiff] = []
    active_file: str = ""
    repo_root: Path = Path(".")
```

- [ ] **Step 2: Add diff-mode route dispatch to do_GET and do_POST**

In `ReviewHandler.do_GET`, add diff-mode routes:

```python
    def do_GET(self) -> None:
        if self.path == "/":
            self._serve_html()
        elif self.path == "/api/review":
            self._get_review()
        elif self.path == "/api/diff" and self.server.mode == "diff":
            self._get_diff()
        else:
            self._not_found()
```

In `ReviewHandler.do_POST`, add diff-mode routes:

```python
    def do_POST(self) -> None:
        if self.path == "/api/comment":
            self._add_comment()
        elif self.path == "/api/resolve-all":
            self._resolve_all()
        elif self.path == "/api/approve":
            self._approve()
        elif self.path == "/api/quit":
            self._quit()
        elif self.path == "/api/select-file" and self.server.mode == "diff":
            self._select_file()
        elif m := re.match(r"^/api/resolve/(\d+)$", self.path):
            self._resolve(int(m.group(1)))
        else:
            self._not_found()
```

- [ ] **Step 3: Update _serve_html to serve diff template in diff mode**

```python
    def _serve_html(self) -> None:
        template = DIFF_HTML_TEMPLATE if self.server.mode == "diff" else HTML_TEMPLATE
        body = template.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
```

- [ ] **Step 4: Add _active_plan_file helper and update existing endpoints**

The existing comment/resolve/approve endpoints use `self.server.plan_file`. In diff mode, this needs to point to the active file so the sidecar is written to the right place. Add this helper method to `ReviewHandler`:

```python
    def _active_plan_file(self) -> Path:
        """Return the file path that comment/resolve/approve should operate on."""
        if self.server.mode == "diff":
            return self.server.repo_root / self.server.active_file
        return self.server.plan_file
```

Then make these replacements in the existing endpoint methods:

In `_get_review`: change `plan_file = self.server.plan_file` to `plan_file = self._active_plan_file()`

In `_add_comment`: change `plan_file = self.server.plan_file` to `plan_file = self._active_plan_file()`

In `_resolve`: change `plan_file = self.server.plan_file` to `plan_file = self._active_plan_file()`

In `_resolve_all`: change `plan_file = self.server.plan_file` to `plan_file = self._active_plan_file()`

`_approve` is handled separately in step 6 (diff mode gets its own approval logic).

- [ ] **Step 5: Implement _get_diff and _select_file endpoints**

```python
    def _get_diff(self) -> None:
        from dataclasses import asdict
        files = []
        for fd in self.server.diff_data:
            plan_file = self.server.repo_root / fd.path
            review = load_review(plan_file)
            files.append({
                "path": fd.path,
                "lines": [asdict(l) for l in fd.lines],
                "review": {
                    "status": review.status,
                    "pending": len(review.pending),
                    "resolved": len(review.resolved),
                    "comments": [
                        {"id": c.id, "line": c.line, "text": c.text,
                         "status": c.status, "created": c.created}
                        for c in review.comments
                    ],
                    "approved_at": review.approved_at,
                },
            })
        self._json_response({
            "files": files,
            "active_file": self.server.active_file,
        })

    def _select_file(self) -> None:
        body = self._read_body()
        path = body.get("path", "")
        valid_paths = [fd.path for fd in self.server.diff_data]
        if path not in valid_paths:
            self._json_response({"error": f"Unknown file: {path}"}, 404)
            return
        self.server.active_file = path
        self._get_diff()
```

- [ ] **Step 6: Update _approve for diff mode to approve all files**

```python
    def _approve(self) -> None:
        if self.server.mode == "diff":
            self._approve_diff()
            return
        plan_file = self.server.plan_file
        review = load_review(plan_file)
        if not review.approve():
            self._json_response(
                {"error": f"Cannot approve: {len(review.pending)} unresolved comment(s)"},
                409,
            )
            return
        save_review(plan_file, review)
        self.server.done = True
        self._json_response(self._review_dict())

    def _approve_diff(self) -> None:
        # Check all files for pending comments
        total_pending = 0
        for fd in self.server.diff_data:
            plan_file = self.server.repo_root / fd.path
            review = load_review(plan_file)
            total_pending += len(review.pending)

        if total_pending > 0:
            self._json_response(
                {"error": f"Cannot approve: {total_pending} unresolved comment(s) across files"},
                409,
            )
            return

        # Approve all files
        for fd in self.server.diff_data:
            plan_file = self.server.repo_root / fd.path
            review = load_review(plan_file)
            review.approve()
            save_review(plan_file, review)

        self.server.done = True
        self._get_diff()
```

- [ ] **Step 7: Add run_diff_web function**

Add at the bottom of `web.py`, after `run_web`:

```python
def run_diff_web(file_diffs: list[FileDiff]) -> dict:
    """Start a local web server for interactive diff review and block until done."""
    server = ReviewServer(("127.0.0.1", 0), ReviewHandler)
    server.mode = "diff"
    server.diff_data = file_diffs
    server.active_file = file_diffs[0].path if file_diffs else ""
    server.repo_root = Path.cwd()
    server.done = False
    server.timeout = 0.5

    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}"
    print(f"compose-review diff: {url}", file=sys.stderr)

    webbrowser.open(url)

    try:
        while not server.done:
            server.handle_request()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

    # Aggregate status across all files
    total_pending = 0
    total_resolved = 0
    all_approved = True
    for fd in file_diffs:
        review = load_review(Path.cwd() / fd.path)
        total_pending += len(review.pending)
        total_resolved += len(review.resolved)
        if review.status != "approved":
            all_approved = False

    return {
        "status": "approved" if all_approved else "in_review",
        "files": len(file_diffs),
        "pending": total_pending,
        "resolved": total_resolved,
    }
```

- [ ] **Step 8: Commit**

```bash
git add src/compose_review/web.py
git commit -m "feat: add diff mode backend to web server"
```

---

### Task 4: Diff HTML Template — Side-by-Side UI

**Files:**
- Modify: `src/compose_review/web.py` (add `DIFF_HTML_TEMPLATE`)

This is the largest task. The HTML template contains the full CSS and JavaScript for the side-by-side diff view with collapsible columns.

- [ ] **Step 1: Add DIFF_HTML_TEMPLATE to web.py**

Add before `run_web`, after the existing `HTML_TEMPLATE`:

```python
DIFF_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>compose-review diff</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🎷</text></svg>">
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

body {
  background: #0d1117;
  color: #e6edf3;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  font-size: 14px;
  line-height: 1.5;
}

header {
  position: sticky;
  top: 0;
  z-index: 10;
  background: #161b22;
  border-bottom: 1px solid #30363d;
  padding: 12px 24px;
  display: flex;
  align-items: center;
  gap: 16px;
  flex-wrap: wrap;
}
header.approved { background: #0a2e1a; border-bottom-color: #238636; }

.title { font-size: 16px; font-weight: 600; flex-shrink: 0; }
.stats { display: flex; gap: 8px; align-items: center; flex: 1; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 12px; font-weight: 500; }
.badge.pending-badge { background: #2d1600; color: #d29922; border: 1px solid #d29922; }
.badge.resolved-badge { background: #0a2e1a; color: #3fb950; border: 1px solid #238636; }
.badge.approved-badge { background: #238636; color: #fff; border: 1px solid #2ea043; }
.actions { display: flex; gap: 8px; flex-shrink: 0; }

button {
  padding: 5px 16px; border-radius: 6px; border: 1px solid #30363d;
  background: #21262d; color: #e6edf3; font-size: 13px; cursor: pointer;
  font-weight: 500; transition: background 0.15s;
}
button:hover { background: #30363d; }
button:disabled { opacity: 0.4; cursor: not-allowed; }
button.btn-approve { background: #238636; border-color: #2ea043; }
button.btn-approve:hover { background: #2ea043; }
button.btn-approve:disabled { background: #238636; }
button.btn-danger { color: #f85149; border-color: #f8514966; }
button.btn-danger:hover { background: #da36332e; }
button.btn-submit { background: #238636; border-color: #2ea043; }
button.btn-submit:hover { background: #2ea043; }
button.btn-cancel { background: transparent; border-color: #30363d; }

/* File tabs */
.file-tabs {
  display: flex; gap: 0; padding: 0 24px;
  background: #161b22; border-bottom: 1px solid #30363d;
  overflow-x: auto;
}
.file-tab {
  padding: 8px 16px; font-size: 13px; cursor: pointer;
  border-bottom: 2px solid transparent; color: #8b949e;
  font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, monospace;
  white-space: nowrap; transition: color 0.15s;
}
.file-tab:hover { color: #e6edf3; }
.file-tab.active { color: #e6edf3; border-bottom-color: #f78166; }
.file-tab .tab-badge {
  display: inline-block; margin-left: 6px; padding: 0 6px;
  border-radius: 10px; font-size: 11px; background: #d29922; color: #0d1117;
}
.file-tab .tab-badge.clean { background: #238636; color: #fff; }

/* Diff container */
.diff-container {
  display: grid; margin: 0; min-height: calc(100vh - 120px);
  transition: grid-template-columns 0.2s ease;
}
.diff-container.both-open { grid-template-columns: 1fr 1fr; }
.diff-container.left-collapsed { grid-template-columns: 40px 1fr; }
.diff-container.right-collapsed { grid-template-columns: 1fr 40px; }

.diff-column {
  border: 1px solid #30363d; overflow: hidden;
  display: flex; flex-direction: column;
}

.col-header {
  display: flex; align-items: center; gap: 8px;
  padding: 8px 12px; background: #161b22; border-bottom: 1px solid #30363d;
  font-size: 13px; font-weight: 600; position: sticky; top: 0;
}
.col-header .col-label { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.col-toggle {
  width: 24px; height: 24px; border: none; background: transparent;
  color: #8b949e; cursor: pointer; font-size: 16px; padding: 0;
  display: flex; align-items: center; justify-content: center;
  border-radius: 4px;
}
.col-toggle:hover { background: #30363d; color: #e6edf3; }

.col-body { flex: 1; overflow-y: auto; overflow-x: auto; }
.collapsed .col-body { display: none; }
.collapsed .col-header { writing-mode: vertical-rl; padding: 12px 4px; }
.collapsed .col-header .col-label { display: none; }

/* Diff lines */
.diff-line {
  display: grid; grid-template-columns: 50px 1fr;
  min-height: 22px; cursor: pointer;
}
.diff-line:hover { filter: brightness(1.15); }
.diff-line.empty-row { opacity: 0.3; }

.diff-line-num {
  color: #484f58; text-align: right; padding: 0 8px;
  font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, monospace;
  font-size: 13px; user-select: none; line-height: 22px;
  border-right: 1px solid #21262d;
}
.diff-line-text {
  padding: 0 12px;
  font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, monospace;
  font-size: 13px; white-space: pre; line-height: 22px; tab-size: 4;
}

.diff-line.removed { background: #3d1214; }
.diff-line.removed .diff-line-num { color: #f85149; }
.diff-line.added { background: #1a2e1a; }
.diff-line.added .diff-line-num { color: #3fb950; }

/* Comments — reuse plan review styles */
.comment-block {
  margin-left: 50px; border-left: 3px solid; padding: 8px 16px;
  display: flex; align-items: flex-start; gap: 10px; font-size: 13px;
  border-bottom: 1px solid #21262d;
}
.comment-block.pending { border-left-color: #d29922; background: #2d160044; }
.comment-block.resolved { border-left-color: #238636; background: #0a2e1a44; }
.comment-meta { color: #8b949e; font-size: 12px; white-space: nowrap; flex-shrink: 0; }
.comment-text { flex: 1; word-break: break-word; }
.comment-actions { flex-shrink: 0; }
.comment-actions button { padding: 2px 10px; font-size: 12px; }
.resolved-tag { color: #3fb950; font-size: 12px; font-weight: 500; }

.comment-form {
  margin-left: 50px; padding: 10px 16px; background: #161b22;
  border-bottom: 1px solid #30363d; border-left: 3px solid #58a6ff;
}
.comment-form textarea {
  width: 100%; min-height: 60px; background: #0d1117; color: #e6edf3;
  border: 1px solid #30363d; border-radius: 6px; padding: 8px 12px;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  font-size: 13px; resize: vertical; outline: none;
}
.comment-form textarea:focus { border-color: #58a6ff; box-shadow: 0 0 0 2px #58a6ff33; }
.form-actions { display: flex; gap: 8px; margin-top: 8px; justify-content: flex-end; }
.form-hint { color: #484f58; font-size: 11px; margin-top: 4px; }
</style>
</head>
<body>

<header id="header">
  <div class="title">compose-review diff</div>
  <div class="stats" id="stats"></div>
  <div class="actions" id="header-actions"></div>
</header>

<div class="file-tabs" id="file-tabs"></div>

<div class="diff-container both-open" id="diff-container">
  <div class="diff-column" id="col-old">
    <div class="col-header">
      <button class="col-toggle" id="toggle-old" title="Collapse old">&lsaquo;</button>
      <span class="col-label">Old</span>
    </div>
    <div class="col-body" id="old-body"></div>
  </div>
  <div class="diff-column" id="col-new">
    <div class="col-header">
      <span class="col-label">New</span>
      <button class="col-toggle" id="toggle-new" title="Collapse new">&rsaquo;</button>
    </div>
    <div class="col-body" id="new-body"></div>
  </div>
</div>

<script>
let state = null;
let activeFormLine = null;
let activeFormSide = null;
let leftCollapsed = false;
let rightCollapsed = false;

async function fetchDiff() {
  const res = await fetch('/api/diff');
  state = await res.json();
  render();
}

function escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

function getActiveFile() {
  if (!state) return null;
  return state.files.find(f => f.path === state.active_file) || state.files[0];
}

function getTotalStats() {
  let pending = 0, resolved = 0;
  let allApproved = true;
  for (const f of state.files) {
    pending += f.review.pending;
    resolved += f.review.resolved;
    if (f.review.status !== 'approved') allApproved = false;
  }
  return { pending, resolved, allApproved };
}

function render() {
  if (!state) return;

  const stats = getTotalStats();
  const hdr = document.getElementById('header');
  hdr.className = stats.allApproved ? 'approved' : '';

  // Stats
  const statsEl = document.getElementById('stats');
  if (stats.allApproved) {
    statsEl.innerHTML = '<span class="badge approved-badge">Approved</span>';
  } else {
    statsEl.innerHTML =
      `<span class="badge pending-badge">${stats.pending} pending</span>` +
      `<span class="badge resolved-badge">${stats.resolved} resolved</span>`;
  }

  // Actions
  const actionsEl = document.getElementById('header-actions');
  if (stats.allApproved) {
    actionsEl.innerHTML = '<button class="btn-danger" onclick="quit()">Close</button>';
  } else {
    actionsEl.innerHTML =
      `<button onclick="resolveAll()" ${stats.pending === 0 ? 'disabled' : ''}>Resolve All</button>` +
      `<button class="btn-approve" onclick="approveReview()" ${stats.pending > 0 ? 'disabled' : ''}>Approve</button>`;
  }

  // File tabs
  const tabsEl = document.getElementById('file-tabs');
  tabsEl.innerHTML = '';
  for (const f of state.files) {
    const tab = document.createElement('div');
    tab.className = 'file-tab' + (f.path === state.active_file ? ' active' : '');
    const badgeClass = f.review.pending > 0 ? '' : ' clean';
    const badgeText = f.review.pending > 0 ? f.review.pending : '\\u2713';
    tab.innerHTML = escapeHtml(f.path) +
      `<span class="tab-badge${badgeClass}">${badgeText}</span>`;
    tab.addEventListener('click', () => selectFile(f.path));
    tabsEl.appendChild(tab);
  }

  // Diff columns
  const file = getActiveFile();
  if (!file) return;

  const oldBody = document.getElementById('old-body');
  const newBody = document.getElementById('new-body');
  oldBody.innerHTML = '';
  newBody.innerHTML = '';

  const commentsByLine = {};
  file.review.comments.forEach(c => {
    const key = c.line;
    (commentsByLine[key] ||= []).push(c);
  });

  for (const line of file.lines) {
    // Old side
    if (line.kind === 'added') {
      // Empty placeholder on old side
      const emptyRow = document.createElement('div');
      emptyRow.className = 'diff-line empty-row';
      emptyRow.innerHTML = '<span class="diff-line-num"></span><span class="diff-line-text"></span>';
      oldBody.appendChild(emptyRow);
    } else {
      const row = document.createElement('div');
      row.className = 'diff-line' + (line.kind === 'removed' ? ' removed' : '');
      row.innerHTML =
        `<span class="diff-line-num">${line.old_num}</span>` +
        `<span class="diff-line-text">${escapeHtml(line.text)}</span>`;
      if (!stats.allApproved) {
        const lineNum = line.kind === 'removed' ? line.old_num : line.new_num;
        const side = line.kind === 'removed' ? 'old' : 'new';
        row.addEventListener('click', () => showCommentForm(lineNum, side));
      }
      oldBody.appendChild(row);
    }

    // New side
    if (line.kind === 'removed') {
      // Empty placeholder on new side
      const emptyRow = document.createElement('div');
      emptyRow.className = 'diff-line empty-row';
      emptyRow.innerHTML = '<span class="diff-line-num"></span><span class="diff-line-text"></span>';
      newBody.appendChild(emptyRow);
    } else {
      const row = document.createElement('div');
      row.className = 'diff-line' + (line.kind === 'added' ? ' added' : '');
      row.innerHTML =
        `<span class="diff-line-num">${line.new_num}</span>` +
        `<span class="diff-line-text">${escapeHtml(line.text)}</span>`;
      if (!stats.allApproved) {
        row.addEventListener('click', () => showCommentForm(line.new_num, 'new'));
      }
      newBody.appendChild(row);
    }

    // Comments on this line (show below the new side)
    const commentLine = line.new_num || line.old_num;
    const lineComments = commentsByLine[commentLine] || [];
    // Remove from map so we don't double-render
    delete commentsByLine[commentLine];

    for (const c of lineComments) {
      const block = document.createElement('div');
      block.className = `comment-block ${c.status}`;
      const actions = c.status === 'pending'
        ? `<div class="comment-actions"><button onclick="resolveComment(${c.id})">Resolve</button></div>`
        : `<div class="comment-actions"><span class="resolved-tag">Resolved</span></div>`;
      block.innerHTML =
        `<span class="comment-meta">#${c.id}</span>` +
        `<span class="comment-text">${escapeHtml(c.text)}</span>` +
        actions;
      newBody.appendChild(block);
      // Keep old side aligned with an empty spacer
      const spacer = document.createElement('div');
      spacer.style.height = '0';
      oldBody.appendChild(spacer);
    }

    // Comment form
    if (activeFormLine === (line.new_num || line.old_num) && !stats.allApproved) {
      newBody.appendChild(createCommentForm(activeFormLine));
      const spacer = document.createElement('div');
      spacer.style.height = '0';
      oldBody.appendChild(spacer);
      activeFormLine = null; // prevent double render
    }
  }

  // Restore activeFormLine for keyboard handler
  // (it was nulled above to prevent double render within the loop)
}

function showCommentForm(lineNum, side) {
  if (state && getTotalStats().allApproved) return;
  if (activeFormLine === lineNum) {
    activeFormLine = null;
    activeFormSide = null;
  } else {
    activeFormLine = lineNum;
    activeFormSide = side;
  }
  render();
  const ta = document.querySelector('.comment-form textarea');
  if (ta) ta.focus();
}

function createCommentForm(lineNum) {
  const form = document.createElement('div');
  form.className = 'comment-form';
  form.innerHTML =
    `<textarea placeholder="Add a comment on line ${lineNum}..." id="comment-input"></textarea>` +
    '<div class="form-actions">' +
    '  <button class="btn-cancel" onclick="cancelForm()">Cancel</button>' +
    `  <button class="btn-submit" onclick="submitComment(${lineNum})">Comment</button>` +
    '</div>' +
    '<div class="form-hint">Ctrl+Enter to submit</div>';
  form.addEventListener('click', e => e.stopPropagation());
  return form;
}

function cancelForm() { activeFormLine = null; activeFormSide = null; render(); }

async function selectFile(path) {
  await fetch('/api/select-file', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path }),
  });
  activeFormLine = null;
  activeFormSide = null;
  await fetchDiff();
}

async function submitComment(lineNum) {
  const ta = document.getElementById('comment-input');
  const text = ta ? ta.value.trim() : '';
  if (!text) return;
  const savedLine = lineNum;
  activeFormLine = null;
  activeFormSide = null;
  await fetch('/api/comment', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ line: savedLine, text }),
  });
  await fetchDiff();
}

async function resolveComment(id) {
  await fetch(`/api/resolve/${id}`, { method: 'POST' });
  await fetchDiff();
}

async function resolveAll() {
  await fetch('/api/resolve-all', { method: 'POST' });
  await fetchDiff();
}

async function approveReview() {
  const res = await fetch('/api/approve', { method: 'POST' });
  if (res.ok) await fetchDiff();
}

async function quit() {
  await fetch('/api/quit', { method: 'POST' });
  document.body.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100vh;color:#8b949e;font-size:16px;">Review closed. You can close this tab.</div>';
}

// Column collapse toggles
document.getElementById('toggle-old').addEventListener('click', () => {
  leftCollapsed = !leftCollapsed;
  updateLayout();
});
document.getElementById('toggle-new').addEventListener('click', () => {
  rightCollapsed = !rightCollapsed;
  updateLayout();
});

function updateLayout() {
  const container = document.getElementById('diff-container');
  const colOld = document.getElementById('col-old');
  const colNew = document.getElementById('col-new');
  container.className = 'diff-container ' +
    (leftCollapsed ? 'left-collapsed' : rightCollapsed ? 'right-collapsed' : 'both-open');
  colOld.classList.toggle('collapsed', leftCollapsed);
  colNew.classList.toggle('collapsed', rightCollapsed);
}

// Keyboard shortcuts
document.addEventListener('keydown', e => {
  if (e.ctrlKey && e.key === 'Enter' && activeFormLine !== null) {
    e.preventDefault();
    submitComment(activeFormLine);
  }
  if (e.key === 'Escape' && activeFormLine !== null) {
    e.preventDefault();
    cancelForm();
  }
});

fetchDiff();
</script>
</body>
</html>
"""
```

- [ ] **Step 2: Commit**

```bash
git add src/compose_review/web.py
git commit -m "feat: add side-by-side diff HTML template with collapsible columns"
```

---

### Task 5: Integration Test and Manual Verification

**Files:**
- No new files — verification only

- [ ] **Step 1: Run all unit tests**

Run: `cd /home/silva/workspace/plan-review-tool && python -m pytest tests/ -v`
Expected: All tests PASS

- [ ] **Step 2: Manual integration test**

In the plan-review-tool repo (which is a git repo with changes):

```bash
cd /home/silva/workspace/plan-review-tool
# Make a small change to test with
echo "# test change" >> README.md
compose-review diff
```

Expected:
- Browser opens with side-by-side diff view
- File tabs show changed files
- Can click lines to add comments
- Comments appear in the review panel
- Collapsible columns work (click chevrons)
- Resolve/approve flow works
- `.review.json` sidecar files are created

- [ ] **Step 3: Verify existing plan review still works**

```bash
compose-review open README.md
```

Expected: Plan review UI opens unchanged.

- [ ] **Step 4: Clean up test changes and sidecar files**

```bash
git checkout -- README.md
rm -f README.md.review.json
```

- [ ] **Step 5: Commit all implementation files**

```bash
git add -A
git commit -m "feat: complete git diff review with side-by-side UI"
```
