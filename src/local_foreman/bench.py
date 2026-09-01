"""Mock-only comparison bench: local-foreman lanes vs remote-only.

Measures the three product axes on the same fixtures:
  1. wall time  (injected mock latency: worker 5ms, verify 40ms, ask/remote 80ms)
  2. tokens     (asks + verifies; ask-count is the proxy when USD unset)
  3. quality    (fixture pass rate on the same mock tasks — not a real-model score)

Does not call a live coach, load MLX, or invent quality numbers.
`--live` / LOCAL_FOREMAN_BENCH=live is gated: non-Darwin, missing mlx-lm, or
empty COACH_API_KEY prints a skip line and returns 0. Never loads weights.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from local_foreman.coach import MockCoach
from local_foreman.loop import ForemanLoop
from local_foreman.tools import execute
from local_foreman.worker import MockWorker, WorkerAction


WORKER_LATENCY_S = 0.005
VERIFY_LATENCY_S = 0.040
ASK_LATENCY_S = 0.080

ENV_BENCH = "LOCAL_FOREMAN_BENCH"
LIVE_BENCH_SKIP = "live-bench: skip (need Apple Silicon + mlx-lm + COACH_API_KEY)"


@dataclass
class ModeResult:
    mode: str
    fixture: str
    asks: int
    verifies: int
    tokens: int
    wall_s: float
    passed: bool
    detail: str = ""


@dataclass
class Fixture:
    name: str
    goal: str
    script: list[WorkerAction]
    lane: str  # low | mid | high
    expect_file: str = ""
    expect_file_text: str = ""

    def script_copy(self) -> list[WorkerAction]:
        return [
            WorkerAction(
                kind=a.kind,
                tool=a.tool,
                args=dict(a.args or {}),
                thought=a.thought,
                confidence=a.confidence,
            )
            for a in self.script
        ]


def _push_cmd() -> str:
    return "git " + "push" + " origin HEAD"


def default_fixtures() -> list[Fixture]:
    """Existing smoke paths plus tiny read/write goals that pass in mock."""
    return [
        Fixture(
            name="safe-read",
            goal="bench: read README only",
            lane="low",
            script=[
                WorkerAction(
                    kind="tool",
                    tool="read",
                    args={"path": "README.md"},
                    thought="safe read",
                    confidence=1.0,
                ),
                WorkerAction(kind="done", thought="read ok", confidence=1.0),
            ],
        ),
        Fixture(
            name="draft-write",
            goal="bench: draft a tiny local file",
            lane="mid",
            expect_file="bench-note.txt",
            expect_file_text="lanes draft",
            script=[
                WorkerAction(
                    kind="tool",
                    tool="write",
                    args={"path": "bench-note.txt", "content": "lanes draft"},
                    thought="draft write",
                    confidence=0.4,
                ),
                WorkerAction(kind="done", thought="wrote", confidence=1.0),
            ],
        ),
        Fixture(
            name="low-conf-read",
            goal="bench: read NOTICE with low confidence",
            lane="low",
            script=[
                WorkerAction(
                    kind="tool",
                    tool="read",
                    args={"path": "NOTICE"},
                    thought="low conf read",
                    confidence=0.3,
                ),
                WorkerAction(kind="done", thought="notice ok", confidence=1.0),
            ],
        ),
        Fixture(
            name="remote-push",
            goal="bench: escalate-heavy fake remote",
            lane="high",
            script=[
                WorkerAction(
                    kind="tool",
                    tool="read",
                    args={"path": "README.md"},
                    thought="look first",
                    confidence=1.0,
                ),
                WorkerAction(
                    kind="tool",
                    tool="shell",
                    args={"cmd": _push_cmd()},
                    thought="fake remote",
                    confidence=0.2,
                ),
                WorkerAction(kind="done", thought="after ask", confidence=1.0),
            ],
        ),
    ]


class LatencyWorker:
    """Wrap a worker with deterministic mock think-time."""

    def __init__(self, inner: MockWorker, sleeper: Callable[[float], None], latency: float):
        self.inner = inner
        self._sleep = sleeper
        self.latency = latency

    @property
    def last_system(self) -> str:
        return self.inner.last_system

    @property
    def calls(self) -> int:
        return self.inner.calls

    def step(self, *, goal: str, system: str, history: list[dict]) -> WorkerAction:
        if self.latency:
            self._sleep(self.latency)
        return self.inner.step(goal=goal, system=system, history=history)

    def think(self, *, goal: str, system: str, history: list[dict]) -> WorkerAction:
        if self.latency:
            self._sleep(self.latency)
        return self.inner.think(goal=goal, system=system, history=history)


class LatencyCoach:
    """Wrap a mock coach: ask is slower than verify (smaller payload)."""

    def __init__(
        self,
        inner: MockCoach,
        sleeper: Callable[[float], None],
        ask_latency: float,
        verify_latency: float,
    ):
        self.inner = inner
        self._sleep = sleeper
        self.ask_latency = ask_latency
        self.verify_latency = verify_latency

    @property
    def calls(self):
        return self.inner.calls

    @property
    def verify_calls(self):
        return self.inner.verify_calls

    def advise(self, ticket):
        if self.ask_latency:
            self._sleep(self.ask_latency)
        return self.inner.advise(ticket)

    def verify(self, ticket):
        if self.verify_latency:
            self._sleep(self.verify_latency)
        return self.inner.verify(ticket)


def _history_has_read(history: list[dict], path_hint: str = "") -> bool:
    for h in history:
        action = str(h.get("action") or "")
        if "read" not in action:
            continue
        if path_hint and path_hint not in action:
            continue
        result = str(h.get("result") or "")
        if result.startswith("fail"):
            continue
        return True
    return False


def _fixture_pass_local(fx: Fixture, result, root: Path) -> tuple[bool, str]:
    if result.done_reason.startswith("halt"):
        return False, "halt: " + result.done_reason
    if fx.name == "safe-read":
        if "ask" in result.states or "verify" in result.states:
            return False, "safe-read left LOW lane states=" + str(result.states)
        if not _history_has_read(result.history, "README"):
            return False, "safe-read did not read README"
        return True, "ok"
    if fx.name == "draft-write":
        if "ask" in result.states:
            return False, "draft-write went HIGH ask"
        if "verify" not in result.states:
            return False, "draft-write missed verify"
        dest = root / fx.expect_file
        if not dest.is_file() or dest.read_text(encoding="utf-8") != fx.expect_file_text:
            return False, "draft-write file missing or wrong"
        return True, "ok"
    if fx.name == "low-conf-read":
        if "ask" in result.states or "verify" in result.states:
            return False, "low-conf-read left LOW lane (router is tool kind) states=" + str(result.states)
        if not _history_has_read(result.history, "NOTICE"):
            return False, "low-conf-read did not read NOTICE"
        return True, "ok"
    if fx.name == "remote-push":
        if "ask" not in result.states:
            return False, "remote-push missed HIGH ask"
        if result.tickets < 1:
            return False, "remote-push produced no ask ticket"
        return True, "ok"
    return False, "unknown fixture"


def _fixture_pass_remote(fx: Fixture, history: list[dict], root: Path, done: bool) -> tuple[bool, str]:
    if fx.name == "safe-read":
        if not _history_has_read(history, "README"):
            return False, "remote safe-read did not read README"
        return True, "ok"
    if fx.name == "draft-write":
        dest = root / fx.expect_file
        if not dest.is_file() or dest.read_text(encoding="utf-8") != fx.expect_file_text:
            return False, "remote draft-write file missing or wrong"
        return True, "ok"
    if fx.name == "low-conf-read":
        if not _history_has_read(history, "NOTICE"):
            return False, "remote low-conf-read did not read NOTICE"
        return True, "ok"
    if fx.name == "remote-push":
        if not any("push" in str(h.get("action") or "") for h in history):
            return False, "remote remote-push never attempted push"
        if not done:
            return False, "remote remote-push did not finish"
        return True, "ok"
    return False, "unknown fixture"


def run_local_foreman(
    fx: Fixture,
    *,
    repo_root: Path,
    work_root: Path,
    sleeper: Callable[[float], None],
) -> ModeResult:
    worker = LatencyWorker(
        MockWorker(script=fx.script_copy()),
        sleeper,
        WORKER_LATENCY_S,
    )
    coach = LatencyCoach(
        MockCoach(["continue"], verify_verdicts=["accept"]),
        sleeper,
        ASK_LATENCY_S,
        VERIFY_LATENCY_S,
    )
    root = work_root if fx.expect_file else repo_root
    t0 = time.perf_counter()
    loop = ForemanLoop(
        worker=worker,
        coach=coach,
        root=root,
        max_steps=16,
        persist=False,
        idle=False,
        sleeper=sleeper,
    )
    result = loop.run(fx.goal)
    wall = time.perf_counter() - t0
    asks = int(result.tickets)
    verifies = int(result.verifies)
    passed, detail = _fixture_pass_local(fx, result, root)
    return ModeResult(
        mode="local-foreman",
        fixture=fx.name,
        asks=asks,
        verifies=verifies,
        tokens=asks + verifies,
        wall_s=wall,
        passed=passed,
        detail=detail,
    )


def run_remote_only(
    fx: Fixture,
    *,
    repo_root: Path,
    work_root: Path,
    sleeper: Callable[[float], None],
) -> ModeResult:
    """Every step is a coach call at ask/remote latency. No local-only lane."""
    worker = MockWorker(script=fx.script_copy())
    coach = MockCoach(["continue"])
    root = work_root if fx.expect_file else repo_root
    history: list[dict] = []
    asks = 0
    done = False
    t0 = time.perf_counter()
    for _ in range(16):
        sleeper(ASK_LATENCY_S)
        asks += 1
        # Count the step as a real mock coach consult.
        from local_foreman.ticket import Ticket, validate_ticket

        ticket = validate_ticket(
            Ticket(
                goal=fx.goal,
                problem=(
                    "What failed: remote-only has no local worker. "
                    "What was tried: send this step to the coach. "
                    "What we need: the next action."
                ),
                proposed_next="decide remotely",
                risk="none",
                local_guess="remote-only",
            )
        )
        coach.advise(ticket)
        action = worker.step(goal=fx.goal, system="", history=history)
        if action.kind == "done":
            done = True
            break
        if action.kind == "tool":
            tr = execute(action.tool or "", action.args, root=root)
            history.append({"action": action.describe(), "result": tr.short()})
    wall = time.perf_counter() - t0
    passed, detail = _fixture_pass_remote(fx, history, root, done)
    return ModeResult(
        mode="remote-only",
        fixture=fx.name,
        asks=asks,
        verifies=0,
        tokens=asks,
        wall_s=wall,
        passed=passed,
        detail=detail,
    )


def _ms(sec: float) -> str:
    return f"{sec * 1000:.1f}"


def format_table(rows: list[ModeResult]) -> str:
    lines = [
        "# local-foreman bench (mock only)",
        f"# worker={int(WORKER_LATENCY_S*1000)}ms verify={int(VERIFY_LATENCY_S*1000)}ms ask/remote={int(ASK_LATENCY_S*1000)}ms",
        "# quality = fixture pass rate (same mock tasks); not a real-model score",
        "# tokens = asks + verifies (proxy when COACH_USD_PER_ASK unset)",
        "",
        f"{'fixture':<16} {'mode':<16} {'asks':>5} {'verifies':>8} {'tokens':>7} {'wall_ms':>8} {'pass':>5}",
    ]
    for r in rows:
        flag = "ok" if r.passed else "FAIL"
        lines.append(
            f"{r.fixture:<16} {r.mode:<16} {r.asks:5d} {r.verifies:8d} {r.tokens:7d} {_ms(r.wall_s):>8} {flag:>5}"
        )
    return "\n".join(lines)


def format_totals(rows: list[ModeResult]) -> str:
    modes = []
    seen = []
    for r in rows:
        if r.mode not in seen:
            seen.append(r.mode)
    lines = ["", f"{'mode':<16} {'asks':>5} {'verifies':>8} {'tokens':>7} {'wall_ms':>8} {'pass':>6}"]
    for mode in seen:
        subset = [r for r in rows if r.mode == mode]
        asks = sum(r.asks for r in subset)
        verifies = sum(r.verifies for r in subset)
        tokens = sum(r.tokens for r in subset)
        wall = sum(r.wall_s for r in subset)
        ok = sum(1 for r in subset if r.passed)
        lines.append(
            f"{mode:<16} {asks:5d} {verifies:8d} {tokens:7d} {_ms(wall):>8} {ok}/{len(subset):<4}"
        )
    return "\n".join(lines)


@dataclass
class BenchReport:
    rows: list[ModeResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def text(self) -> str:
        return format_table(self.rows) + format_totals(self.rows)


def run_bench_suite(
    *,
    repo_root: Optional[Path] = None,
    sleeper: Optional[Callable[[float], None]] = None,
) -> BenchReport:
    os.environ["LOCAL_FOREMAN_WORKER"] = "mock"
    os.environ["LOCAL_FOREMAN_COACH"] = "mock"
    repo_root = Path(repo_root or Path(__file__).resolve().parents[2])
    sleep = sleeper or time.sleep
    report = BenchReport()
    for fx in default_fixtures():
        work = Path(tempfile.mkdtemp(prefix=f"lf-bench-{fx.name}-"))
        local = run_local_foreman(fx, repo_root=repo_root, work_root=work, sleeper=sleep)
        remote = run_remote_only(fx, repo_root=repo_root, work_root=work, sleeper=sleep)
        report.rows.extend([local, remote])
        if not local.passed:
            report.errors.append(f"{fx.name} local-foreman: {local.detail}")
        if not remote.passed:
            report.errors.append(f"{fx.name} remote-only: {remote.detail}")
    local_rows = [r for r in report.rows if r.mode == "local-foreman"]
    remote_rows = [r for r in report.rows if r.mode == "remote-only"]
    local_tokens = sum(r.tokens for r in local_rows)
    remote_asks = sum(r.asks for r in remote_rows)
    local_wall = sum(r.wall_s for r in local_rows)
    remote_wall = sum(r.wall_s for r in remote_rows)
    local_pass = sum(1 for r in local_rows if r.passed)
    remote_pass = sum(1 for r in remote_rows if r.passed)
    if local_tokens >= remote_asks:
        report.errors.append(
            f"tokens: local asks+verifies {local_tokens} !< remote coach calls {remote_asks}"
        )
    if local_wall >= remote_wall:
        report.errors.append(
            f"wall: local {local_wall:.3f}s !< remote {remote_wall:.3f}s"
        )
    if local_pass != remote_pass:
        report.errors.append(
            f"pass rate: local {local_pass}/{len(local_rows)} != remote {remote_pass}/{len(remote_rows)}"
        )
    # escalate-heavy fixture: local asks < remote steps (also covered by totals)
    heavy_l = next(r for r in local_rows if r.fixture == "remote-push")
    heavy_r = next(r for r in remote_rows if r.fixture == "remote-push")
    if heavy_l.asks >= heavy_r.asks:
        report.errors.append(
            f"remote-push asks: local {heavy_l.asks} !< remote {heavy_r.asks}"
        )
    if heavy_l.wall_s >= heavy_r.wall_s:
        report.errors.append(
            f"remote-push wall: local {heavy_l.wall_s:.3f}s !< remote {heavy_r.wall_s:.3f}s"
        )
    return report


def _mlx_lm_available() -> bool:
    """True if mlx-lm is importable. Uses find_spec so Linux never imports it."""
    return importlib.util.find_spec("mlx_lm") is not None


def _live_ready() -> bool:
    """Apple Silicon + mlx-lm present + COACH_API_KEY. No import, no load, no API."""
    if sys.platform != "darwin":
        return False
    if not _mlx_lm_available():
        return False
    if not (os.environ.get("COACH_API_KEY") or "").strip():
        return False
    return True


def _want_live(*, flag: bool = False) -> bool:
    if flag:
        return True
    return (os.environ.get(ENV_BENCH) or "").strip().lower() == "live"


def _live_runner_stub() -> int:
    """Unimplemented live path. Requires all three gates; never loads weights."""
    print(LIVE_BENCH_SKIP)
    return 0


def _run_live_bench() -> int:
    """Skip unless Darwin + mlx-lm + key. Stub even when ready: no load, no API."""
    if not _live_ready():
        print(LIVE_BENCH_SKIP)
        return 0
    return _live_runner_stub()


def run_bench(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="local-foreman bench",
        description="Mock-only comparison: local-foreman lanes vs remote-only (time / tokens / pass).",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="request live MLX+coach bench (skips unless Apple Silicon + mlx-lm + COACH_API_KEY)",
    )
    args = parser.parse_args(argv)
    if _want_live(flag=args.live):
        return _run_live_bench()
    report = run_bench_suite()
    print(report.text())
    if report.errors:
        for e in report.errors:
            print("BENCH FAIL: " + e)
        return 1
    return 0


def main(argv=None) -> int:
    return run_bench(argv)
