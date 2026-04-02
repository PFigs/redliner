"""Local web UI for interactive plan review."""

from __future__ import annotations

import json
import re
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from redliner.diff import FileDiff
from redliner.review import load_review, save_review


class ReviewServer(HTTPServer):
    plan_file: Path
    done: bool = False
    mode: str = "plan"  # "plan" | "diff"
    diff_data: list[FileDiff]
    active_file: str = ""
    repo_root: Path = Path(".")


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

    def _review_dict(self) -> dict:
        review = load_review(self.server.plan_file)
        return {
            "status": review.status,
            "pending": len(review.pending),
            "resolved": len(review.resolved),
            "comments": [
                {
                    "id": c.id,
                    "line": c.line,
                    "text": c.text,
                    "status": c.status,
                    "created": c.created,
                }
                for c in review.comments
            ],
            "approved_at": review.approved_at,
        }

    def _not_found(self) -> None:
        self.send_response(404)
        self.end_headers()

    def _active_plan_file(self) -> Path:
        """Return the file path that comment/resolve/approve should operate on."""
        if self.server.mode == "diff":
            return self.server.repo_root / self.server.active_file
        return self.server.plan_file

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
        lines = plan_file.read_text().splitlines() if plan_file.exists() else []
        self._json_response({
            "filename": plan_file.name,
            "lines": lines,
            "review": self._review_dict(),
        })

    def _add_comment(self) -> None:
        body = self._read_body()
        line = body.get("line")
        text = body.get("text", "").strip()
        if not isinstance(line, int) or not text:
            self._json_response({"error": "line (int) and text required"}, 400)
            return
        plan_file = self._active_plan_file()
        review = load_review(plan_file)
        review.add_comment(line, text)
        save_review(plan_file, review)
        self._json_response(self._review_dict())

    def _resolve(self, comment_id: int) -> None:
        plan_file = self._active_plan_file()
        review = load_review(plan_file)
        if review.resolve(comment_id) is None:
            self._json_response({"error": f"Comment #{comment_id} not found"}, 404)
            return
        save_review(plan_file, review)
        self._json_response(self._review_dict())

    def _delete_comment(self, comment_id: int) -> None:
        plan_file = self._active_plan_file()
        review = load_review(plan_file)
        if review.delete(comment_id) is None:
            self._json_response({"error": f"Comment #{comment_id} not found"}, 404)
            return
        save_review(plan_file, review)
        if self.server.mode == "diff":
            self._get_diff()
        else:
            self._json_response(self._review_dict())

    def _resolve_all(self) -> None:
        plan_file = self._active_plan_file()
        review = load_review(plan_file)
        review.resolve_all()
        save_review(plan_file, review)
        self._json_response(self._review_dict())

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
        reviews = []
        total_pending = 0
        for fd in self.server.diff_data:
            plan_file = self.server.repo_root / fd.path
            review = load_review(plan_file)
            total_pending += len(review.pending)
            reviews.append((plan_file, review))

        if total_pending > 0:
            self._json_response(
                {"error": f"Cannot approve: {total_pending} unresolved comment(s) across files"},
                409,
            )
            return

        for plan_file, review in reviews:
            review.approve()
            save_review(plan_file, review)

        self.server.done = True
        self._get_diff()

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

    def _quit(self) -> None:
        self.server.done = True
        self._json_response({"ok": True})


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
      const actions = c.status === 'pending'
        ? `<div class="comment-actions"><button onclick="resolveComment(${c.id})">Resolve</button><button class="btn-delete" onclick="deleteComment(${c.id})">Delete</button></div>`
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
  white-space: pre;
  overflow-x: auto;
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
</style>
</head>
<body>

<header id="header">
  <div class="title" id="filename"></div>
  <div class="stats" id="stats"></div>
  <div class="actions" id="header-actions"></div>
</header>

<main>
  <div class="file-card" id="file-content"></div>
</main>

<script>
let state = null;
let activeFormLine = null;

async function fetchReview() {
  const res = await fetch('/api/review');
  state = await res.json();
  render();
}

function escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

