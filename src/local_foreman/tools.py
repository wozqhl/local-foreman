"""Tools v1: read / write / shell, with escalate detection and workspace sandbox."""

from __future__ import annotations

import ast
import difflib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

GIT_RO = {
    "status", "log", "diff", "show", "blame", "describe",
    "rev-parse", "rev-list", "ls-files", "ls-tree", "cat-file",
    "symbolic-ref", "name-rev", "shortlog", "version", "help",
}

_REDIRECT_OPS = {">", ">>", "1>", "2>", "&>", "1>>", "2>>", ">&"}


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


def resolve_under_root(path: Union[str, Path], root: Optional[Path]) -> Optional[Path]:
    """Resolve path; if root is set, require it stays under root (symlink-aware).

    Returns None when root is set and the path escapes. When root is None,
    resolve normally and return the path (legacy / no sandbox).
    """
    raw = Path(path).expanduser()
    if root is None:
        try:
            return raw.resolve()
        except OSError:
            return raw

    root_r = root.resolve()
    candidate = raw if raw.is_absolute() else (root_r / raw)
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    try:
        resolved.relative_to(root_r)
    except ValueError:
        return None
    return resolved


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


def _git_after_sub(cmd: str) -> list[str]:
    p = _parts(cmd)
    sub = _git_sub(cmd)
    if sub is None:
        return []
    i = 1
    while i < len(p):
        if p[i] in {"-C", "-c"} and i + 1 < len(p):
            i += 2
            continue
        if p[i].startswith("-") and p[i] != "--":
            i += 1
            continue
        if p[i] == sub:
            return p[i + 1 :]
        i += 1
    return []


def _git_readonly(cmd: str) -> bool:
    """True when this is a git command that only reads state."""
    sub = _git_sub(cmd)
    if sub is None:
        return False
    if sub in GIT_RO:
        return True
    after = _git_after_sub(cmd)

    if sub == "branch":
        mutate_flags = {
            "-d", "-D", "-m", "-M", "-c", "-C",
            "--delete", "--move", "--copy",
        }
        if any(
            x in mutate_flags
            or x.startswith("--delete")
            or x.startswith("--move")
            or x.startswith("--copy")
            for x in after
        ):
            return False
        # bare `git branch name` creates a branch
        if any(not x.startswith("-") for x in after):
            return False
        return True

    if sub == "stash":
        return bool(after) and after[0] in {"list", "show"}

    if sub == "remote":
        if not after or all(x.startswith("-") for x in after):
            return True  # git remote / git remote -v
        if after[0] in {"show", "get-url"}:
            return True
        return False

    if sub == "config":
        ro_exact = {
            "--get", "--list", "-l", "--get-regexp", "--get-all",
            "--get-urlmatch", "--name-only", "--show-origin", "--show-scope",
        }
        if any(x in ro_exact or x.startswith("--get") for x in after):
            return True
        return False

    if sub == "tag":
        mutate_flags = {
            "-d", "-a", "-m", "-f", "-s", "-u",
            "--delete", "--annotate", "--message", "--force", "--sign",
            "--local-user",
        }
        if any(
            x in mutate_flags
            or x.startswith("--delete")
            or x.startswith("--annotate")
            or x.startswith("--message")
            or x.startswith("--force")
            or x.startswith("--sign")
            or x.startswith("--local-user")
            for x in after
        ):
            return False
        if any(x in {"-l", "--list"} or x.startswith("--list") for x in after):
            return True
        if not after:
            return True  # git tag → list
        if any(not x.startswith("-") for x in after):
            return False  # git tag name → create
        return True

    if sub == "worktree":
        return bool(after) and after[0] == "list"

    return False


def is_git_push(cmd: str) -> bool:
    p = _parts(cmd)
    return bool(p) and p[0] == "git" and "push" in p[1:]


def is_git_mutate(cmd: str) -> bool:
    sub = _git_sub(cmd)
    if sub is None:
        return False
    return not _git_readonly(cmd)


def is_git_ro(cmd: str) -> bool:
    return _git_readonly(cmd)


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


def _looks_like_path_token(tok: str) -> bool:
    if not tok or tok in _REDIRECT_OPS:
        return False
    if tok.startswith("-") and not tok.startswith("/"):
        return False
    if tok.startswith("/") or tok.startswith("~"):
        return True
    if ".." in Path(tok).parts:
        return True
    return False


