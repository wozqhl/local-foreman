"""Append-only trajectory jsonl. One log for the loop, UI SSE, and idle thoughts.

Recent entries stay verbatim. Older ones are summarized locally (no invented
memories, no remote coach). The raw file is never rewritten so a later retrieve
can expand a summary back to the original lines.

Coach usage is counted on this same file: asked_coach / coach_instruction.
Verify events (verified_coach / coach_verdict) are a separate MID-lane tally
and are NOT counted as asked_coach. Idle thought / idle_act / retrieved
/ self_verify / demo never increment either tally. Estimated USD is optional and only appears
when COACH_USD_PER_ASK is set (asks only, not verifies).
LOCAL_FOREMAN_MAX_ASKS hard-caps asked_coach (unset = no cap).
LOCAL_FOREMAN_MAX_VERIFIES hard-caps verified_coach (unset = no cap).
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
ENV_USD_PER_ASK = "COACH_USD_PER_ASK"
ENV_MAX_ASKS = "LOCAL_FOREMAN_MAX_ASKS"
ENV_MAX_VERIFIES = "LOCAL_FOREMAN_MAX_VERIFIES"

EVENT_WORK = "work"
EVENT_STUCK = "stuck"
EVENT_ASKED_COACH = "asked_coach"
EVENT_COACH_INSTRUCTION = "coach_instruction"
EVENT_RESUMED = "resumed"
EVENT_THOUGHT = "thought"
EVENT_RETRIEVED = "retrieved"
EVENT_IDLE_ACT = "idle_act"
EVENT_VERIFIED_COACH = "verified_coach"
EVENT_COACH_VERDICT = "coach_verdict"
EVENT_LESSON = "lesson"
EVENT_SELF_VERIFY = "self_verify"
EVENT_DEMO = "demo"

EVENT_KINDS = (
    EVENT_WORK,
    EVENT_STUCK,
    EVENT_ASKED_COACH,
    EVENT_COACH_INSTRUCTION,
    EVENT_RESUMED,
    EVENT_THOUGHT,
    EVENT_RETRIEVED,
    EVENT_IDLE_ACT,
    EVENT_VERIFIED_COACH,
    EVENT_COACH_VERDICT,
    EVENT_LESSON,
    EVENT_SELF_VERIFY,
    EVENT_DEMO,
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

    def retrieve(
        self,
        spec: Optional[dict[str, Any]] = None,
        *,
        first_seq: Optional[int] = None,
        last_seq: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        """Expand a compacted summary (or seq span) back to raw jsonl rows."""
        return retrieve(self.entries, spec, first_seq=first_seq, last_seq=last_seq)

    def stats(self) -> dict[str, Any]:
        """Coach ask/reply tally for this jsonl. Idle thoughts do not count."""
        return coach_stats(self.entries)


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


def retrieve(
    entries: list[dict[str, Any]],
    spec: Optional[dict[str, Any]] = None,
    *,
    first_seq: Optional[int] = None,
    last_seq: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Expand a compacted summary back to the original jsonl rows.

    `spec` is a summary item with `first_seq` / `last_seq`. Does not invent
    entries; only returns rows already in `entries`. Raw file is not rewritten.
    """
    if spec is not None:
        if first_seq is None:
            first_seq = spec.get("first_seq")
        if last_seq is None:
            last_seq = spec.get("last_seq")
    if first_seq is None and last_seq is None:
        return []
    try:
        lo = None if first_seq is None else int(first_seq)
        hi = None if last_seq is None else int(last_seq)
    except (TypeError, ValueError):
        return []
    out: list[dict[str, Any]] = []
    for e in entries:
        try:
            seq = int(e.get("seq"))
        except (TypeError, ValueError):
            continue
        if lo is not None and seq < lo:
            continue
        if hi is not None and seq > hi:
            continue
        out.append(e)
    return out


