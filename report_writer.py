"""Markdown report writer — header, LLM body, deterministic commit breakdown table.

This module assembles the final review artifact from three sources:

1. A deterministic header block generated from ``ReportMeta``.
2. The raw Markdown body returned by the LLM (lightly cleaned).
3. A deterministic "Commit Breakdown" table rendered from GitHub data
   (never from the LLM) — this guarantees accurate SHAs and counts.

If ``meta.commit_count == 0``, callers must short-circuit — ``write_report``
is not meant to handle empty runs (see PRD §9: empty window → exit 0, no
file written). The orchestrator owns that branch.

On any filesystem error, :class:`ReportWriteError` is raised; the
orchestrator maps it to exit code 7 per PRD §9.
"""

from __future__ import annotations

import logging
import textwrap
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from github_client import Commit, FileDiff

log = logging.getLogger(__name__)


class ReportWriteError(Exception):
    """Raised when the report file cannot be written. Maps to exit code 7."""


@dataclass
class ReportMeta:
    reponame: str
    branch: str
    period_display: str
    commit_count: int
    generated_at: datetime
    commits: list[tuple[Commit, list[FileDiff]]]


def build_output_path(
    output_dir: str,
    reponame: str,
    branch: str,
    ts: datetime,
) -> Path:
    """Build the full output path and ensure the parent directory exists.

    Filename format per PRD §8.1:
        review-{reponame}-{branch_safe}-{YYYYMMDD-HHMMSS}.md
    where slashes in ``branch`` are replaced with underscores.
    """
    branch_safe = branch.replace("/", "_")
    ts_str = ts.strftime("%Y%m%d-%H%M%S")
    filename = f"review-{reponame}-{branch_safe}-{ts_str}.md"
    directory = Path(output_dir).expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    return directory / filename


def render_header(meta: ReportMeta) -> str:
    """Render the deterministic header block (PRD §8.2)."""
    return textwrap.dedent(
        f"""\
        # Code Review Report
        **Repo:** ridwanspace/{meta.reponame}
        **Branch:** {meta.branch}
        **Period:** {meta.period_display}
        **Commits reviewed:** {meta.commit_count}
        **Generated:** {meta.generated_at:%Y-%m-%dT%H:%M:%SZ}
        **Model:** gemini-3.1-flash-lite-preview

        ---

        """
    )


def _escape_pipe(s: str) -> str:
    """Escape ``|`` so it does not break Markdown table cells."""
    return s.replace("|", "\\|")


def render_commit_breakdown(
    commits: list[tuple[Commit, list[FileDiff]]],
) -> str:
    """Render the deterministic ``## Commit Breakdown`` table.

    This is generated from GitHub data (never from the LLM) so SHAs,
    authors, dates, and line counts are guaranteed accurate.
    """
    lines = [
        "## Commit Breakdown\n",
        "| SHA | Author | Date (UTC) | Message | Files | +/- |",
        "|---|---|---|---|---|---|",
    ]
    for commit, files in commits:
        sha7 = commit.sha[:7]
        msg = _escape_pipe(commit.message.split("\n", 1)[0])[:80]
        author = _escape_pipe(commit.author)
        adds = sum(f.additions for f in files)
        dels = sum(f.deletions for f in files)
        lines.append(
            f"| `{sha7}` | {author} | {commit.date:%Y-%m-%dT%H:%M:%SZ} | {msg} | {len(files)} | +{adds}/-{dels} |"
        )
    return "\n".join(lines) + "\n"


def _cleanup_llm_body(body: str) -> str:
    """Strip surrounding whitespace and unwrap a single outermost fenced block.

    The system prompt forbids wrapping the whole response in a code fence,
    but models sometimes slip. If the body starts with a ``` line and the
    last non-empty line is exactly ``` we strip that outermost fence.
    This handles both bare ``` and ```markdown-style opens.
    """
    stripped = body.strip()
    if not stripped:
        return stripped

    body_lines = stripped.split("\n")
    first = body_lines[0].rstrip()
    last = body_lines[-1].rstrip()

    if first.startswith("```") and last == "```" and len(body_lines) >= 2:
        # Drop the opening fence line and the closing fence line.
        inner = "\n".join(body_lines[1:-1])
        return inner.strip()

    return stripped


def write_report(
    output_dir: str,
    meta: ReportMeta,
    llm_body: str,
) -> Path:
    """Assemble and write the final report; return its path.

    Raises:
        ReportWriteError: if the file cannot be written (exit code 7).
    """
    path = build_output_path(output_dir, meta.reponame, meta.branch, meta.generated_at)
    parts = [
        render_header(meta),
        _cleanup_llm_body(llm_body),
        "\n\n---\n\n",
        render_commit_breakdown(meta.commits),
    ]
    try:
        path.write_text("".join(parts), encoding="utf-8", newline="\n")
    except OSError as e:
        raise ReportWriteError(f"{path}: {e}") from e
    return path
