"""Tools v1: read / write / shell, with escalate detection."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

GIT_RO = {
    "status", "log", "diff", "show", "blame", "describe",
    "rev-parse", "rev-list", "ls-files", "ls-tree", "cat-file",
    "symbolic-ref", "name-rev", "shortlog", "version", "help",
}


@dataclass
class ToolResult:
    ok: bool
    output: str
    escalated: bool = False
    escalate_reason: Optional[str] = None
    risk: str = "none"

    def short(self) -> str:
        flag = "ok" if self.ok else "fail"
        text = " ".join(self.output.split())
        if len(text) > 200:
            text = text[:197] + "..."
        return f"{flag}: {text}"


def _parts(cmd: str) -> list[str]:
    return cmd.strip().split()


def _git_sub(cmd: str) -> Optional[str]:
    p = _parts(cmd)
    if not p or p[0] != "git":
        return None
    i = 1
    while i < len(p):
        if p[i] in {"-C", "-c"} and i + 1 < len(p):
            i += 2
            continue
        if p[i].startswith("-") and p[i] != "--":
            i += 1
            continue
        return p[i]
    return None


def is_git_push(cmd: str) -> bool:
    p = _parts(cmd)
    return bool(p) and p[0] == "git" and "push" in p[1:]


def is_git_mutate(cmd: str) -> bool:
    sub = _git_sub(cmd)
    if sub is None:
        return False
    if sub in GIT_RO:
        return False
    p = _parts(cmd)
    if sub == "branch" and not any(
        x.startswith("-d") or x.startswith("-D") or x == "-m" for x in p
    ):
        return False
    if sub == "stash" and len(p) > 2 and p[2] in {"list", "show"}:
        return False
    return True


def is_remote_write(cmd: str) -> bool:
    n = " ".join(_parts(cmd))
    if n.startswith("gh "):
        return True
    return "remote add" in n or "remote set-url" in n


def is_spend(cmd: str) -> bool:
    n = " ".join(_parts(cmd)).lower()
    return "stripe " in n or n.startswith("stripe")


def classify_risk(tool: str, args: dict) -> str:
    if tool != "shell":
        return "write" if tool == "write" else "none"
    cmd = str(args.get("cmd") or args.get("command") or "")
    if is_git_push(cmd):
        return "push"
    if is_spend(cmd):
        return "spend"
    if is_git_mutate(cmd) or is_remote_write(cmd):
        return "write"
    return "none"


def needs_ask(tool: str, args: dict) -> tuple[bool, str, str]:
    risk = classify_risk(tool, args)
    if tool == "shell":
        cmd = str(args.get("cmd") or args.get("command") or "")
        hit = is_git_push(cmd) or is_remote_write(cmd) or is_git_mutate(cmd) or is_spend(cmd)
        if hit:
            return True, "git_or_remote", risk if risk != "none" else "write"
    if tool == "write":
        path = str(args.get("path") or "")
        if ".git" in Path(path).parts:
            return True, "git_or_remote", "write"
    return False, "", risk


def read_file(path: str, *, root: Optional[Path] = None) -> ToolResult:
    try:
        pth = Path(path)
        if not pth.is_absolute() and root is not None:
            pth = root / pth
        text = pth.read_text(encoding="utf-8")
        if len(text) > 8000:
            text = text[:8000] + "\n...[truncated]"
        return ToolResult(ok=True, output=text, risk="none")
    except Exception as exc:
        return ToolResult(ok=False, output=f"read failed: {exc}", risk="none")


def write_file(path: str, content: str, *, root: Optional[Path] = None) -> ToolResult:
    ask, reason, risk = needs_ask("write", {"path": path})
    if ask:
        return ToolResult(ok=False, output=f"blocked write (needs ask): {path}",
                          escalated=True, escalate_reason=reason, risk=risk)
    try:
        pth = Path(path)
        if not pth.is_absolute() and root is not None:
            pth = root / pth
        pth.parent.mkdir(parents=True, exist_ok=True)
        pth.write_text(content, encoding="utf-8")
        return ToolResult(ok=True, output=f"wrote {pth} ({len(content)} bytes)", risk="write")
    except Exception as exc:
        return ToolResult(ok=False, output=f"write failed: {exc}", risk="write")


def run_command(cmd: str, *, root: Optional[Path] = None, timeout: int = 30) -> ToolResult:
    ask, reason, risk = needs_ask("shell", {"cmd": cmd})
    if ask:
        return ToolResult(ok=False, output=f"blocked command (needs ask): {cmd}",
                          escalated=True, escalate_reason=reason, risk=risk)
    try:
        proc = subprocess.run(
            cmd, shell=True, cwd=str(root) if root else None,
            capture_output=True, text=True, timeout=timeout, env=os.environ.copy(),
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        if not out.strip():
            out = f"exit {proc.returncode}"
        return ToolResult(ok=proc.returncode == 0, output=out, risk="none")
    except Exception as exc:
        return ToolResult(ok=False, output=f"command failed: {exc}", risk="none")


def execute(tool: str, args: dict, *, root: Optional[Path] = None) -> ToolResult:
    if tool == "read":
        return read_file(str(args.get("path", "")), root=root)
    if tool == "write":
        return write_file(str(args.get("path", "")), str(args.get("content", "")), root=root)
    if tool == "shell":
        return run_command(str(args.get("cmd") or args.get("command") or ""), root=root)
    return ToolResult(ok=False, output=f"unknown tool: {tool}")