def _shell_path_candidates(cmd: str) -> list[str]:
    """Pragmatic path tokens from a shell command (whitespace split + redirects)."""
    parts = _parts(cmd)
    out: list[str] = []
    i = 0
    while i < len(parts):
        t = parts[i]
        attached = False
        for op in sorted(_REDIRECT_OPS, key=len, reverse=True):
            if t.startswith(op) and len(t) > len(op):
                out.append(t[len(op):])
                attached = True
                break
        if attached:
            i += 1
            continue
        if t in _REDIRECT_OPS and i + 1 < len(parts):
            out.append(parts[i + 1])
            i += 2
            continue
        if t == "cd" and i + 1 < len(parts):
            nxt = parts[i + 1]
            if nxt not in _REDIRECT_OPS and not (
                nxt.startswith("-") and not nxt.startswith("/")
            ):
                out.append(nxt)
                i += 2
                continue
        if _looks_like_path_token(t):
            out.append(t)
        i += 1
    return out


def shell_escapes_root(cmd: str, root: Path) -> Optional[str]:
    """Return a sandbox reason if cmd clearly targets outside root; else None."""
    root_r = root.resolve()
    for tok in _shell_path_candidates(cmd):
        # Relative without .. stays under cwd=root — allow.
        p = Path(tok).expanduser()
        if not p.is_absolute() and ".." not in p.parts:
            continue
        if resolve_under_root(tok, root_r) is None:
            return f"sandbox: path escapes workspace root: {tok}"
    return None


def read_file(path: str, *, root: Optional[Path] = None) -> ToolResult:
    if root is not None:
        pth = resolve_under_root(path, root)
        if pth is None:
            return ToolResult(
                ok=False,
                output=f"sandbox: path escapes workspace root: {path}",
                escalated=False,
                risk="none",
            )
    else:
        pth = Path(path)
        if not pth.is_absolute() and root is not None:
            pth = root / pth
    try:
        text = pth.read_text(encoding="utf-8")
        if len(text) > 8000:
            text = text[:8000] + "\n...[truncated]"
        return ToolResult(ok=True, output=text, risk="none")
    except Exception as exc:
        return ToolResult(ok=False, output=f"read failed: {exc}", risk="none")


def write_file(path: str, content: str, *, root: Optional[Path] = None) -> ToolResult:
    if root is not None:
        pth = resolve_under_root(path, root)
        if pth is None:
            return ToolResult(
                ok=False,
                output=f"sandbox: path escapes workspace root: {path}",
                escalated=False,
                risk="write",
            )
    else:
        pth = Path(path)
        if not pth.is_absolute() and root is not None:
            pth = root / pth

    ask, reason, risk = needs_ask("write", {"path": path})
    if ask:
        return ToolResult(ok=False, output=f"blocked write (needs ask): {path}",
                          escalated=True, escalate_reason=reason, risk=risk)
    try:
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
    if root is not None:
        escape = shell_escapes_root(cmd, root)
        if escape:
            return ToolResult(ok=False, output=escape, escalated=False, risk="none")
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


def draft_excerpt(path: str, content: str, *, limit: int = 240, root: Optional[Path] = None) -> str:
    """Back-compat wrapper. Prefer draft_diff for verify tickets."""
    return draft_diff(path, content, root=root, limit=limit)


def is_readonly_speculate(tool: str, args: dict) -> bool:
    """Speculative Actions Assumption 2: only read / git-ro. Never a write."""
    if tool == "read":
        return True
    if tool == "shell":
        cmd = str(args.get("cmd") or args.get("command") or "")
        return is_git_ro(cmd)
    return False


def local_check_write(path: str, content: str) -> Optional[bool]:
    """CRITIC-style local check. True=pass (skip verify), False=fail, None=no check."""
    name = str(path or "")
    if name.endswith(".py"):
        try:
            ast.parse(str(content or ""))
            return True
        except SyntaxError:
            return False
    return None


def draft_diff(path: str, new_content: str, *, root: Optional[Path] = None, limit: int = 480) -> str:
    """Aider-style editor diff: path + truncated unified-diff (or excerpt)."""
    path = str(path or "").strip()
    new_text = str(new_content or "")
    if root is not None and path:
        pth = resolve_under_root(path, root)
        if pth is None:
            body = f"sandbox: path escapes workspace root: {path}"
            if len(body) > limit:
                body = body[: limit - 3] + "..."
            return body
    else:
        pth = None

    old_text = ""
    try:
        if pth is None:
            pth = Path(path)
            if not pth.is_absolute() and root is not None:
                pth = root / pth
        if pth.is_file():
            old_text = pth.read_text(encoding="utf-8")
    except OSError:
        old_text = ""
    label = path or "draft"
    if old_text == new_text:
        body = f"{label}: (no change)"
    elif old_text or new_text:
        diff = list(
            difflib.unified_diff(
                old_text.splitlines(),
                new_text.splitlines(),
                fromfile="a/" + label,
                tofile="b/" + label,
                lineterm="",
            )
        )
        body = "\n".join(diff) if diff else f"{label}: {new_text[:200]}"
    else:
        body = label
    if len(body) > limit:
        body = body[: limit - 3] + "..."
    return body
