"""Data model and sidecar I/O for plan reviews."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class Comment:
    id: int
    line: int
    text: str
    status: str = "pending"  # "pending" | "resolved"
    created: str = ""

    def __post_init__(self) -> None:
        if not self.created:
            self.created = datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Review:
    status: str = "in_review"  # "in_review" | "approved"
    comments: list[Comment] = field(default_factory=list)
    approved_at: str | None = None

    @property
    def pending(self) -> list[Comment]:
        return [c for c in self.comments if c.status == "pending"]

    @property
    def resolved(self) -> list[Comment]:
        return [c for c in self.comments if c.status == "resolved"]

    def next_id(self) -> int:
        if not self.comments:
            return 1
        return max(c.id for c in self.comments) + 1

    def add_comment(self, line: int, text: str) -> Comment:
        comment = Comment(id=self.next_id(), line=line, text=text)
        self.comments.append(comment)
        self.status = "in_review"
        return comment

    def resolve(self, comment_id: int) -> Comment | None:
        for c in self.comments:
            if c.id == comment_id:
                c.status = "resolved"
                return c
        return None

    def delete(self, comment_id: int) -> Comment | None:
        for i, c in enumerate(self.comments):
            if c.id == comment_id:
                return self.comments.pop(i)
        return None

    def resolve_all(self) -> int:
        count = 0
        for c in self.comments:
            if c.status == "pending":
                c.status = "resolved"
                count += 1
        return count

    def approve(self) -> bool:
        if self.pending:
            return False
        self.status = "approved"
        self.approved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return True


def sidecar_path(plan_file: Path) -> Path:
    return plan_file.parent / f"{plan_file.name}.review.json"


def load_review(plan_file: Path) -> Review:
    path = sidecar_path(plan_file)
    if not path.exists():
        return Review()
    data = json.loads(path.read_text())
    comments = [Comment(**c) for c in data.get("comments", [])]
    return Review(
        status=data.get("status", "in_review"),
        comments=comments,
        approved_at=data.get("approved_at"),
    )


def save_review(plan_file: Path, review: Review) -> None:
    path = sidecar_path(plan_file)
    data: dict = {"status": review.status, "comments": [asdict(c) for c in review.comments]}
    if review.approved_at:
        data["approved_at"] = review.approved_at
    path.write_text(json.dumps(data, indent=2) + "\n")
