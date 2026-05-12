"""
Redaction patterns for LLM call logging.

Why
---
When `SHARK_LLM_LOG_FULL_TEXT=1` we persist the full prompt + response into
`stocks/memory/llm-calls.jsonl` so the ModelForge exporter can build SFT pairs.
The logger sits inside a live trading process that occasionally handles
secrets (broker keys, webhook URLs, the operator's email, account ids).
Anything that lands in the JSONL is also rsync'd in nightly backups, so it
must be scrubbed BEFORE it touches disk.

Design
------
- Patterns compiled once at module import. The substitution path runs on
  every persisted call, so it has to be cheap.
- Each pattern carries an explicit `name` used in the replacement marker
  (`<REDACTED:api_key>`, etc.) — that name is the audit trail.
- `redact()` returns `(scrubbed_text, count)` so the logger can record how
  many substitutions fired. A non-zero count on an otherwise innocent
  prompt is the operator's tripwire that something leaked into a system
  message.
- Negative-case tests live in `stocks/tests/test_llm_logger.py` — keep them
  green when tightening the patterns or they will start over-redacting
  ordinary trading prose.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Pattern


@dataclass(frozen=True)
class _Rule:
    name: str
    pattern: Pattern[str]


# ---------------------------------------------------------------------------
# Patterns — order matters: api_key first (broadest), webhook before path
# (a Slack hook URL would also match `/home/...`-style paths if path went
# first, though hooks.slack.com URLs don't actually contain /home/).
# ---------------------------------------------------------------------------


# API keys — strong common prefixes, plus the bare `api_key = ...` form.
# The 16-char-min tail keeps short identifiers (like "sk-test") from
# colliding with arbitrary text.
_API_KEY_RE = re.compile(
    r"(?:sk-|pk-|xoxb-|key-|api[_-]?key[\s=:]+)['\"]?([A-Za-z0-9_\-]{16,})",
    re.IGNORECASE,
)

# Account/wallet/id-adjacent long numerics. We require the keyword on
# either side (within a small gap) so a generic 10-digit number — e.g. a
# unix-ms timestamp in trading prose — doesn't get redacted.
_ACCOUNT_RE = re.compile(
    r"(?:\baccount\b|\bwallet\b|\bid\b)[\s:=#]{0,8}(\d{10,})"
    r"|"
    r"(\d{10,})[\s:=#]{0,8}(?:\baccount\b|\bwallet\b|\bid\b)",
    re.IGNORECASE,
)

# RFC-5322-ish email — intentionally loose, false-positive cost is low.
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

# Operator paths — anything under /home/<user>/ or /Users/<user>/.
# Older revision hardcoded one operator path; the generalised regex catches
# any single-user home directory on Linux and macOS so anyone running this
# bot gets the same redaction guarantees without editing the file. We
# intentionally keep this LOOSE (anything under /home/<dir>/) — false
# positives on bench scripts that mention `/home/runner/work/...` are a
# better failure mode than leaking the operator's home tree to the LLM.
_PATH_RE = re.compile(r"(?:/home|/Users)/[^/\s\"'<>]+/[^\s\"'<>]+")

# Slack webhook URLs.
_WEBHOOK_RE = re.compile(r"https://hooks\.slack\.com/[^\s\"'<>]+")


_RULES: tuple[_Rule, ...] = (
    _Rule("api_key", _API_KEY_RE),
    _Rule("webhook", _WEBHOOK_RE),   # before path, to keep the hook URL whole
    _Rule("path",    _PATH_RE),
    _Rule("email",   _EMAIL_RE),
    _Rule("account", _ACCOUNT_RE),
)


def redact(text: str | None) -> tuple[str, int]:
    """Return *(scrubbed, count)*.

    `count` is the total number of substitutions across all rules — useful
    for the JSONL's `redacted_count` field so the operator can spot
    surprise leaks at a glance.

    A `None` input returns `("", 0)` so callers can pipe optional fields
    through without `if text is not None` guards.
    """
    if not text:
        return ("", 0)
    out = text
    total = 0
    for rule in _RULES:
        out, n = rule.pattern.subn(f"<REDACTED:{rule.name}>", out)
        total += n
    return (out, total)


def redact_messages(messages: Iterable[dict]) -> tuple[list[dict], int]:
    """Redact every string-valued `content` field in a chat-format message
    list. Returns a NEW list — never mutates the caller's data.

    Non-string contents (e.g. multimodal blocks) are passed through
    untouched; redaction only knows how to handle text.
    """
    out: list[dict] = []
    total = 0
    for msg in messages or []:
        if not isinstance(msg, dict):
            out.append(msg)
            continue
        new_msg = dict(msg)
        content = new_msg.get("content")
        if isinstance(content, str):
            scrubbed, n = redact(content)
            new_msg["content"] = scrubbed
            total += n
        out.append(new_msg)
    return (out, total)


__all__ = ["redact", "redact_messages"]
