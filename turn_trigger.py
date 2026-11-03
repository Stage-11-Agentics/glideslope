"""What woke a Claude Code agent: name the transcript row that opened a turn.

A turn is every model request between one wake and the next. The wake is a row in the
transcript, so the label is read from the transcript alone. `row_trigger` returns None for
every row that does not open a turn.

Python 3.9 compatible and standard library only, like the beacon, so this logic can run on a
machine whose only interpreter is /usr/bin/python3.

`c11 send` types into a terminal, so the transcript records it exactly as it records a person
typing. `prompt_digest` gives the collector what it needs to tell them apart afterwards: a
typed prompt whose digest matches one of c11's `panel.input_sent` events was a c11 send. c11
keeps those events about 14 days, so an older typed prompt stays "typed prompt".
"""

from __future__ import annotations

import hashlib
import re

TYPED = "typed prompt"
C11_SEND = "c11 send"
C11_LAUNCH = "c11 launch"
SHELL = "background shell"
AGENT = "agent finished"
MONITOR = "monitor event"
DOORBELL = "mailbox doorbell"
PEER = "peer message"
COMPACTION = "compaction"
SLASH = "slash command"
STOP_HOOK = "stop hook"
SCHEDULED = "scheduled wakeup"
SCRIPTED = "scripted prompt"
SESSION_START = "session start"
SUBAGENT = "inside subagent"

MAIL_PHRASES = ("c11 mail waiting", "You have mail: run c11 mailbox")
_PASTE_RE = re.compile(r"^\s*<pasted_content\b[^>]*>(.*)</pasted_content>\s*$", re.DOTALL)
_SUMMARY_RE = re.compile(r"<summary>\s*([^<]*)")
DIGEST_CHARS = 2000


def prompt_digest(text: str) -> str:
    """A short digest of a prompt, comparable with the text of a c11 send."""
    match = _PASTE_RE.match(text)
    body = (match.group(1) if match else text).strip()[:DIGEST_CHARS]
    return hashlib.sha256(body.encode("utf-8", "ignore")).hexdigest()[:16]


def task_kind(text: str) -> str:
    """Which background task a task notification reports."""
    match = _SUMMARY_RE.search(text)
    summary = match.group(1) if match else ""
    if "<event>" in text or summary.startswith("Monitor"):
        return MONITOR
    if summary.startswith(("Agent ", "Background agent ")):
        return AGENT
    return SHELL


def _text(content) -> str | None:
    """A user row's text, or None when the row carries a tool result."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "tool_result":
            return None
        if item.get("type") == "text":
            parts.append(item.get("text") or "")
    return " ".join(parts)


def _from_text(text: str, origin: str | None) -> str | None:
    """The label a prompt's own text settles, or None to fall through to the row's origin."""
    stripped = text.lstrip()
    if stripped.startswith("Another Claude session sent"):
        return DOORBELL if any(phrase in text for phrase in MAIL_PHRASES) else PEER
    if any(phrase in stripped[:400] for phrase in MAIL_PHRASES):
        return DOORBELL
    if stripped.startswith("<task-notification>"):
        return task_kind(text)
    if stripped.startswith(("<local-command", "<command-name>", "<command-message>")):
        return SLASH
    if stripped.startswith(("Stop hook", "<stop-hook")) or "Stop hook feedback" in stripped[:200]:
        return STOP_HOOK
    if stripped.startswith("This session is being continued"):
        return COMPACTION
    if origin == "scheduled" or "autonomous-loop" in stripped[:200] or stripped.startswith("/loop"):
        return SCHEDULED
    if stripped.startswith("Read the file at ") and "launch-prompts/" in stripped[:300]:
        return C11_LAUNCH
    return None


def row_trigger(row: dict) -> tuple[str, str | None] | None:
    """(label, digest) when ROW opens a turn, else None. Digest is set on typed prompts only."""
    kind = row.get("type")
    if kind == "attachment":
        attachment = row.get("attachment") or {}
        if attachment.get("type") != "queued_command":
            return None
        origin = (attachment.get("origin") or {}).get("kind")
        text = _text(attachment.get("prompt")) or ""
    elif kind == "user":
        if row.get("isMeta") or row.get("isSidechain"):
            return None
        if row.get("isCompactSummary"):
            return COMPACTION, None
        text = _text((row.get("message") or {}).get("content"))
        if text is None or text.startswith("[Request interrupted"):
            return None
        origin = (row.get("origin") or {}).get("kind")
    else:
        return None

    label = _from_text(text, origin)
    if label:
        return label, None
    if origin == "peer":
        return PEER, None
    if row.get("turnOrigin") == "sdk" or row.get("promptSource") == "sdk":
        return SCRIPTED, None
    return TYPED, prompt_digest(text)