def retrieve_from_summary(
    summary: dict[str, Any],
    entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Convenience: expand one compacted summary item to raw jsonl rows."""
    return retrieve(entries, summary)


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


def parse_kinds(spec: Optional[str]) -> Optional[set[str]]:
    """Comma-separated kind filter. Empty spec means no filter."""
    if not spec:
        return None
    kinds = {part.strip() for part in str(spec).split(",") if part.strip()}
    return kinds or None


def select_entries(
    entries: list[dict[str, Any]],
    *,
    last: Optional[int] = None,
    kinds: Optional[set[str]] = None,
) -> list[dict[str, Any]]:
    """Subset of the same jsonl rows. Does not rewrite or invent events."""
    out = list(entries)
    if kinds:
        out = [e for e in out if str(e.get("kind") or "") in kinds]
    if last is not None:
        try:
            n = int(last)
        except (TypeError, ValueError):
            n = 0
        if n <= 0:
            return []
        out = out[-n:]
    return out


def format_entry(ev: dict[str, Any]) -> str:
    """One human line: seq  kind  message. Kind stays a searchable token."""
    kind = str(ev.get("kind") or "event")
    msg = str(ev.get("message") or "")
    seq = ev.get("seq")
    if seq is not None:
        return f"{seq}  {kind}  {msg}".rstrip()
    return f"{kind}  {msg}".rstrip()


def write_jsonl(entries: list[dict[str, Any]], path: Path) -> None:
    """Export rows in the same append-only jsonl shape the loop writes."""
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", encoding="utf-8") as fh:
        for ev in entries:
            fh.write(json.dumps(ev, ensure_ascii=False) + "\n")


def coach_usd_per_ask() -> Optional[float]:
    """Optional dollars per ask. Unset / invalid / negative -> count only."""
    raw = (os.environ.get(ENV_USD_PER_ASK) or "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    if value < 0:
        return None
    return value


def coach_max_asks() -> Optional[int]:
    """Hard cap on asked_coach. Unset / invalid / negative -> no cap."""
    raw = (os.environ.get(ENV_MAX_ASKS) or "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    if value < 0:
        return None
    return value


def coach_max_verifies() -> Optional[int]:
    """Hard cap on verified_coach. Unset / invalid / negative -> no cap."""
    raw = (os.environ.get(ENV_MAX_VERIFIES) or "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    if value < 0:
        return None
    return value


def coach_stats(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Count ask / coach replies on this traj. Idle thoughts do not count.

    asks = asked_coach rows (a real consult). replies = coach_instruction
    rows. thought / idle_act / retrieved / work / stuck are ignored.
    estimated_usd is included only when COACH_USD_PER_ASK is set.
    max_asks is included only when LOCAL_FOREMAN_MAX_ASKS is set.
    """
    asks = 0
    replies = 0
    verifies = 0
    verify_replies = 0
    accepts = 0
    for ev in entries:
        kind = str(ev.get("kind") or "")
        if kind == EVENT_ASKED_COACH:
            asks += 1
        elif kind == EVENT_COACH_INSTRUCTION:
            replies += 1
        elif kind == EVENT_VERIFIED_COACH:
            verifies += 1
        elif kind == EVENT_COACH_VERDICT:
            verify_replies += 1
            reply = ev.get("reply")
            verdict = ""
            if isinstance(reply, dict):
                verdict = str(reply.get("verdict") or "")
            if verdict == "accept":
                accepts += 1
    out: dict[str, Any] = {
        "asks": asks,
        "replies": replies,
        "verifies": verifies,
        "verify_replies": verify_replies,
        "verify_accepts": accepts,
    }
    if verify_replies:
        out["verify_accept_rate"] = round(accepts / verify_replies, 4)
    usd = coach_usd_per_ask()
    if usd is not None:
        out["usd_per_ask"] = usd
        out["estimated_usd"] = round(asks * usd, 6)
    cap = coach_max_asks()
    if cap is not None:
        out["max_asks"] = cap
    vcap = coach_max_verifies()
    if vcap is not None:
        out["max_verifies"] = vcap
    return out


def format_coach_stats(stats: dict[str, Any]) -> str:
    """Human lines for `local-foreman traj --stats`."""
    lines = [
        f"asks={int(stats.get('asks') or 0)}",
        f"replies={int(stats.get('replies') or 0)}",
        f"verifies={int(stats.get('verifies') or 0)}",
    ]
    if "verify_accept_rate" in stats:
        lines.append(f"verify_accept_rate={stats['verify_accept_rate']}")
    if "estimated_usd" in stats:
        usd = stats["estimated_usd"]
        text = f"{float(usd):.6f}".rstrip("0").rstrip(".")
        lines.append(f"estimated_usd={text if text else '0'}")
    if "max_asks" in stats:
        lines.append(f"max_asks={int(stats['max_asks'])}")
    if "max_verifies" in stats:
        lines.append(f"max_verifies={int(stats['max_verifies'])}")
    return "\n".join(lines)

