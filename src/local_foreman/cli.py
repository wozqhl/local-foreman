"""CLI: python -m local_foreman [goal] | --smoke"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from local_foreman.coach import MockCoach
from local_foreman.loop import ForemanLoop
from local_foreman.worker import MockWorker, WorkerAction


def _root() -> Path:
    env = os.environ.get("LOCAL_FOREMAN_ROOT")
    return Path(env) if env else Path.cwd()


def run_goal(goal: str) -> int:
    loop = ForemanLoop(root=_root())
    result = loop.run(goal)
    print("done=" + result.done_reason)
    print("states=" + " > ".join(result.states))
    if result.verdicts:
        print("verdicts=" + ",".join(result.verdicts))
    if result.last_instruction:
        print("instruction=" + result.last_instruction)
    return 0


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
        if verdict == "halt" and not res.done_reason.startswith("halt"):
            errors.append("halt did not stop: " + res.done_reason)
            continue
        seen.append(verdict)
    if seen == ["continue", "revise", "halt"]:
        print("apply-ok")
    else:
        errors.append("apply verdicts seen=" + str(seen))

    if errors:
        for e in errors:
            print("SMOKE FAIL: " + e, file=sys.stderr)
        return 1
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="local_foreman")
    parser.add_argument("goal", nargs="*", help="one goal for the local worker")
    parser.add_argument("--smoke", action="store_true", help="run mock act/ask/apply checks")
    parser.add_argument("--review", action="store_true", help="force ask (user asked for review)")
    args = parser.parse_args(argv)
    if args.smoke:
        os.environ.setdefault("LOCAL_FOREMAN_WORKER", "mock")
        os.environ.setdefault("LOCAL_FOREMAN_COACH", "mock")
        return run_smoke()
    goal = " ".join(args.goal).strip()
    if not goal:
        parser.print_help()
        return 2
    if args.review:
        loop = ForemanLoop(root=_root(), user_review=True)
        result = loop.run(goal)
        print("done=" + result.done_reason)
        print("states=" + " > ".join(result.states))
        return 0
    return run_goal(goal)
