"""AutoMix-style local self-verify: score a pending claim before spending coach.

Router is still tool-kind first. This overlay only answers: is the claim
clearly hopeless (do not burn verify/ask tokens), or high-p with a CRITIC
check already in hand (stay LOW)?

Never calls a live coach. Never loads MLX weights. Mock scores use worker
confidence plus a cheap local check (CRITIC / hopeless markers).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from local_foreman.tools import local_check_write
from local_foreman.worker import WorkerAction, resolve_confidence

VERY_LOW_P = 0.20
HIGH_P = 0.75

HOPELESS_MARKERS = (
    "clearly unsolvable",
    "unsolvable",
    "hopeless",
    "impossible",
    "cannot solve",
    "cannot-solve",
    "no solution",
    "no way to",
)

HIGH_MARKERS = ("self-verify-ok", "sure-local", "high-confidence")
LOW_MARKERS = ("self-verify-low", "very-low", "p-very-low")


@dataclass
class SelfVerifyScore:
    p: float
    reason: str
    hopeless: bool = False
    critic: Optional[bool] = None

    @property
    def very_low(self) -> bool:
        return bool(self.hopeless) or self.p < VERY_LOW_P

    @property
    def high(self) -> bool:
        return (not self.hopeless) and self.p >= HIGH_P


def _blob(action: WorkerAction, goal: str, content: str) -> str:
    return " ".join(
        [
            str(action.thought or ""),
            str(content or ""),
            action.describe(),
            str(goal or ""),
        ]
    ).lower()


def score_pending_claim(
    action: WorkerAction,
    *,
    goal: str = "",
    critic: Optional[bool] = None,
) -> SelfVerifyScore:
    """Cheap local score. Worker confidence is a prior, not the only gate."""
    p = resolve_confidence(action)
    reasons: list[str] = []
    hopeless = False
    path = str((action.args or {}).get("path") or "")
    content = str((action.args or {}).get("content") or "")
    if critic is None and action.kind == "tool" and (action.tool or "") == "write":
        critic = local_check_write(path, content)

    if critic is True:
        p = max(p, 0.85)
        reasons.append("critic-ok")
    elif critic is False:
        # Syntax fail is locally fixable — lower p but do not mark hopeless.
        p = min(p, 0.35)
        reasons.append("critic-fail")

    if action.kind == "tool" and (action.tool or "") == "write":
        if not path.strip() or not str(content).strip():
            hopeless = True
            p = min(p, 0.05)
            reasons.append("empty-write")

    blob = _blob(action, goal, content)
    for marker in HOPELESS_MARKERS:
        if marker in blob:
            hopeless = True
            p = min(p, 0.08)
            reasons.append("hopeless:" + marker)
            break
    if any(m in blob for m in LOW_MARKERS):
        p = min(p, 0.10)
        reasons.append("low-marker")
    if any(m in blob for m in HIGH_MARKERS) and not hopeless:
        p = max(p, 0.90)
        reasons.append("high-marker")

    p = min(1.0, max(0.0, float(p)))
    if p < VERY_LOW_P:
        reasons.append("p-very-low")
    if not reasons:
        reasons.append("worker-p")
    return SelfVerifyScore(
        p=round(p, 4),
        reason="; ".join(reasons),
        hopeless=hopeless,
        critic=critic,
    )


def worker_score_claim(
    worker: object,
    action: WorkerAction,
    *,
    goal: str = "",
) -> SelfVerifyScore:
    """Prefer Worker.score_claim when present; else the cheap mock check."""
    fn = getattr(worker, "score_claim", None)
    if callable(fn):
        try:
            custom = fn(action, goal=goal)
        except TypeError:
            custom = fn(action)
        if isinstance(custom, SelfVerifyScore):
            return custom
    return score_pending_claim(action, goal=goal)
