"""Worker protocol + mock + real MLX adapter (mlx-lm import optional)."""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Optional, Protocol
from urllib.error import URLError

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
ENV_LOAD_RETRIES = "LOCAL_FOREMAN_LOAD_RETRIES"
ENV_LOAD_RETRY_SLEEP = "LOCAL_FOREMAN_LOAD_RETRY_SLEEP"
DEFAULT_LOAD_RETRIES = 3
DEFAULT_LOAD_RETRY_SLEEP = 1.0

_LOAD_TRANSIENT_HINTS = (
    "connection",
    "timeout",
    "timed out",
    "network",
    "hub",
    "huggingface",
    "hf.co",
    "download",
    "temporary",
    "unavailable",
    "reset by peer",
    "name resolution",
    "errno",
    "broken pipe",
    "connection reset",
    "connection refused",
    "temporarily",
)

MLX_MISSING_MSG = (
    "mlx-lm is not installed. On Apple Silicon run: "
    "pip install 'local-foreman[mlx]'. "
    "On this machine use --worker mock or LOCAL_FOREMAN_WORKER=mock."
)

LoadEventCb = Callable[[str, dict[str, Any]], None]
MlxLoader = Callable[[str], Any]

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


def resolve_load_retries(explicit: Optional[int] = None) -> int:
    """How many times to try mlx-lm load. Min 1. Default 3."""
    if explicit is not None:
        try:
            n = int(explicit)
            if n >= 1:
                return n
        except (TypeError, ValueError):
            pass
    raw = (os.environ.get(ENV_LOAD_RETRIES) or "").strip()
    if raw:
        try:
            n = int(raw)
            if n >= 1:
                return n
        except ValueError:
            pass
    return DEFAULT_LOAD_RETRIES


def resolve_load_retry_sleep(explicit: Optional[float] = None) -> float:
    """Seconds between load attempts. Default 1; smoke sets 0."""
    if explicit is not None:
        try:
            n = float(explicit)
            if n >= 0:
                return n
        except (TypeError, ValueError):
            pass
    raw = (os.environ.get(ENV_LOAD_RETRY_SLEEP) or "").strip()
    if raw:
        try:
            n = float(raw)
            if n >= 0:
                return n
        except ValueError:
            pass
    return DEFAULT_LOAD_RETRY_SLEEP


def _short_err(exc: BaseException, limit: int = 120) -> str:
    text = str(exc).strip() or type(exc).__name__
    text = " ".join(text.split())
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def is_transient_load_error(exc: BaseException) -> bool:
    """Connection/timeout/OS/URL or hub/network-looking messages."""
    if isinstance(exc, ImportError):
        return False
    if isinstance(exc, (ConnectionError, TimeoutError, OSError, URLError)):
        return True
    msg = str(exc).lower()
    return any(h in msg for h in _LOAD_TRANSIENT_HINTS)


def default_mlx_loader(model_id: str) -> tuple[Any, Any, Any]:
    """Import mlx-lm and load weights. Returns (model, tokenizer, generate)."""
    from mlx_lm import generate, load  # type: ignore

    model, tokenizer = load(model_id)
    return model, tokenizer, generate


def default_on_load(event: str, detail: dict[str, Any]) -> None:
    """Chinese stderr progress for real Mac users. Not a coach ask."""
    model_id = str(detail.get("model_id") or "")
    if event == "start":
        attempt = detail.get("attempt", 1)
        max_attempts = detail.get("max_attempts", 1)
        print(
            f"加载中 {model_id}（第 {attempt}/{max_attempts} 次）",
            file=sys.stderr,
            flush=True,
        )
    elif event == "retry":
        attempt = detail.get("attempt", 1)
        err = detail.get("error") or ""
        print(
            f"加载失败，第 {attempt} 次重试：{err}",
            file=sys.stderr,
            flush=True,
        )
    elif event == "ok":
        print("加载成功", file=sys.stderr, flush=True)
    elif event == "fail":
        attempts = detail.get("attempts", 1)
        err = detail.get("error") or ""
        print(
            f"加载失败（已尝试 {attempts} 次）：{err}\n"
            f"请检查网络或本地权重缓存。可 pip install 'local-foreman[mlx]'，"
            f"或用 --worker mock / LOCAL_FOREMAN_WORKER=mock。",
            file=sys.stderr,
            flush=True,
        )


