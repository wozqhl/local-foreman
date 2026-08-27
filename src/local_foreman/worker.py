"""Worker protocol + mock + real MLX adapter (mlx-lm import optional)."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, replace
from typing import Any, Optional, Protocol

WORKER_SYSTEM = """You are the local worker. You do the work. The coach only guides.
Reply with one JSON object, no markdown:
  {"kind":"tool","tool":"read|write|shell","args":{...},"thought":"..."}
  {"kind":"done","thought":"..."}
  {"kind":"unsure","thought":"..."}
  {"kind":"thought","thought":"..."}
Tools: read {path}, write {path,content}, shell {cmd}.
Emit unsure when you cannot proceed safely. Do not push remotes yourself.
When idle, emit a short local {"kind":"thought"} monologue. Idle thoughts never
ask the coach. If you want a tool while idle, emit kind=tool; it still goes
through act and the existing escalate rules. Compacted trajectory summaries
may be expanded back to raw jsonl under ## Retrieved; that is local, not coach.
Local EcoAssistant demos may appear under ## Local demos; they are accepted
local writes, never coach rewrites.
"""

DEFAULT_MLX_MODEL = "mlx-community/Qwen3-8B-4bit"
DEFAULT_MAX_TOKENS = 512
ENV_MAX_TOKENS = "LOCAL_FOREMAN_MAX_TOKENS"
ENV_TEMP = "LOCAL_FOREMAN_TEMP"
ENV_TOP_P = "LOCAL_FOREMAN_TOP_P"

ACTION_KINDS = {"tool", "done", "unsure", "thought"}

# Qwen3 official thinking fences (docs: <think>...</think>).
# Qwen3-thinking chat templates often inject the opener, so output may
# only contain </think>. Also strip <thinking> and <redacted_reasoning>.
_THINK_TAG = r"(?:think|thinking|redacted_reasoning)"
_THINK_PAIR_RE = re.compile(
    rf"<{_THINK_TAG}>\s*.*?</{_THINK_TAG}>",
    re.IGNORECASE | re.DOTALL,
)
_THINK_CLOSE_RE = re.compile(rf"</{_THINK_TAG}>", re.IGNORECASE)
_THINK_OPEN_RE = re.compile(rf"^<{_THINK_TAG}>\s*", re.IGNORECASE)


@dataclass
class WorkerAction:
    kind: str  # tool | done | unsure | thought
    tool: Optional[str] = None
    args: dict[str, Any] = field(default_factory=dict)
    thought: str = ""
    confidence: Optional[float] = None

    def describe(self) -> str:
        if self.kind == "tool":
            return f"{self.tool} {self.args}"
        return f"{self.kind}: {self.thought}"


def resolve_confidence(action: WorkerAction) -> float:
    """Missing confidence: 1.0 for read / other, 0.4 for write (MID verify)."""
    if action.confidence is not None:
        try:
            return min(1.0, max(0.0, float(action.confidence)))
        except (TypeError, ValueError):
            pass
    if action.kind == "tool" and (action.tool or "") == "write":
        return 0.4
    return 1.0


def with_resolved_confidence(action: WorkerAction) -> WorkerAction:
    if action.confidence is not None:
        try:
            c = min(1.0, max(0.0, float(action.confidence)))
            if c == action.confidence:
                return action
            return replace(action, confidence=c)
        except (TypeError, ValueError):
            pass
    return replace(action, confidence=resolve_confidence(action))


class Worker(Protocol):
    def step(self, *, goal: str, system: str, history: list[dict]) -> WorkerAction:
        ...

    def think(self, *, goal: str, system: str, history: list[dict]) -> WorkerAction:
        ...


def resolve_max_tokens(explicit: Optional[int] = None) -> int:
    """MLX generate length. explicit, else LOCAL_FOREMAN_MAX_TOKENS, else 512."""
    if explicit is not None:
        try:
            n = int(explicit)
            if n >= 1:
                return n
        except (TypeError, ValueError):
            pass
    raw = (os.environ.get(ENV_MAX_TOKENS) or "").strip()
    if raw:
        try:
            n = int(raw)
            if n >= 1:
                return n
        except ValueError:
            pass
    return DEFAULT_MAX_TOKENS


def resolve_optional_float(name: str, explicit: Optional[float] = None) -> Optional[float]:
    """Sampling knob. Unset / invalid -> None (do not pass a sampler)."""
    if explicit is not None:
        try:
            return float(explicit)
        except (TypeError, ValueError):
            pass
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def strip_thinking(raw: str) -> str:
    """Remove Qwen3 thinking wrappers. Does not invent replacement text.

    Official Qwen3 uses <think>...</think>. Hybrid/thinking templates may
    leave only a dangling </think> (rindex, same as Qwen parser).
    JSON after the last close tag is kept; fences around JSON are left
    for extract_json_object.
    """
    text = raw if isinstance(raw, str) else ("" if raw is None else str(raw))
    if not text:
        return ""
    cleaned = _THINK_PAIR_RE.sub("", text)
    last = None
    for match in _THINK_CLOSE_RE.finditer(cleaned):
        last = match
    if last is not None:
        cleaned = cleaned[last.end() :]
    cleaned = _THINK_OPEN_RE.sub("", cleaned.lstrip())
    return cleaned.strip()


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
    cleaned = strip_thinking(raw)
    data = extract_json_object(cleaned)
    if data is None:
        text = (cleaned or raw or "").strip()
        return WorkerAction(kind="unsure", thought=(text[:200] or "unparseable"))
    kind = str(data.get("kind") or "unsure")
    if kind not in ACTION_KINDS:
        kind = "unsure"
    raw_c = data.get("confidence")
    conf = None
    if raw_c is not None and raw_c != "":
        try:
            conf = min(1.0, max(0.0, float(raw_c)))
        except (TypeError, ValueError):
            conf = None
    return WorkerAction(
        kind=kind,
        tool=data.get("tool"),
        args=dict(data.get("args") or {}),
        thought=str(data.get("thought") or ""),
        confidence=conf,
    )


class MockWorker:
    """Deterministic worker. No model download. Used for smoke."""

    def __init__(
        self,
        script: Optional[list[WorkerAction]] = None,
        think_script: Optional[list[WorkerAction]] = None,
    ):
        self.script = list(script or [])
        self.think_script = list(think_script or [])
        self.calls = 0
        self.think_calls = 0
        self.last_system: str = ""

    def step(self, *, goal: str, system: str, history: list[dict]) -> WorkerAction:
        self.calls += 1
        self.last_system = system
        if self.script:
            idx = min(self.calls - 1, len(self.script) - 1)
            return with_resolved_confidence(self.script[idx])
        g = goal.lower()
        if "unsure" in g and self.calls == 1:
            return WorkerAction(kind="unsure", thought="local emits unsure", confidence=0.0)
        if ("push" in g or "remote" in g) and self.calls == 1:
            return WorkerAction(
                kind="tool",
                tool="shell",
                args={"cmd": "git push origin HEAD"},
                thought="fake remote update for ask path",
                confidence=0.2,
            )
        if self.calls == 1:
            return WorkerAction(
                kind="tool",
                tool="read",
                args={"path": "README.md"},
                thought="safe read",
                confidence=1.0,
            )
        return WorkerAction(kind="done", thought="goal complete", confidence=1.0)

    def think(self, *, goal: str, system: str, history: list[dict]) -> WorkerAction:
        self.think_calls += 1
        self.last_system = system
        if self.think_script:
            idx = min(self.think_calls - 1, len(self.think_script) - 1)
            return self.think_script[idx]
        note = goal.strip() or "the current goal"
        if len(note) > 80:
            note = note[:77] + "..."
        return WorkerAction(
            kind="thought",
            thought=f"空转：还在本地想「{note}」，没有要升级教练的事",
        )


class MlxWorker:
    """Apple Silicon worker. Loads mlx-lm once; import is deferred until first step.

    Default weights: mlx-community/Qwen3-8B-4bit (override with LOCAL_FOREMAN_MLX_MODEL).
    Construction is cheap and does not download. Smoke must never call step() on this class.
    """

    DEFAULT_MODEL = DEFAULT_MLX_MODEL

    def __init__(
        self,
        model_id: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temp: Optional[float] = None,
        top_p: Optional[float] = None,
    ):
        self.model_id = (
            model_id
            or os.environ.get("LOCAL_FOREMAN_MLX_MODEL")
            or self.DEFAULT_MODEL
        )
        self.max_tokens = resolve_max_tokens(max_tokens)
        self.temp = resolve_optional_float(ENV_TEMP, temp)
        self.top_p = resolve_optional_float(ENV_TOP_P, top_p)
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

    def _prompt(self, *, goal: str, system: str, history: list[dict], idle: bool = False) -> str:
        user_parts = ["Goal: " + goal]
        if history:
            user_parts.append(
                "Recent history:\n" + json.dumps(history[-8:], ensure_ascii=False)
            )
        if idle:
            user_parts.append(
                "Idle local think. Reply with one short JSON "
                '{"kind":"thought","thought":"..."} or a tool action. Do not ask the coach.'
            )
        else:
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

    def _sampler(self):
        """Build mlx-lm sampler only when temp/top_p are set. Else None."""
        if self.temp is None and self.top_p is None:
            return None
        try:
            from mlx_lm.sample_utils import make_sampler  # type: ignore
        except ImportError:
            return None
        kwargs: dict[str, Any] = {}
        if self.temp is not None:
            kwargs["temp"] = float(self.temp)
        if self.top_p is not None:
            kwargs["top_p"] = float(self.top_p)
        return make_sampler(**kwargs)

    def _call_generate(self, prompt: str) -> str:
        kwargs: dict[str, Any] = {
            "prompt": prompt,
            "max_tokens": self.max_tokens,
        }
        sampler = self._sampler()
        if sampler is not None:
            kwargs["sampler"] = sampler
        try:
            raw = self._generate(self._model, self._tokenizer, **kwargs)
        except TypeError:
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
        prompt = self._prompt(goal=goal, system=system, history=history, idle=False)
        return parse_action(self._call_generate(prompt))

    def think(self, *, goal: str, system: str, history: list[dict]) -> WorkerAction:
        self.last_system = system
        self._ensure()
        prompt = self._prompt(goal=goal, system=system, history=history, idle=True)
        action = parse_action(self._call_generate(prompt))
        if action.kind == "unsure" and not action.thought:
            action = WorkerAction(kind="thought", thought="idle: still here, no coach")
        return action


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
