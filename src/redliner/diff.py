import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


@dataclass
class DiffLine:
    old_num: int | None
    new_num: int | None
    kind: Literal["context", "added", "removed"]
    text: str


@dataclass
class FileDiff:
    path: str
    lines: list[DiffLine] = field(default_factory=list)


_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
# Matches the old-file header: --- <prefix>/<path> or --- /dev/null
_OLD_FILE_HEADER = re.compile(r"^--- (\S+)")
# Matches the new-file header: +++ <prefix>/<path> or +++ /dev/null
_NEW_FILE_HEADER = re.compile(r"^\+\+\+ (\S+)")
# Strips any single-character prefix followed by slash (a/, b/, c/, w/, i/, o/ etc)
_PREFIX = re.compile(r"^./")


def _strip_prefix(path: str) -> str:
    return _PREFIX.sub("", path, count=1)


def parse_git_diff(path: str | None = None, cwd: Path | None = None) -> list[FileDiff]:
    cmd = ["git", "diff", "HEAD"]
    if path is not None:
        cmd += ["--", path]

    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.returncode not in (0, 1):
        raise subprocess.CalledProcessError(result.returncode, cmd, result.stdout, result.stderr)

    return _parse(result.stdout)


def _parse(output: str) -> list[FileDiff]:
    files: list[FileDiff] = []
    current: FileDiff | None = None
    old_num = 0
    new_num = 0
    pending_old_path: str | None = None

    for line in output.splitlines(keepends=True):
        old_match = _OLD_FILE_HEADER.match(line)
        if old_match:
            raw = old_match.group(1).rstrip("\n")
            pending_old_path = None if raw == "/dev/null" else _strip_prefix(raw)
            continue

        new_match = _NEW_FILE_HEADER.match(line)
        if new_match:
            raw = new_match.group(1).rstrip("\n")
            if raw == "/dev/null":
                # deleted file — path comes from old header
                file_path = pending_old_path or ""
            else:
                file_path = _strip_prefix(raw)
            current = FileDiff(path=file_path)
            files.append(current)
            continue

        if current is None:
            continue

        hunk_match = _HUNK_HEADER.match(line)
        if hunk_match:
            old_num = int(hunk_match.group(1))
            new_num = int(hunk_match.group(2))
            continue

        text = line[1:]
        if line.startswith("+"):
            current.lines.append(DiffLine(old_num=None, new_num=new_num, kind="added", text=text))
            new_num += 1
        elif line.startswith("-"):
            current.lines.append(DiffLine(old_num=old_num, new_num=None, kind="removed", text=text))
            old_num += 1
        elif line.startswith(" "):
            current.lines.append(
                DiffLine(old_num=old_num, new_num=new_num, kind="context", text=text),
            )
            old_num += 1
            new_num += 1

    return files
