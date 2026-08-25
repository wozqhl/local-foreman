"""Main loop: act -> (escalate) ask -> apply -> act.

Event log (one jsonl trajectory): work / stuck+problem / asked_coach /
coach_instruction / resumed / thought / retrieved / idle_act.
Idle local think is extra: never calls the coach. It may pick a tiny
local tool and still go through act + the four escalate rules.
Compacted summaries can be expanded back to raw jsonl for worker context.
After apply, the worker MUST continue with the coach instruction in its
system prompt.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from local_foreman.coach import Coach, make_coach
from local_foreman.state import (
    ESCALATE_GIT_OR_REMOTE,
    ESCALATE_TOOL_FAILS_TWICE,
    ESCALATE_UNSURE,
    ESCALATE_USER_REVIEW,
    State,
)
from local_foreman.ticket import Ticket, build_problem, validate_ticket
from local_foreman.tools import execute, needs_ask
from local_foreman.traj import (
    DEFAULT_RECENT,
    EVENT_ASKED_COACH,
    EVENT_COACH_INSTRUCTION,
    EVENT_IDLE_ACT,
    EVENT_RETRIEVED,
    EVENT_RESUMED,
    EVENT_STUCK,
    EVENT_THOUGHT,
    EVENT_WORK,
    Trajectory,
    compact_entries,
    default_traj_path,
    render_compacted,
    retrieve,
    utc_now,
)
from local_foreman.worker import WORKER_SYSTEM, Worker, WorkerAction, make_worker


REVIEW_HINTS = ("review", "please review", "ask coach", "look this over")

COACH_INSTRUCTION_HEADER = "## Coach instruction (must follow)"
TRAJ_CONTEXT_HEADER = "## Trajectory (compacted, local)"
RETRIEVED_CONTEXT_HEADER = "## Retrieved (raw jsonl, local)"

ENV_PERSIST = "LOCAL_FOREMAN_PERSIST"
ENV_IDLE_START = "LOCAL_FOREMAN_IDLE_START"
ENV_IDLE_CAP = "LOCAL_FOREMAN_IDLE_CAP"

DEFAULT_IDLE_START = 5.0
DEFAULT_IDLE_CAP = 300.0


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass
class RunResult:
    goal: str
    states: list[str] = field(default_factory=list)
    verdicts: list[str] = field(default_factory=list)
    done_reason: str = ""
    history: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    last_instruction: str = ""
    last_problem: str = ""
    tickets: int = 0
    thoughts: list[str] = field(default_factory=list)
    idle_intervals: list[float] = field(default_factory=list)
    traj_path: str = ""


class ForemanLoop:
    def __init__(
        self,
        worker: Optional[Worker] = None,
        coach: Optional[Coach] = None,
        *,
        root: Optional[Path] = None,
        max_steps: int = 12,
        user_review: bool = False,
        on_state: Optional[Callable[[str], None]] = None,
        on_event: Optional[Callable[[dict], None]] = None,
        pace: float = 0.0,
        persist: Optional[bool] = None,
        idle: Optional[bool] = None,
        traj_path: Optional[Path] = None,
        idle_start: Optional[float] = None,
        idle_cap: Optional[float] = None,
        idle_max: Optional[int] = None,
        sleeper: Optional[Callable[[float], None]] = None,
    ):
        self.worker = worker or make_worker()
        self.coach = coach or make_coach()
        self.root = Path(root) if root else Path.cwd()
        self.max_steps = max_steps
        self.user_review = user_review
        self.coach_instruction: str = ""
        self.last_problem: str = ""
        self.on_state = on_state
        self.on_event = on_event
        self.pace = pace
        self._pending_reply = None
        self._pending_problem = ""
        self.persist = env_flag(ENV_PERSIST) if persist is None else bool(persist)
        self.idle = self.persist if idle is None else bool(idle)
        self.traj_path = Path(traj_path) if traj_path else default_traj_path(self.root)
        self.idle_start = (
            DEFAULT_IDLE_START if idle_start is None else float(idle_start)
        )
        if idle_start is None:
            self.idle_start = env_float(ENV_IDLE_START, self.idle_start)
        self.idle_cap = DEFAULT_IDLE_CAP if idle_cap is None else float(idle_cap)
        if idle_cap is None:
            self.idle_cap = env_float(ENV_IDLE_CAP, self.idle_cap)
        self.idle_max = idle_max
        self._sleep = sleeper or time.sleep
        self.traj: Optional[Trajectory] = None
        self._idle_interval = self.idle_start
        self._idle_count = 0
        self._carry_action: Optional[WorkerAction] = None
        self._seq = 0
        self._retrieved_span: Optional[tuple[int, int]] = None
        self._retrieved_entries: list[dict] = []

    def _reset_backoff(self) -> None:
        self._idle_interval = self.idle_start

    def _system(self) -> str:
        parts = [WORKER_SYSTEM]
        if self.coach_instruction:
            parts.append(
                COACH_INSTRUCTION_HEADER
                + "\n"
                + "You were stuck, stated the problem, and asked the coach. "
                + "Resume work. Follow this instruction before choosing the next action:\n"
                + self.coach_instruction
            )
        if self.persist and self.traj is not None and self.traj.entries:
            compacted = render_compacted(self.traj.entries)
            if compacted:
                parts.append(TRAJ_CONTEXT_HEADER + "\n" + compacted)
            if self._retrieved_entries:
                lines = []
                for e in self._retrieved_entries:
                    kind = e.get("kind") or "event"
                    msg = e.get("message") or ""
                    lines.append(f"{kind}: {msg}".rstrip())
                body = "\n".join(line for line in lines if line)
                if body:
                    parts.append(RETRIEVED_CONTEXT_HEADER + "\n" + body)
        return "\n\n".join(parts)

    def _history_for_worker(self, result: RunResult) -> list[dict]:
        if self.persist and self.traj is not None and self.traj.entries:
            recent = list(self.traj.entries[-DEFAULT_RECENT:])
            if not self._retrieved_entries:
                return recent
            seen = {e.get("seq") for e in recent}
            extra = [e for e in self._retrieved_entries if e.get("seq") not in seen]
            return extra + recent
        return result.history

    def _maybe_retrieve(self, result: RunResult) -> None:
        """Expand the newest compacted summary back to raw jsonl for the worker.

        Once per run. Never calls the coach. Same traj / SSE log.
        """
        if self._retrieved_entries:
            return
        if not self.persist or self.traj is None or not self.traj.entries:
            return
        compacted = compact_entries(self.traj.entries)
        summaries = [
            x
            for x in compacted
            if x.get("role") == "summary" or x.get("kind") == "summary"
        ]
        if not summaries:
            return
        newest = summaries[-1]
        raw = retrieve(self.traj.entries, newest)
        if not raw:
            return
        try:
            first = int(newest.get("first_seq"))
            last = int(newest.get("last_seq"))
        except (TypeError, ValueError):
            first = raw[0].get("seq")
            last = raw[-1].get("seq")
        self._retrieved_span = (first, last) if first is not None and last is not None else None
        self._retrieved_entries = raw
        state = result.states[-1] if result.states else State.ACT.value
        self._emit(
            result,
            EVENT_RETRIEVED,
            f"展开原文 seq {first}-{last}（{len(raw)} 条）",
            state=state,
        )

    def _user_wants_review(self, goal: str) -> bool:
        if self.user_review:
            return True
        g = goal.lower()
        return any(h in g for h in REVIEW_HINTS)

    def _emit(
        self,
        result: RunResult,
        kind: str,
        message: str,
        *,
        problem: str = "",
        instruction: str = "",
        state: str = "",
        observation: str = "",
        ticket: Optional[dict] = None,
        reply: Optional[dict] = None,
    ) -> dict:
        ev = {
            "kind": kind,
            "message": message,
            "problem": problem or self.last_problem,
            "instruction": instruction or self.coach_instruction,
            "state": state,
            "goal": result.goal,
        }
        if observation:
            ev["observation"] = observation
        if ticket is not None:
            ev["ticket"] = ticket
        if reply is not None:
            ev["reply"] = reply
        if self.traj is not None:
            ev = self.traj.append(ev)
        else:
            self._seq += 1
            ev["seq"] = self._seq
            ev["ts"] = utc_now()
        result.events.append(ev)
        if kind == EVENT_THOUGHT:
            result.thoughts.append(message)
        if self.on_event:
            self.on_event(ev)
        return ev

    def _mark_stuck(
        self,
        result: RunResult,
        goal: str,
        action: WorkerAction,
        failed: list[str],
        reason: str,
        risk: str,
        *,
        state: str,
    ) -> str:
        problem = build_problem(
            goal=goal,
            reason=reason,
            what_tried=action.describe(),
            risk=risk,
            failed_steps=failed,
        )
        self.last_problem = problem
        self._pending_problem = problem
        result.last_problem = problem
        self._reset_backoff()
        self._emit(
            result,
            EVENT_STUCK,
            problem,
            problem=problem,
            state=state,
        )
        return problem

    def _ticket(
        self,
        goal: str,
        action: WorkerAction,
        failed: list[str],
        reason: str,
        risk: str,
    ) -> Ticket:
        problem = self._pending_problem or build_problem(
            goal=goal,
            reason=reason,
            what_tried=action.describe(),
            risk=risk,
            failed_steps=failed,
        )
        return validate_ticket(
            Ticket(
                goal=goal,
                problem=problem,
                failed_steps=failed[-3:],
                proposed_next=action.describe(),
                risk=risk if risk in {"write", "push", "spend", "none"} else "none",
                local_guess=f"escalate={reason}; thought={action.thought}",
            )
        )

    def _open_traj(self, goal: str) -> None:
        if not self.persist:
            self.traj = None
            return
        self.traj = Trajectory(self.traj_path, goal=goal, cwd=self.root)

    def _handle_act(
        self,
        result: RunResult,
        goal: str,
        action: WorkerAction,
        failed_logs: list[str],
        fail_streak: int,
    ) -> tuple[str, int, Optional[WorkerAction], str, str]:
        """Process one act action. Returns (next_state, fail_streak, pending, reason, risk)."""
        pending = action
        pending_reason = ""
        pending_risk = "none"
        if action.kind == "thought":
            self._emit(
                result,
                EVENT_THOUGHT,
                action.thought or "thought",
                state=State.IDLE.value,
            )
            return State.IDLE.value if self.idle else State.ACT.value, fail_streak, None, "", "none"
        if action.kind == "done":
            self._emit(
                result,
                EVENT_WORK,
                action.thought or "done",
                state=State.ACT.value,
            )
            result.done_reason = action.thought or "done"
            if self.idle:
                return State.IDLE.value, 0, None, "", "none"
            return "", fail_streak, None, "", "none"
        if action.kind == "unsure":
            pending_reason = ESCALATE_UNSURE
            pending_risk = "none"
            self._mark_stuck(
                result,
                goal,
                action,
                failed_logs,
                pending_reason,
                pending_risk,
                state=State.ASK.value,
            )
            return State.ASK.value, fail_streak, pending, pending_reason, pending_risk
        ask, _reason, risk = needs_ask(action.tool or "", action.args)
        if ask:
            pending_reason = ESCALATE_GIT_OR_REMOTE
            pending_risk = risk
            self._mark_stuck(
                result,
                goal,
                action,
                failed_logs,
                pending_reason,
                pending_risk,
                state=State.ASK.value,
            )
            return State.ASK.value, fail_streak, pending, pending_reason, pending_risk
        tr = execute(action.tool or "", action.args, root=self.root)
        result.history.append(
            {"action": action.describe(), "result": tr.short()}
        )
        self._emit(
            result,
            EVENT_WORK,
            f"{action.describe()} → {tr.short()}",
            state=State.ACT.value,
            observation=tr.short(),
        )
        if tr.ok:
            return State.ACT.value, 0, pending, "", "none"
        fail_streak = fail_streak + 1
        failed_logs.append(tr.short())
        if fail_streak >= 2:
            pending_reason = ESCALATE_TOOL_FAILS_TWICE
            pending_risk = tr.risk
            self._mark_stuck(
                result,
                goal,
                action,
                failed_logs,
                pending_reason,
                pending_risk,
                state=State.ASK.value,
            )
            return State.ASK.value, fail_streak, pending, pending_reason, pending_risk
        return State.ACT.value, fail_streak, pending, "", "none"

    def _idle_think(self, result: RunResult, goal: str) -> Optional[WorkerAction]:
        """Local monologue. Never calls the coach. May return a tool to send through act."""
        wait = self._idle_interval
        result.idle_intervals.append(wait)
        if wait > 0:
            self._sleep(wait)
        self._idle_interval = min(max(self._idle_interval * 2, self.idle_start), self.idle_cap)
        think = getattr(self.worker, "think", None)
        if think is None:
            action = WorkerAction(kind="thought", thought="空转：本地还在，不问教练")
        else:
            action = think(
                goal=goal,
                system=self._system(),
                history=self._history_for_worker(result),
            )
        self._idle_count += 1
        if action.kind == "tool":
            # Thought stays on the mind log, then idle_act → act + four rules.
            thought = action.thought or action.describe()
            self._emit(
                result,
                EVENT_THOUGHT,
                thought,
                state=State.IDLE.value,
            )
            self._emit(
                result,
                EVENT_IDLE_ACT,
                action.describe(),
                state=State.IDLE.value,
            )
            return action
        # unsure / done / thought from idle: stay local, do not escalate
        text = action.thought or action.describe() or "thought"
        self._emit(
            result,
            EVENT_THOUGHT,
            text,
            state=State.IDLE.value,
        )
        return None

    def run(self, goal: str) -> RunResult:
        result = RunResult(goal=goal)
        self._open_traj(goal)
        if self.traj is not None:
            result.traj_path = str(self.traj.path)
        self._reset_backoff()
        self._idle_count = 0
        self._carry_action = None
        self._retrieved_span = None
        self._retrieved_entries = []
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
            self._mark_stuck(
                result,
                goal,
                pending_action,
                failed_logs,
                pending_reason,
                pending_risk,
                state=State.ASK.value,
            )
            state = State.ASK

        while self.max_steps <= 0 or steps < self.max_steps:
            steps += 1
            result.states.append(state.value)
            if self.on_state:
                self.on_state(state.value)
            if self.pace and steps > 1:
                self._sleep(self.pace)

            if state == State.ACT:
                self._maybe_retrieve(result)
                if self._carry_action is not None:
                    action = self._carry_action
                    self._carry_action = None
                else:
                    action = self.worker.step(
                        goal=goal,
                        system=self._system(),
                        history=self._history_for_worker(result)
                        if self.persist
                        else result.history,
                    )
                nxt, fail_streak, pending_action, pending_reason, pending_risk = self._handle_act(
                    result, goal, action, failed_logs, fail_streak
                )
                if not nxt:
                    break
                state = State(nxt)
                continue

            if state == State.ASK:
                action = pending_action or WorkerAction(kind="unsure", thought="empty")
                ticket = self._ticket(
                    goal, action, failed_logs, pending_reason, pending_risk
                )
                self._reset_backoff()
                self._emit(
                    result,
                    EVENT_ASKED_COACH,
                    "求助中（正在咨询大模型）",
                    problem=ticket.problem,
                    state=state.value,
                    ticket=ticket.to_dict(),
                )
                reply = self.coach.advise(ticket)
                result.tickets += 1
                result.history.append(
                    {"ticket": ticket.to_dict(), "reply": reply.to_dict()}
                )
                result.verdicts.append(reply.verdict)
                result.last_instruction = reply.instruction
                result.last_problem = ticket.problem
                self._pending_reply = reply
                state = State.APPLY
                continue

            if state == State.APPLY:
                reply = self._pending_reply
                if reply is None:
                    result.done_reason = "apply-missing-reply"
                    break
                # Local MUST inject instruction into the next worker system prompt.
                self.coach_instruction = reply.instruction
                result.last_instruction = reply.instruction
                self._emit(
                    result,
                    EVENT_COACH_INSTRUCTION,
                    reply.instruction,
                    instruction=reply.instruction,
                    problem=self.last_problem,
                    state=state.value,
                    reply=reply.to_dict(),
                )
                if reply.verdict == "halt":
                    result.done_reason = "halt: " + reply.instruction
                    break
                # continue / revise: back to act with instruction in system prompt.
                # Never auto-run a blocked remote.
                self._emit(
                    result,
                    EVENT_RESUMED,
                    "继续：按教练指示回到干活",
                    instruction=reply.instruction,
                    problem=self.last_problem,
                    state=State.ACT.value,
                )
                fail_streak = 0
                pending_action = None
                self._pending_problem = ""
                state = State.ACT
                continue

            if state == State.IDLE:
                if not self.idle:
                    break
                if self.idle_max is not None and self._idle_count >= self.idle_max:
                    if not result.done_reason:
                        result.done_reason = "idle_max"
                    break
                self._maybe_retrieve(result)
                carry = self._idle_think(result, goal)
                if carry is not None:
                    # Do the local act before idle_max can stop the loop.
                    self._carry_action = carry
                    state = State.ACT
                    continue
                if self.idle_max is not None and self._idle_count >= self.idle_max:
                    if not result.done_reason:
                        result.done_reason = "idle_max"
                    break
                continue

        if not result.done_reason:
            result.done_reason = "max_steps"
        return result
