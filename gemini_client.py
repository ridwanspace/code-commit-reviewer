"""Gemini SDK wrapper — system prompt, user prompt builder, single generate_content call."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import google.generativeai as genai

from github_client import Commit, FileDiff

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MODEL_ID = "gemini-3.1-flash-lite-preview"
TEMPERATURE = 0.2
TOP_P = 0.9
MAX_OUTPUT_TOKENS = 8192

# Abort if estimated tokens exceed this
TOKEN_BUDGET_HARD_LIMIT = 800_000
TOKEN_CHARS_PER_TOKEN = 4  # rough estimate

# ---------------------------------------------------------------------------
# System prompt — verbatim from PRD §7.3. Do not paraphrase.
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a senior staff engineer performing an enterprise-grade code review.
You are rigorous, specific, and actionable — never vague. You cite file names
and line ranges from the diffs provided. You do NOT invent code that is not in
the diff. If you are uncertain, you say so explicitly.

Your review MUST cover, in this order, and MUST use these exact section headers:

1. Summary — 2–3 sentence executive summary of what changed and overall health.
   End the Summary with a single line: "Overall Quality Score: X/10" where X is
   an integer from 1 to 10. The score reflects: correctness, security, design,
   maintainability, and test coverage — weighted equally.

2. Critical Issues — bugs that cause incorrect behavior, data loss, security
   vulnerabilities (injection, auth bypass, exposed secrets, CSRF, SSRF,
   insecure deserialization, IDOR, missing authorization), or regressions.
   MUST include file:line reference and a concrete fix.

3. Major Issues — logic errors under specific conditions, type unsafety, missing
   error handling, N+1 queries, blocking I/O on hot paths, race conditions,
   incorrect API contracts, inconsistent status codes, response-shape drift.

4. Minor Issues — readability, naming, small inefficiencies, inconsistent
   formatting patterns, dead code, minor duplication.

5. Suggestions — refactors, abstractions, architectural improvements. Optional.

6. What Was Done Well — minimum 3 concrete positive observations, each tied to
   a specific file or change. Do not invent praise.

Every issue entry MUST follow this format:

   - **[Severity] <one-line headline>**
     - File: `path/to/file.ext:L<start>-L<end>` (or single line)
     - Problem: <what is wrong and why it matters>
     - Fix: <concrete code-level recommendation, snippet if short>

Additional rules:
- Reference ONLY files that appear in the provided diffs.
- If a concern is speculative (depends on code not shown), prefix it with
  "Speculative:" and explain what you'd need to verify.
- Do NOT produce an Executive Summary section, Conclusion, or sign-off —
  just the six sections above, in order.
- Use GitHub-flavored Markdown. Code in fenced blocks with language tags.
- Do NOT wrap your entire response in a code block."""


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class ReviewRequest:
    reponame: str
    branch: str
    period_display: str
    commit_count: int
    context_block: str
    commits: list[tuple[Commit, list[FileDiff]]]


class GeminiError(Exception):
    """Raised for any failure reaching or parsing a response from Gemini."""


class TokenBudgetExceeded(GeminiError):
    """Raised when the estimated prompt size exceeds TOKEN_BUDGET_HARD_LIMIT."""


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------


_MAX_HEADLINE_LEN = 120


def _trim_first_line(message: str) -> str:
    first = message.split("\n", 1)[0]
    if len(first) > _MAX_HEADLINE_LEN:
        return first[:_MAX_HEADLINE_LEN] + "…"
    return first


def _render_file(f: FileDiff) -> str:
    header = f"### {f.filename} ({f.status}, +{f.additions} -{f.deletions})"
    if f.patch is None:
        return f"{header}\n_(no patch — binary or removed)_"
    # Bump fence to four backticks if the patch itself contains a triple-backtick.
    fence = "````" if "```" in f.patch else "```"
    return f"{header}\n{fence}diff\n{f.patch}\n{fence}"


def _render_commit(commit: Commit, files: list[FileDiff]) -> str:
    sha_short = commit.sha[:7]
    headline = _trim_first_line(commit.message)
    iso_date = commit.date.strftime("%Y-%m-%dT%H:%M:%SZ")
    parts = [
        f"## Commit {sha_short} — {headline}",
        f"Author: {commit.author}",
        f"Date: {iso_date}",
        "",
    ]
    for f in files:
        parts.append(_render_file(f))
    return "\n".join(parts)


def build_user_prompt(req: ReviewRequest) -> str:
    """Render the full user prompt per PRD §7.4."""
    if req.commits:
        diff_block = "\n\n".join(
            _render_commit(commit, files) for commit, files in req.commits
        )
    else:
        diff_block = "(no reviewable diffs)"

    context = req.context_block or "(no context files found)"

    return (
        f"# Repository\n"
        f"ridwanspace/{req.reponame} @ {req.branch}\n"
        f"\n"
        f"# Period\n"
        f"{req.period_display}  ({req.commit_count} commits)\n"
        f"\n"
        f"# Codebase Context\n"
        f"{context}\n"
        f"\n"
        f"# Aggregated Diffs\n"
        f"{diff_block}"
    )


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------


def estimate_tokens(text: str) -> int:
    return len(text) // TOKEN_CHARS_PER_TOKEN


# ---------------------------------------------------------------------------
# Gemini client
# ---------------------------------------------------------------------------


_EXPECTED_SECTIONS = (
    "Summary",
    "Critical Issues",
    "Major Issues",
    "Minor Issues",
    "Suggestions",
    "What Was Done Well",
)


class GeminiReviewer:
    def __init__(self, api_key: str):
        genai.configure(api_key=api_key)
        self._model = genai.GenerativeModel(
            model_name=MODEL_ID,
            system_instruction=SYSTEM_PROMPT,
            generation_config={
                "temperature": TEMPERATURE,
                "top_p": TOP_P,
                "max_output_tokens": MAX_OUTPUT_TOKENS,
            },
        )

    def review(self, req: ReviewRequest) -> str:
        prompt = build_user_prompt(req)
        est = estimate_tokens(SYSTEM_PROMPT + prompt)
        log.info("Prompt ~%d tokens (%d chars diff)", est, len(prompt))
        if est > TOKEN_BUDGET_HARD_LIMIT:
            raise TokenBudgetExceeded(
                f"estimated {est} tokens exceeds budget {TOKEN_BUDGET_HARD_LIMIT}"
            )
        try:
            resp = self._model.generate_content(prompt)
        except Exception as e:
            raise GeminiError(str(e)) from e

        # resp.text itself may raise if the response was blocked by safety filters
        # or produced no candidates — guard defensively.
        try:
            text = getattr(resp, "text", None)
        except Exception as e:
            feedback = getattr(resp, "prompt_feedback", None)
            reason = f"; prompt_feedback={feedback}" if feedback else ""
            raise GeminiError(f"could not read response text: {e}{reason}") from e

        if not text:
            feedback = getattr(resp, "prompt_feedback", None)
            reason = f"; prompt_feedback={feedback}" if feedback else ""
            raise GeminiError(f"empty response from Gemini{reason}")

        text = text.strip()
        self._warn_missing_sections(text)
        return text

    def _warn_missing_sections(self, text: str) -> None:
        for h in _EXPECTED_SECTIONS:
            if not re.search(rf"(?mi)^#+\s*{re.escape(h)}", text):
                log.warning("LLM output missing expected section: %s", h)
        if "Overall Quality Score:" not in text:
            log.warning("LLM output missing 'Overall Quality Score:' line")
