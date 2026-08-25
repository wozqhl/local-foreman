"""CLI: python -m local_foreman "goal" | local-foreman ui | --smoke"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from local_foreman.coach import MockCoach
from local_foreman.loop import COACH_INSTRUCTION_HEADER, ForemanLoop
from local_foreman.ticket import problem_is_clear
from local_foreman.worker import MockWorker, WorkerAction


def _root() -> Path:
    env = os.environ.get("LOCAL_FOREMAN_ROOT")
    return Path(env) if env else Path.cwd()


def run_goal(goal: str, *, user_review: bool = False, max_steps: int = 12) -> int:
    def on_state(name: str) -> None:
        print(name, flush=True)

    loop = ForemanLoop(
        root=_root(),
        user_review=user_review,
        max_steps=max_steps,
        on_state=on_state,
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
        description="本机看板：干活中 / 求助中（正在咨询大模型） / 已收到指示 / 继续",
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
        print("ui-ok")
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        errors.append("ui-ok: " + str(exc))
    finally:
        if httpd is not None:
            stop_ui(httpd)


def run_smoke() -> int:
    root = Path(__file__).resolve().parents[2]
    errors = []

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

    if errors:
        for e in errors:
            print("SMOKE FAIL: " + e, file=sys.stderr)
        return 1
    return 0


def main(argv=None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] == "ui":
        return run_ui(raw[1:])

    parser = argparse.ArgumentParser(
        prog="local-foreman",
        description="Mac-first local agent: the local worker does the work; the remote coach only guides.",
    )
    parser.add_argument("goal", nargs="*", help="one goal, or the word ui")
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
    args = parser.parse_args(raw)

    if args.worker:
        os.environ["LOCAL_FOREMAN_WORKER"] = args.worker
    if args.coach:
        os.environ["LOCAL_FOREMAN_COACH"] = args.coach

    if args.smoke:
        os.environ["LOCAL_FOREMAN_WORKER"] = "mock"
        os.environ["LOCAL_FOREMAN_COACH"] = "mock"
        return run_smoke()

    goal = " ".join(args.goal).strip()
    if not goal:
        parser.print_help()
        return 2
    try:
        return run_goal(goal, user_review=args.review, max_steps=args.max_steps)
    except (RuntimeError, ValueError) as exc:
        print("error: " + str(exc), file=sys.stderr)
        return 1