def load_fail_message(*, model_id: str, attempts: int, error: str) -> str:
    return (
        f"MLX model load failed for {model_id} after {attempts} attempt(s): {error}. "
        f"模型加载失败（已重试）。Check network / local cache, "
        f"pip install 'local-foreman[mlx]', or use --worker mock / "
        f"LOCAL_FOREMAN_WORKER=mock."
    )


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


# Traj / loop kinds that originated from the local worker (assistant turns).
_ASSISTANT_HISTORY_KINDS = frozenset(
    {
        "thought",
        "work",
        "idle_act",
        "self_verify",
        "demo",
        "resumed",
        "lesson",
    }
)


def history_to_chat_turns(
    history: list[dict],
    *,
    limit: int = 8,
) -> list[dict[str, str]]:
    """Map loop / traj history into chat turns for apply_chat_template.

    Does not invent content — only reformats fields already on the entry.
    Non-persist rows use action -> assistant, result -> user.
    Persist traj rows: worker-origin kinds -> assistant; coach / env -> user.
    """
    if not history:
        return []
    recent = [h for h in history[-limit:] if isinstance(h, dict)]
    turns: list[dict[str, str]] = []
    for item in recent:
        if "action" in item or ("result" in item and "kind" not in item and "message" not in item):
            action = str(item.get("action") or "").strip()
            result = str(item.get("result") or "").strip()
            if action:
                turns.append({"role": "assistant", "content": action})
            if result:
                turns.append({"role": "user", "content": "Observation: " + result})
            continue
        kind = str(item.get("kind") or item.get("role") or "event").strip() or "event"
        msg = str(item.get("message") or "").strip()
        obs = str(item.get("observation") or "").strip()
        instruction = str(item.get("instruction") or "").strip()
        if kind in _ASSISTANT_HISTORY_KINDS:
            content = msg or kind
            turns.append({"role": "assistant", "content": content})
            if obs:
                turns.append({"role": "user", "content": "Observation: " + obs})
            continue
        parts: list[str] = []
        if msg:
            parts.append(msg)
        if instruction and kind in (
            "coach_instruction",
            "coach_verdict",
            "asked_coach",
        ):
            parts.append("instruction: " + instruction)
        if obs:
            parts.append("Observation: " + obs)
        body = "\n".join(parts) if parts else kind
        turns.append({"role": "user", "content": f"[{kind}] {body}"})
    return turns


def build_chat_messages(
    *,
    goal: str,
    system: str,
    history: list[dict],
    idle: bool = False,
    limit: int = 8,
) -> list[dict[str, str]]:
    """Full chat message list: system, goal, history turns, final user nudge."""
    messages: list[dict[str, str]] = [{"role": "system", "content": system}]
    messages.append({"role": "user", "content": "Goal: " + goal})
    messages.extend(history_to_chat_turns(history, limit=limit))
    if idle:
        nudge = (
            "Idle local think. Reply with one short JSON "
            '{"kind":"thought","thought":"..."} or a tool action. Do not ask the coach.'
        )
    else:
        nudge = "Reply with one JSON action now."
    if messages and messages[-1]["role"] == "user":
        prev = messages[-1]["content"]
        messages[-1] = {
            "role": "user",
            "content": prev + "\n\n" + nudge if prev else nudge,
        }
    else:
        messages.append({"role": "user", "content": nudge})
    return messages


def format_messages_fallback(messages: list[dict[str, str]]) -> str:
    """Plain-text prompt when no chat_template is available."""
    chunks: list[str] = []
    for m in messages:
        role = m.get("role") or "user"
        content = m.get("content") or ""
        if role == "system":
            chunks.append(content)
        elif role == "assistant":
            chunks.append("Assistant:\n" + content)
        else:
            chunks.append("User:\n" + content)
    chunks.append("JSON action:")
    return "\n\n".join(chunks)