function render() {
  if (!state) return;
  const { filename, lines, review } = state;

  // Header
  const hdr = document.getElementById('header');
  hdr.className = review.status === 'approved' ? 'approved' : '';

  document.title = filename;
  document.getElementById('filename').textContent = filename;

  // Stats
  const statsEl = document.getElementById('stats');
  if (review.status === 'approved') {
    statsEl.innerHTML = '<span class="badge approved-badge">Approved</span>';
  } else {
    statsEl.innerHTML =
      `<span class="badge pending-badge">${review.pending} pending</span>` +
      `<span class="badge resolved-badge">${review.resolved} resolved</span>`;
  }

  // Actions
  const actionsEl = document.getElementById('header-actions');
  if (review.status === 'approved') {
    actionsEl.innerHTML = '<button class="btn-danger" onclick="quit()">Close</button>';
  } else {
    const hasPending = review.pending > 0;
    actionsEl.innerHTML =
      `<button onclick="resolveAll()" ${review.pending === 0 ? 'disabled' : ''}>Resolve All</button>` +
      `<button class="btn-approve" onclick="approveReview()" ${hasPending ? 'disabled' : ''}>Approve</button>`;
  }

  // File content
  const container = document.getElementById('file-content');
  container.innerHTML = '';

  if (lines.length === 0) {
    container.innerHTML = '<div class="empty-state">No file content</div>';
    return;
  }

  // Index comments by line
  const commentsByLine = {};
  review.comments.forEach(c => {
    (commentsByLine[c.line] ||= []).push(c);
  });

  lines.forEach((text, i) => {
    const lineNum = i + 1;

    // Line row
    const row = document.createElement('div');
    row.className = 'line-row';
    row.innerHTML =
      `<span class="line-num">${lineNum}</span>` +
      `<span class="line-text">${escapeHtml(text)}</span>`;
    if (review.status !== 'approved') {
      row.addEventListener('click', () => showCommentForm(lineNum));
    }
    container.appendChild(row);

    // Comments on this line
    (commentsByLine[lineNum] || []).forEach(c => {
      const block = document.createElement('div');
      block.className = `comment-block ${c.status}`;
      const actions = c.status === 'pending'
        ? `<div class="comment-actions"><button onclick="resolveComment(${c.id})">Resolve</button><button class="btn-delete" onclick="deleteComment(${c.id})">Delete</button></div>`
        : `<div class="comment-actions"><span class="resolved-tag">Resolved</span><button class="btn-delete" onclick="deleteComment(${c.id})">Delete</button></div>`;
      block.innerHTML =
        `<span class="comment-meta">#${c.id}</span>` +
        `<span class="comment-text">${escapeHtml(c.text)}</span>` +
        actions;
      container.appendChild(block);
    });

    // Show comment form if active on this line
    if (activeFormLine === lineNum && review.status !== 'approved') {
      container.appendChild(createCommentForm(lineNum));
    }
  });
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

async function resolveAll() {
  const res = await fetch('/api/resolve-all', { method: 'POST' });
  state.review = await res.json();
  await fetchReview();
}

async function approveReview() {
  const res = await fetch('/api/approve', { method: 'POST' });
  if (res.ok) {
    state.review = await res.json();
    await fetchReview();
  }
}

async function quit() {
  await fetch('/api/quit', { method: 'POST' });
  document.body.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100vh;color:#8b949e;font-size:16px;">Review closed. You can close this tab.</div>';
}

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

fetchReview();
</script>
</body>
</html>
"""


def run_web(plan_file: Path) -> dict:
    """Start a local web server for interactive review and block until done."""
    server = ReviewServer(("127.0.0.1", 0), ReviewHandler)
    server.plan_file = plan_file
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
    return {
        "status": review.status,
        "pending": len(review.pending),
        "resolved": len(review.resolved),
    }


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
    print(f"redliner diff: {url}", file=sys.stderr)

    webbrowser.open(url)

    try:
        while not server.done:
            server.handle_request()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

    total_pending = 0
    total_resolved = 0
    all_approved = True
    for fd in file_diffs:
        review = load_review(server.repo_root / fd.path)
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
