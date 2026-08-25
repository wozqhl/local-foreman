"""Coach protocol + mock + OpenAI-compatible HTTP client."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Optional, Protocol

from local_foreman.ticket import CoachReply, Ticket, validate_reply

COACH_SYSTEM = """You are a remote coach. You do not do the work.
Reply with JSON only: {"verdict":"continue|revise|halt","instruction":"1-2 sentences","next_tool":optional}
No repo dump. Guide or correct. halt if the local plan is unsafe.
"""


class Coach(Protocol):
    def advise(self, ticket: Ticket) -> CoachReply:
        ...


_INSTRUCT = {
    "continue": "Proceed with the proposed next step. Keep all writes local.",
    "revise": "Do not touch remotes. Read local state and pick a safer next step.",
    "halt": "Stop now. Do not mutate remotes or spend. Report and wait.",
}


class MockCoach:
    """Deterministic coach. No network. Used for smoke."""

    def __init__(self, verdicts: Optional[list[str]] = None):
        self._verdicts = list(verdicts or [])
        self.calls: list[Ticket] = []

    def advise(self, ticket: Ticket) -> CoachReply:
        self.calls.append(ticket)
        if self._verdicts:
            verdict = self._verdicts.pop(0)
        elif "halt" in (ticket.proposed_next + ticket.local_guess).lower():
            verdict = "halt"
        elif "revise" in (ticket.proposed_next + ticket.local_guess).lower():
            verdict = "revise"
        else:
            verdict = "continue"
        next_tool = "read" if verdict == "revise" else None
        return validate_reply(
            CoachReply(verdict=verdict, instruction=_INSTRUCT[verdict], next_tool=next_tool)
        )


class OpenAICoach:
    """OpenAI-compatible chat completions client (base_url + api_key env)."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
    ):
        self.base_url = (base_url or os.environ.get("COACH_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
        self.api_key = api_key or os.environ.get("COACH_API_KEY") or ""
        self.model = model or os.environ.get("COACH_MODEL") or "gpt-4o"

    def advise(self, ticket: Ticket) -> CoachReply:
        if not self.api_key:
            raise RuntimeError("COACH_API_KEY is empty; use LOCAL_FOREMAN_COACH=mock for smoke")
        url = self.base_url + "/chat/completions"
        body = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": COACH_SYSTEM},
                {"role": "user", "content": json.dumps(ticket.to_dict(), ensure_ascii=False)},
            ],
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": "Bearer " + self.api_key,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise RuntimeError(f"coach HTTP failed: {exc}") from exc
        content = payload["choices"][0]["message"]["content"]
        text = content if isinstance(content, str) else json.dumps(content)
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end < 0:
            raise RuntimeError("coach returned no JSON object")
        return validate_reply(CoachReply.from_dict(json.loads(text[start : end + 1])))


def make_coach(verdicts: Optional[list[str]] = None) -> Coach:
    backend = os.environ.get("LOCAL_FOREMAN_COACH", "mock").strip().lower()
    if backend == "mock":
        return MockCoach(verdicts=verdicts)
    if backend == "openai":
        return OpenAICoach()
    raise ValueError(f"unknown LOCAL_FOREMAN_COACH={backend!r} (mock|openai)")
