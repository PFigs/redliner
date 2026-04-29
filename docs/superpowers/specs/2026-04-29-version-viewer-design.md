# Version Viewer for Review Rounds

## Context

When an agent revises a plan based on review comments, the human reviewer wants to see what changed since the last round without re-reading the whole document. Today, redliner stores at most one `Edit` per file (latest overwrites previous), so there is no history to compare against. This spec adds a versioning model that lets reviewers snapshot the document at meaningful checkpoints ("rounds") and browse past versions through a dropdown in the header. Selecting a past version shows the diff vs. the previous version by default, with a toggle to view the full document at that point in time. Comments are pinned to the version they were authored on.

## Scope

- Plan review only (`redliner open <file>`). Diff-mode versioning is out of scope.
- Snapshots are explicit: created by user action (UI button or CLI command). Auto-save is unchanged.
- Versions are numbered monotonically: v0 (baseline), v1, v2, ... No labels.
- Diff display is incremental only: v_n shows changes vs. v_{n-1}. Cumulative-vs-baseline is not provided.
- Comments are version-pinned at creation. Status (pending/resolved) remains mutable on any version.
- Past versions are read-only for content; existing comments on past versions can still be resolved/unresolved/deleted.

## Data model changes (`review.py`)

### New dataclass

```python
@dataclass
class Version:
    file: str
    version: int      # 0, 1, 2, ...
    content: str
    created: str      # ISO timestamp; "" auto-fills in __post_init__
```

v0 is the immutable baseline. It is materialized on first read of a session if `versions.jsonl` does not yet exist for the file. Its `content` is the file's on-disk content at session-start time (the same string the existing `set_edit` calls `original`).

### `Comment` gains a `version` field

```python
@dataclass
class Comment:
    id: int
    file: str
    line: int
    text: str
    status: str = "pending"
    created: str = ""
    version: int = 0     # version the comment was pinned to at creation
```

Default of 0 keeps existing comment files loadable. The API caller passes the version the user is viewing; the server stamps it on the comment.

### `Review` gains a `versions` map and methods

```python
versions: dict[str, list[Version]] = field(default_factory=dict)

def head_version(self, file: str) -> int: ...
def snapshot(self, file: str, content: str) -> Version: ...
def version_content(self, file: str, n: int) -> str: ...
def version_diff(self, file: str, n: int) -> str: ...
def comments_for_version(self, file: str, n: int) -> list[Comment]: ...
```

- `head_version` — highest sealed version number (0 if only baseline exists).
- `snapshot` — appends new `Version` with `version = head + 1`. Returns it. Always succeeds, even if `content` matches head (no-op snapshots are allowed; users may want to mark a checkpoint after only commenting).
- `version_content` — returns content at version n. Raises if n does not exist.
- `version_diff` — unified diff of v_n vs v_{n-1}. Computed on read using `difflib.unified_diff`. v0 has no previous; callers should not request its diff (API surfaces full content for v0 instead).
- `comments_for_version` — filters `comments` by `(file, version)`.

### `Edit` (live draft) is unchanged

Still one per file, still in `edits.jsonl`. Conceptually it is the working copy on top of the head version. `set_edit(file, content, original)` continues to take `original` from the caller; the caller passes head-version content rather than baseline-only content (web.py adjusts to fetch head content via `Review.version_content(file, head_version(file))`).

## Storage layout

Under existing `session_dir(<plan>)`:

- `comments.jsonl` — gains `version` field per row. Rows missing the field on load default to 0.
- `meta.json` — unchanged.
- `edits.jsonl` — unchanged. Holds the live draft per file.
- **new** `versions.jsonl` — append-only, one `Version` per row.

`load_review` and `save_review` extend symmetrically. On load, if `versions.jsonl` is missing, synthesize v0 from the file's current on-disk content. If the file is no longer readable on disk, fall back to the existing edit's content as v0; if neither exists, surface a clear error rather than creating an empty v0.

## API changes (`web.py`)

### New endpoints

- **`GET /api/versions?file=<path>`**
  Returns `{"versions": [{"version": 0, "created": "...", "pending": 3, "resolved": 1}, ...]}`. Sorted ascending by version number. Comment counts are per-version.

- **`GET /api/version?file=<path>&n=<int>&view=diff|full`**
  Returns `{"content": "...", "diff": "...", "comments": [...]}`.
  - `view=diff` returns the unified diff of v_n vs v_{n-1}. For v0, server returns `view=full` automatically (no previous to diff against).
  - `view=full` returns the full document content at v_n.
  - `comments` contains only comments pinned to v_n.

- **`POST /api/snapshot`** body `{"file": "<path>"}`
  Reads the current live draft content (or head-version content if no draft exists), creates a new sealed `Version` with `version = head + 1`, persists, and returns the new `Version`. The server serializes snapshot writes per session so concurrent requests from two tabs cannot produce duplicate version numbers.

### Modified endpoints

- **`POST /api/comments`** accepts an optional `version: int` field in the body, the version the user was viewing when authoring. If omitted, server uses the head version. Server validates the version exists for the file; otherwise returns 400.
- Resolve, unresolve, edit, and delete endpoints are unchanged. Status mutation works identically across all versions.
- Approval endpoint is unchanged. Approval is per file; pending comments on any version (including v0) still block approval.

