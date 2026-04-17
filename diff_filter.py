"""Diff filtering — extension whitelist, per-file caps, aggregate budget enforcement."""

from __future__ import annotations

import logging
import os
from dataclasses import replace

from github_client import Commit, FileDiff

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

CODE_EXTENSIONS = {
    ".ts", ".tsx", ".js", ".jsx",
    ".py", ".sql", ".go", ".java",
    ".rb", ".php", ".swift", ".kt", ".rs",
    ".css", ".scss", ".html", ".vue",
    ".json", ".yaml", ".yml", ".toml",
}

# Whole-filename match (takes precedence over extension rules below).
SPECIAL_FILENAMES = {".env.example"}

EXCLUDED_FILENAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "poetry.lock", "Pipfile.lock", "Gemfile.lock",
    "composer.lock", "Cargo.lock", "go.sum",
}

EXCLUDED_EXTENSIONS = {
    ".md", ".mdx", ".txt", ".lock", ".log",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
    ".woff", ".woff2", ".ttf", ".eot",
}

# Compound suffixes — must be checked with endswith(), not splitext().
EXCLUDED_SUFFIXES = {".min.js", ".min.css"}

PER_FILE_CHANGE_CAP = 500
AGGREGATE_SOFT_LIMIT_BYTES = 250_000

# Priority for dropping under aggregate pressure (first = drop first).
DROP_PRIORITY_EXT = [".json", ".yaml", ".yml", ".toml", ".html", ".css", ".scss"]


# --------------------------------------------------------------------------- #
# Reviewability
# --------------------------------------------------------------------------- #

def is_reviewable(filename: str) -> bool:
    """Return True if ``filename`` should be sent to the reviewer.

    Rules are applied in order:

    1. Basename in ``EXCLUDED_FILENAMES`` -> False
    2. Any compound suffix in ``EXCLUDED_SUFFIXES`` matches -> False
    3. ``splitext`` suffix in ``EXCLUDED_EXTENSIONS`` -> False
    4. Basename in ``SPECIAL_FILENAMES`` -> True
    5. ``splitext`` suffix in ``CODE_EXTENSIONS`` -> True
    6. Otherwise -> False
    """
    if not filename:
        return False

    lower_name = filename.lower()
    basename = os.path.basename(lower_name)
    _, suffix = os.path.splitext(lower_name)

    # 1. Explicit filename blocklist (lockfiles, etc.).
    if basename in EXCLUDED_FILENAMES:
        return False

    # 2. Compound suffixes (.min.js, .min.css) — splitext can't see these.
    for bad_suffix in EXCLUDED_SUFFIXES:
        if lower_name.endswith(bad_suffix):
            return False

    # 3. Single-suffix blocklist (docs, images, fonts, ...).
    if suffix in EXCLUDED_EXTENSIONS:
        return False

    # 4. Whole-filename allowlist (.env.example).
    if basename in SPECIAL_FILENAMES:
        return True

    # 5. Code extension allowlist.
    if suffix in CODE_EXTENSIONS:
        return True

    # 6. Default: not reviewable.
    return False


# --------------------------------------------------------------------------- #
# Per-file trimming
# --------------------------------------------------------------------------- #

def trim_large_diff(fd: FileDiff) -> FileDiff:
    """Replace oversized patch bodies with a SKIPPED placeholder.

    Returns the same ``FileDiff`` instance when no trim is needed, or a new
    one (via ``dataclasses.replace``) with only the ``patch`` field swapped.
    Other fields are preserved so the commit-breakdown table stays accurate.
    """
    if fd.patch is None:
        # Binary / removed / otherwise empty — nothing to trim.
        return fd

    total_changes = fd.additions + fd.deletions
    if total_changes > PER_FILE_CHANGE_CAP:
        placeholder = (
            f"<SKIPPED: {fd.filename} — {total_changes} lines changed, "
            f"exceeds {PER_FILE_CHANGE_CAP}-line threshold; review manually>"
        )
        return replace(fd, patch=placeholder)

    return fd


# --------------------------------------------------------------------------- #
# Per-commit filtering
# --------------------------------------------------------------------------- #

def _is_skipped_patch(patch: str | None) -> bool:
    """True if ``patch`` is a trim_large_diff placeholder."""
    return isinstance(patch, str) and patch.startswith("<SKIPPED:")


