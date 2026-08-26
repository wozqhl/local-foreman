"""CLI: python -m local_foreman "goal" | local-foreman ui | traj | bench | --smoke

One-shot stays task-driven unless --persist / LOCAL_FOREMAN_PERSIST=1.
`ui` defaults persist+idle ON. Idle think never calls the coach.
`traj` tails/cats/exports the same jsonl the loop writes.
`traj --stats` tallies ask / coach replies on that file (idle thoughts do not count).
LOCAL_FOREMAN_MAX_ASKS hard-caps asked_coach (unset = no cap).
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from local_foreman.coach import MockCoach
from local_foreman.demo import (
    DEMO_CONTEXT_HEADER,
    compact_demo,
    default_demo_path,
    load_demos,
)
from local_foreman.loop import (
    COACH_INSTRUCTION_HEADER,
    ENV_PERSIST,
    RETRIEVED_CONTEXT_HEADER,
    ForemanLoop,
    env_flag,
)
from local_foreman.self_verify import score_pending_claim
from local_foreman.ticket import problem_is_clear
from local_foreman.traj import (
    Trajectory,
    coach_stats,
    compact_entries,
    default_traj_path,
    format_coach_stats,
    format_entry,
    parse_kinds,
    retrieve,
    retrieve_from_summary,
    select_entries,
    write_jsonl,
)
from local_foreman.worker import MockWorker, WorkerAction


def _root() -> Path:
    env = os.environ.get("LOCAL_FOREMAN_ROOT")
    return Path(env) if env else Path.cwd()


def run_goal(
    goal: str,
    *,
    user_review: bool = False,
    max_steps: int = 12,
    persist: Optional[bool] = None,
) -> int:
    def on_state(name: str) -> None:
        print(name, flush=True)

    if persist is None:
        persist = env_flag(ENV_PERSIST)
    loop = ForemanLoop(
        root=_root(),
        user_review=user_review,
        max_steps=max_steps,
        on_state=on_state,
        persist=persist,
        idle=persist,
    )
    result = loop.run(goal)
    print("done=" + result.done_reason)
    print("states=" + " > ".join(result.states))
    if result.last_problem:
        print("problem=" + result.last_problem)
    if result.verdicts:
        print("verdicts=" + ",".join(result.verdicts))
    if result.last_instruction:
        print("instruction=" + result.last_instruction)
    if result.done_reason.startswith("halt"):
        return 1
    return 0


def run_ui(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="local-foreman ui",
        description="本机看板：干活中 / 求助中（正在咨询大模型） / 已收到指示 / 继续 / 空转中 / 自己在想 / 展开原文 / 空转动手",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("LOCAL_FOREMAN_UI_HOST", "127.0.0.1"),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("LOCAL_FOREMAN_UI_PORT", "8765")),
    )
    parser.add_argument("--no-demo", action="store_true", help="打开页面但不自动跑 mock 演示")
    args = parser.parse_args(argv)
    from local_foreman.ui import serve_forever

    return serve_forever(
        host=args.host,
        port=args.port,
        root=_root(),
        auto_demo=not args.no_demo,
    )


def run_traj(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="local-foreman traj",
        description="Inspect the same append-only traj jsonl the loop writes. No second log. --stats tallies coach asks on that file.",
    )
    parser.add_argument(
        "path",
        nargs="?",
        help="jsonl path (default: $LOCAL_FOREMAN_TRAJ or <cwd>/.local-foreman/traj.jsonl)",
    )
    parser.add_argument(
        "--last",
        nargs="?",
        const=20,
        type=int,
        metavar="N",
        help="only the last N matching events (N defaults to 20)",
    )
    parser.add_argument(
        "--kind",
        default="",
        help="comma-separated kinds, e.g. thought,idle_act,retrieved",
    )
    parser.add_argument(
        "--out",
        metavar="PATH",
        help="export matching rows as the same jsonl",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="print ask / coach-reply tally for this jsonl (idle thoughts do not count; shows max_asks when LOCAL_FOREMAN_MAX_ASKS is set)",
    )
    args = parser.parse_args(argv)

    path = Path(args.path) if args.path else default_traj_path(_root())
    kinds = parse_kinds(args.kind)
    loaded = Trajectory(path)
    selected = select_entries(loaded.entries, last=args.last, kinds=kinds)

    if args.out:
        dest = Path(args.out)
        try:
            if path.is_file() and dest.resolve() == path.resolve():
                print("traj: --out will not rewrite the live jsonl", file=sys.stderr)
                return 2
        except OSError:
            pass
        write_jsonl(selected, dest)

    if args.stats:
        print(format_coach_stats(coach_stats(loaded.entries)))
        return 0

    for ev in selected:
        print(format_entry(ev))
    return 0


def _smoke_problem(ask_res, errors: list[str]) -> None:
    tickets = [h["ticket"] for h in ask_res.history if isinstance(h, dict) and "ticket" in h]
    if not tickets:
        errors.append("problem-ok: no ticket")
        return
    problem = str(tickets[0].get("problem") or "")
    if not problem_is_clear(problem):
        errors.append("problem-ok: ticket.problem is not a clear statement: " + problem[:160])
        return
    if not ask_res.last_problem or ask_res.last_problem != problem:
        errors.append("problem-ok: last_problem missing or mismatched")
        return
    kinds = [e.get("kind") for e in ask_res.events]
    # ask path: blocked push (stuck) → asked_coach → instruction → resumed → done (work)
    missing = [k for k in ("stuck", "asked_coach", "coach_instruction", "resumed") if k not in kinds]
    if missing:
        errors.append("problem-ok: event log missing " + ",".join(missing) + " got=" + str(kinds))
        return
    print("problem-ok")


def _smoke_ui(root: Path, errors: list[str]) -> None:
    from local_foreman.ui import start_ui, stop_ui

    httpd = None
    try:
        httpd, _board, port = start_ui(host="127.0.0.1", port=0, root=root)
        base = f"http://127.0.0.1:{port}"
        req = urllib.request.Request(base + "/")
        with urllib.request.urlopen(req, timeout=5) as resp:
            ctype = resp.headers.get("Content-Type", "")
            body = resp.read().decode("utf-8", errors="replace")
        if "text/html" not in ctype or "<html" not in body.lower():
            errors.append("ui-ok: GET / is not HTML")
            return
        if "求助中（正在咨询大模型）" not in body:
            errors.append("ui-ok: HTML missing consult copy")
            return
        if "展开原文" not in body or "空转动手" not in body:
            errors.append("ui-ok: HTML missing retrieved/idle-act copy")
            return
        if "下载轨迹" not in body or 'href="/traj"' not in body:
            errors.append("ui-ok: HTML missing traj download")
            return
        with urllib.request.urlopen(base + "/demo?sync=1", timeout=8) as resp:
            demo = json.loads(resp.read().decode("utf-8"))
        with urllib.request.urlopen(base + "/state", timeout=5) as resp:
            snap = json.loads(resp.read().decode("utf-8"))
        events = (snap.get("events") or demo.get("events") or [])
        kinds = [e.get("kind") for e in events]
        consult = any(
            k == "asked_coach"
            or "咨询" in str(e.get("message") or "")
            or k == "stuck"
            for e, k in ((e, e.get("kind")) for e in events)
        )
        if not consult:
            errors.append("ui-ok: no ask/consult event observable kinds=" + str(kinds))
            return
        with urllib.request.urlopen(base + "/traj", timeout=5) as resp:
            if resp.status != 200:
                errors.append("ui-ok: GET /traj not 200")
                return
        print("ui-ok")
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        errors.append("ui-ok: " + str(exc))
    finally:
        if httpd is not None:
            stop_ui(httpd)



def _smoke_traj(root: Path, errors: list[str]) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="lf-traj-"))
    path = tmp / "traj.jsonl"
    push_cmd = "git " + "push" + " origin HEAD"
    worker = MockWorker(script=[
        WorkerAction(kind="tool", tool="read", args={"path": "README.md"}, thought="safe read"),
        WorkerAction(kind="tool", tool="shell", args={"cmd": push_cmd}, thought="fake remote"),
        WorkerAction(kind="done", thought="after persist"),
    ])
    coach = MockCoach(["continue"])
    loop = ForemanLoop(
        worker=worker,
        coach=coach,
        root=root,
        max_steps=10,
        persist=True,
        idle=False,
        traj_path=path,
    )
    res = loop.run("smoke: persist traj")
    if not path.is_file():
        errors.append("traj-ok: jsonl not written")
        return
    loaded = Trajectory(path)
    if not loaded.entries:
        errors.append("traj-ok: jsonl empty after restart load")
        return
    kinds = [e.get("kind") for e in loaded.entries]
    need = ("work", "stuck", "asked_coach", "coach_instruction", "resumed")
    missing = [k for k in need if k not in kinds]
    if missing:
        errors.append("traj-ok: missing " + ",".join(missing) + " got=" + str(kinds))
        return
    has_goal = any(e.get("goal") == "smoke: persist traj" for e in loaded.entries)
    has_ticket = any(isinstance(e.get("ticket"), dict) for e in loaded.entries)
    has_reply = any(isinstance(e.get("reply"), dict) for e in loaded.entries)
    has_obs = any(e.get("observation") for e in loaded.entries)
    if not (has_goal and has_ticket and has_reply and has_obs):
        errors.append(
            "traj-ok: incomplete fields goal/ticket/reply/observation "
            + str((has_goal, has_ticket, has_reply, has_obs))
        )
        return
    if res.tickets < 1:
        errors.append("traj-ok: expected a coach ticket on the persist path")
        return
    # same source: in-memory events match disk
    disk_kinds = [e.get("kind") for e in loaded.entries[-len(res.events):]]
    mem_kinds = [e.get("kind") for e in res.events]
    if disk_kinds != mem_kinds:
        errors.append("traj-ok: disk/memory kinds diverge " + str(disk_kinds) + " vs " + str(mem_kinds))
        return
    print("traj-ok")


def _smoke_idle(root: Path, errors: list[str]) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="lf-idle-"))
    path = tmp / "traj.jsonl"
    worker = MockWorker(script=[
        WorkerAction(kind="tool", tool="read", args={"path": "README.md"}, thought="safe read"),
        WorkerAction(kind="done", thought="idle after work"),
    ])
    coach = MockCoach(["halt"])
    slept: list[float] = []

    def sleeper(sec: float) -> None:
        slept.append(sec)

    loop = ForemanLoop(
        worker=worker,
        coach=coach,
        root=root,
        max_steps=12,
        persist=True,
        idle=True,
        idle_start=1.0,
        idle_cap=8.0,
        idle_max=3,
        traj_path=path,
        sleeper=sleeper,
    )
    res = loop.run("smoke: idle think")
    if coach.calls:
        errors.append("idle-ok: idle path called coach " + str(len(coach.calls)))
        return
    if "ask" in res.states or "apply" in res.states:
        errors.append("idle-ok: idle leaked to coach states=" + str(res.states))
        return
    if "idle" not in res.states:
        errors.append("idle-ok: never entered idle states=" + str(res.states))
        return
    thoughts = [e for e in res.events if e.get("kind") == "thought"]
    if len(thoughts) < 3:
        errors.append("idle-ok: expected 3 thoughts got " + str(len(thoughts)))
        return
    if res.idle_intervals != [1.0, 2.0, 4.0]:
        errors.append("idle-ok: backoff intervals=" + str(res.idle_intervals))
        return
    if not path.is_file():
        errors.append("idle-ok: traj missing")
        return
    loaded = Trajectory(path)
    if not any(e.get("kind") == "thought" for e in loaded.entries):
        errors.append("idle-ok: thought not persisted")
        return
    # New goal resets backoff
    loop2 = ForemanLoop(
        worker=MockWorker(script=[WorkerAction(kind="done", thought="second goal")]),
        coach=coach,
        root=root,
        max_steps=6,
        persist=True,
        idle=True,
        idle_start=1.0,
        idle_cap=8.0,
        idle_max=1,
        traj_path=path,
        sleeper=lambda _s: None,
    )
    res2 = loop2.run("smoke: new goal resets backoff")
    if res2.idle_intervals[:1] != [1.0]:
        errors.append("idle-ok: new goal did not reset backoff " + str(res2.idle_intervals))
        return
    if coach.calls:
        errors.append("idle-ok: second goal called coach")
        return
    print("idle-ok")


def _smoke_compact(errors: list[str]) -> None:
    entries = [
        {"kind": "work", "message": f"step-{i}", "seq": i}
        for i in range(30)
    ]
    out = compact_entries(entries, recent=4, layer=4)
    if not out:
        errors.append("compact-ok: empty compact")
        return
    verbatim = [x for x in out if x.get("role") == "verbatim"]
    summaries = [x for x in out if x.get("role") == "summary" or x.get("kind") == "summary"]
    if len(verbatim) != 4:
        errors.append("compact-ok: expected 4 recent verbatim got " + str(len(verbatim)))
        return
    vmsgs = [str(x.get("message") or "") for x in verbatim]
    if vmsgs != ["step-26", "step-27", "step-28", "step-29"]:
        errors.append("compact-ok: recent not verbatim " + str(vmsgs))
        return
    if not summaries:
        errors.append("compact-ok: older entries were not summarized")
        return
    # older standalone verbatim must not include the oldest line
    if any(x.get("message") == "step-0" for x in verbatim):
        errors.append("compact-ok: oldest entry still verbatim")
        return
    joined = " ".join(str(s.get("message") or "") for s in summaries)
    if "summarized" not in joined:
        errors.append("compact-ok: summary missing extractive marker")
        return
    if "step-0" not in joined:
        errors.append("compact-ok: extractive summary dropped older text")
        return
    # do not invent: summary text is built from existing kind/message only
    if "remembered" in joined.lower() or "invented" in joined.lower():
        errors.append("compact-ok: summary invented extra words")
        return
    print("compact-ok")


def _smoke_retrieve(root: Path, errors: list[str]) -> None:
    entries = [
        {"kind": "work", "message": f"step-{i}", "seq": i}
        for i in range(30)
    ]
    compacted = compact_entries(entries, recent=4, layer=4)
    summaries = [
        x for x in compacted if x.get("role") == "summary" or x.get("kind") == "summary"
    ]
    if not summaries:
        errors.append("retrieve-ok: no summary to expand")
        return
    oldest = summaries[0]
    try:
        first = int(oldest["first_seq"])
        last = int(oldest["last_seq"])
    except (KeyError, TypeError, ValueError):
        errors.append("retrieve-ok: summary missing first_seq/last_seq")
        return
    raw = retrieve(entries, oldest)
    expected = [e for e in entries if first <= int(e["seq"]) <= last]
    if not raw:
        errors.append("retrieve-ok: expand returned empty")
        return
    if [e.get("seq") for e in raw] != [e.get("seq") for e in expected]:
        errors.append("retrieve-ok: seq mismatch " + str([e.get("seq") for e in raw]))
        return
    if [e.get("message") for e in raw] != [e.get("message") for e in expected]:
        errors.append("retrieve-ok: raw messages diverge from jsonl")
        return
    if any(e.get("role") == "summary" or e.get("kind") == "summary" for e in raw):
        errors.append("retrieve-ok: expand returned summaries, not raw jsonl")
        return
    newest = summaries[-1]
    raw_n = retrieve_from_summary(newest, entries)
    try:
        nf, nl = int(newest["first_seq"]), int(newest["last_seq"])
    except (KeyError, TypeError, ValueError):
        errors.append("retrieve-ok: newest summary missing seq span")
        return
    exp_n = [e for e in entries if nf <= int(e["seq"]) <= nl]
    if [e.get("message") for e in raw_n] != [e.get("message") for e in exp_n]:
        errors.append("retrieve-ok: newest summary expand mismatch")
        return
    if retrieve(entries, {"first_seq": 1000, "last_seq": 1005}):
        errors.append("retrieve-ok: invented entries outside jsonl")
        return

    tmp = Path(tempfile.mkdtemp(prefix="lf-retrieve-"))
    path = tmp / "traj.jsonl"
    seeded = Trajectory(path, goal="seed")
    for i in range(20):
        seeded.append({"kind": "work", "message": f"seed-{i}"})
    worker = MockWorker(script=[
        WorkerAction(kind="done", thought="after retrieve"),
    ])
    coach = MockCoach(["halt"])
    loop = ForemanLoop(
        worker=worker,
        coach=coach,
        root=root,
        max_steps=6,
        persist=True,
        idle=False,
        traj_path=path,
    )
    res = loop.run("smoke: retrieve expand")
    if coach.calls:
        errors.append("retrieve-ok: retrieve path called coach")
        return
    kinds = [e.get("kind") for e in res.events]
    if "retrieved" not in kinds:
        errors.append("retrieve-ok: loop did not emit retrieved got=" + str(kinds))
        return
    if RETRIEVED_CONTEXT_HEADER not in (worker.last_system or ""):
        errors.append("retrieve-ok: worker context missing retrieved raw section")
        return
    if "seed-0" not in (worker.last_system or "") and "seed-" not in (worker.last_system or ""):
        errors.append("retrieve-ok: worker context missing expanded raw messages")
        return
    loaded = Trajectory(path)
    if not any(e.get("kind") == "retrieved" for e in loaded.entries):
        errors.append("retrieve-ok: retrieved not on the one traj")
        return
    print("retrieve-ok")


def _smoke_idle_act(root: Path, errors: list[str]) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="lf-idle-act-"))
    path = tmp / "traj.jsonl"
    worker = MockWorker(
        script=[WorkerAction(kind="done", thought="go idle")],
        think_script=[
            WorkerAction(
                kind="tool",
                tool="read",
                args={"path": "README.md"},
                thought="空转：本地读一眼 README",
            ),
            WorkerAction(kind="thought", thought="空转：读完继续想，不问教练"),
        ],
    )
    coach = MockCoach(["halt"])
    loop = ForemanLoop(
        worker=worker,
        coach=coach,
        root=root,
        max_steps=16,
        persist=True,
        idle=True,
        idle_start=0.0,
        idle_cap=1.0,
        idle_max=2,
        traj_path=path,
        sleeper=lambda _s: None,
    )
    res = loop.run("smoke: idle local act")
    if coach.calls:
        errors.append("idle-act-ok: local-safe idle-act called coach")
        return
    if "ask" in res.states or "apply" in res.states:
        errors.append("idle-act-ok: local-safe leaked to coach states=" + str(res.states))
        return
    kinds = [e.get("kind") for e in res.events]
    if "idle_act" not in kinds:
        errors.append("idle-act-ok: missing idle_act event got=" + str(kinds))
        return
    if "thought" not in kinds:
        errors.append("idle-act-ok: missing thought got=" + str(kinds))
        return
    works = [e for e in res.events if e.get("kind") == "work"]
    if not any("read" in str(e.get("message") or "") for e in works):
        errors.append("idle-act-ok: idle-act did not run through act/read")
        return
    if "idle" not in res.states or "act" not in res.states:
        errors.append("idle-act-ok: states=" + str(res.states))
        return
    loaded = Trajectory(path)
    if not any(e.get("kind") == "idle_act" for e in loaded.entries):
        errors.append("idle-act-ok: idle_act not on the one traj")
        return

    # Four escalate rules still apply when idle picks a remote write.
    push_cmd = "git " + "push" + " origin HEAD"
    worker2 = MockWorker(
        script=[WorkerAction(kind="done", thought="go idle")],
        think_script=[
            WorkerAction(
                kind="tool",
                tool="shell",
                args={"cmd": push_cmd},
                thought="空转：想推远端",
            ),
        ],
    )
    coach2 = MockCoach(["continue"])
    loop2 = ForemanLoop(
        worker=worker2,
        coach=coach2,
        root=root,
        max_steps=16,
        persist=True,
        idle=True,
        idle_start=0.0,
        idle_max=1,
        traj_path=tmp / "traj2.jsonl",
        sleeper=lambda _s: None,
    )
    res2 = loop2.run("smoke: idle-act escalate")
    kinds2 = [e.get("kind") for e in res2.events]
    if "idle_act" not in kinds2:
        errors.append("idle-act-ok: escalate path missing idle_act got=" + str(kinds2))
        return
    if "ask" not in res2.states:
        errors.append("idle-act-ok: remote idle-act skipped escalate states=" + str(res2.states))
        return
    if not coach2.calls:
        errors.append("idle-act-ok: escalate path never asked via act")
        return
    print("idle-act-ok")


def _smoke_traj_cli(errors: list[str]) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="lf-traj-cli-"))
    path = tmp / "traj.jsonl"
    seeded = Trajectory(path, goal="smoke traj cli")
    seeded.append({"kind": "thought", "message": "inspect thought"})
    seeded.append({"kind": "idle_act", "message": "inspect idle act"})
    seeded.append({"kind": "retrieved", "message": "inspect retrieved"})

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = main(["traj", str(path), "--last"])
    if rc != 0:
        errors.append("traj-cli-ok: traj --last exit " + str(rc))
        return
    text = buf.getvalue()
    for kind in ("thought", "idle_act", "retrieved"):
        if kind not in text:
            errors.append("traj-cli-ok: --last missing " + kind + " got=" + text[:240])
            return

    outp = tmp / "export.jsonl"
    buf2 = io.StringIO()
    with contextlib.redirect_stdout(buf2):
        rc2 = main(
            [
                "traj",
                str(path),
                "--kind",
                "thought,idle_act,retrieved",
                "--last",
                "3",
                "--out",
                str(outp),
            ]
        )
    if rc2 != 0:
        errors.append("traj-cli-ok: --kind/--out exit " + str(rc2))
        return
    if not outp.is_file():
        errors.append("traj-cli-ok: --out did not write")
        return
    exported = Trajectory(outp)
    ekinds = [e.get("kind") for e in exported.entries]
    if ekinds != ["thought", "idle_act", "retrieved"]:
        errors.append("traj-cli-ok: --out kinds=" + str(ekinds))
        return
    if any(e.get("kind") == "summary" for e in exported.entries):
        errors.append("traj-cli-ok: export invented a second format")
        return
    print("traj-cli-ok")


def _smoke_ask_cost(root: Path, errors: list[str]) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="lf-ask-cost-"))
    ask_path = tmp / "ask.jsonl"
    push_cmd = "git " + "push" + " origin HEAD"
    ask_worker = MockWorker(script=[
        WorkerAction(kind="tool", tool="shell", args={"cmd": push_cmd}, thought="fake remote"),
        WorkerAction(kind="done", thought="after persist ask"),
    ])
    ask_coach = MockCoach(["continue"])
    ask_loop = ForemanLoop(
        worker=ask_worker,
        coach=ask_coach,
        root=root,
        max_steps=10,
        persist=True,
        idle=False,
        traj_path=ask_path,
    )
    ask_res = ask_loop.run("smoke: ask cost")
    loaded = Trajectory(ask_path)
    stats = loaded.stats()
    if ask_res.tickets < 1:
        errors.append("ask-cost-ok: mock ask/apply produced no ticket")
        return
    if int(stats.get("asks") or 0) < 1:
        errors.append("ask-cost-ok: after ask/apply asks=" + str(stats.get("asks")))
        return
    if int(stats.get("replies") or 0) < 1:
        errors.append("ask-cost-ok: after ask/apply replies=" + str(stats.get("replies")))
        return
    if "estimated_usd" in stats:
        errors.append("ask-cost-ok: USD shown while COACH_USD_PER_ASK unset")
        return
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = main(["traj", str(ask_path), "--stats"])
    if rc != 0:
        errors.append("ask-cost-ok: traj --stats exit " + str(rc))
        return
    text = buf.getvalue()
    if "asks=0" in text.splitlines():
        errors.append("ask-cost-ok: traj --stats still asks=0 after ask/apply")
        return
    if not any(line.startswith("asks=") and line != "asks=0" for line in text.splitlines()):
        errors.append("ask-cost-ok: traj --stats missing asks>=1 got=" + text[:200])
        return
    if "estimated_usd=" in text:
        errors.append("ask-cost-ok: traj --stats printed USD with env unset")
        return

    idle_path = tmp / "idle.jsonl"
    idle_worker = MockWorker(script=[
        WorkerAction(kind="done", thought="idle only"),
    ])
    idle_coach = MockCoach(["halt"])
    idle_loop = ForemanLoop(
        worker=idle_worker,
        coach=idle_coach,
        root=root,
        max_steps=8,
        persist=True,
        idle=True,
        idle_start=0.0,
        idle_cap=1.0,
        idle_max=2,
        traj_path=idle_path,
        sleeper=lambda _s: None,
    )
    idle_res = idle_loop.run("smoke: idle cost")
    if idle_coach.calls:
        errors.append("ask-cost-ok: idle-only slice called coach")
        return
    if "ask" in idle_res.states or "apply" in idle_res.states:
        errors.append("ask-cost-ok: idle leaked to coach states=" + str(idle_res.states))
        return
    idle_loaded = Trajectory(idle_path)
    idle_stats = idle_loaded.stats()
    if not any(e.get("kind") == "thought" for e in idle_loaded.entries):
        errors.append("ask-cost-ok: idle-only slice wrote no thought")
        return
    if int(idle_stats.get("asks") or 0) != 0:
        errors.append("ask-cost-ok: idle thoughts incremented asks=" + str(idle_stats))
        return
    if int(idle_stats.get("replies") or 0) != 0:
        errors.append("ask-cost-ok: idle thoughts incremented replies=" + str(idle_stats))
        return
    buf2 = io.StringIO()
    with contextlib.redirect_stdout(buf2):
        rc2 = main(["traj", str(idle_path), "--stats"])
    if rc2 != 0:
        errors.append("ask-cost-ok: idle traj --stats exit " + str(rc2))
        return
    idle_text = buf2.getvalue()
    if "asks=0" not in idle_text.splitlines():
        errors.append("ask-cost-ok: idle traj --stats expected asks=0 got=" + idle_text[:200])
        return

    prev = os.environ.get("COACH_USD_PER_ASK")
    os.environ["COACH_USD_PER_ASK"] = "0.02"
    try:
        priced = coach_stats(loaded.entries)
        if "estimated_usd" not in priced:
            errors.append("ask-cost-ok: COACH_USD_PER_ASK set but no estimated_usd")
            return
        expected = round(int(priced["asks"]) * 0.02, 6)
        if priced.get("estimated_usd") != expected:
            errors.append(
                "ask-cost-ok: estimated_usd="
                + str(priced.get("estimated_usd"))
                + " expected="
                + str(expected)
            )
            return
        buf3 = io.StringIO()
        with contextlib.redirect_stdout(buf3):
            rc3 = main(["traj", str(ask_path), "--stats"])
        if rc3 != 0 or "estimated_usd=" not in buf3.getvalue():
            errors.append("ask-cost-ok: priced traj --stats missing estimated_usd")
            return
    finally:
        if prev is None:
            os.environ.pop("COACH_USD_PER_ASK", None)
        else:
            os.environ["COACH_USD_PER_ASK"] = prev

    from local_foreman.ui import LiveBoard, load_index_html

    html = load_index_html()
    if "教练用量" not in html or "coach-usage" not in html:
        errors.append("ask-cost-ok: UI missing coach usage copy")
        return
    board = LiveBoard()
    board.push({"kind": "thought", "message": "空转：本地还在，不问教练"})
    snap0 = board.snapshot()
    if int(snap0.get("asks") or 0) != 0:
        errors.append("ask-cost-ok: UI thought incremented asks=" + str(snap0.get("asks")))
        return
    board.push({"kind": "asked_coach", "message": "求助中（正在咨询大模型）"})
    board.push({"kind": "coach_instruction", "message": "continue locally"})
    snap1 = board.snapshot()
    if int(snap1.get("asks") or 0) < 1:
        errors.append("ask-cost-ok: UI snapshot asks=" + str(snap1.get("asks")))
        return
    if int(snap1.get("replies") or 0) < 1:
        errors.append("ask-cost-ok: UI snapshot replies=" + str(snap1.get("replies")))
        return

    print("ask-cost-ok")


def _smoke_max_asks(root: Path, errors: list[str]) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="lf-max-ask-"))
    path = tmp / "traj.jsonl"
    push_cmd = "git " + "push" + " origin HEAD"
    worker = MockWorker(script=[
        WorkerAction(kind="tool", tool="shell", args={"cmd": push_cmd}, thought="first remote"),
        WorkerAction(kind="tool", tool="shell", args={"cmd": push_cmd}, thought="second remote"),
        WorkerAction(kind="done", thought="should not need a third step"),
    ])
    coach = MockCoach(["continue", "continue"])
    prev = os.environ.get("LOCAL_FOREMAN_MAX_ASKS")
    os.environ["LOCAL_FOREMAN_MAX_ASKS"] = "1"
    try:
        loop = ForemanLoop(
            worker=worker,
            coach=coach,
            root=root,
            max_steps=12,
            persist=True,
            idle=False,
            traj_path=path,
        )
        res = loop.run("smoke: max asks")
    finally:
        if prev is None:
            os.environ.pop("LOCAL_FOREMAN_MAX_ASKS", None)
        else:
            os.environ["LOCAL_FOREMAN_MAX_ASKS"] = prev
    if len(coach.calls) != 1:
        errors.append("max-ask-ok: expected 1 coach call after first ask, got " + str(len(coach.calls)))
        return
    asked = [e for e in res.events if e.get("kind") == "asked_coach"]
    if len(asked) != 1:
        errors.append("max-ask-ok: expected 1 asked_coach got " + str([e.get("kind") for e in res.events]))
        return
    if not path.is_file():
        errors.append("max-ask-ok: traj missing")
        return
    loaded = Trajectory(path)
    stats = loaded.stats()
    if int(stats.get("asks") or 0) != 1:
        errors.append("max-ask-ok: traj asks=" + str(stats.get("asks")))
        return
    if not any(e.get("kind") == "stuck" for e in res.events):
        errors.append("max-ask-ok: missing stuck before skip")
        return
    stuck = [e for e in res.events if e.get("kind") == "stuck"]
    if len(stuck) < 2:
        errors.append("max-ask-ok: second escalate did not mark stuck got=" + str([e.get("kind") for e in res.events]))
        return
    if not res.done_reason.startswith("max_asks"):
        errors.append("max-ask-ok: expected halt reason max_asks got=" + res.done_reason)
        return
    if "apply" not in res.states:
        errors.append("max-ask-ok: first ask never applied states=" + str(res.states))
        return
    if any("咨询" in str(e.get("message") or "") and e.get("kind") == "asked_coach" for e in res.events[res.events.index(asked[0])+1:]):
        errors.append("max-ask-ok: later asked_coach after cap")
        return

    # Pre-existing traj asks already at the cap: first escalate stays local.
    path2 = tmp / "seeded.jsonl"
    seeded = Trajectory(path2, goal="seeded cap")
    seeded.append({"kind": "asked_coach", "message": "prior ask"})
    coach2 = MockCoach(["continue"])
    worker2 = MockWorker(script=[
        WorkerAction(kind="tool", tool="shell", args={"cmd": push_cmd}, thought="would ask"),
        WorkerAction(kind="done", thought="no"),
    ])
    prev = os.environ.get("LOCAL_FOREMAN_MAX_ASKS")
    os.environ["LOCAL_FOREMAN_MAX_ASKS"] = "1"
    try:
        loop2 = ForemanLoop(
            worker=worker2,
            coach=coach2,
            root=root,
            max_steps=10,
            persist=True,
            idle=True,
            idle_start=0.0,
            idle_cap=1.0,
            idle_max=1,
            traj_path=path2,
            sleeper=lambda _s: None,
        )
        res2 = loop2.run("smoke: already at cap")
    finally:
        if prev is None:
            os.environ.pop("LOCAL_FOREMAN_MAX_ASKS", None)
        else:
            os.environ["LOCAL_FOREMAN_MAX_ASKS"] = prev
    if coach2.calls:
        errors.append("max-ask-ok: seeded traj still called coach")
        return
    if any(e.get("kind") == "asked_coach" and e.get("message") != "prior ask" for e in res2.events):
        errors.append("max-ask-ok: seeded run wrote a new asked_coach")
        return
    if "idle" not in res2.states and not res2.done_reason.startswith("max_asks"):
        errors.append("max-ask-ok: seeded run did not stay local states=" + str(res2.states) + " done=" + res2.done_reason)
        return
    print("max-ask-ok")



def _smoke_verify(root: Path, errors: list[str]) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="lf-verify-"))
    coach = MockCoach(verify_verdicts=["accept"])
    worker = MockWorker(script=[
        WorkerAction(
            kind="tool",
            tool="write",
            args={"path": "note.txt", "content": "hello lanes"},
            thought="draft write",
        ),
        WorkerAction(kind="done", thought="after verify"),
    ])
    loop = ForemanLoop(
        worker=worker,
        coach=coach,
        root=tmp,
        max_steps=10,
        persist=False,
        idle=False,
    )
    res = loop.run("smoke: verify write")
    if "verify" not in res.states:
        errors.append("verify-ok: write missed verify states=" + str(res.states))
        return
    if "ask" in res.states:
        errors.append("verify-ok: write went ask states=" + str(res.states))
        return
    if coach.calls:
        errors.append("verify-ok: write used advise not verify")
        return
    if len(coach.verify_calls) != 1:
        errors.append("verify-ok: expected 1 verify call got " + str(len(coach.verify_calls)))
        return
    kinds = [e.get("kind") for e in res.events]
    if "verified_coach" not in kinds or "coach_verdict" not in kinds:
        errors.append("verify-ok: missing verified_coach/coach_verdict got=" + str(kinds))
        return
    if "asked_coach" in kinds:
        errors.append("verify-ok: verify counted as asked_coach")
        return
    stats = coach_stats(res.events)
    if int(stats.get("asks") or 0) != 0:
        errors.append("verify-ok: asks incremented by verify " + str(stats))
        return
    if int(stats.get("verifies") or 0) != 1:
        errors.append("verify-ok: verifies=" + str(stats.get("verifies")))
        return
    note = tmp / "note.txt"
    if not note.is_file() or note.read_text(encoding="utf-8") != "hello lanes":
        errors.append("verify-ok: accept did not apply held write")
        return
    ticket = coach.verify_calls[0]
    draft = str(getattr(ticket, "draft", "") or "")
    if "note.txt" not in draft and "b/note.txt" not in draft:
        errors.append("verify-ok: draft missing path: " + draft[:160])
        return
    if getattr(ticket, "kind", "") != "verify":
        errors.append("verify-ok: ticket.kind is not verify")
        return
    verdict_ev = next((e for e in res.events if e.get("kind") == "coach_verdict"), None)
    if verdict_ev is None:
        errors.append("verify-ok: no coach_verdict event")
        return
    if verdict_ev.get("verdict") != "accept":
        errors.append("verify-ok: calibration verdict=" + str(verdict_ev.get("verdict")))
        return
    if "conf" not in verdict_ev or "act" not in verdict_ev:
        errors.append("verify-ok: missing EAGLE-2 calibration fields conf/act")
        return

    # lossless hold: revise discards the draft
    tmp2 = Path(tempfile.mkdtemp(prefix="lf-verify-rev-"))
    coach2 = MockCoach(verify_verdicts=["revise"])
    worker2 = MockWorker(script=[
        WorkerAction(
            kind="tool",
            tool="write",
            args={"path": "drop.txt", "content": "should not land"},
            thought="draft then revise",
        ),
        WorkerAction(kind="done", thought="after revise"),
    ])
    res2 = ForemanLoop(
        worker=worker2, coach=coach2, root=tmp2, max_steps=10, persist=False, idle=False
    ).run("smoke: verify revise discards")
    if (tmp2 / "drop.txt").is_file():
        errors.append("verify-ok: revise applied the held write")
        return
    if not any(e.get("kind") == "lesson" for e in res2.events):
        errors.append("verify-ok: revise did not write a lesson")
        return
    if "ask" in res2.states:
        errors.append("verify-ok: revise path went ask")
        return

    # CRITIC: valid .py skips verify
    tmp3 = Path(tempfile.mkdtemp(prefix="lf-critic-"))
    coach3 = MockCoach(verify_verdicts=["accept"])
    worker3 = MockWorker(script=[
        WorkerAction(
            kind="tool",
            tool="write",
            args={"path": "ok.py", "content": "x = 1\n"},
            thought="syntax-ok write",
        ),
        WorkerAction(kind="done", thought="critic skip"),
    ])
    res3 = ForemanLoop(
        worker=worker3, coach=coach3, root=tmp3, max_steps=8, persist=False, idle=False
    ).run("smoke: critic skip")
    if coach3.verify_calls:
        errors.append("verify-ok: critic-ok .py still called verify")
        return
    if "verify" in res3.states or "ask" in res3.states:
        errors.append("verify-ok: critic-ok left act states=" + str(res3.states))
        return
    if (tmp3 / "ok.py").read_text(encoding="utf-8") != "x = 1\n":
        errors.append("verify-ok: critic-ok did not write")
        return

    # speculation tax: rolling accept < 0.5 sends the next write to ask
    tmp4 = Path(tempfile.mkdtemp(prefix="lf-tax-"))
    tax_path = tmp4 / "traj.jsonl"
    from local_foreman.traj import Trajectory
    seeded = Trajectory(tax_path, goal="tax")
    seeded.append({"kind": "coach_verdict", "reply": {"verdict": "revise", "instruction": "fix 1"}})
    seeded.append({"kind": "coach_verdict", "reply": {"verdict": "revise", "instruction": "fix 2"}})
    coach4 = MockCoach(["continue"])
    worker4 = MockWorker(script=[
        WorkerAction(
            kind="tool",
            tool="write",
            args={"path": "tax.txt", "content": "taxed"},
            thought="should go ask",
        ),
        WorkerAction(kind="done", thought="after tax ask"),
    ])
    res4 = ForemanLoop(
        worker=worker4,
        coach=coach4,
        root=tmp4,
        max_steps=10,
        persist=True,
        idle=False,
        traj_path=tax_path,
    ).run("smoke: speculation tax")
    if "ask" not in res4.states:
        errors.append("verify-ok: low accept rate did not send write to ask states=" + str(res4.states))
        return
    if (tmp4 / "tax.txt").is_file():
        errors.append("verify-ok: taxed write was applied without accept")
        return

    from local_foreman.ui import load_index_html
    html = load_index_html()
    if "核对中" not in html:
        errors.append("verify-ok: UI missing 核对中")
        return
    if "求助中（正在咨询大模型）" not in html:
        errors.append("verify-ok: UI lost 求助中 copy")
        return
    print("verify-ok")


def _smoke_bench(root: Path, errors: list[str]) -> None:
    from local_foreman.bench import run_bench_suite

    report = run_bench_suite(repo_root=root)
    print(report.text())
    if report.errors:
        for e in report.errors:
            errors.append("bench-ok: " + e)
        return
    local_rows = [r for r in report.rows if r.mode == "local-foreman"]
    remote_rows = [r for r in report.rows if r.mode == "remote-only"]
    if not local_rows or not remote_rows:
        errors.append("bench-ok: missing mode rows")
        return
    print("bench-ok")


def _smoke_self_verify(root: Path, errors: list[str]) -> None:
    from local_foreman.self_verify import HIGH_P, VERY_LOW_P

    hopeless = score_pending_claim(
        WorkerAction(
            kind="tool",
            tool="write",
            args={"path": "nope.txt", "content": "still clearly unsolvable"},
            thought="this is clearly unsolvable",
            confidence=0.05,
        ),
        goal="smoke: hopeless claim",
    )
    if not hopeless.very_low or not hopeless.hopeless:
        errors.append(
            "self-verify-ok: hopeless claim not very-low "
            + str((hopeless.p, hopeless.reason, hopeless.hopeless))
        )
        return
    high = score_pending_claim(
        WorkerAction(
            kind="tool",
            tool="write",
            args={"path": "ok.py", "content": "x = 1\n"},
            thought="syntax-ok write",
            confidence=0.9,
        ),
        goal="smoke: high critic",
    )
    if not high.high or high.critic is not True or high.p < HIGH_P:
        errors.append(
            "self-verify-ok: high+critic score failed "
            + str((high.p, high.critic, high.reason))
        )
        return
    if very_low_used := (VERY_LOW_P <= 0):
        errors.append("self-verify-ok: VERY_LOW_P unset")
        return
    del very_low_used

    tmp = Path(tempfile.mkdtemp(prefix="lf-self-hi-"))
    coach = MockCoach(verify_verdicts=["accept"])
    worker = MockWorker(script=[
        WorkerAction(
            kind="tool",
            tool="write",
            args={"path": "ok.py", "content": "x = 1\n"},
            thought="syntax-ok write",
            confidence=0.9,
        ),
        WorkerAction(kind="done", thought="high+critic stay low"),
    ])
    res = ForemanLoop(
        worker=worker, coach=coach, root=tmp, max_steps=8, persist=False, idle=False
    ).run("smoke: self-verify high critic")
    if coach.verify_calls or coach.calls:
        errors.append("self-verify-ok: high+critic still spent coach")
        return
    if "verify" in res.states or "ask" in res.states:
        errors.append("self-verify-ok: high+critic left LOW states=" + str(res.states))
        return
    if (tmp / "ok.py").read_text(encoding="utf-8") != "x = 1\n":
        errors.append("self-verify-ok: high+critic did not write")
        return
    if not any(e.get("kind") == "self_verify" for e in res.events):
        errors.append("self-verify-ok: missing self_verify event")
        return

    tmp2 = Path(tempfile.mkdtemp(prefix="lf-self-lo-"))
    coach2 = MockCoach(verify_verdicts=["accept", "accept"], verdicts=["continue"])
    worker2 = MockWorker(script=[
        WorkerAction(
            kind="tool",
            tool="write",
            args={"path": "a.txt", "content": "clearly unsolvable draft"},
            thought="clearly unsolvable",
            confidence=0.05,
        ),
        WorkerAction(
            kind="tool",
            tool="write",
            args={"path": "b.txt", "content": "still hopeless"},
            thought="hopeless again",
            confidence=0.04,
        ),
        WorkerAction(kind="done", thought="stayed local"),
    ])
    res2 = ForemanLoop(
        worker=worker2, coach=coach2, root=tmp2, max_steps=10, persist=False, idle=False
    ).run("smoke: self-verify hopeless twice")
    if coach2.verify_calls or coach2.calls:
        errors.append("self-verify-ok: hopeless writes spent coach tokens")
        return
    if "verify" in res2.states or "ask" in res2.states:
        errors.append("self-verify-ok: hopeless twice went coach states=" + str(res2.states))
        return
    if (tmp2 / "a.txt").is_file() or (tmp2 / "b.txt").is_file():
        errors.append("self-verify-ok: hopeless writes were applied")
        return
    lows = [e for e in res2.events if e.get("kind") == "self_verify"]
    if len(lows) < 2:
        errors.append("self-verify-ok: expected 2 self_verify events got " + str(len(lows)))
        return
    if not any("twice" in str(e.get("message") or "") for e in res2.events):
        errors.append("self-verify-ok: missing twice-stay-local thought")
        return

    tmp3 = Path(tempfile.mkdtemp(prefix="lf-self-esc-"))
    push_cmd = "git " + "push" + " origin HEAD"
    coach3 = MockCoach(["continue"])
    worker3 = MockWorker(script=[
        WorkerAction(
            kind="tool",
            tool="write",
            args={"path": "c.txt", "content": "clearly unsolvable"},
            thought="unsolvable",
            confidence=0.05,
        ),
        WorkerAction(
            kind="tool",
            tool="write",
            args={"path": "d.txt", "content": "hopeless"},
            thought="hopeless",
            confidence=0.05,
        ),
        WorkerAction(
            kind="tool",
            tool="shell",
            args={"cmd": push_cmd},
            thought="real escalate",
            confidence=0.05,
        ),
        WorkerAction(kind="done", thought="after escalate"),
    ])
    res3 = ForemanLoop(
        worker=worker3, coach=coach3, root=tmp3, max_steps=12, persist=False, idle=False
    ).run("smoke: self-verify then escalate")
    if coach3.verify_calls:
        errors.append("self-verify-ok: hopeless path used verify")
        return
    if len(coach3.calls) != 1:
        errors.append("self-verify-ok: escalate expected 1 ask got " + str(len(coach3.calls)))
        return
    if "ask" not in res3.states:
        errors.append("self-verify-ok: escalate condition missed ask states=" + str(res3.states))
        return
    print("self-verify-ok")


def _smoke_demo(root: Path, errors: list[str]) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="lf-demo-"))
    demo_path = default_demo_path(tmp)
    coach = MockCoach(verify_verdicts=["accept"])
    worker = MockWorker(script=[
        WorkerAction(
            kind="tool",
            tool="write",
            args={"path": "note.txt", "content": "hello demos"},
            thought="draft write",
            confidence=0.4,
        ),
        WorkerAction(kind="done", thought="after accept"),
    ])
    res = ForemanLoop(
        worker=worker,
        coach=coach,
        root=tmp,
        max_steps=10,
        persist=False,
        idle=False,
        demo_path=demo_path,
    ).run("smoke: demo cache write a local note")
    if "verify" not in res.states:
        errors.append("demo-ok: seed write missed verify states=" + str(res.states))
        return
    if not (tmp / "note.txt").is_file():
        errors.append("demo-ok: accept did not land the file")
        return
    if not demo_path.is_file():
        errors.append("demo-ok: demos.jsonl not written under .local-foreman")
        return
    if ".local-foreman" not in demo_path.parts:
        errors.append("demo-ok: cache not in .local-foreman dir")
        return
    cached = load_demos(demo_path)
    if not cached:
        errors.append("demo-ok: cache empty after accept")
        return
    rec = cached[-1]
    for key in ("goal", "task_sketch", "claim", "path"):
        if not rec.get(key):
            errors.append("demo-ok: missing " + key)
            return
    if "demo cache write" not in str(rec.get("goal") or "") and "demo cache" not in str(rec.get("task_sketch") or ""):
        errors.append("demo-ok: goal/task_sketch missing the seed goal")
        return
    if rec.get("path") != "note.txt":
        errors.append("demo-ok: path=" + str(rec.get("path")))
        return
    for bad in ("instruction", "rewrite", "verdict", "coach", "reply"):
        if bad in rec:
            errors.append("demo-ok: stored coach rewrite field " + bad)
            return
    if not any(e.get("kind") == "demo" for e in res.events):
        errors.append("demo-ok: missing demo event after accept")
        return

    dirty = compact_demo(
        goal="x", claim="c", path="p.txt", draft="d"
    )
    dirty["instruction"] = "coach rewrite should never persist"
    dirty["verdict"] = "accept"
    from local_foreman.demo import store_demo
    store_demo(demo_path, dirty)
    for rec2 in load_demos(demo_path):
        if "instruction" in rec2 or "verdict" in rec2:
            errors.append("demo-ok: sanitize kept coach rewrite")
            return

    worker2 = MockWorker(script=[
        WorkerAction(
            kind="tool",
            tool="write",
            args={"path": "note2.txt", "content": "similar note"},
            thought="another write",
            confidence=0.4,
        ),
        WorkerAction(kind="done", thought="after inject"),
    ])
    coach2 = MockCoach(verify_verdicts=["accept"])
    loop2 = ForemanLoop(
        worker=worker2,
        coach=coach2,
        root=tmp,
        max_steps=10,
        persist=False,
        idle=False,
        demo_path=demo_path,
    )
    res2 = loop2.run("smoke: demo cache write a local note again")
    sys_txt = worker2.last_system or ""
    if DEMO_CONTEXT_HEADER not in sys_txt:
        errors.append("demo-ok: similar write missing demo header in system")
        return
    if "note.txt" not in sys_txt and "hello demos" not in sys_txt:
        errors.append("demo-ok: injected demos missing prior path/claim")
        return
    if "coach rewrite" in sys_txt or "must follow" in sys_txt.lower() and "Coach instruction" in sys_txt:
        # coach instruction header is fine if empty; rewrite text must not appear
        if "coach rewrite should never persist" in sys_txt:
            errors.append("demo-ok: coach rewrite leaked into worker prompt")
            return

    tmp3 = Path(tempfile.mkdtemp(prefix="lf-demo-rev-"))
    demo3 = default_demo_path(tmp3)
    coach3 = MockCoach(verify_verdicts=["revise"])
    worker3 = MockWorker(script=[
        WorkerAction(
            kind="tool",
            tool="write",
            args={"path": "drop.txt", "content": "should not cache"},
            thought="draft then revise",
            confidence=0.4,
        ),
        WorkerAction(kind="done", thought="after revise"),
    ])
    ForemanLoop(
        worker=worker3, coach=coach3, root=tmp3, max_steps=10,
        persist=False, idle=False, demo_path=demo3,
    ).run("smoke: demo revise must not cache")
    if demo3.is_file() and load_demos(demo3):
        errors.append("demo-ok: revise stored a demo")
        return
    if (tmp3 / "drop.txt").is_file():
        errors.append("demo-ok: revise landed a file")
        return

    tmp4 = Path(tempfile.mkdtemp(prefix="lf-demo-ask-"))
    demo4 = default_demo_path(tmp4)
    push_cmd = "git " + "push" + " origin HEAD"
    coach4 = MockCoach(["continue"])
    worker4 = MockWorker(script=[
        WorkerAction(kind="tool", tool="shell", args={"cmd": push_cmd}, thought="fake remote"),
        WorkerAction(kind="done", thought="after ask"),
    ])
    ForemanLoop(
        worker=worker4, coach=coach4, root=tmp4, max_steps=10,
        persist=False, idle=False, demo_path=demo4,
    ).run("smoke: demo ask must not cache")
    if demo4.is_file() and load_demos(demo4):
        errors.append("demo-ok: ask path stored a demo")
        return

    # LOCAL_FOREMAN_DEMOS overrides the default .local-foreman/demos.jsonl path.
    tmp5 = Path(tempfile.mkdtemp(prefix="lf-demo-env-"))
    custom = tmp5 / "custom-demos.jsonl"
    prev = os.environ.get("LOCAL_FOREMAN_DEMOS")
    os.environ["LOCAL_FOREMAN_DEMOS"] = str(custom)
    try:
        coach5 = MockCoach(verify_verdicts=["accept"])
        worker5 = MockWorker(script=[
            WorkerAction(
                kind="tool",
                tool="write",
                args={"path": "env-note.txt", "content": "via env"},
                thought="draft write",
                confidence=0.4,
            ),
            WorkerAction(kind="done", thought="after env demo"),
        ])
        ForemanLoop(
            worker=worker5, coach=coach5, root=tmp5, max_steps=10,
            persist=False, idle=False,
        ).run("smoke: demo cache via LOCAL_FOREMAN_DEMOS")
    finally:
        if prev is None:
            os.environ.pop("LOCAL_FOREMAN_DEMOS", None)
        else:
            os.environ["LOCAL_FOREMAN_DEMOS"] = prev
    if not custom.is_file() or not load_demos(custom):
        errors.append("demo-ok: LOCAL_FOREMAN_DEMOS did not receive the cache")
        return
    default_under_root = tmp5 / ".local-foreman" / "demos.jsonl"
    if default_under_root.is_file() and load_demos(default_under_root):
        errors.append("demo-ok: env override still wrote default demos.jsonl")
        return

    # Path overlap (not just goal): seed a demo, then a different goal writing the same path.
    tmp6 = Path(tempfile.mkdtemp(prefix="lf-demo-path-"))
    demo6 = tmp6 / "demos.jsonl"
    from local_foreman.demo import store_demo as _store
    _store(demo6, compact_demo(
        goal="alpha-unique-seed-goal",
        claim="Claim: wrote overlap.txt",
        path="overlap.txt",
        draft="overlap body",
    ))
    worker6 = MockWorker(script=[
        WorkerAction(
            kind="tool",
            tool="write",
            args={"path": "overlap.txt", "content": "second"},
            thought="same path later",
            confidence=0.4,
        ),
        WorkerAction(kind="done", thought="after path overlap"),
    ])
    loop6 = ForemanLoop(
        worker=worker6, coach=MockCoach(verify_verdicts=["accept"]),
        root=tmp6, max_steps=10, persist=False, idle=False, demo_path=demo6,
    )
    loop6.run("please update overlap.txt with no shared sketch words")
    sys6 = worker6.last_system or ""
    if DEMO_CONTEXT_HEADER not in sys6 or "overlap.txt" not in sys6:
        errors.append("demo-ok: path overlap did not inject demo")
        return
    print("demo-ok")



def _smoke_calibrate(root: Path, errors: list[str]) -> None:
    from local_foreman.calibrate import (
        MIN_SAMPLES,
        P_SKIP,
        act_type_of,
        action_is_git_mutate,
        conf_bucket,
        extract_verdicts,
        rolling_table,
        should_skip_verify,
    )
    from local_foreman.traj import Trajectory

    if MIN_SAMPLES < 8:
        errors.append("calibrate-ok: MIN_SAMPLES expected >= 8 got " + str(MIN_SAMPLES))
        return
    if P_SKIP < 0.9:
        errors.append("calibrate-ok: P_SKIP expected >= 0.9 got " + str(P_SKIP))
        return
    if conf_bucket(0.39) != "low" or conf_bucket(0.4) != "mid" or conf_bucket(0.7) != "high":
        errors.append("calibrate-ok: conf buckets " + ",".join(
            [conf_bucket(0.39), conf_bucket(0.4), conf_bucket(0.7)]
        ))
        return
    if act_type_of("write {'path': 'n.txt'}") != "write":
        errors.append("calibrate-ok: act_type write parse failed")
        return
    push = WorkerAction(
        kind="tool",
        tool="shell",
        args={"cmd": "git " + "push" + " origin HEAD"},
        thought="remote",
    )
    if not action_is_git_mutate(push):
        errors.append("calibrate-ok: git push not treated as git-mutate")
        return
    note = WorkerAction(
        kind="tool",
        tool="write",
        args={"path": "note.txt", "content": "x"},
        thought="local write",
        confidence=0.4,
    )
    if action_is_git_mutate(note):
        errors.append("calibrate-ok: ordinary write marked git-mutate")
        return

    def _seed_verdicts(path: Path, rows: list[tuple[float, str, str]]) -> None:
        seeded = Trajectory(path, goal="calibrate seed")
        for conf, act, verdict in rows:
            seeded.append(
                {
                    "kind": "coach_verdict",
                    "conf": conf,
                    "act": act,
                    "verdict": verdict,
                    "conf_bucket": conf_bucket(conf),
                    "act_type": act_type_of(act),
                    "reply": {"verdict": verdict, "instruction": "seed " + verdict},
                }
            )

    write_act = "write {'path': 'note.txt'}"

    # Table-only: <8 samples is not trusted; 8 accepts is trusted P=1.0
    thin = [
        {"kind": "coach_verdict", "conf": 0.4, "act": write_act, "verdict": "accept",
         "reply": {"verdict": "accept"}}
        for _ in range(3)
    ]
    thin_table = rolling_table(thin)
    if thin_table.trusted or thin_table.samples != 3:
        errors.append("calibrate-ok: thin table trusted=" + str(thin_table.trusted))
        return
    if should_skip_verify(thin_table, note):
        errors.append("calibrate-ok: skip verify before 8 samples")
        return

    fat = [
        {"kind": "coach_verdict", "conf": 0.4, "act": write_act, "verdict": "accept",
         "reply": {"verdict": "accept"}}
        for _ in range(8)
    ]
    fat_table = rolling_table(fat)
    if not fat_table.trusted or fat_table.samples != 8:
        errors.append("calibrate-ok: 8 accepts not trusted")
        return
    p = fat_table.lookup(note)
    if p is None or p < 0.9:
        errors.append("calibrate-ok: expected P>=0.9 got " + str(p))
        return
    if not should_skip_verify(fat_table, note, p=p):
        errors.append("calibrate-ok: trusted high P did not skip verify")
        return
    if should_skip_verify(fat_table, push, p=p):
        errors.append("calibrate-ok: git-mutate must not skip verify")
        return
    if not extract_verdicts(fat):
        errors.append("calibrate-ok: extract_verdicts empty")
        return

    # 1) fewer than 8: keep DSP 0.75 skip + tax <0.5 (no calibrate-skip)
    tmp = Path(tempfile.mkdtemp(prefix="lf-cal-thin-"))
    thin_path = tmp / "thin.jsonl"
    _seed_verdicts(thin_path, [
        (0.4, write_act, "accept"),
        (0.4, write_act, "revise"),
        (0.4, write_act, "accept"),
    ])
    coach = MockCoach(verify_verdicts=["accept"])
    worker = MockWorker(script=[
        WorkerAction(
            kind="tool",
            tool="write",
            args={"path": "thin.txt", "content": "need verify"},
            thought="thin table write",
            confidence=0.4,
        ),
        WorkerAction(kind="done", thought="after thin"),
    ])
    res = ForemanLoop(
        worker=worker,
        coach=coach,
        root=tmp,
        max_steps=10,
        persist=True,
        idle=False,
        traj_path=thin_path,
    ).run("smoke: calibrate thin")
    if any("calibrate-skip" in str(e.get("message") or "") for e in res.events):
        errors.append("calibrate-ok: calibrate-skip with <8 samples")
        return
    if "verify" not in res.states:
        errors.append("calibrate-ok: thin table should still verify states=" + str(res.states))
        return
    if "ask" in res.states:
        errors.append("calibrate-ok: thin mixed rate invented ask")
        return
    if len(coach.verify_calls) != 1:
        errors.append("calibrate-ok: thin expected 1 verify got " + str(len(coach.verify_calls)))
        return

    # DSP fallback still works when table is not trusted (3 accepts → rate 1.0)
    tmp_dsp = Path(tempfile.mkdtemp(prefix="lf-cal-dsp-"))
    dsp_path = tmp_dsp / "dsp.jsonl"
    _seed_verdicts(dsp_path, [(0.4, write_act, "accept")] * 3)
    coach_d = MockCoach(verify_verdicts=["accept"])
    worker_d = MockWorker(script=[
        WorkerAction(
            kind="tool",
            tool="write",
            args={"path": "dsp.txt", "content": "dsp fallback"},
            thought="dsp write",
            confidence=0.4,
        ),
        WorkerAction(kind="done", thought="after dsp"),
    ])
    res_d = ForemanLoop(
        worker=worker_d,
        coach=coach_d,
        root=tmp_dsp,
        max_steps=8,
        persist=True,
        idle=False,
        traj_path=dsp_path,
    ).run("smoke: calibrate dsp fallback")
    if coach_d.verify_calls or coach_d.calls:
        errors.append("calibrate-ok: DSP fallback still called coach")
        return
    if not any("dsp-skip" in str(e.get("message") or "") for e in res_d.events):
        errors.append("calibrate-ok: expected dsp-skip when table untrusted")
        return
    if any("calibrate-skip" in str(e.get("message") or "") for e in res_d.events):
        errors.append("calibrate-ok: calibrate-skip stole DSP fallback")
        return
    if not (tmp_dsp / "dsp.txt").is_file():
        errors.append("calibrate-ok: DSP fallback did not land file")
        return

    # 2) >=8 accepts → skip verify, stay LOW, apply (skip hold)
    tmp2 = Path(tempfile.mkdtemp(prefix="lf-cal-ok-"))
    ok_path = tmp2 / "ok.jsonl"
    _seed_verdicts(ok_path, [(0.4, write_act, "accept")] * 8)
    coach2 = MockCoach(verify_verdicts=["accept"], verdicts=["continue"])
    worker2 = MockWorker(script=[
        WorkerAction(
            kind="tool",
            tool="write",
            args={"path": "ok.txt", "content": "calibrated"},
            thought="trusted write",
            confidence=0.4,
        ),
        WorkerAction(kind="done", thought="after calibrate skip"),
    ])
    res2 = ForemanLoop(
        worker=worker2,
        coach=coach2,
        root=tmp2,
        max_steps=8,
        persist=True,
        idle=False,
        traj_path=ok_path,
    ).run("smoke: calibrate skip")
    if coach2.verify_calls or coach2.calls:
        errors.append("calibrate-ok: trusted P still spent coach")
        return
    if "verify" in res2.states or "ask" in res2.states:
        errors.append("calibrate-ok: trusted P left LOW states=" + str(res2.states))
        return
    if not any("calibrate-skip" in str(e.get("message") or "") for e in res2.events):
        errors.append("calibrate-ok: missing calibrate-skip work")
        return
    landed = tmp2 / "ok.txt"
    if not landed.is_file() or landed.read_text(encoding="utf-8") != "calibrated":
        errors.append("calibrate-ok: skip-hold apply did not land file")
        return
    # lossless hold still: a later revise path with no table should discard
    if any(e.get("kind") == "asked_coach" for e in res2.events):
        errors.append("calibrate-ok: skip path counted as ask")
        return

    # 3) git-mutate still HIGH even with a trusted table
    tmp3 = Path(tempfile.mkdtemp(prefix="lf-cal-git-"))
    git_path = tmp3 / "git.jsonl"
    _seed_verdicts(git_path, [(0.4, write_act, "accept")] * 8)
    push_cmd = "git " + "push" + " origin HEAD"
    coach3 = MockCoach(["continue"])
    worker3 = MockWorker(script=[
        WorkerAction(kind="tool", tool="shell", args={"cmd": push_cmd}, thought="remote"),
        WorkerAction(kind="done", thought="after ask"),
    ])
    res3 = ForemanLoop(
        worker=worker3,
        coach=coach3,
        root=tmp3,
        max_steps=10,
        persist=True,
        idle=False,
        traj_path=git_path,
    ).run("smoke: calibrate git still high")
    if "ask" not in res3.states:
        errors.append("calibrate-ok: git-mutate skipped HIGH states=" + str(res3.states))
        return
    if not coach3.calls:
        errors.append("calibrate-ok: git-mutate never asked")
        return
    if coach3.verify_calls:
        errors.append("calibrate-ok: git-mutate used verify")
        return

    # 4) long disagreement: do not invent HIGH (tax would have asked)
    tmp4 = Path(tempfile.mkdtemp(prefix="lf-cal-dis-"))
    dis_path = tmp4 / "dis.jsonl"
    mixed = [(0.1, write_act, "accept")] * 8 + [(0.1, write_act, "revise")] * 4
    _seed_verdicts(dis_path, mixed)
    loaded4 = Trajectory(dis_path)
    table4 = rolling_table(loaded4.entries)
    if not table4.trusted:
        errors.append("calibrate-ok: mixed 12-row table not trusted")
        return
    if not table4.disagree_window(loaded4.entries):
        errors.append("calibrate-ok: expected long disagree window")
        return
    coach4 = MockCoach(verify_verdicts=["revise"], verdicts=["continue"])
    worker4 = MockWorker(script=[
        WorkerAction(
            kind="tool",
            tool="write",
            args={"path": "dis.txt", "content": "should verify not ask"},
            thought="disagreement write",
            confidence=0.25,
        ),
        WorkerAction(kind="done", thought="after disagree"),
    ])
    res4 = ForemanLoop(
        worker=worker4,
        coach=coach4,
        root=tmp4,
        max_steps=10,
        persist=True,
        idle=False,
        traj_path=dis_path,
    ).run("smoke: calibrate disagree")
    if "ask" in res4.states:
        errors.append("calibrate-ok: disagreement invented HIGH ask states=" + str(res4.states))
        return
    if any(
        "accept rate below 0.5" in str(e.get("message") or "")
        or (e.get("kind") == "stuck" and "accept rate" in str(e.get("message") or ""))
        for e in res4.events
    ):
        errors.append("calibrate-ok: disagreement used tax as a new ask reason")
        return
    if "verify" not in res4.states:
        errors.append("calibrate-ok: disagreement should still verify states=" + str(res4.states))
        return
    if not any("calibrate disagree" in str(e.get("message") or "") for e in res4.events):
        errors.append("calibrate-ok: missing disagree-window thought")
        return
    if (tmp4 / "dis.txt").is_file():
        errors.append("calibrate-ok: revise applied the held write")
        return
    if not any(e.get("kind") == "lesson" for e in res4.events):
        errors.append("calibrate-ok: revise did not write a lesson")
        return
    # existing HIGH rule still wins
    if any(e.get("kind") == "asked_coach" for e in res4.events):
        errors.append("calibrate-ok: disagreement wrote asked_coach")
        return

    # tax <0.5 still works when table is untrusted (2 revises, no conf needed by DSP)
    tmp5 = Path(tempfile.mkdtemp(prefix="lf-cal-tax-"))
    tax_path = tmp5 / "tax.jsonl"
    seeded5 = Trajectory(tax_path, goal="tax")
    seeded5.append({"kind": "coach_verdict", "reply": {"verdict": "revise", "instruction": "fix 1"}})
    seeded5.append({"kind": "coach_verdict", "reply": {"verdict": "revise", "instruction": "fix 2"}})
    coach5 = MockCoach(["continue"])
    worker5 = MockWorker(script=[
        WorkerAction(
            kind="tool",
            tool="write",
            args={"path": "tax.txt", "content": "taxed"},
            thought="should go ask",
            confidence=0.4,
        ),
        WorkerAction(kind="done", thought="after tax"),
    ])
    res5 = ForemanLoop(
        worker=worker5,
        coach=coach5,
        root=tmp5,
        max_steps=10,
        persist=True,
        idle=False,
        traj_path=tax_path,
    ).run("smoke: calibrate tax fallback")
    if "ask" not in res5.states:
        errors.append("calibrate-ok: untrusted tax <0.5 lost ask states=" + str(res5.states))
        return
    if (tmp5 / "tax.txt").is_file():
        errors.append("calibrate-ok: taxed write landed")
        return

    print("calibrate-ok")


def run_smoke() -> int:
    root = Path(__file__).resolve().parents[2]
    errors = []
    os.environ["LOCAL_FOREMAN_WORKER"] = "mock"
    os.environ["LOCAL_FOREMAN_COACH"] = "mock"
    os.environ["LOCAL_FOREMAN_PERSIST"] = "0"
    os.environ["LOCAL_FOREMAN_TRAJ"] = str(
        Path(tempfile.mkdtemp(prefix="lf-smoke-")) / "traj.jsonl"
    )

    act_worker = MockWorker(script=[
        WorkerAction(kind="tool", tool="read", args={"path": "README.md"}, thought="safe read"),
        WorkerAction(kind="done", thought="read ok"),
    ])
    act = ForemanLoop(worker=act_worker, coach=MockCoach(), root=root, max_steps=6)
    act_res = act.run("smoke: read README only")
    if "ask" in act_res.states or "apply" in act_res.states:
        errors.append("act leaked to coach: " + str(act_res.states))
    elif "act" not in act_res.states:
        errors.append("act never entered act")
    elif not any("read" in (h.get("action") or "") for h in act_res.history):
        errors.append("act did not execute a read")
    else:
        print("act-ok")

    push_cmd = "git " + "push" + " origin HEAD"
    ask_worker = MockWorker(script=[
        WorkerAction(kind="tool", tool="shell", args={"cmd": push_cmd}, thought="fake remote"),
        WorkerAction(kind="done", thought="stopped after ask"),
    ])
    ask = ForemanLoop(worker=ask_worker, coach=MockCoach(["continue"]), root=root, max_steps=8)
    ask_res = ask.run("smoke: attempt remote update")
    if "ask" not in ask_res.states:
        errors.append("ask not entered: " + str(ask_res.states))
    elif ask_res.tickets < 1:
        errors.append("ask produced no ticket")
    else:
        print("ask-ok")

    _smoke_problem(ask_res, errors)

    push_cmd = "git " + "push" + " origin HEAD"
    seen = []
    for verdict in ("continue", "revise", "halt"):
        w = MockWorker(script=[
            WorkerAction(kind="tool", tool="shell", args={"cmd": push_cmd}, thought="fake remote"),
            WorkerAction(kind="done", thought="after apply"),
        ])
        c = MockCoach([verdict])
        loop = ForemanLoop(worker=w, coach=c, root=root, max_steps=8)
        res = loop.run("smoke apply " + verdict)
        if "apply" not in res.states:
            errors.append(verdict + " missed apply: " + str(res.states))
            continue
        if verdict not in res.verdicts:
            errors.append(verdict + " missing from verdicts: " + str(res.verdicts))
            continue
        if not res.last_instruction:
            errors.append(verdict + " did not inject instruction")
            continue
        if verdict != "halt" and res.last_instruction not in (w.last_system or ""):
            errors.append(verdict + " instruction not in next worker system")
            continue
        if verdict != "halt" and COACH_INSTRUCTION_HEADER not in (w.last_system or ""):
            errors.append(verdict + " missing instruction header in worker system")
            continue
        if verdict == "halt" and not res.done_reason.startswith("halt"):
            errors.append("halt did not stop: " + res.done_reason)
            continue
        seen.append(verdict)
    if seen == ["continue", "revise", "halt"]:
        print("apply-ok")
    else:
        errors.append("apply verdicts seen=" + str(seen))

    _smoke_ui(root, errors)

    if (root / "LICENSE").is_file() and (root / ".github" / "workflows" / "smoke.yml").is_file():
        print("oss-ok")

    _smoke_traj(root, errors)
    _smoke_idle(root, errors)
    _smoke_compact(errors)
    _smoke_retrieve(root, errors)
    _smoke_idle_act(root, errors)
    _smoke_traj_cli(errors)
    _smoke_ask_cost(root, errors)
    _smoke_max_asks(root, errors)
    _smoke_verify(root, errors)
    _smoke_bench(root, errors)
    _smoke_self_verify(root, errors)
    _smoke_demo(root, errors)
    _smoke_calibrate(root, errors)

    if errors:
        for e in errors:
            print("SMOKE FAIL: " + e, file=sys.stderr)
        return 1
    return 0


def main(argv=None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] == "ui":
        return run_ui(raw[1:])
    if raw and raw[0] == "traj":
        return run_traj(raw[1:])
    if raw and raw[0] == "bench":
        from local_foreman.bench import run_bench
        return run_bench(raw[1:])

    parser = argparse.ArgumentParser(
        prog="local-foreman",
        description="Mac-first local agent: three risk lanes (low act / mid verify / high ask). Coach only guides.",
    )
    parser.add_argument("goal", nargs="*", help="one goal, or the word ui / traj / bench")
    parser.add_argument("--smoke", action="store_true", help="run mock act/ask/apply checks")
    parser.add_argument("--bench", action="store_true", help="mock comparison: lanes vs remote-only")
    parser.add_argument("--review", action="store_true", help="force ask (user asked for review)")
    parser.add_argument(
        "--worker",
        choices=("mock", "mlx"),
        help="worker backend (default: $LOCAL_FOREMAN_WORKER or mock)",
    )
    parser.add_argument(
        "--coach",
        choices=("mock", "openai"),
        help="coach backend (default: $LOCAL_FOREMAN_COACH or mock)",
    )
    parser.add_argument("--max-steps", type=int, default=12, metavar="N")
    parser.add_argument(
        "--persist",
        action="store_true",
        help="write traj jsonl and idle-think (or set LOCAL_FOREMAN_PERSIST=1)",
    )
    args = parser.parse_args(raw)

    if args.worker:
        os.environ["LOCAL_FOREMAN_WORKER"] = args.worker
    if args.coach:
        os.environ["LOCAL_FOREMAN_COACH"] = args.coach
    if args.persist:
        os.environ["LOCAL_FOREMAN_PERSIST"] = "1"

    if args.smoke:
        os.environ["LOCAL_FOREMAN_WORKER"] = "mock"
        os.environ["LOCAL_FOREMAN_COACH"] = "mock"
        return run_smoke()

    if args.bench:
        os.environ["LOCAL_FOREMAN_WORKER"] = "mock"
        os.environ["LOCAL_FOREMAN_COACH"] = "mock"
        from local_foreman.bench import run_bench
        return run_bench([])

    goal = " ".join(args.goal).strip()
    if not goal:
        parser.print_help()
        return 2
    try:
        return run_goal(
            goal,
            user_review=args.review,
            max_steps=args.max_steps,
            persist=bool(args.persist) or env_flag(ENV_PERSIST),
        )
    except (RuntimeError, ValueError) as exc:
        print("error: " + str(exc), file=sys.stderr)
        return 1
