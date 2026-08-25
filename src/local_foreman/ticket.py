"""Ticket to coach and coach reply. No full repo dump."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional

RISKS = ("write", "push", "spend", "none")
VERDICTS = ("continue", "revise", "halt")
MAX_FAILED_STEPS = 3
MAX_LOG_CHARS = 240


class TicketError(ValueError):
    pass


@dataclass
class Ticket:
    goal: str
    failed_steps: list[str] = field(default_factory=list)
    proposed_next: str = ""
    risk: Literal["write", "push", "spend", "none"] = "none"
    local_guess: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "failed_steps": list(self.failed_steps),
            "proposed_next": self.proposed_next,
            "risk": self.risk,
            "local_guess": self.local_guess,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Ticket":
        return cls(
            goal=str(data.get("goal", "")),
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


def _short(text: str) -> str:
    text = " ".join(str(text).split())
    if len(text) <= MAX_LOG_CHARS:
        return text
    return text[: MAX_LOG_CHARS - 3] + "..."


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
