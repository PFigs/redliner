# redliner

A local review tool for AI agent work. Comment on plans and code diffs line-by-line, resolve feedback, and approve changes through a web UI or CLI.

The idea: an agent writes code, you redline it. Comments are stored as JSON files that the agent can read and act on.

## Install

```
pip install redliner
```

## Plan review

Review a plan file with inline comments:

```bash
redliner open plan.md        # web UI in your browser
redliner show plan.md        # print with line numbers and comments
redliner comment plan.md 10 "This needs more detail"
redliner list plan.md        # list all comments
redliner resolve plan.md 1   # resolve comment #1
redliner approve plan.md     # approve (fails if unresolved comments)
```

## Diff review

Review working tree changes side by side:

```bash
redliner diff                 # opens all changes in web UI
redliner diff --path src/foo.py   # single file
redliner diff --no-open       # just print status as JSON
```

The diff view shows old and new versions in collapsible columns. Click any line to leave a comment.

## How comments are stored

Comments are stored under `$XDG_DATA_HOME/redliner/reviews/` (defaults to `~/.local/share/redliner/reviews/`). Each reviewed file gets a JSON file keyed by a hash of its absolute path, keeping your repo clean.

```json
{
  "status": "in_review",
  "comments": [
    {"id": 1, "line": 42, "text": "Handle the None case", "status": "pending"}
  ]
}
```

Use `redliner status <file>` to check review state, or `redliner list <file>` to see all comments. Agents can read the JSON files directly from the data directory.

## Agent workflow

1. Agent writes code or a plan
2. You run `redliner diff` or `redliner open plan.md`
3. You leave comments on specific lines
4. Agent reads the review JSON files and addresses feedback
5. You review again until satisfied, then approve

## Requirements

Python 3.12+. No external dependencies.
