"""Main loop: act -> (low | verify | ask) -> apply -> act.

Three risk lanes after each WorkerAction. Router is tool-kind first
(FrugalGPT / RouteLLM cascade), not raw worker confidence:
  LOW  — read / git-ro / idle thought: stay in act (0 coach tokens).
  MID  — write (held losslessly): state=verify, aider-style draft ticket.
  HIGH — git-mutate / remote / unsure / two fails / user review: state=ask.

While waiting for verify or ask, local may only pre-run read / git-ro.
Never speculate a write (Speculative Actions Assumption 2).
Pending write is applied only on coach accept; revise/halt discards it.

AutoMix self-verify scores a pending claim locally before any verify/ask.
Very-low p twice is HIGH ask only if it is already an escalate condition;
hopeless work is not sent to the coach just to burn tokens. High p plus
an existing CRITIC check stays LOW.

EcoAssistant demo cache: on verify accept (file landed) store a compact
(task_sketch, claim, draft/path) in `.local-foreman`. Later similar writes
inject 1-2 demos into the worker system prompt. Local-only; never store
coach rewrites.

EAGLE-2 rolling calibration: same traj jsonl, recent coach_verdict extras
→ P(accept | conf_bucket, act_type). Trusted only after enough samples
(>= 8). If P>=0.9 and the act is not git-mutate, skip verify (stay LOW;
apply a held write only if already checked, otherwise skip the hold).
If calibrated P and raw conf disagree for a long window, force HIGH only
when an existing HIGH rule also matches — do not invent a new ask reason.
Fewer than 8 verdicts: keep DSP 0.75 skip and tax <0.5.

Event log (one jsonl trajectory): work / stuck+problem / asked_coach /
coach_instruction / resumed / thought / retrieved / idle_act /
verified_coach / coach_verdict / self_verify / demo.
Idle local think is extra: never calls the coach. Idle-act still honors
the four HIGH rules; MID verify is skipped so idle never spends on verify.
After ask-apply, the worker MUST continue with the coach instruction.
HIGH apply + continue + git/remote/push asks the user once more (TTY default)
before resuming act. Decline is halt-like: user_denied, no git/remote execute,
no extra coach call. LOCAL_FOREMAN_CONFIRM=0 / --no-confirm skips.
Verify accept executes a held write; the coach does not rewrite the file.
asked_coach / coach_instruction tally HIGH asks. verified_coach /
coach_verdict tally MID verifies and are NOT counted as asks.
LOCAL_FOREMAN_MAX_ASKS hard-caps asked_coach (unset = no cap).
LOCAL_FOREMAN_MAX_VERIFIES hard-caps verified_coach (unset = no cap).
Inject confirm(prompt)->bool so smoke never blocks on stdin.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from local_foreman.calibrate import (
    act_type_of,
    conf_bucket,
    rolling_table,
    should_skip_verify,
)
from local_foreman.coach import Coach, make_coach
from local_foreman.demo import (
    DEMO_CONTEXT_HEADER,
    compact_demo,
    default_demo_path,
    load_demos,
    render_demos,
    similar_demos,
    store_demo,
)
from local_foreman.self_verify import worker_score_claim
from local_foreman.state import (
    ESCALATE_GIT_OR_REMOTE,
    ESCALATE_TOOL_FAILS_TWICE,
    ESCALATE_UNSURE,
    ESCALATE_USER_REVIEW,
    State,
)
from local_foreman.ticket import (
    Ticket,
    VerifyTicket,
    build_problem,
    build_verify_claim,
    validate_ticket,
    validate_verify_ticket,
)
from local_foreman.tools import (
    draft_diff,
    execute,
    is_readonly_speculate,
    local_check_write,
    needs_ask,
)
from local_foreman.traj import (
    DEFAULT_RECENT,
    EVENT_ASKED_COACH,
    EVENT_COACH_INSTRUCTION,
    EVENT_COACH_VERDICT,
    EVENT_IDLE_ACT,
    EVENT_RETRIEVED,
    EVENT_RESUMED,
    EVENT_STUCK,
    EVENT_THOUGHT,
    EVENT_LESSON,
    EVENT_VERIFIED_COACH,
    EVENT_WORK,
    EVENT_SELF_VERIFY,
    EVENT_DEMO,
    EVENT_USER_DENIED,
    Trajectory,
    compact_entries,
    default_traj_path,
    render_compacted,
    retrieve,
    utc_now,
    coach_max_asks,
    coach_max_verifies,
    coach_stats,
)
from local_foreman.worker import (
    WORKER_SYSTEM,
    Worker,
    WorkerAction,
    make_worker,
    resolve_confidence,
)


REVIEW_HINTS = ("review", "please review", "ask coach", "look this over")

COACH_INSTRUCTION_HEADER = "## Coach instruction (must follow)"
TRAJ_CONTEXT_HEADER = "## Trajectory (compacted, local)"
RETRIEVED_CONTEXT_HEADER = "## Retrieved (raw jsonl, local)"
# Re-export so smoke / CLI can assert the same header the loop injects.
DEMO_CONTEXT_HEADER = DEMO_CONTEXT_HEADER

ENV_PERSIST = "LOCAL_FOREMAN_PERSIST"
ENV_IDLE_START = "LOCAL_FOREMAN_IDLE_START"
ENV_IDLE_CAP = "LOCAL_FOREMAN_IDLE_CAP"
ENV_MAX_ASKS = "LOCAL_FOREMAN_MAX_ASKS"
ENV_MAX_VERIFIES = "LOCAL_FOREMAN_MAX_VERIFIES"
ENV_VERIFY_BELOW = "LOCAL_FOREMAN_VERIFY_BELOW"
ENV_CONFIRM = "LOCAL_FOREMAN_CONFIRM"

DEFAULT_IDLE_START = 5.0
DEFAULT_IDLE_CAP = 300.0
DEFAULT_VERIFY_BELOW = 0.55


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


def stdin_is_tty() -> bool:
    try:
        return bool(sys.stdin.isatty())
    except Exception:
        return False


def tty_confirm(prompt: str) -> bool:
    """CLI second confirmation. Empty / EOF / n -> False."""
    print(prompt, flush=True)
    try:
        raw = input("待确认 [y/N]: ").strip().lower()
    except EOFError:
        return False
    return raw in {"y", "yes", "是"}


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
    verifies: int = 0
    verify_verdicts: list[str] = field(default_factory=list)
    last_claim: str = ""
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
        max_asks: Optional[int] = None,
        max_verifies: Optional[int] = None,
        verify_below: Optional[float] = None,
        demo_path: Optional[Path] = None,
        confirm: Optional[Callable[[str], bool]] = None,
        require_confirm: Optional[bool] = None,
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
        if max_asks is None:
            self.max_asks = coach_max_asks()
        else:
            self.max_asks = None if int(max_asks) < 0 else int(max_asks)
        if max_verifies is None:
            self.max_verifies = coach_max_verifies()
        else:
            self.max_verifies = None if int(max_verifies) < 0 else int(max_verifies)
        if verify_below is None:
            self.verify_below = env_float(ENV_VERIFY_BELOW, DEFAULT_VERIFY_BELOW)
        else:
            self.verify_below = float(verify_below)
        self._sleep = sleeper or time.sleep
        self.traj: Optional[Trajectory] = None
        self._idle_interval = self.idle_start
        self._idle_count = 0
        self._carry_action: Optional[WorkerAction] = None
        self._seq = 0
        self._retrieved_span: Optional[tuple[int, int]] = None
        self._retrieved_entries: list[dict] = []
        self._pending_kind = ""
        self._pending_verify_action: Optional[WorkerAction] = None
        self._pending_claim = ""
        self._pending_draft = ""
        self._pending_verify_risk = "none"
        self.demo_path = Path(demo_path) if demo_path else default_demo_path(self.root)
        self.confirm = confirm
        if require_confirm is None:
            self.require_confirm = env_flag(ENV_CONFIRM, default=stdin_is_tty())
        else:
            self.require_confirm = bool(require_confirm)
        self._goal = ""
        self._last_write_path = ""
        self._low_p_streak = 0
        self._wire_mlx_load_progress()

    def _wire_mlx_load_progress(self) -> None:
        """Map MlxWorker load events to board states. Not a coach ask."""
        w = self.worker
        if type(w).__name__ != "MlxWorker":
            return
        if not self.on_state:
            return
        prev = getattr(w, "on_load", None)

        def combined(event: str, detail: dict) -> None:
            if event == "start":
                self.on_state("加载中")
            elif event == "retry":
                self.on_state("重试加载")
            if prev is not None:
                prev(event, detail)

        w.on_load = combined

    def _reset_backoff(self) -> None:
        self._idle_interval = self.idle_start

    def _system(self) -> str:
        parts = [WORKER_SYSTEM]
        demos = similar_demos(
            load_demos(self.demo_path),
            goal=self._goal,
            path=self._last_write_path,
        )
        if demos:
            body = render_demos(demos)
            if body:
                parts.append(DEMO_CONTEXT_HEADER + "\n" + body)
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
        extra: Optional[dict] = None,
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
        if extra:
            for key, val in extra.items():
                if val is not None:
                    ev[key] = val
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

    def _coach_ask_count(self, result: RunResult) -> int:
        """asked_coach on this traj (disk) or this-run events if persist is off."""
        entries = self.traj.entries if self.traj is not None else result.events
        return int(coach_stats(entries).get("asks") or 0)

    def _skip_ask_for_cap(self, result: RunResult) -> Optional[str]:
        """If the next asked_coach would exceed the cap, return a halt reason."""
        if self.max_asks is None:
            return None
        used = self._coach_ask_count(result)
        if used + 1 <= self.max_asks:
            return None
        return (
            f"max_asks: {ENV_MAX_ASKS}={self.max_asks} already used {used}"
        )

    def _coach_verify_count(self, result: RunResult) -> int:
        entries = self.traj.entries if self.traj is not None else result.events
        return int(coach_stats(entries).get("verifies") or 0)

    def _skip_verify_for_cap(self, result: RunResult) -> Optional[str]:
        """If the next verified_coach would exceed the cap, stay local."""
        if self.max_verifies is None:
            return None
        used = self._coach_verify_count(result)
        if used + 1 <= self.max_verifies:
            return None
        return (
            f"max_verifies: {ENV_MAX_VERIFIES}={self.max_verifies} already used {used}"
        )

    def _verify_ticket(self, goal: str) -> VerifyTicket:
        risk = self._pending_verify_risk
        if risk not in {"write", "none"}:
            risk = "write" if risk else "none"
        return validate_verify_ticket(
            VerifyTicket(
                goal=goal,
                claim=self._pending_claim
                or build_verify_claim(goal=goal, what="a local draft"),
                draft=self._pending_draft,
                risk=risk,  # type: ignore[arg-type]
            )
        )

    def _discard_held_write(self) -> None:
        """Lossless hold: revise/halt drop the draft. Never apply it."""
        self._pending_verify_action = None
        self._pending_claim = ""
        self._pending_draft = ""
        self._pending_verify_risk = "none"

    def _hold_write_for_verify(self, action: WorkerAction, goal: str) -> None:
        path = str((action.args or {}).get("path") or "")
        content = str((action.args or {}).get("content") or "")
        self._pending_verify_action = action
        self._pending_claim = build_verify_claim(
            goal=goal,
            what=f"will write {path or 'a file'} (draft held until accept)",
        )
        self._pending_draft = draft_diff(path, content, root=self.root)
        self._pending_verify_risk = "write"

    def _apply_held_write(self, result: RunResult) -> None:
        held = self._pending_verify_action
        self._pending_verify_action = None
        if held is None or held.kind != "tool" or (held.tool or "") != "write":
            return
        ask, _reason, _risk = needs_ask(held.tool or "", held.args)
        if ask:
            return
        tr = execute(held.tool or "", held.args, root=self.root)
        result.history.append({"action": held.describe(), "result": tr.short()})
        self._emit(
            result,
            EVENT_WORK,
            f"{held.describe()} → {tr.short()}",
            state=State.ACT.value,
            observation=tr.short(),
        )

    def _speculate_readonly(
        self,
        result: RunResult,
        action: Optional[WorkerAction],
        *,
        waiting: str,
    ) -> None:
        """While waiting for verify/ask: only read / git-ro. Never a write."""
        if action is None or action.kind != "tool":
            return
        tool = action.tool or ""
        args = action.args or {}
        if not is_readonly_speculate(tool, args):
            return
        tr = execute(tool, args, root=self.root)
        result.history.append(
            {"action": f"speculate {action.describe()}", "result": tr.short()}
        )
        self._emit(
            result,
            EVENT_WORK,
            f"speculative {action.describe()} ({waiting}) → {tr.short()}",
            state=waiting,
            observation=tr.short(),
        )

    def _emit_lesson(self, result: RunResult, instruction: str, *, about: str = "") -> None:
        """Reflexion: one-line lesson on the same traj retrieve can pick up."""
        text = "lesson: " + ((about + ": ") if about else "")
        text += " ".join(str(instruction).split())
        if len(text) > 240:
            text = text[:237] + "..."
        self._emit(
            result,
            EVENT_LESSON,
            text,
            instruction=instruction,
            state=State.ACT.value,
        )

    def _is_escalate_reason(self, reason: str) -> bool:
        return reason in {
            ESCALATE_TOOL_FAILS_TWICE,
            ESCALATE_GIT_OR_REMOTE,
            ESCALATE_UNSURE,
            ESCALATE_USER_REVIEW,
        }

    def _self_verify(self, result: RunResult, action: WorkerAction, goal: str):
        """Score locally before spending verify/ask. Never calls the coach."""
        score = worker_score_claim(self.worker, action, goal=goal)
        self._emit(
            result,
            EVENT_SELF_VERIFY,
            f"self-verify p={score.p:.2f} {score.reason}",
            state=State.ACT.value,
            extra={
                "p": score.p,
                "self_verify_reason": score.reason,
                "hopeless": score.hopeless,
                "critic": score.critic,
            },
        )
        return score

    def _stay_local_low_p(
        self,
        result: RunResult,
        score,
        *,
        escalate_reason: str = "",
    ) -> str:
        """Very-low p: do not spend coach unless this is already a HIGH escalate."""
        if self._low_p_streak >= 2 and self._is_escalate_reason(escalate_reason):
            return State.ASK.value
        twice = self._low_p_streak >= 2
        msg = (
            "self-verify p very low twice, not an escalate — do not spend coach"
            if twice
            else "self-verify p very low — stay local, do not spend coach"
        )
        self._emit(
            result,
            EVENT_THOUGHT,
            msg + f" (p={score.p:.2f})",
            state=State.ACT.value,
        )
        return State.ACT.value

    def _store_accepted_demo(
        self,
        result: RunResult,
        *,
        goal: str,
        path: str,
        claim: str,
        draft: str,
    ) -> None:
        rec = compact_demo(goal=goal, claim=claim, path=path, draft=draft)
        stored = store_demo(self.demo_path, rec)
        self._last_write_path = path
        self._emit(
            result,
            EVENT_DEMO,
            f"cached local demo {path}",
            state=State.ACT.value,
            extra={"demo": stored},
        )

    def _traj_entries(self, result: RunResult) -> list:
        return self.traj.entries if self.traj is not None else result.events

    def _cal_table(self, result: RunResult):
        return rolling_table(self._traj_entries(result))

    def _rolling_accept_rate(self, result: RunResult) -> Optional[float]:
        entries = self._traj_entries(result)
        recent: list[str] = []
        for ev in entries:
            if ev.get("kind") != EVENT_COACH_VERDICT:
                continue
            reply = ev.get("reply")
            if isinstance(reply, dict) and reply.get("verdict"):
                recent.append(str(reply["verdict"]))
        if not recent:
            recent = [v for v in result.verify_verdicts if v]
        recent = recent[-4:]
        if len(recent) < 2:
            return None
        accepts = sum(1 for v in recent if v == "accept")
        return accepts / len(recent)

    def _high_risk_continue(self, verdict: str, reason: str) -> bool:
        """HIGH apply continue whose escalate was git mutate / remote / push."""
        if str(verdict or "") != "continue":
            return False
        return reason == ESCALATE_GIT_OR_REMOTE

    def _should_confirm(self, verdict: str, reason: str) -> bool:
        if not self._high_risk_continue(verdict, reason):
            return False
        if self.confirm is not None:
            return True
        return bool(self.require_confirm)

    def _confirm_prompt(
        self,
        action: Optional[WorkerAction],
        instruction: str,
        reason: str,
        risk: str,
    ) -> str:
        what = action.describe() if action is not None else (reason or "git/remote")
        ins = " ".join(str(instruction or "").split())
        if len(ins) > 160:
            ins = ins[:157] + "..."
        return (
            "待确认：教练建议 continue，但升级原因是高风险 "
            f"({reason or ESCALATE_GIT_OR_REMOTE} / {risk or 'push'}): {what}. "
            f"指示：{ins or '(无)'}。"
            "同意则按指示回到干活（仍不自动执行被拦住的 remote）；"
            "拒绝则当作 halt，不执行 git/remote。"
        )

    def _ask_confirm(self, prompt: str) -> bool:
        fn = self.confirm if self.confirm is not None else tty_confirm
        return bool(fn(prompt))

    def _handle_act(
        self,
        result: RunResult,
        goal: str,
        action: WorkerAction,
        failed_logs: list[str],
        fail_streak: int,
        *,
        from_idle: bool = False,
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
        is_write = action.kind == "tool" and (action.tool or "") == "write"
        # Router: tool kind first (read=local, write=verify). Not raw confidence.
        if is_write and not from_idle:
            path = str((action.args or {}).get("path") or "")
            content = str((action.args or {}).get("content") or "")
            score = self._self_verify(result, action, goal)
            if score.high and score.critic is True:
                self._low_p_streak = 0
                tr = execute(action.tool or "", action.args, root=self.root)
                result.history.append(
                    {"action": action.describe(), "result": tr.short()}
                )
                self._emit(
                    result,
                    EVENT_WORK,
                    f"critic-ok {action.describe()} → {tr.short()}",
                    state=State.ACT.value,
                    observation=tr.short(),
                )
                return State.ACT.value, 0, pending, "", "none"
            if score.very_low:
                self._low_p_streak += 1
                nxt = self._stay_local_low_p(result, score)
                if nxt == State.ASK.value:
                    pending_reason = ESCALATE_UNSURE
                    pending_risk = "write"
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
            self._low_p_streak = 0
            raw_conf = resolve_confidence(action)
            table = self._cal_table(result)
            p_cal = table.lookup(action)
            long_disagree = table.disagree_window(self._traj_entries(result))
            rate = self._rolling_accept_rate(result)
            if rate is not None and rate < 0.5:
                # tax <0.5. If calibrated P and raw conf disagree for a long
                # window, do not invent HIGH — only ask when an existing
                # HIGH rule already matches.
                if long_disagree:
                    self._emit(
                        result,
                        EVENT_THOUGHT,
                        (
                            "calibrate disagree window — not inventing ask "
                            f"(p={p_cal if p_cal is not None else 'n/a'} "
                            f"conf={raw_conf:.2f})"
                        ),
                        state=State.ACT.value,
                    )
                else:
                    pending_reason = "verify accept rate below 0.5; sending write to ask"
                    pending_risk = "write"
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
            check = local_check_write(path, content)
            if check is True:
                tr = execute(action.tool or "", action.args, root=self.root)
                result.history.append(
                    {"action": action.describe(), "result": tr.short()}
                )
                self._emit(
                    result,
                    EVENT_WORK,
                    f"critic-ok {action.describe()} → {tr.short()}",
                    state=State.ACT.value,
                    observation=tr.short(),
                )
                return State.ACT.value, 0, pending, "", "none"
            # EAGLE-2: trusted table + high P(accept), not git-mutate → skip verify.
            if should_skip_verify(table, action, p=p_cal):
                extra = {
                    "conf": raw_conf,
                    "act": action.describe(),
                    "conf_bucket": conf_bucket(raw_conf),
                    "act_type": act_type_of(action),
                    "p_accept": p_cal,
                    "calibrate": "skip",
                }
                if check is False:
                    # already checked (failed): do not apply; skip the hold.
                    self._emit(
                        result,
                        EVENT_THOUGHT,
                        (
                            f"calibrate-skip hold (critic-fail, not apply) "
                            f"p={p_cal:.2f}"
                        ),
                        state=State.ACT.value,
                        extra=extra,
                    )
                    return State.ACT.value, fail_streak, pending, "", "none"
                tr = execute(action.tool or "", action.args, root=self.root)
                result.history.append(
                    {"action": action.describe(), "result": tr.short()}
                )
                self._emit(
                    result,
                    EVENT_WORK,
                    f"calibrate-skip {action.describe()} → {tr.short()}",
                    state=State.ACT.value,
                    observation=tr.short(),
                    extra=extra,
                )
                return State.ACT.value, 0, pending, "", "none"
            # DSP: do not verify every write once accept-rate is high.
            # Used when the calibration table is not yet trusted, or P < 0.9.
            if rate is not None and rate >= 0.75:
                tr = execute(action.tool or "", action.args, root=self.root)
                result.history.append(
                    {"action": action.describe(), "result": tr.short()}
                )
                self._emit(
                    result,
                    EVENT_WORK,
                    f"dsp-skip {action.describe()} → {tr.short()}",
                    state=State.ACT.value,
                    observation=tr.short(),
                    extra={"conf": raw_conf, "act": action.describe()},
                )
                return State.ACT.value, 0, pending, "", "none"
            self._hold_write_for_verify(action, goal)
            self._emit(
                result,
                EVENT_WORK,
                f"held write {path} for verify",
                state=State.VERIFY.value,
            )
            return State.VERIFY.value, 0, pending, "verify_write", "write"
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
        # MID: one failure, not yet twice. Idle skips verify.
        if from_idle:
            return State.ACT.value, fail_streak, pending, "", "none"
        score = self._self_verify(result, action, goal)
        if score.very_low:
            self._low_p_streak += 1
            nxt = self._stay_local_low_p(result, score)
            if nxt == State.ASK.value:
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
        self._low_p_streak = 0
        table = self._cal_table(result)
        p_cal = table.lookup(action)
        if should_skip_verify(table, action, p=p_cal):
            self._emit(
                result,
                EVENT_THOUGHT,
                (
                    "calibrate-skip verify after one fail "
                    f"(p={p_cal:.2f})"
                ),
                state=State.ACT.value,
                extra={
                    "conf": resolve_confidence(action),
                    "act": action.describe(),
                    "p_accept": p_cal,
                    "calibrate": "skip",
                },
            )
            return State.ACT.value, fail_streak, pending, "", "none"
        self._pending_verify_action = action
        self._pending_claim = build_verify_claim(
            goal=goal,
            what=f"{action.tool or action.kind} failed once",
            reason=tr.short(),
        )
        self._pending_draft = tr.short()
        self._pending_verify_risk = "write" if tr.risk == "write" else "none"
        return State.VERIFY.value, fail_streak, pending, "verify_fail_once", self._pending_verify_risk

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
        self._pending_kind = ""
        self._pending_verify_action = None
        self._pending_claim = ""
        self._pending_draft = ""
        self._pending_verify_risk = "none"
        self._goal = goal
        self._last_write_path = ""
        self._low_p_streak = 0
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
                from_idle = False
                if self._carry_action is not None:
                    action = self._carry_action
                    self._carry_action = None
                    from_idle = True
                else:
                    action = self.worker.step(
                        goal=goal,
                        system=self._system(),
                        history=self._history_for_worker(result)
                        if self.persist
                        else result.history,
                    )
                nxt, fail_streak, pending_action, pending_reason, pending_risk = self._handle_act(
                    result, goal, action, failed_logs, fail_streak, from_idle=from_idle
                )
                if not nxt:
                    break
                state = State(nxt)
                continue

            if state == State.VERIFY:
                cap_reason = self._skip_verify_for_cap(result)
                if cap_reason is not None:
                    used = self._coach_verify_count(result)
                    cap = self.max_verifies
                    self._emit(
                        result,
                        EVENT_THOUGHT,
                        f"达到核对上限（已用 {used}/{cap}），不核对，留在本地",
                        state=State.IDLE.value if self.idle else State.ACT.value,
                    )
                    self._apply_held_write(result)
                    self._pending_claim = ""
                    self._pending_draft = ""
                    if self.idle:
                        state = State.IDLE
                        continue
                    state = State.ACT
                    continue
                ticket = self._verify_ticket(goal)
                result.last_claim = ticket.claim
                held = self._pending_verify_action
                if held and (held.tool or "") == "write":
                    path = str((held.args or {}).get("path") or "")
                    if path:
                        self._speculate_readonly(
                            result,
                            WorkerAction(
                                kind="tool",
                                tool="read",
                                args={"path": path},
                                thought="speculative read while verify waits",
                            ),
                            waiting=State.VERIFY.value,
                        )
                self._emit(
                    result,
                    EVENT_VERIFIED_COACH,
                    "核对中",
                    state=state.value,
                    ticket=ticket.to_dict(),
                )
                reply = self.coach.verify(ticket)
                result.verifies += 1
                result.verify_verdicts.append(reply.verdict)
                result.history.append(
                    {"verify": ticket.to_dict(), "reply": reply.to_dict()}
                )
                result.last_instruction = reply.instruction
                self._pending_reply = reply
                self._pending_kind = "verify"
                state = State.APPLY
                continue

            if state == State.ASK:
                cap_reason = self._skip_ask_for_cap(result)
                if cap_reason is not None:
                    used = self._coach_ask_count(result)
                    cap = self.max_asks
                    self._emit(
                        result,
                        EVENT_THOUGHT,
                        f"达到询问上限（已用 {used}/{cap}），不问教练，留在本地",
                        state=State.IDLE.value if self.idle else state.value,
                    )
                    pending_action = None
                    self._pending_problem = ""
                    if self.idle:
                        state = State.IDLE
                        continue
                    result.done_reason = cap_reason
                    break
                action = pending_action or WorkerAction(kind="unsure", thought="empty")
                ticket = self._ticket(
                    goal, action, failed_logs, pending_reason, pending_risk
                )
                self._reset_backoff()
                self._speculate_readonly(
                    result,
                    WorkerAction(
                        kind="tool",
                        tool="shell",
                        args={"cmd": "git status --porcelain"},
                        thought="speculative git-ro while ask waits",
                    ),
                    waiting=State.ASK.value,
                )
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
                self._pending_kind = "ask"
                state = State.APPLY
                continue

            if state == State.APPLY:
                reply = self._pending_reply
                if reply is None:
                    result.done_reason = "apply-missing-reply"
                    break
                if self._pending_kind == "verify":
                    result.last_instruction = reply.instruction
                    held = self._pending_verify_action
                    held_conf = resolve_confidence(held) if held is not None else None
                    held_act = held.describe() if held is not None else (self._pending_claim or "")
                    cal = {
                        "conf": held_conf,
                        "act": held_act,
                        "verdict": reply.verdict,
                        "conf_bucket": conf_bucket(held_conf) if held_conf is not None else None,
                        "act_type": act_type_of(held) if held is not None else act_type_of(held_act),
                    }
                    self._emit(
                        result,
                        EVENT_COACH_VERDICT,
                        reply.instruction,
                        instruction=reply.instruction,
                        state=state.value,
                        reply=reply.to_dict(),
                        extra=cal,
                    )
                    if reply.verdict == "halt":
                        self._discard_held_write()
                        result.done_reason = "halt: " + reply.instruction
                        break
                    if reply.verdict == "accept":
                        held = self._pending_verify_action
                        claim = self._pending_claim
                        draft = self._pending_draft
                        held_path = ""
                        if held is not None:
                            held_path = str((held.args or {}).get("path") or "")
                        self._apply_held_write(result)
                        if held is not None and (held.tool or "") == "write" and held_path:
                            landed = self.root / held_path if not Path(held_path).is_absolute() else Path(held_path)
                            if landed.is_file():
                                self._store_accepted_demo(
                                    result,
                                    goal=goal,
                                    path=held_path,
                                    claim=claim,
                                    draft=draft,
                                )
                        self._pending_kind = ""
                        self._pending_claim = ""
                        self._pending_draft = ""
                        fail_streak = 0
                        pending_action = None
                        state = State.ACT
                        continue
                    # revise: discard the draft (lossless hold), inject lesson
                    self.coach_instruction = reply.instruction
                    about = ""
                    if self._pending_verify_action is not None:
                        about = str((self._pending_verify_action.args or {}).get("path") or "draft")
                    self._discard_held_write()
                    self._emit_lesson(result, reply.instruction, about=about)
                    self._emit(
                        result,
                        EVENT_RESUMED,
                        "继续：按核对意见回到干活",
                        instruction=reply.instruction,
                        state=State.ACT.value,
                    )
                    self._pending_kind = ""
                    fail_streak = 0
                    pending_action = None
                    state = State.ACT
                    continue
                # HIGH ask apply. Local MUST inject instruction.
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
                if self._should_confirm(reply.verdict, pending_reason):
                    if self.on_state:
                        self.on_state("待确认")
                    prompt = self._confirm_prompt(
                        pending_action,
                        reply.instruction,
                        pending_reason,
                        pending_risk,
                    )
                    if not self._ask_confirm(prompt):
                        # Decline: halt-like. Do not execute the git/remote.
                        # Do not invent a new coach call.
                        self._emit(
                            result,
                            EVENT_USER_DENIED,
                            "用户拒绝高风险继续，不执行 git/remote",
                            instruction=reply.instruction,
                            problem=self.last_problem,
                            state="待确认",
                            extra={
                                "reason": pending_reason,
                                "risk": pending_risk,
                            },
                        )
                        result.done_reason = "halt: user denied"
                        pending_action = None
                        self._pending_problem = ""
                        self._pending_kind = ""
                        break
                if reply.verdict == "revise":
                    self._emit_lesson(result, reply.instruction, about="ask")
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
                self._pending_kind = ""
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