class MlxWorker:
    """Apple Silicon worker. Loads mlx-lm once; import is deferred until first step.

    Default weights: mlx-community/Qwen3-8B-4bit (override with LOCAL_FOREMAN_MLX_MODEL).
    Construction is cheap and does not download. Smoke must never call step() or the
    default loader — inject loader= for load-retry checks instead.
    """

    DEFAULT_MODEL = DEFAULT_MLX_MODEL

    def __init__(
        self,
        model_id: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temp: Optional[float] = None,
        top_p: Optional[float] = None,
        *,
        loader: Optional[MlxLoader] = None,
        on_load: Optional[LoadEventCb] = None,
        load_retries: Optional[int] = None,
        load_retry_sleep: Optional[float] = None,
    ):
        self.model_id = (
            model_id
            or os.environ.get("LOCAL_FOREMAN_MLX_MODEL")
            or self.DEFAULT_MODEL
        )
        self.max_tokens = resolve_max_tokens(max_tokens)
        self.temp = resolve_optional_float(ENV_TEMP, temp)
        self.top_p = resolve_optional_float(ENV_TOP_P, top_p)
        self._loader = loader
        self.on_load: LoadEventCb = (
            default_on_load if on_load is None else on_load
        )
        self.load_retries = resolve_load_retries(load_retries)
        self.load_retry_sleep = resolve_load_retry_sleep(load_retry_sleep)
        self._model = None
        self._tokenizer = None
        self._generate = None
        self.last_system: str = ""

    def _emit_load(self, event: str, **detail: Any) -> None:
        cb = self.on_load
        if cb is None:
            return
        payload = dict(detail)
        payload.setdefault("model_id", self.model_id)
        try:
            cb(event, payload)
        except Exception:
            # Progress must never break load itself.
            pass

    def _unpack_loader_result(self, result: Any) -> tuple[Any, Any, Any]:
        if not isinstance(result, (tuple, list)) or len(result) < 2:
            raise RuntimeError(
                "MlxWorker loader must return (model, tokenizer) or "
                "(model, tokenizer, generate)"
            )
        model, tokenizer = result[0], result[1]
        generate_fn = result[2] if len(result) >= 3 else None
        return model, tokenizer, generate_fn

    def _ensure(self) -> None:
        if self._model is not None:
            return
        loader = self._loader if self._loader is not None else default_mlx_loader
        max_attempts = self.load_retries
        last_exc: Optional[BaseException] = None
        attempts_done = 0
        for attempt in range(1, max_attempts + 1):
            attempts_done = attempt
            self._emit_load(
                "start",
                model_id=self.model_id,
                attempt=attempt,
                max_attempts=max_attempts,
            )
            try:
                model, tokenizer, generate_fn = self._unpack_loader_result(
                    loader(self.model_id)
                )
                self._model = model
                self._tokenizer = tokenizer
                self._generate = generate_fn
                self._emit_load(
                    "ok",
                    model_id=self.model_id,
                    attempts=attempt,
                )
                return
            except ImportError as exc:
                # Missing mlx-lm (or loader signaling the same). Do not retry.
                raise RuntimeError(MLX_MISSING_MSG) from exc
            except Exception as exc:
                last_exc = exc
                transient = is_transient_load_error(exc)
                if attempt < max_attempts and transient:
                    self._emit_load(
                        "retry",
                        model_id=self.model_id,
                        attempt=attempt + 1,
                        error=_short_err(exc),
                    )
                    sleep_s = self.load_retry_sleep
                    if sleep_s and sleep_s > 0:
                        time.sleep(sleep_s)
                    continue
                break
        if attempts_done < 1:
            attempts_done = 1
        err_text = _short_err(last_exc) if last_exc is not None else "unknown"
        self._emit_load(
            "fail",
            model_id=self.model_id,
            attempts=attempts_done,
            error=err_text,
        )
        raise RuntimeError(
            load_fail_message(
                model_id=self.model_id,
                attempts=attempts_done,
                error=err_text,
            )
        ) from last_exc

    def _prompt(self, *, goal: str, system: str, history: list[dict], idle: bool = False) -> str:
        messages = build_chat_messages(
            goal=goal, system=system, history=history, idle=idle
        )
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
        return format_messages_fallback(messages)

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
