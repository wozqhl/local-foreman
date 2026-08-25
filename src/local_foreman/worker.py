"""Worker protocol + mock + mlx stub (mlx import optional)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

WORKER_SYSTEM = """You are the local worker. You do the work. The coach only guides.
Reply with one JSON object, no markdown:
  {"kind":"tool","tool":"read|write|shell","args":{...},"thought":"..."}
  {"kind":"done","thought":"..."}
  {"kind":"unsure","thought":"..."}
Tools: read {path}, write {path,content}, shell {cmd}.
Emit unsure when you cannot proceed safely. Do not push remotes yourself.
"""


@dataclass
class WorkerAction:
    kind: str  # tool | done | unsure
    tool: Optional[str] = None
    args: dict[str, Any] = field(default_factory=dict)
    thought: str = ""

    def describe(self) -> str:
        if self.kind == "tool":
            return f"{self.tool} {self.args}"
        return f"{self.kind}: {self.thought}"


class Worker(Protocol):
    def step(self, *, goal: str, system: str, history: list[dict]) -> WorkerAction:
        ...


def parse_action(raw: str) -> WorkerAction:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < 0:
        return WorkerAction(kind="unsure", thought=text[:200] or "unparseable")
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return WorkerAction(kind="unsure", thought="invalid json")
    kind = str(data.get("kind") or "unsure")
    if kind not in {"tool", "done", "unsure"}:
        kind = "unsure"
    return WorkerAction(
        kind=kind,
        tool=data.get("tool"),
        args=dict(data.get("args") or {}),
        thought=str(data.get("thought") or ""),
    )


class MockWorker:
    """Deterministic worker. No model download. Used for smoke."""

    def __init__(self, script: Optional[list[WorkerAction]] = None):
        self.script = list(script or [])
        self.calls = 0
        self.last_system: str = ""

    def step(self, *, goal: str, system: str, history: list[dict]) -> WorkerAction:
        self.calls += 1
        self.last_system = system
        if self.script:
            idx = min(self.calls - 1, len(self.script) - 1)
            return self.script[idx]
        g = goal.lower()
        if "unsure" in g and self.calls == 1:
            return WorkerAction(kind="unsure", thought="local emits unsure")
        if ("push" in g or "remote" in g) and self.calls == 1:
            return WorkerAction(
                kind="tool",
                tool="shell",
                args={"cmd": "git push origin HEAD"},
                thought="fake remote update for ask path",
            )
        if self.calls == 1:
            return WorkerAction(
                kind="tool",
                tool="read",
                args={"path": "README.md"},
                thought="safe read",
            )
        return WorkerAction(kind="done", thought="goal complete")


class MlxWorker:
    """Apple Silicon adapter. Does not import mlx until first step."""

    MODEL_ID = "mlx-community/Qwen3-8B-4bit"

    def __init__(self, model_id: str = MODEL_ID):
        self.model_id = model_id
        self._model = None
        self._tokenizer = None
        self._generate = None

    def _ensure(self) -> None:
        if self._model is not None:
            return
        try:
            from mlx_lm import generate, load  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "mlx-lm is not installed. Worker=mlx needs Apple Silicon. "
                "Set LOCAL_FOREMAN_WORKER=mock on this machine."
            ) from exc
        # load() may download weights — never call this from smoke.
        self._model, self._tokenizer = load(self.model_id)
        self._generate = generate

    def step(self, *, goal: str, system: str, history: list[dict]) -> WorkerAction:
        self._ensure()
        prompt = system + "\n\nGoal: " + goal
        if history:
            prompt += "\n\nHistory:\n" + json.dumps(history[-6:], ensure_ascii=False)
        prompt += "\n\nJSON action:"
        raw = self._generate(self._model, self._tokenizer, prompt=prompt, max_tokens=256)
        return parse_action(raw if isinstance(raw, str) else str(raw))


def make_worker(script: Optional[list[WorkerAction]] = None) -> Worker:
    backend = os.environ.get("LOCAL_FOREMAN_WORKER", "mock").strip().lower()
    if backend == "mock":
        return MockWorker(script=script)
    if backend == "mlx":
        return MlxWorker()
    raise ValueError(f"unknown LOCAL_FOREMAN_WORKER={backend!r} (mock|mlx)")
