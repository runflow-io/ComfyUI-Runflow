from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import NamedTuple

_CRED_RE = re.compile(r"^(https?://)[^@/]+@")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class GitInfo(NamedTuple):
    origin: str | None
    commit: str | None
    dirty: bool | None


def _run(args: list[str], cwd: Path, timeout: float = 2.0) -> str | None:
    try:
        result = subprocess.run(
            args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _scrub_credentials(url: str | None) -> str | None:
    if url is None:
        return None
    return _CRED_RE.sub(r"\1", url)


def _resolve_gitdirs(path: Path) -> tuple[Path, Path] | None:
    """Resolve ``path/.git`` into ``(gitdir, commondir)``.

    A plain checkout has ``.git`` as a directory and ``commondir == gitdir``.
    Worktrees, submodules and ``git init --separate-git-dir`` use a ``.git``
    *file* containing ``gitdir: <path>``; worktrees additionally split state
    so that HEAD/per-worktree refs live in the per-worktree gitdir while
    ``config``, ``refs/heads/...`` and ``packed-refs`` live in the common
    dir referenced by ``gitdir/commondir``.
    """
    git_path = path / ".git"
    gitdir: Path | None = None
    if git_path.is_dir():
        gitdir = git_path
    elif git_path.is_file():
        try:
            content = git_path.read_text().strip()
        except OSError:
            return None
        if content.startswith("gitdir:"):
            target = Path(content.split(":", 1)[1].strip())
            if not target.is_absolute():
                target = (path / target).resolve()
            if target.is_dir():
                gitdir = target
    if gitdir is None:
        return None
    commondir = gitdir
    marker = gitdir / "commondir"
    if marker.is_file():
        try:
            rel = marker.read_text().strip()
        except OSError:
            rel = ""
        if rel:
            candidate = Path(rel)
            if not candidate.is_absolute():
                candidate = (gitdir / candidate).resolve()
            if candidate.is_dir():
                commondir = candidate
    return gitdir, commondir


def _read_origin_from_config(commondir: Path) -> str | None:
    """Parse ``commondir/config`` for the ``[remote "origin"]`` url."""
    try:
        text = (commondir / "config").read_text()
    except OSError:
        return None
    in_origin = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("["):
            in_origin = line.replace(" ", "") == '[remote"origin"]'
            continue
        if in_origin and "=" in line:
            key, _, value = line.partition("=")
            if key.strip() == "url":
                value = value.strip()
                return value or None
    return None


def _read_commit_from_refs(gitdir: Path, commondir: Path) -> str | None:
    """Resolve the commit at HEAD without invoking ``git``.

    Handles direct SHAs (detached HEAD), symbolic refs (loose refs under
    ``refs/heads/``), and the ``packed-refs`` fallback for repos whose loose
    refs have been packed. Per-worktree refs are looked up in ``gitdir``
    first, then in the shared ``commondir``.
    """
    try:
        head = (gitdir / "HEAD").read_text().strip()
    except OSError:
        return None
    if _SHA_RE.match(head):
        return head
    if not head.startswith("ref:"):
        return None
    ref = head.split(":", 1)[1].strip()
    for base in (gitdir, commondir):
        try:
            sha = (base / ref).read_text().strip()
        except OSError:
            continue
        if _SHA_RE.match(sha):
            return sha
    try:
        packed = (commondir / "packed-refs").read_text()
    except OSError:
        return None
    for raw_line in packed.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", "^")):
            continue
        parts = line.split(" ", 1)
        if len(parts) == 2 and parts[1] == ref and _SHA_RE.match(parts[0]):
            return parts[0]
    return None


def read_git(path: Path) -> GitInfo:
    """Return git metadata for `path`, or Nones if unavailable.

    Tries the ``git`` CLI first, then falls back to parsing ``.git/`` files
    directly for any field that came back empty — keeps origin/commit
    populated when the ``git`` binary is missing (common in slimmed-down
    container images) or rejects the repo (e.g. dubious-ownership).

    `dirty` is None when git state couldn't be read at all, True/False when it
    could — callers can distinguish 'unknown' from 'clean'. The file-based
    fallback can't determine working-tree state, so `dirty` stays None when
    the CLI is unavailable.
    """
    if not (path / ".git").exists():
        return GitInfo(None, None, None)
    commit = _run(["git", "rev-parse", "HEAD"], path)
    origin = _run(["git", "config", "--get", "remote.origin.url"], path)
    porcelain = _run(["git", "status", "--porcelain"], path)
    dirty = bool(porcelain) if porcelain is not None else None

    if commit is None or origin is None:
        dirs = _resolve_gitdirs(path)
        if dirs is not None:
            gitdir, commondir = dirs
            if commit is None:
                commit = _read_commit_from_refs(gitdir, commondir)
            if origin is None:
                origin = _read_origin_from_config(commondir)

    return GitInfo(_scrub_credentials(origin), commit, dirty)
