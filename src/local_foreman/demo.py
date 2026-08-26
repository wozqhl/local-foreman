"""EcoAssistant-style local demo cache.

On verify accept (file landed) store a compact
{goal, task_sketch, claim, path, draft excerpt} in
`.local-foreman/demos.jsonl` (or LOCAL_FOREMAN_DEMOS).
Later similar writes (path or goal overlap) inject 1-2 demos
into the worker system prompt.

Local-only. Never store coach rewrites / instructions / verdicts.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Optional

from local_foreman.traj import default_state_dir

ENV_DEMOS = "LOCAL_FOREMAN_DEMOS"
DEMO_FILENAME = "demos.jsonl"
DEMO_CONTEXT_HEADER = "## Local demos (EcoAssistant, local-only)"

MAX_GOAL = 160
MAX_SKETCH = 80
MAX_CLAIM = 160
MAX_DRAFT = 200
MAX_PATH = 160
MAX_CACHE = 32
MAX_INJECT = 2

# Never persist coach-side rewrite material.
FORBIDDEN_KEYS = (
    "instruction",
    "rewrite",
    "coach_rewrite",
    "verdict",
    "coach",
    "reply",
)


def default_demo_path(cwd: Optional[Path] = None) -> Path:
    raw = (os.environ.get(ENV_DEMOS) or "").strip()
    if raw:
        return Path(raw)
    return default_state_dir(cwd) / DEMO_FILENAME


def _short(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def task_sketch(goal: str, path: str = "") -> str:
    sketch = _short(goal, MAX_SKETCH)
    suffix = Path(str(path or "")).suffix.lower()
    if suffix and suffix not in sketch:
        room = MAX_SKETCH - len(sketch) - 1
        if room > 0:
            sketch = (sketch + " " + suffix).strip()
            sketch = _short(sketch, MAX_SKETCH)
    return sketch


def compact_demo(
    *,
    goal: str,
    claim: str,
    path: str,
    draft: str = "",
) -> dict[str, str]:
    """Compact local triple. Coach rewrite fields are dropped."""
    rec = {
        "goal": _short(goal, MAX_GOAL),
        "task_sketch": task_sketch(goal, path),
        "claim": _short(claim, MAX_CLAIM),
        "path": _short(path, MAX_PATH),
        "draft": _short(draft or path, MAX_DRAFT),
    }
    for key in FORBIDDEN_KEYS:
        rec.pop(key, None)
    return rec


def _sanitize(rec: dict[str, Any]) -> dict[str, str]:
    clean = {
        "goal": _short(str(rec.get("goal") or ""), MAX_GOAL),
        "task_sketch": _short(str(rec.get("task_sketch") or rec.get("goal") or ""), MAX_SKETCH),
        "claim": _short(str(rec.get("claim") or ""), MAX_CLAIM),
        "path": _short(str(rec.get("path") or ""), MAX_PATH),
        "draft": _short(str(rec.get("draft") or ""), MAX_DRAFT),
    }
    return clean


def store_demo(path: Path, rec: dict[str, Any]) -> dict[str, str]:
    dest = Path(path)
    clean = _sanitize(rec)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(clean, ensure_ascii=False) + "\n")
        fh.flush()
    return clean


def load_demos(path: Path, *, limit: int = MAX_CACHE) -> list[dict[str, str]]:
    src = Path(path)
    if not src.is_file():
        return []
    out: list[dict[str, str]] = []
    text = src.read_text(encoding="utf-8")
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        out.append(_sanitize(rec))
    if limit and len(out) > limit:
        return out[-limit:]
    return out


_TOKEN = re.compile(r"[a-z0-9_\-\.]+")


def _tokens(text: str) -> set[str]:
    return set(_TOKEN.findall(str(text or "").lower()))


def _similarity(demo: dict[str, str], *, goal: str, path: str = "") -> float:
    left = (
        _tokens(demo.get("task_sketch") or "")
        | _tokens(demo.get("goal") or "")
        | _tokens(demo.get("path") or "")
    )
    right = _tokens(goal) | _tokens(path)
    if not left or not right:
        return 0.0
    inter = left & right
    if not inter:
        return 0.0
    score = len(inter) / len(left | right)
    demo_path = str(demo.get("path") or "")
    if path and demo_path:
        if Path(demo_path).suffix.lower() == Path(path).suffix.lower() and Path(path).suffix:
            score += 0.1
        if Path(demo_path).name == Path(path).name:
            score += 0.15
    return score


def similar_demos(
    demos: list[dict[str, str]],
    *,
    goal: str,
    path: str = "",
    k: int = MAX_INJECT,
) -> list[dict[str, str]]:
    """Return up to k similar local demos. Empty if nothing overlaps."""
    ranked: list[tuple[float, dict[str, str]]] = []
    for demo in demos:
        score = _similarity(demo, goal=goal, path=path)
        if score <= 0:
            continue
        ranked.append((score, demo))
    ranked.sort(key=lambda item: item[0], reverse=True)
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for _score, demo in ranked:
        key = (demo.get("path") or "", demo.get("claim") or "")
        if key in seen:
            continue
        seen.add(key)
        out.append(demo)
        if len(out) >= max(1, int(k)):
            break
    return out


def render_demos(demos: list[dict[str, str]]) -> str:
    lines: list[str] = []
    for i, demo in enumerate(demos[:MAX_INJECT], start=1):
        sketch = demo.get("task_sketch") or ""
        claim = demo.get("claim") or ""
        path = demo.get("path") or ""
        draft = demo.get("draft") or ""
        lines.append(f"{i}. sketch={sketch}")
        if claim:
            lines.append(f"   claim={claim}")
        if path:
            lines.append(f"   path={path}")
        if draft and draft != path:
            lines.append(f"   draft={draft}")
    return "\n".join(lines)
