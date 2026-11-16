"""Display model for the translation accounts (Codex / Claude) shown in the GUI.

Only the status the official CLIs report is used. The app never reads or stores
tokens; login and logout are delegated to `codex login|logout` and
`claude auth login|logout`.
"""

from __future__ import annotations

from typing import Any

from .preflight import BLOCKED, LOGIN_REQUIRED, MISSING, READY, CheckResult

PROVIDER_LABELS = {"codex": "Codex · ChatGPT", "claude": "Claude Code"}


def summarize_account(provider: str, check: CheckResult | None) -> dict[str, Any]:
    label = PROVIDER_LABELS.get(provider, provider)
    if check is None:
        return {
            "provider": provider,
            "label": label,
            "installed": False,
            "logged_in": False,
            "state": "확인 전",
            "detail": "상태를 확인하지 않았습니다.",
            "problem": False,
        }
    details = check.details or {}
    installed = check.status != MISSING
    logged_in = bool(check.ok and check.status == READY)
    if logged_in and provider == "claude":
        method = str(details.get("authMethod") or "unknown")
        subscription = str(details.get("subscriptionType") or "unknown")
        state = "로그인됨"
        detail = f"구독 로그인 · 인증 {method} · 구독 {subscription}"
    elif logged_in:
        state = "로그인됨"
        detail = "ChatGPT 구독 로그인"
    elif check.status == MISSING:
        state = "설치 필요"
        detail = check.message
    elif check.status == LOGIN_REQUIRED:
        state = "로그인 필요"
        detail = check.message
    elif check.status == BLOCKED:
        state = "사용 불가"
        detail = check.message
    else:
        state = "확인 실패"
        detail = check.message
    version = str(details.get("version") or "")
    return {
        "provider": provider,
        "label": label,
        "installed": installed,
        "logged_in": logged_in,
        "state": state,
        "detail": detail,
        "version": version if version and version != "unknown" else "",
        "problem": not logged_in,
    }


def account_line(account: dict[str, Any]) -> str:
    """One-line summary for the new-job screen."""

    return f"{account['label']} · {account['state']}"
