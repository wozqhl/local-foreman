"""Worker protocol + mock + real MLX adapter (mlx-lm import optional)."""

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

DEFAULT_MLX_MODEL = "mlx-community/Qwen3-8B-4bit"


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


def extract_json_object(raw: str) -> Optional[dict[str, Any]]:
    """Pull the first JSON object out of messy model or API text."""
    text = (raw or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < 0:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def parse_action(raw: str) -> WorkerAction:
    data = extract_json_object(raw)
    if data is None:
        text = (raw or "").strip()
        return WorkerAction(kind="unsure", thought=(text[:200] or "unparseable"))
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
    """Apple Silicon worker. Loads mlx-lm once; import is deferred until first step.

    Default weights: mlx-community/Qwen3-8B-4bit (override with LOCAL_FOREMAN_MLX_MODEL).
    Construction is cheap and does not download. Smoke must never call step() on this class.
    """

    DEFAULT_MODEL = DEFAULT_MLX_MODEL

    def __init__(self, model_id: Optional[str] = None, max_tokens: int = 512):
        self.model_id = (
            model_id
            or os.environ.get("LOCAL_FOREMAN_MLX_MODEL")
            or self.DEFAULT_MODEL
        )
        self.max_tokens = max_tokens
        self._model = None
        self._tokenizer = None
        self._generate = None
        self.last_system: str = ""

    def _ensure(self) -> None:
        if self._model is not None:
            return
        try:
            from mlx_lm import generate, load  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "mlx-lm is not installed. On Apple Silicon run: "
                "pip install 'local-foreman[mlx]'. "
                "On this machine use --worker mock or LOCAL_FOREMAN_WORKER=mock."
            ) from exc
        # load() may download weights — never call step() / _ensure() from smoke.
        self._model, self._tokenizer = load(self.model_id)
        self._generate = generate

    def _prompt(self, *, goal: str, system: str, history: list[dict]) -> str:
        user_parts = ["Goal: " + goal]
        if history:
            user_parts.append(
                "Recent history:\n" + json.dumps(history[-6:], ensure_ascii=False)
            )
        user_parts.append("Reply with one JSON action now.")
        user = "\n\n".join(user_parts)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        tok = self._tokenizer
        if (
            tok is not None
            and getattr(tok, "chat_template", None)
            and hasattr(tok, "apply_chat_template")
        ):
            kwargs: dict[str, Any] = {"tokenize": False, "add_generation_prompt": True}
            try:
                return tok.apply_chat_template(messages, enable_thinking=False, **kwargs)
            except TypeError:
                try:
                    return tok.apply_chat_template(messages, **kwargs)
                except Exception:
                    pass
            except Exception:
                pass
        return system + "\n\n" + user + "\n\nJSON action:"

    def _call_generate(self, prompt: str) -> str:
        try:
            raw = self._generate(
                self._model,
                self._tokenizer,
                prompt=prompt,
                max_tokens=self.max_tokens,
            )
        except TypeError:
            raw = self._generate(self._model, self._tokenizer, prompt)
        return raw if isinstance(raw, str) else str(raw)

    def step(self, *, goal: str, system: str, history: list[dict]) -> WorkerAction:
        self.last_system = system
        self._ensure()
        prompt = self._prompt(goal=goal, system=system, history=history)
        return parse_action(self._call_generate(prompt))


def make_worker(
    script: Optional[list[WorkerAction]] = None,
    backend: Optional[str] = None,
) -> Worker:
    name = (backend or os.environ.get("LOCAL_FOREMAN_WORKER") or "mock").strip().lower()
    if name == "mock":
        return MockWorker(script=script)
    if name == "mlx":
        return MlxWorker()
    raise ValueError(f"unknown LOCAL_FOREMAN_WORKER={name!r} (mock|mlx)")
