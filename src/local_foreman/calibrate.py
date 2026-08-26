"""EAGLE-2-style rolling calibration from coach_verdict extras.

P(accept | conf_bucket, act_type) is estimated from recent coach_verdict
rows on the same traj jsonl (conf, act, verdict). The table is trusted
only after MIN_SAMPLES extras; otherwise the loop keeps DSP skip at
accept>=0.75 and speculation tax at <0.5.

When trusted: skip verify if P(accept) >= P_SKIP and the act is not
git-mutate. Calibrated P vs raw conf disagreement never invents a new
HIGH ask — only the existing four HIGH rules escalate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Union

from local_foreman.traj import EVENT_COACH_VERDICT
from local_foreman.worker import WorkerAction, resolve_confidence

MIN_SAMPLES = 8
P_SKIP = 0.90
DSP_SKIP_RATE = 0.75
TAX_RATE = 0.50
DISAGREE_WINDOW = 8
RECENT_VERDICTS = 32

HIGH_CONF = 0.70
LOW_CONF = 0.40

GIT_MUTATE_HINTS = (
    "git push",
    "git commit",
    "git reset",
    "git rebase",
    "git checkout",
    "git merge",
    "git add",
    "git revert",
    "gh ",
)

ActRef = Union[WorkerAction, str, None]


def conf_bucket(conf: float) -> str:
    """Three coarse buckets. Default write confidence 0.4 is mid."""
    try:
        c = float(conf)
    except (TypeError, ValueError):
        return "mid"
    if c < LOW_CONF:
        return "low"
    if c < HIGH_CONF:
        return "mid"
    return "high"


def action_is_git_mutate(action: ActRef) -> bool:
    """True for git-mutate / remote / .git writes. Those stay HIGH."""
    if isinstance(action, WorkerAction):
        from local_foreman.tools import needs_ask

        if action.kind != "tool":
            return False
        ask, _reason, _risk = needs_ask(action.tool or "", action.args or {})
        return bool(ask)
    text = str(action or "").lower()
    return any(h in text for h in GIT_MUTATE_HINTS)


def act_type_of(act: ActRef) -> str:
    """Map a describe() string or WorkerAction to a coarse act_type."""
    if isinstance(act, WorkerAction):
        if action_is_git_mutate(act):
            return "git-mutate"
        if act.kind == "tool":
            tool = (act.tool or "").strip()
            if tool in {"write", "read", "shell"}:
                return tool
            return tool or "other"
        return act.kind or "other"
    text = str(act or "").strip()
    if action_is_git_mutate(text):
        return "git-mutate"
    first = text.split(" ", 1)[0].strip().lower() if text else ""
    if first in {"write", "read", "shell"}:
        return first
    return "other"


def _as_conf(raw: Any) -> Optional[float]:
    if raw is None or raw == "":
        return None
    try:
        return min(1.0, max(0.0, float(raw)))
    except (TypeError, ValueError):
        return None


def extract_verdicts(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Recent coach_verdict extras that have (conf, act, verdict)."""
    out: list[dict[str, Any]] = []
    for ev in entries:
        if str(ev.get("kind") or "") != EVENT_COACH_VERDICT:
            continue
        verdict = ev.get("verdict")
        reply = ev.get("reply")
        if not verdict and isinstance(reply, dict):
            verdict = reply.get("verdict")
        if not verdict:
            continue
        conf = _as_conf(ev.get("conf"))
        act = ev.get("act")
        if conf is None or act is None or str(act) == "":
            continue
        bucket = str(ev.get("conf_bucket") or "") or conf_bucket(conf)
        atype = str(ev.get("act_type") or "") or act_type_of(str(act))
        out.append(
            {
                "conf": conf,
                "act": str(act),
                "verdict": str(verdict),
                "conf_bucket": bucket,
                "act_type": atype,
            }
        )
    if RECENT_VERDICTS and len(out) > RECENT_VERDICTS:
        return out[-RECENT_VERDICTS:]
    return out


def row_disagrees(conf: float, verdict: str) -> bool:
    """Raw conf and the coach outcome point opposite ways."""
    v = str(verdict or "")
    if conf >= HIGH_CONF and v != "accept":
        return True
    if conf < LOW_CONF and v == "accept":
        return True
    return False


@dataclass
class CalibrationTable:
    samples: int = 0
    trusted: bool = False
    cells: dict[tuple[str, str], tuple[int, int]] = field(default_factory=dict)
    extras: list[dict[str, Any]] = field(default_factory=list)

    def lookup(self, action: ActRef) -> Optional[float]:
        """P(accept | conf_bucket, act_type). None if the table is not trusted."""
        if not self.trusted:
            return None
        if isinstance(action, WorkerAction):
            conf = resolve_confidence(action)
        else:
            conf = LOW_CONF
        atype = act_type_of(action)
        cell = self.cells.get((conf_bucket(conf), atype))
        if cell is None or cell[1] <= 0:
            return None
        return cell[0] / cell[1]

    def disagree_window(self, entries: list[dict[str, Any]]) -> bool:
        """True when raw conf and outcomes disagree for a long stretch.

        Count extras whose raw conf and verdict point opposite ways.
        Need >= DISAGREE_WINDOW such rows — do not invent a new HIGH ask.
        """
        rows = extract_verdicts(entries)
        n = sum(1 for r in rows if row_disagrees(float(r["conf"]), str(r["verdict"])))
        return n >= DISAGREE_WINDOW


def rolling_table(entries: list[dict[str, Any]]) -> CalibrationTable:
    extras = extract_verdicts(entries)
    cells: dict[tuple[str, str], tuple[int, int]] = {}
    for row in extras:
        key = (str(row["conf_bucket"]), str(row["act_type"]))
        accepts, total = cells.get(key, (0, 0))
        total += 1
        if str(row["verdict"]) == "accept":
            accepts += 1
        cells[key] = (accepts, total)
    return CalibrationTable(
        samples=len(extras),
        trusted=len(extras) >= MIN_SAMPLES,
        cells=cells,
        extras=extras,
    )


def should_skip_verify(
    table: CalibrationTable,
    action: ActRef,
    p: Optional[float] = None,
) -> bool:
    """Skip verify only when the table is trusted, P>=0.9, and not git-mutate."""
    if action_is_git_mutate(action):
        return False
    if not getattr(table, "trusted", False):
        return False
    if p is None:
        p = table.lookup(action)
    return p is not None and p >= P_SKIP
