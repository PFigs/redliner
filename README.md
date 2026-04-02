# compose-review

A PR-like review tool for plan files. Add comments on specific lines, resolve them, and approve plans through a CLI or interactive web UI.

## Install

```
pip install compose-review
```

## Usage

```
compose-review show plan.md
compose-review comment plan.md 10 "This needs more detail"
compose-review list plan.md
compose-review resolve plan.md 1
compose-review approve plan.md
compose-review open plan.md   # launches web UI
```
