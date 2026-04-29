"""Data model and sidecar I/O for plan reviews."""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path


@dataclass
class Comment:
    id: int
    file: str
    line: int
    text: str
    status: str = "pending"  # "pending" | "resolved"
    created: str = ""
    version: int = 0

    def __post_init__(self) -> None:
        if not self.created:
            self.created = datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class Edit:
    file: str
    content: str
    diff: str
    saved: str = ""

    def __post_init__(self) -> None:
        if not self.saved:
            self.saved = datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class Version:
    file: str
    version: int
    content: str
    created: str = ""

    def __post_init__(self) -> None:
        if not self.created:
            self.created = datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class FileState:
    status: str = "in_review"  # "in_review" | "approved"
    approved_at: str | None = None


@dataclass
class Review:
    """A review session holding comments across one or more files."""

    comments: list[Comment] = field(default_factory=list)
    files: dict[str, FileState] = field(default_factory=dict)
    edits: dict[str, Edit] = field(default_factory=dict)
    versions: dict[str, list[Version]] = field(default_factory=dict)

    def _ensure_file(self, file: str) -> FileState:
        state = self.files.get(file)
        if state is None:
            state = FileState()
            self.files[file] = state
        return state

    def next_id(self) -> int:
        if not self.comments:
            return 1
        return max(c.id for c in self.comments) + 1

    def comments_for(self, file: str) -> list[Comment]:
        return [c for c in self.comments if c.file == file]

    def pending_for(self, file: str) -> list[Comment]:
        return [c for c in self.comments_for(file) if c.status == "pending"]

    def resolved_for(self, file: str) -> list[Comment]:
        return [c for c in self.comments_for(file) if c.status == "resolved"]

    def add_comment(self, file: str, line: int, text: str) -> Comment:
        comment = Comment(id=self.next_id(), file=file, line=line, text=text)
        self.comments.append(comment)
        self._ensure_file(file).status = "in_review"
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

    def edit(self, comment_id: int, text: str) -> Comment | None:
        for c in self.comments:
            if c.id == comment_id:
                c.text = text
                return c
        return None

    def set_edit(self, file: str, content: str, original: str) -> Edit:
        diff = "".join(
            difflib.unified_diff(
                original.splitlines(keepends=True),
                content.splitlines(keepends=True),
                fromfile=file,
                tofile=file,
            )
        )
        edit = Edit(file=file, content=content, diff=diff)
        self.edits[file] = edit
        return edit

    def get_edit(self, file: str) -> Edit | None:
        return self.edits.get(file)

    def clear_edit(self, file: str) -> Edit | None:
        return self.edits.pop(file, None)

    def resolve_all(self, file: str | None = None) -> int:
        count = 0
        for c in self.comments:
            if (file is None or c.file == file) and c.status == "pending":
                c.status = "resolved"
                count += 1
        return count

    def approve(self, file: str) -> bool:
        """Approve a file. Returns False if any pending comments remain on it."""
        if self.pending_for(file):
            return False
        state = self._ensure_file(file)
        state.status = "approved"
        state.approved_at = datetime.now(UTC).isoformat(timespec="seconds")
        return True

    def status_for(self, file: str) -> str:
        state = self.files.get(file)
        return state.status if state else "in_review"

    def approved_at_for(self, file: str) -> str | None:
        state = self.files.get(file)
        return state.approved_at if state else None


def _data_dir() -> Path:
    """Return the XDG data directory for redliner."""
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "redliner" / "reviews"


_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(name: str) -> str:
    slug = _SLUG_RE.sub("-", name).strip("-")
    return slug[:40] if slug else "review"


def session_dir(session: Path) -> Path:
    """Map a review-session path to its storage directory under XDG_DATA_HOME."""
    resolved = session.resolve()
    path_hash = hashlib.sha256(str(resolved).encode()).hexdigest()[:12]
    return _data_dir() / f"{path_hash}-{_slug(resolved.name)}"


def comments_path(session: Path) -> Path:
    return session_dir(session) / "comments.jsonl"


def meta_path(session: Path) -> Path:
    return session_dir(session) / "meta.json"


def edits_path(session: Path) -> Path:
    return session_dir(session) / "edits.jsonl"


def versions_path(session: Path) -> Path:
    return session_dir(session) / "versions.jsonl"


def load_review(session: Path) -> Review:
    review = Review()
    cpath = comments_path(session)
    if cpath.exists():
        for raw in cpath.read_text().splitlines():
            line = raw.strip()
            if not line:
                continue
            review.comments.append(Comment(**json.loads(line)))
    mpath = meta_path(session)
    if mpath.exists():
        meta = json.loads(mpath.read_text())
        for file_path, state_data in meta.get("files", {}).items():
            review.files[file_path] = FileState(**state_data)
    epath = edits_path(session)
    if epath.exists():
        for raw in epath.read_text().splitlines():
            line = raw.strip()
            if not line:
                continue
            e = Edit(**json.loads(line))
            review.edits[e.file] = e
    vpath = versions_path(session)
    if vpath.exists():
        for raw in vpath.read_text().splitlines():
            line = raw.strip()
            if not line:
                continue
            v = Version(**json.loads(line))
            review.versions.setdefault(v.file, []).append(v)
        for vlist in review.versions.values():
            vlist.sort(key=lambda v: v.version)
    else:
        # Synthesize v0 from the file on disk; fall back to existing edit content
        # if the file is no longer readable. If neither is available, leave versions
        # empty (callers see head_version() == 0 and version_content() raises).
        key = str(session.resolve())
        content: str | None = None
        try:
            if session.is_file():
                content = session.read_text()
        except OSError:
            content = None
        if content is None:
            existing = review.edits.get(key)
            if existing is not None:
                content = existing.content
        if content is not None:
            review.versions[key] = [Version(file=key, version=0, content=content)]
    return review


def save_review(session: Path, review: Review) -> None:
    d = session_dir(session)
    d.mkdir(parents=True, exist_ok=True)
    cpath = comments_path(session)
    lines = [json.dumps(asdict(c)) for c in review.comments]
    cpath.write_text(("\n".join(lines) + "\n") if lines else "")
    mpath = meta_path(session)
    if review.files:
        meta = {"files": {k: asdict(v) for k, v in review.files.items()}}
        mpath.write_text(json.dumps(meta, indent=2) + "\n")
    elif mpath.exists():
        mpath.unlink()
    epath = edits_path(session)
    if review.edits:
        edit_lines = [json.dumps(asdict(e)) for e in review.edits.values()]
        epath.write_text("\n".join(edit_lines) + "\n")
    elif epath.exists():
        epath.unlink()
    vpath = versions_path(session)
    if review.versions:
        version_lines: list[str] = []
        for vlist in review.versions.values():
            for v in vlist:
                version_lines.append(json.dumps(asdict(v)))
        vpath.write_text("\n".join(version_lines) + "\n")
    elif vpath.exists():
        vpath.unlink()
