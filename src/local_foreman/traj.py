"""Append-only trajectory jsonl. One log for the loop, UI SSE, and idle thoughts.

Recent entries stay verbatim. Older ones are summarized locally (no invented
memories, no remote coach). The raw file is never rewritten so a later retrieve
can expand a summary back to the original lines.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

STATE_DIR_NAME = ".local-foreman"
TRAJ_FILENAME = "traj.jsonl"
ENV_TRAJ = "LOCAL_FOREMAN_TRAJ"

EVENT_WORK = "work"
EVENT_STUCK = "stuck"
EVENT_ASKED_COACH = "asked_coach"
EVENT_COACH_INSTRUCTION = "coach_instruction"
EVENT_RESUMED = "resumed"
EVENT_THOUGHT = "thought"

EVENT_KINDS = (
    EVENT_WORK,
    EVENT_STUCK,
    EVENT_ASKED_COACH,
    EVENT_COACH_INSTRUCTION,
    EVENT_RESUMED,
    EVENT_THOUGHT,
)

DEFAULT_RECENT = 8
DEFAULT_LAYER = 8
SUMMARY_CHAR_CAP = 600


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def default_state_dir(cwd: Optional[Path] = None) -> Path:
    return Path(cwd or Path.cwd()) / STATE_DIR_NAME


def default_traj_path(cwd: Optional[Path] = None) -> Path:
    raw = (os.environ.get(ENV_TRAJ) or "").strip()
    if raw:
        return Path(raw)
    return default_state_dir(cwd) / TRAJ_FILENAME


class Trajectory:
    """On-disk append-only jsonl. Survives process restart."""

    def __init__(self, path: Optional[Path] = None, *, goal: str = "", cwd: Optional[Path] = None):
        self.path = Path(path) if path else default_traj_path(cwd)
        self.goal = goal
        self.entries: list[dict[str, Any]] = []
        self._seq = 0
        self.reload()

    def reload(self) -> list[dict[str, Any]]:
        self.entries = []
        self._seq = 0
        if not self.path.is_file():
            return self.entries
        text = self.path.read_text(encoding="utf-8")
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(ev, dict):
                continue
            self.entries.append(ev)
            try:
                self._seq = max(self._seq, int(ev.get("seq") or 0))
            except (TypeError, ValueError):
                pass
        return self.entries

    def append(self, ev: dict[str, Any]) -> dict[str, Any]:
        self._seq += 1
        rec = dict(ev)
        rec.setdefault("ts", utc_now())
        rec["seq"] = self._seq
        if self.goal and not rec.get("goal"):
            rec["goal"] = self.goal
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()
        self.entries.append(rec)
        return rec


def _kind_counts(kinds: dict[str, int]) -> str:
    return ", ".join(f"{k}×{n}" for k, n in kinds.items())


def summarize_entries(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Extractive local summary. Only repeats kind + message already on disk."""
    kinds: dict[str, int] = {}
    parts: list[str] = []
    for e in entries:
        k = str(e.get("kind") or "event")
        kinds[k] = kinds.get(k, 0) + 1
        msg = " ".join(str(e.get("message") or "").split())
        if msg:
            parts.append(f"{k}: {msg}")
    body = "; ".join(parts)
    if len(body) > SUMMARY_CHAR_CAP:
        body = body[: SUMMARY_CHAR_CAP - 3] + "..."
    first = entries[0] if entries else {}
    last = entries[-1] if entries else {}
    return {
        "role": "summary",
        "kind": "summary",
        "count": len(entries),
        "kinds": kinds,
        "message": (
            f"summarized {len(entries)} events ({_kind_counts(kinds)}): {body}"
            if body
            else f"summarized {len(entries)} events ({_kind_counts(kinds)})"
        ),
        "first_seq": first.get("seq"),
        "last_seq": last.get("seq"),
    }


def _verbatim(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": "verbatim",
        "kind": entry.get("kind"),
        "message": entry.get("message", ""),
        "seq": entry.get("seq"),
        "entry": entry,
    }


def compact_entries(
    entries: list[dict[str, Any]],
    *,
    recent: int = DEFAULT_RECENT,
    layer: int = DEFAULT_LAYER,
) -> list[dict[str, Any]]:
    """Layered compaction: last `recent` verbatim; older blocks grow exponentially.

    Raw jsonl is not rewritten. Summaries are an index, not a replacement.
    """
    if recent < 1:
        recent = 1
    if layer < 1:
        layer = 1
    if not entries:
        return []
    if len(entries) <= recent:
        return [_verbatim(e) for e in entries]

    recent_part = list(entries[-recent:])
    older = list(entries[:-recent])
    newest_first: list[list[dict[str, Any]]] = []
    size = layer
    rest = older
    while rest:
        if len(rest) <= size:
            newest_first.append(rest)
            break
        newest_first.append(rest[-size:])
        rest = rest[:-size]
        size *= 2
    out = [summarize_entries(chunk) for chunk in reversed(newest_first)]
    out.extend(_verbatim(e) for e in recent_part)
    return out


def render_compacted(
    entries: list[dict[str, Any]],
    *,
    recent: int = DEFAULT_RECENT,
    layer: int = DEFAULT_LAYER,
) -> str:
    items = compact_entries(entries, recent=recent, layer=layer)
    lines: list[str] = []
    for item in items:
        if item.get("role") == "summary" or item.get("kind") == "summary":
            lines.append(str(item.get("message") or ""))
        else:
            kind = item.get("kind") or "event"
            msg = item.get("message") or ""
            lines.append(f"{kind}: {msg}".rstrip())
    return "\n".join(line for line in lines if line)
