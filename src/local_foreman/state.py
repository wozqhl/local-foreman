"""Loop states: act | ask | apply. idle is extra (local think, never coach)."""

from __future__ import annotations

from enum import Enum


class State(str, Enum):
    ACT = "act"
    ASK = "ask"
    APPLY = "apply"
    IDLE = "idle"


# Escalate only for these reasons (see protocol.md).
ESCALATE_TOOL_FAILS_TWICE = "tool_fails_twice"
ESCALATE_GIT_OR_REMOTE = "git_or_remote"
ESCALATE_USER_REVIEW = "user_review"
ESCALATE_UNSURE = "unsure"
