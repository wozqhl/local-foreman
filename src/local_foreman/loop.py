"""Main loop: act -> (escalate) ask -> apply -> act."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from local_foreman.coach import Coach, make_coach
from local_foreman.state import (
    ESCALATE_GIT_OR_REMOTE,
    ESCALATE_TOOL_FAILS_TWICE,
    ESCALATE_UNSURE,
    ESCALATE_USER_REVIEW,
    State,
)
from local_foreman.ticket import Ticket, validate_ticket
from local_foreman.tools import execute, needs_ask
from local_foreman.worker import WORKER_SYSTEM, Worker, WorkerAction, make_worker


REVIEW_HINTS = ("review", "please review", "ask coach", "look this over")


@dataclass
class RunResult:
    goal: str
    states: list[str] = field(default_factory=list)
    verdicts: list[str] = field(default_factory=list)
    done_reason: str = ""
    history: list[dict] = field(default_factory=list)
    last_instruction: str = ""
    tickets: int = 0


class ForemanLoop:
    def __init__(
        self,
        worker: Optional[Worker] = None,
        coach: Optional[Coach] = None,
        *,
        root: Optional[Path] = None,
        max_steps: int = 12,
        user_review: bool = False,
        on_state=None,
    ):
        self.worker = worker or make_worker()
        self.coach = coach or make_coach()
        self.root = Path(root) if root else Path.cwd()
        self.max_steps = max_steps
        self.user_review = user_review
        self.coach_instruction: str = ""
        self.on_state = on_state

    def _system(self) -> str:
        parts = [WORKER_SYSTEM]
        if self.coach_instruction:
            parts.append("## Coach instruction (must follow)\n" + self.coach_instruction)
        return "\n\n".join(parts)

    def _user_wants_review(self, goal: str) -> bool:
        if self.user_review:
            return True
        g = goal.lower()
        return any(h in g for h in REVIEW_HINTS)


    def _ticket(
        self,
        goal: str,
        action: WorkerAction,
        failed: list[str],
        reason: str,
        risk: str,
    ) -> Ticket:
        return validate_ticket(
            Ticket(
                goal=goal,
                failed_steps=failed[-3:],
                proposed_next=action.describe(),
                risk=risk if risk in {"write", "push", "spend", "none"} else "none",
                local_guess=f"escalate={reason}; thought={action.thought}",
            )
        )

    def run(self, goal: str) -> RunResult:
        result = RunResult(goal=goal)
        state = State.ACT
        fail_streak = 0
        failed_logs: list[str] = []
        pending_action: Optional[WorkerAction] = None
        pending_risk = "none"
        pending_reason = ""
        steps = 0

        if self._user_wants_review(goal) and state == State.ACT:
            pending_action = WorkerAction(kind="unsure", thought="user asked for review")
            pending_reason = ESCALATE_USER_REVIEW
            pending_risk = "none"
            state = State.ASK

        while steps < self.max_steps:
            steps += 1
            result.states.append(state.value)
            if self.on_state:
                self.on_state(state.value)

            if state == State.ACT:
                action = self.worker.step(
                    goal=goal, system=self._system(), history=result.history
                )
                pending_action = action
                if action.kind == "done":
                    result.done_reason = action.thought or "done"
                    break
                if action.kind == "unsure":
                    pending_reason = ESCALATE_UNSURE
                    pending_risk = "none"
                    state = State.ASK
                    continue
                ask, reason, risk = needs_ask(action.tool or "", action.args)
                if ask:
                    pending_reason = ESCALATE_GIT_OR_REMOTE
                    pending_risk = risk
                    state = State.ASK
                    continue
                tr = execute(action.tool or "", action.args, root=self.root)
                result.history.append(
                    {"action": action.describe(), "result": tr.short()}
                )
                if tr.ok:
                    fail_streak = 0
                else:
                    fail_streak += 1
                    failed_logs.append(tr.short())
                    if fail_streak >= 2:
                        pending_reason = ESCALATE_TOOL_FAILS_TWICE
                        pending_risk = tr.risk
                        state = State.ASK
                continue


            if state == State.ASK:
                action = pending_action or WorkerAction(kind="unsure", thought="empty")
                ticket = self._ticket(goal, action, failed_logs, pending_reason, pending_risk)
                reply = self.coach.advise(ticket)
                result.tickets += 1
                result.history.append({"ticket": ticket.to_dict(), "reply": reply.to_dict()})
                result.verdicts.append(reply.verdict)
                result.last_instruction = reply.instruction
                self._pending_reply = reply
                state = State.APPLY
                continue

            if state == State.APPLY:
                reply = getattr(self, "_pending_reply", None)
                if reply is None:
                    result.done_reason = "apply-missing-reply"
                    break
                # Local MUST inject instruction into the next worker system prompt.
                self.coach_instruction = reply.instruction
                result.last_instruction = reply.instruction
                if reply.verdict == "halt":
                    result.done_reason = "halt: " + reply.instruction
                    break
                # continue / revise: back to act. Never auto-run a blocked remote.
                fail_streak = 0
                pending_action = None
                state = State.ACT
                continue

        if not result.done_reason:
            result.done_reason = "max_steps"
        return result
