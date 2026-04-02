# redliner

A local review tool for AI agent work. Comment on plans and code diffs line-by-line, resolve feedback, and approve changes through a web UI or CLI.

The idea: an agent writes code, you redline it. Comments are saved as JSON sidecar files that the agent can read and act on.

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

Comments live in `.review.json` sidecar files next to whatever you're reviewing:

```
plan.md           -> plan.md.review.json
src/foo.py        -> src/foo.py.review.json
```

```json
{
  "status": "in_review",
  "comments": [
    {"id": 1, "line": 42, "text": "Handle the None case", "status": "pending"}
  ]
}
```

Agents read these files, fix the issues, and you re-run the review.

## Agent workflow

1. Agent writes code or a plan
2. You run `redliner diff` or `redliner open plan.md`
3. You leave comments on specific lines
4. Agent reads the `.review.json` files and addresses feedback
5. You review again until satisfied, then approve

## Requirements

Python 3.12+. No external dependencies.