def filter_commit_files(files: list[FileDiff]) -> list[FileDiff]:
    """Keep only reviewable files; annotate renames; trim oversized patches.

    A file is kept when either its current filename OR its previous filename
    (for renames) is reviewable.  For ``renamed`` status files, a one-line
    ``# renamed from: ...\\n`` header is prepended to the patch so the reviewer
    can see the rename without a separate field — skipped when the patch is
    ``None`` or already a SKIPPED placeholder.
    """
    kept: list[FileDiff] = []

    for fd in files:
        current_ok = is_reviewable(fd.filename)
        previous_ok = bool(fd.previous_filename) and is_reviewable(fd.previous_filename)
        if not (current_ok or previous_ok):
            continue

        # Annotate renames before trimming so the header survives on
        # non-oversized patches.  Oversized patches become SKIPPED placeholders
        # and we don't want to corrupt those with a rename header.
        if (
            fd.status == "renamed"
            and fd.previous_filename
            and fd.patch is not None
            and not _is_skipped_patch(fd.patch)
        ):
            annotated_patch = f"# renamed from: {fd.previous_filename}\n{fd.patch}"
            fd = replace(fd, patch=annotated_patch)

        kept.append(trim_large_diff(fd))

    return kept


# --------------------------------------------------------------------------- #
# Aggregate-budget enforcement
# --------------------------------------------------------------------------- #

def total_patch_bytes(commits: list[tuple[Commit, list[FileDiff]]]) -> int:
    """Sum ``len(patch or "")`` across all files across all commits."""
    total = 0
    for _commit, files in commits:
        for fd in files:
            total += len(fd.patch or "")
    return total


def _drop_priority_index(filename: str) -> int:
    """Return the index of ``filename``'s extension in ``DROP_PRIORITY_EXT``.

    Lower index = drop sooner.  Returns ``len(DROP_PRIORITY_EXT)`` for files
    that aren't in the priority list (those are dropped only after every
    priority-listed file has been removed).
    """
    _, suffix = os.path.splitext(filename.lower())
    try:
        return DROP_PRIORITY_EXT.index(suffix)
    except ValueError:
        return len(DROP_PRIORITY_EXT)


def enforce_aggregate_budget(
    commits: list[tuple[Commit, list[FileDiff]]],
) -> list[tuple[Commit, list[FileDiff]]]:
    """Drop low-signal files until aggregate patch size is under the soft limit.

    Caller passes commits newest-first (GitHub's default ``/commits`` ordering).
    We drop from oldest commits first (the tail of the list).  Within a commit
    we drop in ``DROP_PRIORITY_EXT`` order (``.json`` before ``.yaml`` before
    ``.toml`` ...), and only then fall back to language files.

    The function is pure — the caller's lists/tuples are not mutated.  Files
    are copied into fresh lists before any removal happens.
    """
    if total_patch_bytes(commits) <= AGGREGATE_SOFT_LIMIT_BYTES:
        return commits

    # Copy the per-commit file lists so we never mutate the caller's state.
    working: list[tuple[Commit, list[FileDiff]]] = [
        (commit, list(files)) for commit, files in commits
    ]

    # Build an ordered drop queue of (commit_idx, filename, original_file_idx).
    #
    # Outer order: oldest commit first  -> reversed(range(len(working)))
    # Inner order: DROP_PRIORITY_EXT rank (stable within same rank)
    drop_queue: list[tuple[int, str]] = []
    for commit_idx in reversed(range(len(working))):
        _commit, files = working[commit_idx]
        indexed = list(enumerate(files))
        indexed.sort(key=lambda pair: (_drop_priority_index(pair[1].filename), pair[0]))
        for _orig_idx, fd in indexed:
            drop_queue.append((commit_idx, fd.filename))

    current_total = total_patch_bytes(working)

    for commit_idx, filename in drop_queue:
        if current_total <= AGGREGATE_SOFT_LIMIT_BYTES:
            break

        _commit, files = working[commit_idx]
        # Locate and remove the first file matching this name — names within
        # a single commit are unique in GitHub's API, but we stay defensive.
        target_idx = next(
            (i for i, fd in enumerate(files) if fd.filename == filename),
            None,
        )
        if target_idx is None:
            continue

        removed = files.pop(target_idx)
        current_total -= len(removed.patch or "")
        logger.info("dropped %s (low-signal, aggregate budget)", filename)

    if current_total > AGGREGATE_SOFT_LIMIT_BYTES:
        logger.warning(
            "aggregate patch size %d bytes still exceeds soft limit %d after drops",
            current_total,
            AGGREGATE_SOFT_LIMIT_BYTES,
        )

    return working
