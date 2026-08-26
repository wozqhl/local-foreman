"""Loop states: act | verify | ask | apply. idle is extra (local think, never coach)."""

from __future__ import annotations

from enum import Enum


class State(str, Enum):
    ACT = "act"
    VERIFY = "verify"
    ASK = "ask"
    APPLY = "apply"
    IDLE = "idle"


# Escalate only for these reasons (see protocol.md). HIGH lane.
ESCALATE_TOOL_FAILS_TWICE = "tool_fails_twice"
ESCALATE_GIT_OR_REMOTE = "git_or_remote"
ESCALATE_USER_REVIEW = "user_review"
ESCALATE_UNSURE = "unsure"

# MID lane (verify), not an ask.
VERIFY_WRITE = "verify_write"
VERIFY_CONFIDENCE = "verify_confidence"
VERIFY_FAIL_ONCE = "verify_fail_once"
