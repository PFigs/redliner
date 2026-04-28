# Edit Mode Design

## Goal

Let a reviewer edit a plan directly in the web UI instead of (or alongside) leaving line comments. The reviewer's edited version is persisted to the session storage so the agent can read it back the same way it reads `comments.jsonl`.

## Scope

- Plan view only (`redliner open <file>`). Diff view is unchanged.
- Whole-document editing via a single textarea. No per-line inline editing.
- One latest-edit-wins record per file. No history, no undo beyond the browser textarea.
- Edits do not modify the source file on disk. They live in session storage and the agent applies them.

## Architecture

### Data model (`review.py`)

A new dataclass mirroring the shape of `Comment`:

```python
@dataclass
class Edit:
    file: str
    content: str          # full edited text
    diff: str             # unified diff vs the file content at save time
    saved: str = ""       # ISO timestamp, set in __post_init__ if blank
```

Extend `Review`:

```python
@dataclass
class Review:
    comments: list[Comment] = field(default_factory=list)
    files: dict[str, FileState] = field(default_factory=dict)
    edits: dict[str, Edit] = field(default_factory=dict)   # file path -> Edit

    def set_edit(self, file: str, content: str, original: str) -> Edit: ...
    def get_edit(self, file: str) -> Edit | None: ...
    def clear_edit(self, file: str) -> Edit | None: ...
```

`set_edit` computes the unified diff using `difflib.unified_diff(original, content)` and stores both. The original is passed in by the caller (the server reads it from disk at save time).

### Storage layout

A new file in the session directory alongside the existing artifacts:

```
<session-dir>/
  comments.jsonl
  meta.json
  edits.jsonl       # NEW
```

`edits.jsonl` has one JSON object per line, one row per edited file:

```json
{"file": "/abs/path/plan.md", "content": "<full edited text>", "diff": "<unified diff>", "saved": "2026-04-29T..."}
```

If the user re-saves an edit for the same file, the row is overwritten (rewrite the whole jsonl). If the user clears the edit, the row is removed; if no rows remain, the file is deleted.

`load_review`/`save_review` are extended to round-trip `edits` the same way they handle `comments`.

### Server API (`web.py`)

Three additions:

| Method | Path | Body | Purpose |
| --- | --- | --- | --- |
| `POST` | `/api/edit-content` | `{"content": "..."}` | Save edit for the active file |
| `POST` | `/api/clear-edit` | _(none)_ | Remove edit for the active file |
| `GET` | `/api/review` | _(unchanged)_ | Response now includes `edit` |

The `GET /api/review` payload gains:

```json
{
  "filename": "...",
  "file_path": "...",
  "storage": "...",
  "lines": [...],
  "review": {...},
  "edit": {"content": "...", "diff": "...", "saved": "..."}  // or null
}
```

`POST /api/edit-content`:
1. Read body, validate `content` is a string (empty allowed — represents wiping the plan).
2. Load review.
3. Read current file content from disk (this is the "original" baseline for the diff).
4. `review.set_edit(file_key, content, original)`.
5. Save review.
6. Return the same shape as `GET /api/review`.

`POST /api/clear-edit`:
1. Load review, `review.clear_edit(file_key)`.
2. Save review.
3. Return the same shape as `GET /api/review`.

Edit mode is plan-only; these endpoints reject when `server.mode == "diff"` with HTTP 400.

### CLI (`cli.py`)

- `redliner status <file>` JSON output gains two keys when an edit exists:
  - `"has_edits": true`
  - `"edits_path": "<session-dir>/edits.jsonl"`
- New subcommand `redliner edits <file>`: prints the unified diff for that file (the `diff` field of its Edit row) to stdout. Exits 0 if an edit exists, 1 otherwise.
- No changes to `approve` — approving requires comments resolved as before; edits are independent.

### UI (plan template only)

**Header.** A two-button segmented toggle is added between `.stats` and `.actions`:

```
[Comment] [Edit]
```

The toggle is hidden in the diff template. State lives in a JS variable `mode` (default `"comment"`). Switching mode re-renders.

**Comment mode (default).** Identical to current behavior, with one addition: if `state.edit` is non-null, a banner appears above the file content:

> Plan edited — +A / -R lines in saved version.

Where A is the count of `+` lines and R the count of `-` lines in the unified diff body (excluding the `---`/`+++` file header lines). The banner is informational only; clicking does nothing in v1.

**Edit mode.** The file content area is replaced with a single full-height `<textarea>` containing:
- `state.edit.content` if it exists
- Otherwise the joined `state.lines` (the original file content)

Header actions in Edit mode become:

```
[Save] [Revert to original] [Switch to Comment]
```

- **Save** — POSTs current textarea value to `/api/edit-content`. On success, refetches and stays in Edit mode.
- **Revert to original** — POSTs to `/api/clear-edit` and resets the textarea to the original file content. Confirmation prompt if the textarea has unsaved changes.
- **Switch to Comment** — flips `mode` back to `"comment"`. If there are unsaved changes (textarea differs from `state.edit.content` or, if no edit, from original), confirm before discarding.

**Unload guard.** A `beforeunload` handler warns if the user closes/refreshes the tab while in Edit mode with unsaved changes.

**Comments in Edit mode.** Hidden — they are line-anchored and would no longer line up against edited content. Switching back to Comment mode shows them again against the original lines.

### Agent workflow

1. Agent produces `plan.md`.
2. User runs `redliner open plan.md`, switches to Edit mode, edits, saves.
3. `edits.jsonl` is written in the session directory.
4. Agent reads `edits.jsonl` (path discoverable via `redliner status` or the storage bar in the UI) and either:
   - applies the unified diff (`diff` field) via `patch`/`git apply`, or
   - overwrites the file with `content`.
5. Comments and edits are independent — both may be present.

## What is intentionally not in v1

- Diff-view editing
- Per-line inline editing
- Conflict detection if the source file changes between save and agent read
- Edit history / multiple revisions
- Auto-applying edits to the source file on approve
- Visual diff view (old vs new) inside the UI — only the textarea and a banner

## Testing

- Unit: `Review.set_edit` / `get_edit` / `clear_edit`; `load_review` / `save_review` round-trip with and without edits; diff computation against a known baseline.
- Server: `POST /api/edit-content` happy path, empty content allowed, diff-mode rejection; `POST /api/clear-edit`; `GET /api/review` shape with and without `edit`.
- CLI: `redliner status` with and without edits; `redliner edits` for both cases.
- Manual UI: toggle, save, revert, switch-back-with-unsaved-changes confirmation, banner display in Comment mode after a save.
