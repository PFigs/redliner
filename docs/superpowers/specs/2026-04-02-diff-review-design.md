# Git Diff Review Feature

## Context

compose-review currently reviews plan files (static text documents). Users want to also review git diffs -- the actual code changes an agent has made -- using the same comment/resolve/approve workflow. The diff view should show old and new versions side by side with collapsible columns, and store comments in per-file sidecar JSON files that agents can read to address feedback.

## Scope

- New CLI subcommand: `compose-review diff`
- New module: `src/compose_review/diff.py` for parsing `git diff` output
- Extended `web.py` with a diff mode (second HTML template, shared server)
- Working tree diffs only (`git diff HEAD`)
- Per-file `.review.json` sidecar storage (reuses existing model)

## New module: `diff.py`

### Data model

```python
@dataclass
class DiffLine:
    old_num: int | None   # None for added lines
    new_num: int | None   # None for removed lines
    kind: str             # "context" | "added" | "removed"
    text: str

@dataclass
class FileDiff:
    path: str
    lines: list[DiffLine]
```

### Function: `parse_git_diff(path: str | None = None) -> list[FileDiff]`

- Runs `git diff HEAD` (or `git diff HEAD -- <path>` if path provided) via `subprocess.run`
- Parses unified diff output into `FileDiff` objects
- Each hunk's lines are tagged with their type and both-side line numbers
- Returns empty list if no changes

## CLI changes: `cli.py`

New subcommand:

```
compose-review diff [--path <file>]
```

- `--path`: optional, filter to a single file
- Calls `parse_git_diff()`, then launches the diff web UI
- Same blocking behavior as `open` -- exits when approved or quit
- Outputs review status JSON on exit

## Web changes: `web.py`

### Server

- `ReviewServer` gains:
  - `mode: str` -- `"plan"` or `"diff"`
  - `diff_data: list[FileDiff]` -- parsed diff (only in diff mode)
  - `active_file: str` -- currently selected file in diff mode

### New endpoints (diff mode)

- `GET /api/diff` -- returns:
  ```json
  {
    "files": [
      {
        "path": "src/foo.py",
        "lines": [{"old_num": 1, "new_num": 1, "kind": "context", "text": "..."}],
        "review": {"status": "...", "comments": [...], ...}
      }
    ],
    "active_file": "src/foo.py"
  }
  ```
- `POST /api/select-file` -- switches the active file tab
- Comment/resolve/approve endpoints work the same but operate on the active file's sidecar (resolved path = repo root / active_file)

### Route dispatch

`do_GET` and `do_POST` check `self.server.mode` to decide which endpoints and HTML to serve. Plan mode routes are unchanged.

### Diff HTML template: `DIFF_HTML_TEMPLATE`

**Layout:**
```
+--------------------------------------------------+
| Header: file tabs | stats | actions              |
+--------------------------------------------------+
| [v] Old                  | [v] New               |
|--------------------------|------------------------|
| 1  def foo():            | 1  def foo():          |
| 2- old_line              |                        |
|                          | 2+ new_line            |
| 3  unchanged             | 3  unchanged           |
+--------------------------------------------------+
```

**File tabs:**
- Horizontal tabs, one per changed file
- Active tab highlighted
- Clicking a tab calls `POST /api/select-file`

**Side-by-side columns:**
- CSS grid: two equal columns
- Column headers with file path fragment and collapse toggle (chevron button)
- Collapsing a column: sets its width to ~40px (just the toggle button visible), other column expands to fill
- Both expanded by default
- Lines aligned by hunk -- context lines appear on both sides, added lines only on right (left side shows empty row), removed lines only on left (right side shows empty row)

**Diff coloring (GitHub-style dark theme):**
- Removed lines: `background: #3d1214` (red tint)
- Added lines: `background: #1a2e1a` (green tint)
- Context lines: transparent
- Line numbers match existing monospace style

**Comments:**
- Click any line (either side) to open comment form
- Comments store `line` as the new-side line number for context/added lines
- For removed lines (no new-side equivalent), store `line` as the old-side line number and set `side: "old"` on the comment
- Same comment block rendering as plan review (pending=yellow border, resolved=green border)
- Comment form identical to plan review

**Approve flow:**
- Approve button in header, disabled while pending comments exist
- Approving sets all per-file reviews to approved
- Server shuts down on approve (same as plan review)

## Storage

Each changed file gets a sidecar: `<filepath>.review.json` relative to the repo root.

Example: changes in `src/foo.py` -> sidecar at `src/foo.py.review.json`

The agent reads these files to find feedback. The JSON format is identical to plan review:

```json
{
  "status": "in_review",
  "comments": [
    {"id": 1, "line": 42, "text": "This should handle None", "status": "pending", "created": "..."}
  ]
}
```

## Verification

1. Make some changes to files in a git repo
2. Run `compose-review diff` -- browser opens with side-by-side view
3. Add comments on specific lines
4. Verify `.review.json` sidecar files are created
5. Resolve comments, approve
6. Verify exit status and JSON output
7. Verify `compose-review diff --path <file>` filters correctly
8. Test collapsible columns toggle in both directions
9. Existing `compose-review open` plan review still works unchanged
