"""Ticket to coach and coach reply. Clear problem statement, no repo dump."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional

RISKS = ("write", "push", "spend", "none")
VERDICTS = ("continue", "revise", "halt")
MAX_FAILED_STEPS = 3
MAX_LOG_CHARS = 240
MAX_PROBLEM_CHARS = 480

REASON_FAILED = {
    "tool_fails_twice": "the same tool failed twice in a row",
    "git_or_remote": "a git/remote write was blocked before execution",
    "user_review": "the user asked for a review before continuing",
    "unsure": "the local worker is unsure how to proceed safely",
}


class TicketError(ValueError):
    pass


@dataclass
class Ticket:
    goal: str
    problem: str = ""
    failed_steps: list[str] = field(default_factory=list)
    proposed_next: str = ""
    risk: Literal["write", "push", "spend", "none"] = "none"
    local_guess: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "problem": self.problem,
            "failed_steps": list(self.failed_steps),
            "proposed_next": self.proposed_next,
            "risk": self.risk,
            "local_guess": self.local_guess,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Ticket":
        return cls(
            goal=str(data.get("goal", "")),
            problem=str(data.get("problem", "")),
            failed_steps=list(data.get("failed_steps") or []),
            proposed_next=str(data.get("proposed_next", "")),
            risk=data.get("risk", "none"),  # type: ignore[arg-type]
            local_guess=str(data.get("local_guess", "")),
        )


@dataclass
class CoachReply:
    verdict: Literal["continue", "revise", "halt"]
    instruction: str
    next_tool: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "verdict": self.verdict,
            "instruction": self.instruction,
        }
        if self.next_tool:
            out["next_tool"] = self.next_tool
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CoachReply":
        return cls(
            verdict=data.get("verdict", ""),  # type: ignore[arg-type]
            instruction=str(data.get("instruction", "")),
            next_tool=data.get("next_tool") or None,
        )


def _short(text: str, limit: int = MAX_LOG_CHARS) -> str:
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def build_problem(
    *,
    goal: str,
    reason: str,
    what_tried: str,
    risk: str = "none",
    failed_steps: Optional[list[str]] = None,
) -> str:
    """One clear sentence: what failed, what was tried, what we need."""
    what_failed = REASON_FAILED.get(reason, reason or "the worker got stuck")
    tried = " ".join(str(what_tried).split()) or "no successful next step"
    extras = [_short(s, 80) for s in (failed_steps or []) if str(s).strip()]
    if extras:
        tried = tried + "; earlier: " + "; ".join(extras)
    if risk and risk != "none":
        tried = f"{tried} (risk={risk})"
    need = (
        "a coach verdict (continue|revise|halt) plus a short instruction "
        f"so the local worker can resume: {goal}"
    )
    return _short(
        f"What failed: {what_failed}. What was tried: {tried}. What we need: {need}.",
        MAX_PROBLEM_CHARS,
    )


def problem_is_clear(problem: str) -> bool:
    text = " ".join(str(problem or "").split())
    if len(text) < 24:
        return False
    low = text.lower()
    has_failed = "what failed" in low or any(
        k in low for k in ("failed", "blocked", "stuck", "unsure", "review")
    )
    has_tried = "what was tried" in low or "tried" in low
    has_need = "what we need" in low or "need" in low
    return has_failed and has_tried and has_need


def validate_ticket(ticket: Ticket) -> Ticket:
    if not ticket.goal or not str(ticket.goal).strip():
        raise TicketError("ticket.goal is required")
    if ticket.risk not in RISKS:
        raise TicketError(f"ticket.risk must be one of {RISKS}, got {ticket.risk!r}")
    steps = [_short(s) for s in ticket.failed_steps if str(s).strip()]
    ticket.failed_steps = steps[:MAX_FAILED_STEPS]
    ticket.proposed_next = _short(ticket.proposed_next)
    ticket.local_guess = _short(ticket.local_guess)
    ticket.goal = str(ticket.goal).strip()
    problem = _short(ticket.problem, MAX_PROBLEM_CHARS)
    if not problem:
        raise TicketError(
            "ticket.problem is required (what failed, what was tried, what we need)"
        )
    if not problem_is_clear(problem):
        raise TicketError(
            "ticket.problem must be a clear sentence: what failed, what was tried, what we need"
        )
    ticket.problem = problem
    return ticket


def validate_reply(reply: CoachReply) -> CoachReply:
    if reply.verdict not in VERDICTS:
        raise TicketError(f"reply.verdict must be one of {VERDICTS}, got {reply.verdict!r}")
    text = " ".join(str(reply.instruction).split())
    if not text:
        raise TicketError("reply.instruction is required (1-2 sentences)")
    reply.instruction = text
    if reply.next_tool is not None:
        reply.next_tool = str(reply.next_tool).strip() or None
    return reply
