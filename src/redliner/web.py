"""Local web UI for interactive plan review."""

from __future__ import annotations

import json
import re
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from redliner.diff import FileDiff
from redliner.review import load_review, save_review, session_dir


class ReviewServer(HTTPServer):
    plan_file: Path
    done: bool = False
    mode: str = "plan"  # "plan" | "diff"
    diff_data: list[FileDiff]
    active_file: str = ""
    repo_root: Path = Path(".")
    session: Path = Path(".")
    snapshot_lock: threading.Lock

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.snapshot_lock = threading.Lock()


class ReviewHandler(BaseHTTPRequestHandler):
    server: ReviewServer

    def log_message(self, format: str, *args: object) -> None:
        pass

    # -- routing --

    def do_GET(self) -> None:
        if self.path == "/":
            self._serve_html()
        elif self.path == "/api/review":
            self._get_review()
        elif self.path == "/api/diff" and self.server.mode == "diff":
            self._get_diff()
        elif self.path.startswith("/api/versions"):
            # Plural must precede singular: both startswith match.
            self._get_versions()
        elif self.path.startswith("/api/version"):
            self._get_version()
        else:
            self._not_found()

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
        elif m := re.match(r"^/api/delete/(\d+)$", self.path):
            self._delete_comment(int(m.group(1)))
        elif m := re.match(r"^/api/edit/(\d+)$", self.path):
            self._edit_comment(int(m.group(1)))
        elif self.path == "/api/edit-content":
            self._save_edit_content()
        elif self.path == "/api/clear-edit":
            self._clear_edit()
        elif self.path == "/api/snapshot":
            self._snapshot()
        else:
            self._not_found()

    # -- helpers --

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length))

    def _json_response(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _not_found(self) -> None:
        self.send_response(404)
        self.end_headers()

    def _active_plan_file(self) -> Path:
        """Return the file path that comment/resolve/approve should operate on."""
        if self.server.mode == "diff":
            return self.server.repo_root / self.server.active_file
        return self.server.plan_file

    def _active_key(self) -> str:
        return str(self._active_plan_file().resolve())

    def _parse_query(self) -> dict[str, str]:
        qs = parse_qs(urlparse(self.path).query)
        return {k: v[0] for k, v in qs.items() if v}

    def _file_review_dict(self, file_key: str) -> dict:
        review = load_review(self.server.session)
        pending = review.pending_for(file_key)
        resolved = review.resolved_for(file_key)
        return {
            "status": review.status_for(file_key),
            "pending": len(pending),
            "resolved": len(resolved),
            "comments": [
                {
                    "id": c.id,
                    "file": c.file,
                    "line": c.line,
                    "text": c.text,
                    "status": c.status,
                    "created": c.created,
                }
                for c in review.comments_for(file_key)
            ],
            "approved_at": review.approved_at_for(file_key),
        }

    # -- endpoints --

    def _serve_html(self) -> None:
        template = DIFF_HTML_TEMPLATE if self.server.mode == "diff" else HTML_TEMPLATE
        body = template.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _get_review(self) -> None:
        plan_file = self._active_plan_file()
        key = self._active_key()
        raw = plan_file.read_text() if plan_file.exists() else ""
        lines = raw.splitlines()
        review = load_review(self.server.session)
        edit = review.get_edit(key)
        self._json_response({
            "filename": plan_file.name,
            "file_path": key,
            "storage": str(session_dir(self.server.session)),
            "lines": lines,
            "raw": raw,
            "review": self._file_review_dict(key),
            "edit": (
                {"content": edit.content, "diff": edit.diff, "saved": edit.saved}
                if edit
                else None
            ),
        })

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

    def _resolve(self, comment_id: int) -> None:
        review = load_review(self.server.session)
        if review.resolve(comment_id) is None:
            self._json_response({"error": f"Comment #{comment_id} not found"}, 404)
            return
        save_review(self.server.session, review)
        if self.server.mode == "diff":
            self._get_diff()
        else:
            self._json_response(self._file_review_dict(self._active_key()))

    def _delete_comment(self, comment_id: int) -> None:
        review = load_review(self.server.session)
        if review.delete(comment_id) is None:
            self._json_response({"error": f"Comment #{comment_id} not found"}, 404)
            return
        save_review(self.server.session, review)
        if self.server.mode == "diff":
            self._get_diff()
        else:
            self._json_response(self._file_review_dict(self._active_key()))

    def _edit_comment(self, comment_id: int) -> None:
        body = self._read_body()
        text = body.get("text", "").strip()
        if not text:
            self._json_response({"error": "text required"}, 400)
            return
        review = load_review(self.server.session)
        if review.edit(comment_id, text) is None:
            self._json_response({"error": f"Comment #{comment_id} not found"}, 404)
            return
        save_review(self.server.session, review)
        if self.server.mode == "diff":
            self._get_diff()
        else:
            self._json_response(self._file_review_dict(self._active_key()))

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

    def _clear_edit(self) -> None:
        if self.server.mode != "plan":
            self._json_response({"error": "editing only available in plan mode"}, 400)
            return
        review = load_review(self.server.session)
        review.clear_edit(self._active_key())
        save_review(self.server.session, review)
        self._get_review()

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

    def _resolve_all(self) -> None:
        review = load_review(self.server.session)
        if self.server.mode == "diff":
            for fd in self.server.diff_data:
                review.resolve_all(file=str((self.server.repo_root / fd.path).resolve()))
        else:
            review.resolve_all(file=self._active_key())
        save_review(self.server.session, review)
        if self.server.mode == "diff":
            self._get_diff()
        else:
            self._json_response(self._file_review_dict(self._active_key()))

    def _approve(self) -> None:
        if self.server.mode == "diff":
            self._approve_diff()
            return
        key = self._active_key()
        review = load_review(self.server.session)
        if not review.approve(key):
            self._json_response(
                {"error": f"Cannot approve: {len(review.pending_for(key))} unresolved comment(s)"},
                409,
            )
            return
        save_review(self.server.session, review)
        self.server.done = True
        self._json_response(self._file_review_dict(key))

    def _approve_diff(self) -> None:
        review = load_review(self.server.session)
        keys = [str((self.server.repo_root / fd.path).resolve()) for fd in self.server.diff_data]

        total_pending = sum(len(review.pending_for(k)) for k in keys)
        if total_pending > 0:
            self._json_response(
                {"error": f"Cannot approve: {total_pending} unresolved comment(s) across files"},
                409,
            )
            return

        for k in keys:
            review.approve(k)
        save_review(self.server.session, review)
        self.server.done = True
        self._get_diff()

    def _get_diff(self) -> None:
        from dataclasses import asdict
        review = load_review(self.server.session)
        files = []
        for fd in self.server.diff_data:
            key = str((self.server.repo_root / fd.path).resolve())
            pending = review.pending_for(key)
            resolved = review.resolved_for(key)
            files.append({
                "path": fd.path,
                "file_path": key,
                "lines": [asdict(dl) for dl in fd.lines],
                "review": {
                    "status": review.status_for(key),
                    "pending": len(pending),
                    "resolved": len(resolved),
                    "comments": [
                        {"id": c.id, "file": c.file, "line": c.line, "text": c.text,
                         "status": c.status, "created": c.created}
                        for c in review.comments_for(key)
                    ],
                    "approved_at": review.approved_at_for(key),
                },
            })
        self._json_response({
            "files": files,
            "active_file": self.server.active_file,
            "storage": str(session_dir(self.server.session)),
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

    def _quit(self) -> None:
        self.server.done = True
        self._json_response({"ok": True})


STORAGE_BAR_CSS = """
.storage-bar {
  display: flex; align-items: center; gap: 8px;
  padding: 6px 24px; background: #0d1117; border-bottom: 1px solid #21262d;
  font-size: 12px; color: #8b949e;
  font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, monospace;
}
.storage-bar .storage-label { flex-shrink: 0; text-transform: uppercase; letter-spacing: 0.5px; }
.storage-bar .storage-path {
  flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  color: #e6edf3; user-select: all;
}
.storage-bar .storage-copy {
  flex-shrink: 0; padding: 2px 10px; font-size: 11px;
  border: 1px solid #30363d; background: #21262d; color: #e6edf3;
  border-radius: 4px; cursor: pointer;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
}
.storage-bar .storage-copy:hover { background: #30363d; }
.storage-bar .storage-copy.copied { background: #238636; border-color: #2ea043; }
"""

STORAGE_BAR_JS = """
async function copyStorage() {
  const el = document.getElementById('storage-path');
  if (!el) return;
  const btn = document.getElementById('storage-copy');
  try {
    await navigator.clipboard.writeText(el.textContent);
  } catch {
    const r = document.createRange();
    r.selectNodeContents(el);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(r);
    try { document.execCommand('copy'); } catch {}
    sel.removeAllRanges();
  }
  if (btn) {
    btn.classList.add('copied');
    const orig = btn.textContent;
    btn.textContent = 'Copied';
    setTimeout(() => { btn.classList.remove('copied'); btn.textContent = orig; }, 1200);
  }
}
"""


DIFF_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>redliner diff</title>
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

__STORAGE_BAR_CSS__

/* File tabs */
.file-tabs {
  display: flex; gap: 0; padding: 0 24px;
  background: #161b22; border-bottom: 1px solid #30363d;
  overflow-x: auto; align-items: center;
}
.file-tabs-empty {
  padding: 8px 16px; color: #484f58; font-size: 13px; font-style: italic;
}
.file-tab {
  padding: 8px 16px; font-size: 13px; cursor: pointer;
  border-bottom: 2px solid transparent; color: #8b949e;
  font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, monospace;
  white-space: nowrap; transition: color 0.15s;
  display: flex; align-items: center; gap: 6px;
}
.file-tab:hover { color: #e6edf3; }
.file-tab.active { color: #e6edf3; border-bottom-color: #f78166; }
.file-tab .tab-badge {
  display: inline-block; padding: 0 6px;
  border-radius: 10px; font-size: 11px; background: #d29922; color: #0d1117;
}
.file-tab .tab-badge.clean { background: #238636; color: #fff; }
.file-tab .tab-viewed {
  color: #484f58; cursor: pointer; font-size: 14px; margin-left: 4px;
  border: none; background: none; padding: 0; line-height: 1;
}
.file-tab .tab-viewed:hover { color: #8b949e; }
.file-tab.viewed { opacity: 0.5; }
.file-tab.viewed span:first-child { text-decoration: line-through; }
.file-tab.viewed .tab-viewed { color: #3fb950; }

/* Main area: sidebar + diff */
.main-area {
  display: grid; min-height: calc(100vh - 120px);
  transition: grid-template-columns 0.2s ease;
}
.main-area.sidebar-open { grid-template-columns: 250px 1fr; }
.main-area.sidebar-collapsed { grid-template-columns: 36px 1fr; }

/* File tree sidebar */
.file-tree {
  background: #0d1117; border-right: 1px solid #30363d;
  overflow-y: auto; overflow-x: hidden;
  display: flex; flex-direction: column;
}
.tree-header {
  display: flex; align-items: center; gap: 8px;
  padding: 8px 10px; background: #161b22; border-bottom: 1px solid #30363d;
  font-size: 12px; font-weight: 600; color: #8b949e; text-transform: uppercase;
  letter-spacing: 0.5px; position: sticky; top: 0;
}
.tree-header .tree-toggle {
  width: 20px; height: 20px; border: none; background: transparent;
  color: #8b949e; cursor: pointer; font-size: 14px; padding: 0;
  display: flex; align-items: center; justify-content: center;
  border-radius: 4px; flex-shrink: 0;
}
.tree-header .tree-toggle:hover { background: #30363d; color: #e6edf3; }
.tree-label { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sidebar-collapsed .tree-label { display: none; }
.sidebar-collapsed .tree-body { display: none; }
.tree-body { flex: 1; padding: 4px 0; }

.tree-dir {
  user-select: none;
}
.tree-dir-label {
  display: flex; align-items: center; gap: 4px;
  padding: 3px 10px; cursor: pointer; color: #8b949e;
  font-size: 13px; font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, monospace;
}
.tree-dir-label:hover { color: #e6edf3; background: #161b22; }
.tree-dir-label .chevron {
  font-size: 10px; width: 14px; text-align: center;
  transition: transform 0.15s;
}
.tree-dir.collapsed .chevron { transform: rotate(-90deg); }
.tree-dir.collapsed .tree-dir-children { display: none; }
.tree-dir-children { padding-left: 12px; }

.tree-file {
  display: flex; align-items: center; gap: 4px;
  padding: 3px 10px 3px 14px; cursor: pointer; color: #e6edf3;
  font-size: 13px; font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, monospace;
  border-left: 2px solid transparent;
}
.tree-file:hover { background: #161b22; }
.tree-file.active { border-left-color: #f78166; background: #161b22; }
.tree-file.viewed { opacity: 0.4; }
.tree-file.viewed .tree-file-name { text-decoration: line-through; }
.tree-file-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tree-file .tree-badge {
  font-size: 10px; padding: 0 5px; border-radius: 8px;
  background: #d29922; color: #0d1117; flex-shrink: 0;
}
.tree-file .tree-badge.clean { background: #238636; color: #fff; }
.tree-file .tree-viewed-btn {
  width: 18px; height: 18px; border: none; background: transparent;
  color: #484f58; cursor: pointer; font-size: 13px; padding: 0;
  display: flex; align-items: center; justify-content: center;
  border-radius: 3px; flex-shrink: 0;
}
.tree-file .tree-viewed-btn:hover { background: #30363d; color: #e6edf3; }
.tree-file.viewed .tree-viewed-btn { color: #3fb950; }

/* Diff container */
.diff-container {
  display: grid; margin: 0;
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
  min-height: 22px; cursor: pointer; align-items: start;
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
  font-size: 13px; white-space: pre-wrap; word-break: break-word;
  line-height: 22px; tab-size: 4;
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
.comment-actions .btn-delete { color: #f85149; border-color: #f8514966; background: transparent; }
.comment-actions .btn-delete:hover { background: #da36332e; }
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
.edit-textarea {
  width: 100%; min-height: 40px; background: #0d1117; color: #e6edf3;
  border: 1px solid #58a6ff; border-radius: 6px; padding: 6px 10px;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  font-size: 13px; resize: vertical; outline: none;
}
.edit-textarea:focus { box-shadow: 0 0 0 2px #58a6ff33; }
.form-actions { display: flex; gap: 8px; margin-top: 8px; justify-content: flex-end; }
.form-hint { color: #484f58; font-size: 11px; margin-top: 4px; }
</style>
</head>
<body>

<header id="header">
  <div class="title">redliner diff</div>
  <div class="stats" id="stats"></div>
  <div class="actions" id="header-actions"></div>
</header>

<div class="storage-bar" id="storage-bar" title="Review comments are stored here">
  <span class="storage-label">Storage</span>
  <span class="storage-path" id="storage-path"></span>
  <button class="storage-copy" id="storage-copy" onclick="copyStorage()">Copy</button>
</div>

<div class="file-tabs" id="file-tabs"></div>

<div class="main-area sidebar-open" id="main-area">
  <div class="file-tree" id="file-tree">
    <div class="tree-header">
      <button class="tree-toggle" id="sidebar-toggle" title="Toggle sidebar">&lsaquo;</button>
      <span class="tree-label">Files</span>
    </div>
    <div class="tree-body" id="tree-body"></div>
  </div>
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
</div>

<script>
let state = null;
let activeFormLine = null;
let activeFormSide = null;
let leftCollapsed = false;
let rightCollapsed = false;
let sidebarCollapsed = false;
const viewedFiles = new Set();

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

  // Storage path
  document.getElementById('storage-path').textContent = state.storage || '';

  // File tabs
  const tabsEl = document.getElementById('file-tabs');
  tabsEl.innerHTML = '';
  for (const f of state.files) {
    const isViewed = viewedFiles.has(f.path);
    const tab = document.createElement('div');
    tab.className = 'file-tab' + (f.path === state.active_file ? ' active' : '') + (isViewed ? ' viewed' : '');
    const badgeClass = f.review.pending > 0 ? '' : ' clean';
    const badgeText = f.review.pending > 0 ? f.review.pending : '\\u2713';
    const nameSpan = `<span>${escapeHtml(f.path)}</span>`;
    const badge = `<span class="tab-badge${badgeClass}">${badgeText}</span>`;
    const title = isViewed ? 'Unmark viewed' : 'Mark viewed';
    const viewBtn = `<button class="tab-viewed" title="${title}" onclick="event.stopPropagation(); toggleViewed('${f.path}')">\\u2713</button>`;
    tab.innerHTML = nameSpan + badge + viewBtn;
    tab.addEventListener('click', () => selectFile(f.path));
    tabsEl.appendChild(tab);
  }

  // File tree
  renderTree();

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

  let savedFormLine = activeFormLine;

  for (const line of file.lines) {
    // Old side
    if (line.kind === 'added') {
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
    delete commentsByLine[commentLine];

    for (const c of lineComments) {
      const block = document.createElement('div');
      block.className = `comment-block ${c.status}`;
      block.setAttribute('data-comment-id', c.id);
      const actions = c.status === 'pending'
        ? `<div class="comment-actions"><button onclick="startEdit(${c.id})">Edit</button><button onclick="resolveComment(${c.id})">Resolve</button><button class="btn-delete" onclick="deleteComment(${c.id})">Delete</button></div>`
        : `<div class="comment-actions"><span class="resolved-tag">Resolved</span><button class="btn-delete" onclick="deleteComment(${c.id})">Delete</button></div>`;
      block.innerHTML =
        `<span class="comment-meta">#${c.id}</span>` +
        `<span class="comment-text">${escapeHtml(c.text)}</span>` +
        actions;
      newBody.appendChild(block);
      const spacer = document.createElement('div');
      spacer.style.height = '0';
      oldBody.appendChild(spacer);
    }

    // Comment form
    if (savedFormLine === (line.new_num || line.old_num) && !stats.allApproved) {
      newBody.appendChild(createCommentForm(savedFormLine));
      const spacer = document.createElement('div');
      spacer.style.height = '0';
      oldBody.appendChild(spacer);
      savedFormLine = null;
    }
  }
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
  activeFormLine = null;
  activeFormSide = null;
  await fetch('/api/comment', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ line: lineNum, text }),
  });
  await fetchDiff();
}

async function resolveComment(id) {
  await fetch(`/api/resolve/${id}`, { method: 'POST' });
  await fetchDiff();
}

async function deleteComment(id) {
  await fetch(`/api/delete/${id}`, { method: 'POST' });
  await fetchDiff();
}

function startEdit(id) {
  const block = document.querySelector(`[data-comment-id="${id}"]`);
  if (!block) return;
  const textEl = block.querySelector('.comment-text');
  const actionsEl = block.querySelector('.comment-actions');
  const original = textEl.textContent;
  textEl.innerHTML = `<textarea class="edit-textarea">${escapeHtml(original)}</textarea>`;
  const ta = textEl.querySelector('textarea');
  ta.focus();
  ta.selectionStart = ta.value.length;
  ta.addEventListener('keydown', e => {
    if (e.key === 'Enter' && e.ctrlKey) { e.preventDefault(); saveEdit(id); }
    if (e.key === 'Escape') { cancelEdit(id); }
  });
  actionsEl.innerHTML =
    `<button onclick="saveEdit(${id})">Save</button>` +
    `<button class="btn-delete" onclick="cancelEdit(${id})">Cancel</button>`;
}

async function saveEdit(id) {
  const block = document.querySelector(`[data-comment-id="${id}"]`);
  if (!block) return;
  const ta = block.querySelector('.edit-textarea');
  const text = ta.value.trim();
  if (!text) return;
  await fetch(`/api/edit/${id}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text }),
  });
  await fetchDiff();
}

function cancelEdit(id) {
  render();
}

async function resolveAll() {
  await fetch('/api/resolve-all', { method: 'POST' });
  await fetchDiff();
}

async function approveReview() {
  const btn = document.querySelector('.btn-approve');
  if (btn) { btn.disabled = true; btn.textContent = 'Approving...'; }
  const res = await fetch('/api/approve', { method: 'POST' });
  if (res.ok) await fetchDiff();
  else if (btn) { btn.disabled = false; btn.textContent = 'Approve'; }
}

async function quit() {
  await fetch('/api/quit', { method: 'POST' });
  document.body.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100vh;color:#8b949e;font-size:16px;">Review closed. You can close this tab.</div>';
}

__STORAGE_BAR_JS__

// File tree
function buildTree(files) {
  const root = {};
  for (const f of files) {
    const parts = f.path.split('/');
    let node = root;
    for (let i = 0; i < parts.length - 1; i++) {
      if (!node[parts[i]]) node[parts[i]] = {};
      node = node[parts[i]];
    }
    node[parts[parts.length - 1]] = f;
  }
  return root;
}

function renderTree() {
  if (!state) return;
  const body = document.getElementById('tree-body');
  body.innerHTML = '';
  const tree = buildTree(state.files);
  renderTreeNode(body, tree, 0);
}

function renderTreeNode(container, node, depth) {
  const dirs = [];
  const files = [];
  for (const [name, val] of Object.entries(node)) {
    if (val && val.path !== undefined) files.push({ name, data: val });
    else dirs.push({ name, children: val });
  }
  dirs.sort((a, b) => a.name.localeCompare(b.name));
  files.sort((a, b) => a.name.localeCompare(b.name));

  for (const dir of dirs) {
    const dirEl = document.createElement('div');
    dirEl.className = 'tree-dir';
    const label = document.createElement('div');
    label.className = 'tree-dir-label';
    label.style.paddingLeft = (10 + depth * 12) + 'px';
    label.innerHTML = `<span class="chevron">&#9660;</span> ${escapeHtml(dir.name)}/`;
    label.addEventListener('click', () => dirEl.classList.toggle('collapsed'));
    dirEl.appendChild(label);
    const children = document.createElement('div');
    children.className = 'tree-dir-children';
    renderTreeNode(children, dir.children, depth + 1);
    dirEl.appendChild(children);
    container.appendChild(dirEl);
  }

  for (const file of files) {
    const f = file.data;
    const el = document.createElement('div');
    const isViewed = viewedFiles.has(f.path);
    const isActive = f.path === state.active_file;
    el.className = 'tree-file' + (isActive ? ' active' : '') + (isViewed ? ' viewed' : '');
    el.style.paddingLeft = (14 + depth * 12) + 'px';
    const badgeClass = f.review.pending > 0 ? '' : ' clean';
    const badgeText = f.review.pending > 0 ? f.review.pending : '\\u2713';
    el.innerHTML =
      `<span class="tree-file-name">${escapeHtml(file.name)}</span>` +
      `<span class="tree-badge${badgeClass}">${badgeText}</span>` +
      `<button class="tree-viewed-btn" title="${isViewed ? 'Unmark viewed' : 'Mark viewed'}">${isViewed ? '\\u2713' : '\\u25CB'}</button>`;
    el.querySelector('.tree-file-name').addEventListener('click', () => selectFile(f.path));
    el.querySelector('.tree-viewed-btn').addEventListener('click', (e) => {
      e.stopPropagation();
      toggleViewed(f.path);
    });
    container.appendChild(el);
  }
}

function toggleViewed(path) {
  if (viewedFiles.has(path)) {
    viewedFiles.delete(path);
  } else {
    viewedFiles.add(path);
    // Auto-advance if marking the active file as viewed
    if (state && path === state.active_file) {
      const next = state.files.find(f => !viewedFiles.has(f.path) && f.path !== path);
      if (next) {
        selectFile(next.path);
        return;
      }
    }
  }
  render();
}

// Sidebar toggle
document.getElementById('sidebar-toggle').addEventListener('click', () => {
  sidebarCollapsed = !sidebarCollapsed;
  const main = document.getElementById('main-area');
  main.className = 'main-area ' + (sidebarCollapsed ? 'sidebar-collapsed' : 'sidebar-open');
});

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

HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>redliner</title>
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
header.approved {
  background: #0a2e1a;
  border-bottom-color: #238636;
}

.title {
  font-size: 16px;
  font-weight: 600;
  font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, monospace;
  flex-shrink: 0;
}

.saved-indicator {
  font-size: 12px;
  color: #8b949e;
  flex-shrink: 0;
  font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, monospace;
  transition: color 0.4s ease;
}
.saved-indicator.flash { color: #3fb950; }

.stats {
  display: flex;
  gap: 8px;
  align-items: center;
  flex: 1;
}

.badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 12px;
  font-size: 12px;
  font-weight: 500;
}
.badge.pending-badge {
  background: #2d1600;
  color: #d29922;
  border: 1px solid #d29922;
}
.badge.resolved-badge {
  background: #0a2e1a;
  color: #3fb950;
  border: 1px solid #238636;
}
.badge.approved-badge {
  background: #238636;
  color: #fff;
  border: 1px solid #2ea043;
}

.actions {
  display: flex;
  gap: 8px;
  flex-shrink: 0;
}

.mode-toggle {
  display: flex;
  border: 1px solid #30363d;
  border-radius: 6px;
  overflow: hidden;
  flex-shrink: 0;
}
.mode-toggle button {
  padding: 5px 14px;
  border: none;
  border-radius: 0;
  background: transparent;
  color: #8b949e;
  font-size: 13px;
  cursor: pointer;
}
.mode-toggle button.active {
  background: #21262d;
  color: #e6edf3;
}
.mode-toggle button:hover:not(.active) { background: #161b22; color: #e6edf3; }

.version-picker { display: flex; align-items: center; gap: 8px; position: relative; }
.version-btn {
  background: #21262d; color: #e6edf3; border: 1px solid #30363d;
  padding: 4px 10px; border-radius: 4px; font: inherit; cursor: pointer;
}
.version-btn:hover { background: #30363d; }
.version-menu {
  position: absolute; top: 100%; left: 0; margin-top: 4px;
  background: #161b22; border: 1px solid #30363d; border-radius: 6px;
  min-width: 320px; max-height: 400px; overflow: auto;
  box-shadow: 0 8px 24px rgba(0,0,0,0.4); z-index: 10;
}
.version-menu.hidden { display: none; }
.version-menu .item {
  padding: 8px 12px; cursor: pointer; display: flex; gap: 12px; align-items: center;
  border-bottom: 1px solid #21262d;
}
.version-menu .item:hover { background: #21262d; }
.version-menu .item .num { font-weight: 600; min-width: 90px; }
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

.line-row.historical { cursor: default; }
.diff-notice {
  background: #21262d; padding: 6px 10px; margin-bottom: 12px;
  border-left: 3px solid #d29922; color: #d29922;
}
.diff-text {
  white-space: pre; background: #0d1117; padding: 12px; border-radius: 4px;
  overflow: auto; margin: 0;
}
.diff-comments { margin-top: 12px; }
.diff-comment-anchor { margin-top: 8px; }
.diff-comment-line {
  display: inline-block; color: #8b949e; font-size: 0.85em;
  margin-right: 8px;
}

button {
  padding: 5px 16px;
  border-radius: 6px;
  border: 1px solid #30363d;
  background: #21262d;
  color: #e6edf3;
  font-size: 13px;
  cursor: pointer;
  font-weight: 500;
  transition: background 0.15s;
}
button:hover { background: #30363d; }

button.btn-approve {
  background: #238636;
  border-color: #2ea043;
}
button.btn-approve:hover { background: #2ea043; }
button.btn-approve:disabled {
  opacity: 0.4;
  cursor: not-allowed;
  background: #238636;
}

button.btn-danger {
  color: #f85149;
  border-color: #f8514966;
}
button.btn-danger:hover { background: #da36332e; }

button.btn-submit {
  background: #238636;
  border-color: #2ea043;
}
button.btn-submit:hover { background: #2ea043; }

button.btn-cancel {
  background: transparent;
  border-color: #30363d;
}

__STORAGE_BAR_CSS__

main {
  margin: 0 auto;
  padding: 16px 24px;
}

.file-card {
  border: 1px solid #30363d;
  border-radius: 6px;
  overflow: hidden;
  margin: 0;
}

.line-row {
  display: grid;
  grid-template-columns: 60px 1fr;
  border-bottom: 1px solid transparent;
  cursor: pointer;
  min-height: 22px;
  align-items: start;
}
.line-row:hover {
  background: #1c2128;
}
.line-row:hover .line-num {
  color: #8b949e;
}

.line-num {
  color: #484f58;
  text-align: right;
  padding: 0 12px 0 0;
  font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, monospace;
  font-size: 13px;
  user-select: none;
  line-height: 22px;
  border-right: 1px solid #21262d;
}

.line-text {
  padding: 0 12px;
  font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, monospace;
  font-size: 13px;
  white-space: pre-wrap;
  word-break: break-word;
  line-height: 22px;
  tab-size: 4;
}

.comment-block {
  margin-left: 60px;
  border-left: 3px solid;
  padding: 8px 16px;
  display: flex;
  align-items: flex-start;
  gap: 10px;
  font-size: 13px;
  border-bottom: 1px solid #21262d;
}
.comment-block.pending {
  border-left-color: #d29922;
  background: #2d160044;
}
.comment-block.resolved {
  border-left-color: #238636;
  background: #0a2e1a44;
}

.comment-meta {
  color: #8b949e;
  font-size: 12px;
  white-space: nowrap;
  flex-shrink: 0;
}

.comment-text {
  flex: 1;
  word-break: break-word;
}

.comment-actions {
  flex-shrink: 0;
}
.comment-actions button {
  padding: 2px 10px;
  font-size: 12px;
}
.comment-actions .btn-delete { color: #f85149; border-color: #f8514966; background: transparent; }
.comment-actions .btn-delete:hover { background: #da36332e; }

.resolved-tag {
  color: #3fb950;
  font-size: 12px;
  font-weight: 500;
}

.comment-form {
  margin-left: 60px;
  padding: 10px 16px;
  background: #161b22;
  border-bottom: 1px solid #30363d;
  border-left: 3px solid #58a6ff;
}
.comment-form textarea {
  width: 100%;
  min-height: 60px;
  background: #0d1117;
  color: #e6edf3;
  border: 1px solid #30363d;
  border-radius: 6px;
  padding: 8px 12px;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  font-size: 13px;
  resize: vertical;
  outline: none;
}
.comment-form textarea:focus {
  border-color: #58a6ff;
  box-shadow: 0 0 0 2px #58a6ff33;
}
.edit-textarea {
  width: 100%; min-height: 40px; background: #0d1117; color: #e6edf3;
  border: 1px solid #58a6ff; border-radius: 6px; padding: 6px 10px;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  font-size: 13px; resize: vertical; outline: none;
}
.edit-textarea:focus { box-shadow: 0 0 0 2px #58a6ff33; }

.edit-banner {
  margin: 0 0 12px 0;
  padding: 10px 14px;
  background: #1c2c4a;
  border: 1px solid #2f5fb2;
  border-radius: 6px;
  color: #c9d8f3;
  font-size: 13px;
  font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, monospace;
}

.form-actions {
  display: flex;
  gap: 8px;
  margin-top: 8px;
  justify-content: flex-end;
}
.form-hint {
  color: #484f58;
  font-size: 11px;
  margin-top: 4px;
}

.empty-state {
  text-align: center;
  padding: 48px 24px;
  color: #8b949e;
}

.edit-area {
  width: 100%;
  min-height: calc(100vh - 220px);
  background: #0d1117;
  color: #e6edf3;
  border: 1px solid #30363d;
  border-radius: 6px;
  padding: 12px 16px;
  font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, monospace;
  font-size: 13px;
  line-height: 1.6;
  resize: vertical;
  outline: none;
  tab-size: 4;
  white-space: pre;
}
.edit-area:focus { border-color: #58a6ff; box-shadow: 0 0 0 2px #58a6ff33; }
</style>
</head>
<body>

<header id="header">
  <div class="title" id="filename"></div>
  <span class="saved-indicator" id="saved-indicator"></span>
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
  <div class="mode-toggle" id="mode-toggle">
    <button id="mode-comment" onclick="setMode('comment')">Comment</button>
    <button id="mode-edit" onclick="setMode('edit')">Edit</button>
  </div>
  <div class="actions" id="header-actions"></div>
</header>

<div class="storage-bar" id="storage-bar" title="Review comments are stored here">
  <span class="storage-label">Storage</span>
  <span class="storage-path" id="storage-path"></span>
  <button class="storage-copy" id="storage-copy" onclick="copyStorage()">Copy</button>
</div>

<main>
  <div class="file-card" id="file-content"></div>
</main>

<script>
let state = null;
let activeFormLine = null;
let mode = 'comment';            // 'comment' | 'edit'
let editBuffer = null;           // textarea contents while in Edit mode
let selectedVersion = null;     // null = "current"; integer = past version
let availableVersions = [];
let currentView = 'diff';

async function fetchReview() {
  const res = await fetch('/api/review');
  state = await res.json();
  render();
  await loadVersions();
}

async function loadVersions() {
  if (!state || !state.file_path) return;
  const url = '/api/versions?file=' + encodeURIComponent(state.file_path);
  const resp = await fetch(url);
  const data = await resp.json();
  availableVersions = data.versions || [];
  renderVersionMenu();
  updateVersionButton();
}

function renderVersionMenu() {
  const menu = document.getElementById('version-menu');
  if (!menu) return;
  const head = availableVersions.length ? Math.max(...availableVersions.map(v => v.version)) : 0;
  const headEntry = availableVersions.find(v => v.version === head) || { pending: 0, resolved: 0 };
  const items = [];
  items.push(itemHTML({
    kind: 'current',
    label: 'current',
    ts: '',
    pending: headEntry.pending,
    resolved: headEntry.resolved,
  }));
  // Versions newest-first.
  const sorted = [...availableVersions].sort((a, b) => b.version - a.version);
  for (const v of sorted) {
    const label = v.version === 0 ? 'v0 baseline' : 'v' + v.version;
    items.push(itemHTML({
      kind: 'past',
      version: v.version,
      label,
      ts: v.created,
      pending: v.pending,
      resolved: v.resolved,
    }));
  }
  menu.innerHTML = items.join('');
}

function itemHTML(o) {
  const onclick = o.kind === 'current'
    ? "selectVersion(null)"
    : "selectVersion(" + o.version + ")";
  const ts = o.ts ? new Date(o.ts).toLocaleString() : '';
  return '<div class="item" onclick="' + onclick + '">' +
    '<span class="num">' + o.label + '</span>' +
    '<span class="ts">' + ts + '</span>' +
    '<span class="counts">' + o.pending + ' pending, ' + o.resolved + ' resolved</span>' +
    '</div>';
}

function updateVersionButton() {
  const btn = document.getElementById('version-btn');
  if (!btn) return;
  btn.textContent = (selectedVersion === null ? 'current' : 'v' + selectedVersion) + ' ▾';
}

function toggleVersionMenu() {
  const menu = document.getElementById('version-menu');
  if (menu) menu.classList.toggle('hidden');
}

async function selectVersion(n) {
  selectedVersion = n;
  document.getElementById('version-menu').classList.add('hidden');
  updateVersionButton();
  if (n === null) {
    document.getElementById('view-toggle').classList.add('hidden');
    document.getElementById('snapshot-btn').classList.remove('hidden');
    await fetchReview();
  } else {
    document.getElementById('view-toggle').classList.remove('hidden');
    document.getElementById('snapshot-btn').classList.add('hidden');
    await renderHistoricalVersion();
  }
}

async function renderHistoricalVersion() {
  const filePath = state && state.file_path ? state.file_path : '';
  const view = (selectedVersion === 0) ? 'full' : currentView;
  const url = '/api/version?file=' + encodeURIComponent(filePath) +
              '&n=' + selectedVersion + '&view=' + view;
  const resp = await fetch(url);
  if (!resp.ok) {
    alert('Failed to load v' + selectedVersion + ': ' + resp.status);
    return;
  }
  const data = await resp.json();
  if (view === 'diff') {
    renderHistoricalDiff(data.diff, data.comments);
  } else {
    renderHistoricalFull(data.content, data.comments);
  }
  updateStatsForHistorical(data.comments);
}

function renderHistoricalFull(content, comments) {
  const container = document.getElementById('file-content');
  container.innerHTML = '';
  const lines = content.split('\\n');
  // Drop trailing empty entry from terminating newline so line numbers match.
  if (lines.length && lines[lines.length - 1] === '') lines.pop();
  const byLine = {};
  for (const c of comments) {
    (byLine[c.line] ||= []).push(c);
  }
  lines.forEach((text, idx) => {
    const lineNum = idx + 1;
    const row = document.createElement('div');
    row.className = 'line-row historical';
    row.innerHTML =
      '<span class="line-num">' + lineNum + '</span>' +
      '<span class="line-text">' + escapeHtml(text) + '</span>';
    container.appendChild(row);
    (byLine[lineNum] || []).forEach(c => container.appendChild(renderCommentBlock(c)));
  });
}

function renderHistoricalDiff(diffText, comments) {
  const container = document.getElementById('file-content');
  container.innerHTML = '';

  // Compute which "new" line numbers are present in the diff so we know
  // which comments fall inside hunks vs. outside.
  const visible = new Set();
  let newLine = 0;
  for (const raw of diffText.split('\\n')) {
    if (raw.startsWith('@@')) {
      const m = raw.match(/\\+(\\d+)/);
      if (m) newLine = parseInt(m[1]) - 1;
    } else if (raw.startsWith('+++ ') || raw.startsWith('--- ')) {
      // header
    } else if (raw.startsWith('+')) {
      newLine += 1;
      visible.add(newLine);
    } else if (raw.startsWith(' ')) {
      newLine += 1;
      visible.add(newLine);
    } else if (raw.startsWith('-')) {
      // removed line — does not advance newLine
    }
  }

  const visibleComments = comments.filter(c => visible.has(c.line));
  const hidden = comments.length - visibleComments.length;
  if (hidden > 0) {
    const notice = document.createElement('div');
    notice.className = 'diff-notice';
    notice.textContent = '(' + hidden + ' comment' + (hidden === 1 ? '' : 's') + ' hidden — switch to Full)';
    container.appendChild(notice);
  }

  const pre = document.createElement('pre');
  pre.className = 'diff-text';
  pre.textContent = diffText;
  container.appendChild(pre);

  if (visibleComments.length) {
    const wrap = document.createElement('div');
    wrap.className = 'diff-comments';
    for (const c of visibleComments) {
      const anchor = document.createElement('div');
      anchor.className = 'diff-comment-anchor';
      const label = document.createElement('span');
      label.className = 'diff-comment-line';
      label.textContent = 'Line ' + c.line + ':';
      anchor.appendChild(label);
      anchor.appendChild(renderCommentBlock(c));
      wrap.appendChild(anchor);
    }
    container.appendChild(wrap);
  }
}

function updateStatsForHistorical(comments) {
  const stats = document.getElementById('stats');
  if (!stats) return;
  const pending = comments.filter(c => c.status === 'pending').length;
  const resolved = comments.filter(c => c.status === 'resolved').length;
  stats.innerHTML =
    '<span class="badge pending-badge">' + pending + ' pending</span>' +
    '<span class="badge resolved-badge">' + resolved + ' resolved</span>';
}

document.addEventListener('click', (e) => {
  const picker = document.querySelector('.version-picker');
  if (picker && !picker.contains(e.target)) {
    const menu = document.getElementById('version-menu');
    if (menu) menu.classList.add('hidden');
  }
});

// Stub for Task 15 — defined here so the onclick="takeSnapshot()" doesn't error in this task.
async function takeSnapshot() {
  console.log('snapshot button clicked (wired in Task 15)');
}

function setView(view) {
  currentView = view;
  document.getElementById('toggle-diff').classList.toggle('active', view === 'diff');
  document.getElementById('toggle-full').classList.toggle('active', view === 'full');
  if (selectedVersion !== null) {
    renderHistoricalVersion();
  }
}

function escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

function renderCommentBlock(c) {
  const block = document.createElement('div');
  block.className = 'comment-block ' + c.status;
  block.setAttribute('data-comment-id', c.id);
  const actions = c.status === 'pending'
    ? '<div class="comment-actions">' +
      '<button onclick="startEdit(' + c.id + ')">Edit</button>' +
      '<button onclick="resolveComment(' + c.id + ')">Resolve</button>' +
      '<button class="btn-delete" onclick="deleteComment(' + c.id + ')">Delete</button>' +
      '</div>'
    : '<div class="comment-actions">' +
      '<span class="resolved-tag">Resolved</span>' +
      '<button class="btn-delete" onclick="deleteComment(' + c.id + ')">Delete</button>' +
      '</div>';
  block.innerHTML =
    '<span class="comment-meta">#' + c.id + '</span>' +
    '<span class="comment-text">' + escapeHtml(c.text) + '</span>' +
    actions;
  return block;
}

function setMode(next) {
  if (mode === next) return;
  if (mode === 'edit' && hasUnsavedEdits()) {
    if (!confirm('Discard unsaved edits?')) return;
  }
  mode = next;
  editBuffer = null;
  render();
}

function hasUnsavedEdits() {
  if (mode !== 'edit') return false;
  const ta = document.getElementById('edit-area');
  if (!ta) return false;
  const baseline = state.edit ? state.edit.content : (state.raw || '');
  return ta.value !== baseline;
}

function render() {
  if (!state) return;
  const { filename, lines, review } = state;

  document.title = filename;
  document.getElementById('filename').textContent = filename;
  document.getElementById('storage-path').textContent = state.storage || '';
  document.getElementById('saved-indicator').textContent =
    state.edit ? `Saved ${formatSavedTime(state.edit.saved)}` : '';

  const hdr = document.getElementById('header');
  hdr.className = review.status === 'approved' ? 'approved' : '';

  // Mode toggle button states (hide entirely once approved)
  const toggle = document.getElementById('mode-toggle');
  toggle.style.display = review.status === 'approved' ? 'none' : 'flex';
  document.getElementById('mode-comment').classList.toggle('active', mode === 'comment');
  document.getElementById('mode-edit').classList.toggle('active', mode === 'edit');

  // Stats
  const statsEl = document.getElementById('stats');
  if (review.status === 'approved') {
    statsEl.innerHTML = '<span class="badge approved-badge">Approved</span>';
  } else if (mode === 'edit') {
    statsEl.innerHTML = state.edit
      ? `<span class="badge resolved-badge">Edited</span>`
      : `<span class="badge pending-badge">Editing</span>`;
  } else {
    statsEl.innerHTML =
      `<span class="badge pending-badge">${review.pending} pending</span>` +
      `<span class="badge resolved-badge">${review.resolved} resolved</span>`;
  }

  // Header actions depend on mode
  const actionsEl = document.getElementById('header-actions');
  if (review.status === 'approved') {
    actionsEl.innerHTML = '<button class="btn-danger" onclick="quit()">Close</button>';
  } else if (mode === 'edit') {
    const revertDisabled = state.edit ? '' : 'disabled';
    actionsEl.innerHTML =
      `<button class="btn-submit" onclick="saveEditContent()">Save</button>` +
      `<button onclick="revertEdit()" ${revertDisabled}>Revert to original</button>`;
  } else {
    const hasPending = review.pending > 0;
    actionsEl.innerHTML =
      `<button onclick="resolveAll()" ${review.pending === 0 ? 'disabled' : ''}>Resolve All</button>` +
      `<button class="btn-approve" onclick="approveReview()" ${hasPending ? 'disabled' : ''}>Approve</button>`;
  }

  const container = document.getElementById('file-content');
  container.innerHTML = '';

  if (mode === 'edit') {
    renderEditMode(container);
    return;
  }

  renderCommentMode(container, lines, review);
}

function renderEditMode(container) {
  const baseline = state.edit ? state.edit.content : (state.raw || '');
  const value = editBuffer !== null ? editBuffer : baseline;
  const ta = document.createElement('textarea');
  ta.id = 'edit-area';
  ta.className = 'edit-area';
  ta.value = value;
  ta.addEventListener('input', () => { editBuffer = ta.value; });
  container.appendChild(ta);
  // Focus on first render of edit mode
  setTimeout(() => ta.focus(), 0);
}

function renderCommentMode(container, lines, review) {
  // When edits exist, show the edited version as the source of truth
  const displayLines = state.edit ? splitLines(state.edit.content) : lines;

  // Banner explaining what the user is looking at
  if (state.edit) {
    const stats = countDiffLines(state.edit.diff);
    const banner = document.createElement('div');
    banner.className = 'edit-banner';
    banner.textContent =
      `Showing your edited version — +${stats.added} / -${stats.removed} from original. ` +
      `Comments anchor to lines as displayed.`;
    container.appendChild(banner);
  }

  if (displayLines.length === 0) {
    const empty = document.createElement('div');
    empty.className = 'empty-state';
    empty.textContent = 'No file content';
    container.appendChild(empty);
    return;
  }

  const commentsByLine = {};
  review.comments.forEach(c => {
    (commentsByLine[c.line] ||= []).push(c);
  });

  displayLines.forEach((text, i) => {
    const lineNum = i + 1;
    const row = document.createElement('div');
    row.className = 'line-row';
    row.innerHTML =
      `<span class="line-num">${lineNum}</span>` +
      `<span class="line-text">${escapeHtml(text)}</span>`;
    if (review.status !== 'approved') {
      row.addEventListener('click', () => showCommentForm(lineNum));
    }
    container.appendChild(row);

    (commentsByLine[lineNum] || []).forEach(c => {
      container.appendChild(renderCommentBlock(c));
    });

    if (activeFormLine === lineNum && review.status !== 'approved') {
      container.appendChild(createCommentForm(lineNum));
    }
  });
}

function splitLines(s) {
  if (!s) return [];
  const out = s.split('\\n');
  if (out.length && out[out.length - 1] === '') out.pop();
  return out;
}

function formatSavedTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return '';
  const hh = String(d.getHours()).padStart(2, '0');
  const mm = String(d.getMinutes()).padStart(2, '0');
  return `${hh}:${mm}`;
}

function flashSavedIndicator() {
  const el = document.getElementById('saved-indicator');
  if (!el) return;
  el.classList.add('flash');
  setTimeout(() => el.classList.remove('flash'), 1200);
}

function countDiffLines(diff) {
  if (!diff) return { added: 0, removed: 0 };
  let added = 0, removed = 0;
  for (const line of diff.split('\\n')) {
    if (line.startsWith('+++ ') || line.startsWith('--- ')) continue;
    if (line.startsWith('+')) added++;
    else if (line.startsWith('-')) removed++;
  }
  return { added, removed };
}

function showCommentForm(lineNum) {
  if (state.review.status === 'approved') return;
  activeFormLine = activeFormLine === lineNum ? null : lineNum;
  render();
  if (activeFormLine !== null) {
    const ta = document.querySelector('.comment-form textarea');
    if (ta) ta.focus();
  }
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
  // Stop click from toggling the form off
  form.addEventListener('click', e => e.stopPropagation());
  return form;
}

function cancelForm() {
  activeFormLine = null;
  render();
}

async function submitComment(lineNum) {
  const ta = document.getElementById('comment-input');
  const text = ta ? ta.value.trim() : '';
  if (!text) return;
  activeFormLine = null;
  const res = await fetch('/api/comment', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ line: lineNum, text }),
  });
  state.review = await res.json();
  await fetchReview();
}

async function resolveComment(id) {
  const res = await fetch(`/api/resolve/${id}`, { method: 'POST' });
  state.review = await res.json();
  await fetchReview();
}

async function deleteComment(id) {
  const res = await fetch(`/api/delete/${id}`, { method: 'POST' });
  state.review = await res.json();
  await fetchReview();
}

function startEdit(id) {
  const block = document.querySelector(`[data-comment-id="${id}"]`);
  if (!block) return;
  const textEl = block.querySelector('.comment-text');
  const actionsEl = block.querySelector('.comment-actions');
  const original = textEl.textContent;
  textEl.innerHTML = `<textarea class="edit-textarea">${escapeHtml(original)}</textarea>`;
  const ta = textEl.querySelector('textarea');
  ta.focus();
  ta.selectionStart = ta.value.length;
  ta.addEventListener('keydown', e => {
    if (e.key === 'Enter' && e.ctrlKey) { e.preventDefault(); saveEdit(id); }
    if (e.key === 'Escape') { cancelEdit(id); }
  });
  actionsEl.innerHTML =
    `<button onclick="saveEdit(${id})">Save</button>` +
    `<button class="btn-delete" onclick="cancelEdit(${id})">Cancel</button>`;
}

async function saveEdit(id) {
  const block = document.querySelector(`[data-comment-id="${id}"]`);
  if (!block) return;
  const ta = block.querySelector('.edit-textarea');
  const text = ta.value.trim();
  if (!text) return;
  const res = await fetch(`/api/edit/${id}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text }),
  });
  state.review = await res.json();
  await fetchReview();
}

function cancelEdit(id) {
  render();
}

async function saveEditContent() {
  const ta = document.getElementById('edit-area');
  if (!ta) return;
  const content = ta.value;
  const res = await fetch('/api/edit-content', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content }),
  });
  if (!res.ok) {
    alert(`Save failed: ${res.status} ${await res.text()}`);
    return;
  }
  editBuffer = null;
  await fetchReview();
  flashSavedIndicator();
}

async function revertEdit() {
  if (!confirm('Discard saved edits and revert to the original file?')) return;
  const res = await fetch('/api/clear-edit', { method: 'POST' });
  if (!res.ok) {
    alert(`Revert failed: ${res.status} ${await res.text()}`);
    return;
  }
  editBuffer = null;
  await fetchReview();
}

async function resolveAll() {
  const res = await fetch('/api/resolve-all', { method: 'POST' });
  state.review = await res.json();
  await fetchReview();
}

async function approveReview() {
  const btn = document.querySelector('.btn-approve');
  if (btn) { btn.disabled = true; btn.textContent = 'Approving...'; }
  const res = await fetch('/api/approve', { method: 'POST' });
  if (res.ok) {
    state.review = await res.json();
    await fetchReview();
  } else if (btn) {
    btn.disabled = false;
    btn.textContent = 'Approve';
  }
}

async function quit() {
  await fetch('/api/quit', { method: 'POST' });
  document.body.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100vh;color:#8b949e;font-size:16px;">Review closed. You can close this tab.</div>';
}

__STORAGE_BAR_JS__

// Ctrl+Enter to submit
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

window.addEventListener('beforeunload', e => {
  if (hasUnsavedEdits()) {
    e.preventDefault();
    e.returnValue = '';
  }
});

fetchReview();
</script>
</body>
</html>
"""


# Inject shared storage-bar CSS/JS so both templates stay in sync.
DIFF_HTML_TEMPLATE = DIFF_HTML_TEMPLATE.replace("__STORAGE_BAR_CSS__", STORAGE_BAR_CSS).replace(
    "__STORAGE_BAR_JS__", STORAGE_BAR_JS,
)
HTML_TEMPLATE = HTML_TEMPLATE.replace("__STORAGE_BAR_CSS__", STORAGE_BAR_CSS).replace(
    "__STORAGE_BAR_JS__", STORAGE_BAR_JS,
)


def run_web(plan_file: Path) -> dict:
    """Start a local web server for interactive review and block until done."""
    server = ReviewServer(("127.0.0.1", 0), ReviewHandler)
    server.plan_file = plan_file
    server.session = plan_file
    server.timeout = 0.5

    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}"
    print(f"redliner: {url}", file=sys.stderr)

    webbrowser.open(url)

    try:
        while not server.done:
            server.handle_request()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

    review = load_review(plan_file)
    key = str(plan_file.resolve())
    return {
        "status": review.status_for(key),
        "pending": len(review.pending_for(key)),
        "resolved": len(review.resolved_for(key)),
    }


def run_diff_web(file_diffs: list[FileDiff]) -> dict:
    """Start a local web server for interactive diff review and block until done."""
    repo_root = Path.cwd()
    server = ReviewServer(("127.0.0.1", 0), ReviewHandler)
    server.mode = "diff"
    server.diff_data = file_diffs
    server.active_file = file_diffs[0].path if file_diffs else ""
    server.repo_root = repo_root
    server.session = repo_root
    server.done = False
    server.timeout = 0.5

    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}"
    print(f"redliner diff: {url}", file=sys.stderr)

    webbrowser.open(url)

    try:
        while not server.done:
            server.handle_request()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

    review = load_review(repo_root)
    total_pending = 0
    total_resolved = 0
    all_approved = True
    for fd in file_diffs:
        key = str((repo_root / fd.path).resolve())
        total_pending += len(review.pending_for(key))
        total_resolved += len(review.resolved_for(key))
        if review.status_for(key) != "approved":
            all_approved = False

    return {
        "status": "approved" if all_approved else "in_review",
        "files": len(file_diffs),
        "pending": total_pending,
        "resolved": total_resolved,
    }