## CLI changes (`cli.py`)

### New commands

```
redliner snapshot <file>                      # create new sealed version
redliner versions <file>                      # list versions with timestamps + counts
redliner show <file> --version N              # show document at version N
redliner show <file> --version N --diff       # show diff of vN vs v(N-1)
```

`redliner snapshot` prints the new version number and timestamp.
`redliner versions` prints a table: version, created, pending count, resolved count.
`redliner show --version N` reuses existing rendering with line numbers; `--diff` switches to diff rendering.

### Unchanged commands

- `redliner comment <file> <line> <text>` continues to pin to the head version. There is no CLI flag to comment on past versions; that is a viewer activity, and past-version line numbers do not necessarily map to head.
- `redliner list`, `redliner resolve`, `redliner approve`, `redliner status` are unchanged. `list` shows comments across all versions with their version stamp.

## UI changes (`web.py` template)

### Header dropdown

Placement: in the existing `<header>` element, between the filename `<div class="title">` and the stats `<div class="stats">` containing the pending pill.

Visual: a button-style dropdown labelled with the currently selected version (e.g. `current ▾` or `v2 ▾`). Click opens a menu listing entries newest-first plus "current" at the top:

```
current     (3 pending, 0 resolved)
v2          2026-04-29 14:30   (1 pending, 2 resolved)
v1          2026-04-29 11:05   (0 pending, 4 resolved)
v0 baseline                    (0 pending, 0 resolved)
```

Comment counts come from `GET /api/versions`. The pending pill in `stats` reflects the **currently selected** version, so users always know which scope they are looking at.

### Behavior when a non-current version is selected

- Default rendering: unified diff of v_n vs v_{n-1}, using the existing diff styling from `redliner diff`. v0 has no previous, so its default rendering is the full document.
- "Diff / Full" toggle next to the dropdown switches between the two views for any selected version.
- Comments pinned to v_n render inline. In Full view, every v_n comment is visible at its anchored line. In Diff view, only comments anchored to lines that appear in a hunk render inline; if any pinned comments fall outside the displayed hunks, a small "(N comments hidden — switch to Full)" affordance appears at the top of the diff. Resolve, unresolve, edit, and delete buttons remain functional. New comments are disabled.
- The Comment/Edit mode toggle is disabled when a non-current version is selected. You cannot edit history.
- When "current" is reselected, all current behaviors return.

### Snapshot button

A "Snapshot" button appears next to the dropdown when "current" is selected. Single click → `POST /api/snapshot` → dropdown refreshes → new "v_N" entry is added → selection stays on "current" so the user can keep editing.

## Migration / backwards compatibility

- Sessions without `versions.jsonl`: on load, synthesize v0 from the file's current on-disk content. Existing edits load as the live draft on top of v0; no version is auto-created from existing edits because we cannot know when those edits were made or whether they represent a meaningful checkpoint.
- Comments without a `version` field default to 0 so they appear under the baseline in the dropdown rather than vanishing.

## Edge cases

- **File deleted/moved on disk** when synthesizing v0: fall back to the existing edit's content as v0. If neither exists, surface a clear error and do not create an empty v0.
- **No-op snapshot** (current draft equals head content): create the version anyway. Users may want to mark a round even when only commentary changed.
- **Concurrent snapshots** from two browser tabs: server serializes snapshot writes per session; version numbers stay monotonic.

- **Comments authored on "current" while a draft is active:** they pin to the head version and their line numbers match the rendered (head + draft) view. When that head version is later viewed standalone in History mode, the line anchors may not align perfectly with raw head content. Users who want clean anchoring should snapshot before commenting on heavy drafts. This is rare and not worth special handling.
- **Approving while viewing v_n:** Approve always operates on the whole file (current behavior). The dropdown selection does not narrow approval scope.
- **Pre-snapshot comments:** comments authored before any explicit snapshot are pinned to v0 (the baseline they were viewed against).

## Testing

### Unit tests (`tests/redliner/`)

- `snapshot` creates monotonically numbered versions.
- `version_diff` between adjacent versions matches a known-expected unified diff.
- `comments_for_version` filters correctly.
- A comment created via `add_comment` with `version=N` persists with that version through save/load.
- Loading a session with no `versions.jsonl` synthesizes v0 from disk.
- Loading comments without a `version` field defaults to 0.

### API tests

- `/api/versions` returns the expected list with per-version comment counts.
- `/api/version?view=diff` returns the diff; `view=full` returns the full content.
- `/api/version?n=0&view=diff` returns the full document (no previous).
- `/api/snapshot` creates a new version visible in subsequent `/api/versions` calls.
- `POST /api/comments` with a `version` body field stamps that version on the persisted comment.
- `POST /api/comments` with a non-existent `version` returns 400.

### Manual UI verification

- Dropdown opens, lists versions newest-first with counts.
- Selecting a past version renders the diff; toggle switches to full document.
- Edit mode toggle is disabled when viewing a past version; new-comment input is disabled.
- Resolve/unresolve on past-version comments persists and updates counts in the dropdown.
- Snapshot button creates a new version and the dropdown refreshes without a page reload.
