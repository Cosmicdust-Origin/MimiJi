"""Fake `codex exec` for tests. Never contacts a model.

Behaviour is scripted by a JSON plan file named in FAKE_CODEX_PLAN: a list of steps,
consumed one per invocation (the last step repeats). Every invocation is appended to
<plan>.log as JSON: argv, resume session id, target raw_ids.

Step keys:
  kind: ok | usage_limit | auth | refusal | invalid | sleep | quiet_work
  drop: raw_ids to leave out of an ok response
  usage: dict for turn.completed.usage (omitted when absent)
Event shapes follow codex-cli 0.167 `exec --json` (verified 2026-11-15).
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def main() -> int:
    argv = sys.argv[1:]
    plan_path = Path(os.environ["FAKE_CODEX_PLAN"])
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    counter = plan_path.with_suffix(".count")
    index = int(counter.read_text()) if counter.exists() else 0
    counter.write_text(str(index + 1))
    step = plan[min(index, len(plan) - 1)]

    sys.stdout.reconfigure(encoding="utf-8")
    # The parent writes UTF-8 text (with Windows newlines), whatever the console code page.
    prompt = sys.stdin.buffer.read().decode("utf-8").replace("\r\n", "\n")
    target_ids: list[int] = []
    if "입력 데이터:\n" in prompt:
        payload = json.loads(prompt.split("입력 데이터:\n", 1)[1])
        target_ids = list(payload.get("target_raw_ids", []))
    resume_id = None
    if "resume" in argv:
        resume_id = argv[-2]
    output_path = Path(argv[argv.index("--output-last-message") + 1])
    with plan_path.with_suffix(".log").open("a", encoding="utf-8") as log:
        log.write(
            json.dumps(
                {"argv": argv, "resume": resume_id, "targets": target_ids},
                ensure_ascii=False,
            )
            + "\n"
        )

    thread_id = resume_id or f"thread-{index}"

    def emit(event: dict) -> None:
        sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    emit({"type": "thread.started", "thread_id": thread_id})
    emit({"type": "turn.started"})
    kind = step.get("kind", "ok")
    # Like real Codex, keep a session log that grows while the model works.
    session_log = None
    if os.environ.get("CODEX_HOME"):
        folder = Path(os.environ["CODEX_HOME"]) / "sessions" / "2026" / "11" / "16"
        folder.mkdir(parents=True, exist_ok=True)
        session_log = folder / f"rollout-2026-11-16T00-00-00-{thread_id}.jsonl"
        with session_log.open("a", encoding="utf-8") as handle:
            handle.write("{}\n")
    if kind == "quiet_work":
        # Reasoning for a while with no stdout, but the session log keeps growing.
        for _ in range(int(step.get("seconds", 3) / 0.3)):
            time.sleep(0.3)
            if session_log is not None:
                with session_log.open("a", encoding="utf-8") as handle:
                    handle.write("{}\n")
        kind = "ok"
    if kind == "sleep":
        time.sleep(60)
        return 0
    if kind in {"usage_limit", "auth"}:
        sys.stderr.write("user\n" + prompt + "\n")  # CLI echoes the prompt on failure
        message = (
            "You've hit your usage limit. Try again later."
            if kind == "usage_limit"
            else "401 Unauthorized: not logged in"
        )
        emit({"type": "error", "message": message})
        emit({"type": "turn.failed", "error": {"message": message}})
        return 1
    if kind == "refusal":
        text = "I can't help with that request."
        emit({"type": "item.completed", "item": {"type": "agent_message", "text": text}})
        output_path.write_text(text, encoding="utf-8")
        emit({"type": "turn.completed"})
        return 0
    if kind == "invalid":
        output_path.write_text("not json", encoding="utf-8")
        emit({"type": "turn.completed"})
        return 0
    drop = set(step.get("drop", []))
    items = [
        {
            "raw_id": raw_id,
            "ja_fixed": f"日本語{raw_id}",
            "ko": f"한국어 {raw_id}",
            "action": "translated",
            "merge_with_previous": False,
            "reason": None,
            "register_note": None,
        }
        for raw_id in target_ids
        if raw_id not in drop
    ]
    text = json.dumps({"items": items}, ensure_ascii=False)
    emit({"type": "item.completed", "item": {"type": "agent_message", "text": text}})
    output_path.write_text(text, encoding="utf-8")
    completed: dict = {"type": "turn.completed"}
    if "usage" in step:
        completed["usage"] = step["usage"]
    emit(completed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
