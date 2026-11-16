"""Parse `codex exec --json` JSONL events.

Verified against codex-cli 0.167 (2026-11-15):
  {"type":"thread.started","thread_id":"<uuid>"}
  {"type":"turn.started"}
  {"type":"item.completed","item":{"type":"agent_message","text":"..."}}
  {"type":"turn.completed","usage":{"input_tokens":..,"cached_input_tokens":..,
      "cache_write_input_tokens":..,"output_tokens":..,"reasoning_output_tokens":..}}
Failures arrive as {"type":"turn.failed","error":{"message":...}} or
{"type":"error","message":...}. No event reports the service tier actually applied, so
callers must record it as unavailable instead of guessing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)
UNAVAILABLE = "unavailable"


def parse_event(line: str) -> dict[str, Any] | None:
    text = line.strip()
    if not text.startswith("{"):
        return None
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


@dataclass(slots=True)
class CodexRunState:
    """What a single `codex exec` run reported about itself."""

    thread_id: str | None = None
    turn_started: bool = False
    turn_completed: bool = False
    usage: dict[str, int | None] = field(
        default_factory=lambda: {name: None for name in USAGE_FIELDS}
    )
    error_messages: list[str] = field(default_factory=list)
    agent_message_chars: int = 0
    event_count: int = 0
    observed_service_tier: str | None = None

    def apply(self, event: dict[str, Any]) -> str | None:
        """Update state from one event; return a short phase label for the UI."""

        self.event_count += 1
        kind = str(event.get("type", ""))
        if kind == "thread.started":
            thread_id = event.get("thread_id")
            if isinstance(thread_id, str) and thread_id.strip():
                self.thread_id = thread_id.strip()
            return "세션 연결"
        if kind == "turn.started":
            self.turn_started = True
            return "모델 응답 대기"
        if kind.startswith("item."):
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                self.agent_message_chars = max(
                    self.agent_message_chars, len(str(item.get("text") or ""))
                )
            return "응답 생성 중"
        if kind == "turn.completed":
            self.turn_completed = True
            usage = event.get("usage")
            if isinstance(usage, dict):
                for name in USAGE_FIELDS:
                    value = usage.get(name)
                    self.usage[name] = int(value) if isinstance(value, int) else None
                tier = usage.get("service_tier") or event.get("service_tier")
                if isinstance(tier, str) and tier:
                    self.observed_service_tier = tier
            return "응답 수신 완료"
        if kind in {"turn.failed", "error"}:
            error = event.get("error")
            message = (
                error.get("message") if isinstance(error, dict) else event.get("message")
            )
            if message:
                self.error_messages.append(str(message)[:2000])
            return "오류 수신"
        return None

    def metrics(self) -> dict[str, Any]:
        return {
            "session_id": self.thread_id,
            **{name: self.usage.get(name) for name in USAGE_FIELDS},
            "observed_service_tier": self.observed_service_tier or UNAVAILABLE,
            "event_count": self.event_count,
        }
