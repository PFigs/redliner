"""CLI entry point for redliner."""

from __future__ import annotations

import argparse
import json
import sys
from importlib.metadata import version
from pathlib import Path

from redliner.review import load_review, save_review


def cmd_show(args: argparse.Namespace) -> None:
    plan_file = Path(args.file).resolve()
    if not plan_file.exists():
        print(f"File not found: {plan_file}", file=sys.stderr)
        sys.exit(1)

    review = load_review(plan_file)
    lines = plan_file.read_text().splitlines()
    comments_by_line: dict[int, list[str]] = {}
    for c in review.comments:
        tag = ">>>" if c.status == "pending" else "~~~"
        comments_by_line.setdefault(c.line, []).append(f"     {tag} [#{c.id}] {c.text}")

    for i, line in enumerate(lines, 1):
        print(f"{i:4d} | {line}")
        for comment_line in comments_by_line.get(i, []):
            print(comment_line)


def cmd_comment(args: argparse.Namespace) -> None:
    plan_file = Path(args.file).resolve()
    if not plan_file.exists():
        print(f"File not found: {plan_file}", file=sys.stderr)
        sys.exit(1)

    review = load_review(plan_file)
    line_count = len(plan_file.read_text().splitlines())
    if args.line < 1 or args.line > line_count:
        print(f"Line {args.line} out of range (1-{line_count})", file=sys.stderr)
        sys.exit(1)

    comment = review.add_comment(args.line, args.text)
    save_review(plan_file, review)
    print(f"Added comment #{comment.id} at line {args.line}")


def cmd_list(args: argparse.Namespace) -> None:
    plan_file = Path(args.file).resolve()
    review = load_review(plan_file)

    if not review.comments:
        print("No comments.")
        return

    lines = plan_file.read_text().splitlines() if plan_file.exists() else []

    for c in review.comments:
        status_tag = "pending" if c.status == "pending" else "resolved"
        print(f"[#{c.id}] Line {c.line} ({status_tag})")
        print(f'  "{c.text}"')
        if 0 < c.line <= len(lines):
            print(f"  Context: {lines[c.line - 1].strip()}")
        print()


def cmd_resolve(args: argparse.Namespace) -> None:
    plan_file = Path(args.file).resolve()
    review = load_review(plan_file)
    comment = review.resolve(args.id)
    if comment is None:
        print(f"Comment #{args.id} not found", file=sys.stderr)
        sys.exit(1)
    save_review(plan_file, review)
    print(f"Resolved comment #{args.id}")


def cmd_delete(args: argparse.Namespace) -> None:
    plan_file = Path(args.file).resolve()
    review = load_review(plan_file)
    comment = review.delete(args.id)
    if comment is None:
        print(f"Comment #{args.id} not found", file=sys.stderr)
        sys.exit(1)
    save_review(plan_file, review)
    print(f"Deleted comment #{args.id}")


def cmd_resolve_all(args: argparse.Namespace) -> None:
    plan_file = Path(args.file).resolve()
    review = load_review(plan_file)
    count = review.resolve_all()
    save_review(plan_file, review)
    print(f"Resolved {count} comment(s)")


def cmd_approve(args: argparse.Namespace) -> None:
    plan_file = Path(args.file).resolve()
    review = load_review(plan_file)
    if not review.approve():
        pending = len(review.pending)
        print(f"Cannot approve: {pending} unresolved comment(s)", file=sys.stderr)
        for c in review.pending:
            print(f"  [#{c.id}] Line {c.line}: {c.text}", file=sys.stderr)
        sys.exit(1)
    save_review(plan_file, review)
    print("Approved.")


def cmd_status(args: argparse.Namespace) -> None:
    plan_file = Path(args.file).resolve()
    review = load_review(plan_file)
    data: dict = {
        "status": review.status,
        "pending": len(review.pending),
        "resolved": len(review.resolved),
        "total": len(review.comments),
    }
    if review.approved_at:
        data["approved_at"] = review.approved_at
    print(json.dumps(data))


def cmd_open(args: argparse.Namespace) -> None:
    plan_file = Path(args.file).resolve()
    if not plan_file.exists():
        print(f"File not found: {plan_file}", file=sys.stderr)
        sys.exit(1)

    from redliner.web import run_web

    result = run_web(plan_file)
    print(json.dumps(result))
    sys.exit(0 if result["status"] == "approved" else 1)


def cmd_diff(args: argparse.Namespace) -> None:
    from redliner.diff import parse_git_diff

    file_diffs = parse_git_diff(path=args.path)

    if not file_diffs:
        print(json.dumps({"files": 0, "status": "no_changes"}))
        sys.exit(0)

    if args.no_open:
        print(json.dumps({"files": len(file_diffs), "status": "pending"}))
        sys.exit(0)

    from redliner.web import run_diff_web

    result = run_diff_web(file_diffs)
    print(json.dumps(result))
    sys.exit(0 if result["status"] == "approved" else 1)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="redliner",
        description="Review tool for plans and code diffs",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {version('redliner')}",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # show
    p = sub.add_parser("show", help="Display plan with line numbers and comments")
    p.add_argument("file", help="Path to plan file")
    p.set_defaults(func=cmd_show)

    # comment
    p = sub.add_parser("comment", help="Add a comment at a line")
    p.add_argument("file", help="Path to plan file")
    p.add_argument("line", type=int, help="Line number")
    p.add_argument("text", help="Comment text")
    p.set_defaults(func=cmd_comment)

    # list
    p = sub.add_parser("list", help="List all comments with context")
    p.add_argument("file", help="Path to plan file")
    p.set_defaults(func=cmd_list)

    # resolve
    p = sub.add_parser("resolve", help="Resolve a comment")
    p.add_argument("file", help="Path to plan file")
    p.add_argument("id", type=int, help="Comment ID")
    p.set_defaults(func=cmd_resolve)

    # delete
    p = sub.add_parser("delete", help="Delete a comment")
    p.add_argument("file", help="Path to plan file")
    p.add_argument("id", type=int, help="Comment ID")
    p.set_defaults(func=cmd_delete)

    # resolve-all
    p = sub.add_parser("resolve-all", help="Resolve all pending comments")
    p.add_argument("file", help="Path to plan file")
    p.set_defaults(func=cmd_resolve_all)

    # approve
    p = sub.add_parser("approve", help="Approve the plan")
    p.add_argument("file", help="Path to plan file")
    p.set_defaults(func=cmd_approve)

    # status
    p = sub.add_parser("status", help="Show review status")
    p.add_argument("file", help="Path to plan file")
    p.set_defaults(func=cmd_status)

    # open
    p = sub.add_parser("open", help="Open web review in browser")
    p.add_argument("file", help="Path to plan file")
    p.set_defaults(func=cmd_open)

    # diff
    p = sub.add_parser("diff", help="Review git diff in side-by-side web UI")
    p.add_argument("--path", default=None, help="Filter to a single file path")
    p.add_argument("--no-open", action="store_true", help="Print status without launching browser")
    p.set_defaults(func=cmd_diff)

    args = parser.parse_args()
    args.func(args)
