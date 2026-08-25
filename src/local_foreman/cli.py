"""CLI: python -m local_foreman "goal" | local-foreman ui | local-foreman traj | --smoke

One-shot stays task-driven unless --persist / LOCAL_FOREMAN_PERSIST=1.
`ui` defaults persist+idle ON. Idle think never calls the coach.
`traj` tails/cats/exports the same jsonl the loop writes.
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
from local_foreman.loop import (
    COACH_INSTRUCTION_HEADER,
    ENV_PERSIST,
    RETRIEVED_CONTEXT_HEADER,
    ForemanLoop,
    env_flag,
)
from local_foreman.ticket import problem_is_clear
from local_foreman.traj import (
    Trajectory,
    compact_entries,
    default_traj_path,
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
        description="Inspect the same append-only traj jsonl the loop writes. No second log.",
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
    args = parser.parse_args(argv)

    path = Path(args.path) if args.path else default_traj_path(_root())
    kinds = parse_kinds(args.kind)
    selected = select_entries(Trajectory(path).entries, last=args.last, kinds=kinds)

    if args.out:
        dest = Path(args.out)
        try:
            if path.is_file() and dest.resolve() == path.resolve():
                print("traj: --out will not rewrite the live jsonl", file=sys.stderr)
                return 2
        except OSError:
            pass
        write_jsonl(selected, dest)

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

    parser = argparse.ArgumentParser(
        prog="local-foreman",
        description="Mac-first local agent: the local worker does the work; the remote coach only guides.",
    )
    parser.add_argument("goal", nargs="*", help="one goal, or the word ui / traj")
    parser.add_argument("--smoke", action="store_true", help="run mock act/ask/apply checks")
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
