"""Smoke-test a packaged onedir build.

Usage: py -3.14 packaging/smoke_test.py dist/MimiJi [--media sample.wav]

Checks, without touching the user's real app data or Hugging Face cache:
1. "clean" environment (fresh LOCALAPPDATA, no HF cache, PATH = Windows folders only):
   - CLI starts, `diagnose` runs, CUDA is not reported usable without cuBLAS
   - GUI starts offscreen, writes its log, does not report a startup failure
2. normal environment (current PATH and HF cache):
   - `diagnose` runs
   - with --media: `transcribe` on CUDA produces a raw JSON with VAD applied
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def _run(
    command: list[str], env: dict[str, str], timeout: int = 600
) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def _clean_env(root: Path) -> dict[str, str]:
    windows = os.environ.get("WINDIR", r"C:\Windows")
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "USERNAME"}
    }
    env.update(
        {
            "PATH": f"{windows}\\System32;{windows}",
            "LOCALAPPDATA": str(root / "LocalAppData"),
            "APPDATA": str(root / "AppData"),
            "USERPROFILE": str(root / "Profile"),
            "HF_HOME": str(root / "hf_home"),
            "HF_HUB_CACHE": str(root / "hf_home" / "hub"),
            "PYTHONUTF8": "1",
        }
    )
    for key in ("LOCALAPPDATA", "APPDATA", "USERPROFILE"):
        Path(env[key]).mkdir(parents=True, exist_ok=True)
    return env


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("build", type=Path)
    parser.add_argument("--media", type=Path)
    args = parser.parse_args()
    build = args.build.resolve()
    cli = build / "MimiJiCli.exe"
    gui = build / "MimiJi.exe"
    failures: list[str] = []

    def check(condition: bool, label: str) -> None:
        print(("PASS " if condition else "FAIL ") + label)
        if not condition:
            failures.append(label)

    with tempfile.TemporaryDirectory(prefix="lst-smoke-") as directory:
        root = Path(directory)
        clean = _clean_env(root)

        result = _run([str(cli), "diagnose"], clean, timeout=180)
        try:
            checks = {item["name"]: item for item in json.loads(result.stdout)}
        except json.JSONDecodeError:
            checks = {}
            print(result.stdout[-2000:], result.stderr[-2000:])
        check(bool(checks), "clean: CLI diagnose returns JSON")
        check(
            checks.get("python_dependencies", {}).get("ok") is True,
            "clean: bundled Python dependencies (incl. onnxruntime) import",
        )
        cuda = checks.get("cuda", {})
        check(
            cuda.get("ok") is False,
            f"clean: CUDA not usable without runtime ({cuda.get('message')})",
        )

        gui_env = dict(clean, QT_QPA_PLATFORM="offscreen")
        process = subprocess.Popen([str(gui)], env=gui_env)
        time.sleep(15)
        alive = process.poll() is None
        process.kill()
        process.wait(timeout=10)
        log = Path(clean["LOCALAPPDATA"]) / "MimiJi" / "logs" / "app.log"
        log_text = log.read_text(encoding="utf-8") if log.is_file() else ""
        check(alive, "clean: GUI still running after 15 s (no startup crash)")
        check("GUI를 시작합니다" in log_text, "clean: GUI wrote its log file")
        check(
            "초기화에 실패" not in log_text and "Traceback" not in log_text,
            "clean: no startup error in log",
        )

        normal = dict(os.environ, PYTHONUTF8="1")
        result = _run([str(cli), "diagnose"], normal, timeout=180)
        try:
            normal_checks = {item["name"]: item for item in json.loads(result.stdout)}
        except json.JSONDecodeError:
            normal_checks = {}
        check(bool(normal_checks), "normal: CLI diagnose returns JSON")
        print("      cuda:", normal_checks.get("cuda", {}).get("message"))

        if args.media:
            output = root / "job"
            result = _run(
                [
                    str(cli),
                    "transcribe",
                    "--video",
                    str(args.media.resolve()),
                    "--code",
                    "SMOKE-001",
                    "--output",
                    str(output),
                ],
                dict(normal, LOCALAPPDATA=str(root / "LocalAppData")),
                timeout=900,
            )
            raw_matches = list(
                (output / ".subtitle_job").rglob("SMOKE-001_merged_raw_ja.json")
            )
            raw = raw_matches[0] if len(raw_matches) == 1 else Path()
            check(result.returncode == 0 and raw.is_file(), "normal: packaged transcription ran")
            if raw.is_file():
                payload = json.loads(raw.read_text(encoding="utf-8"))
                meta = payload["meta"]
                check(meta.get("vad_applied") is True, "normal: VAD applied in packaged build")
                print(
                    f"      device={meta.get('device')} compute={meta.get('compute_type')} "
                    f"segments={len(payload['segments'])}"
                )
            else:
                print(result.stdout[-1500:], result.stderr[-1500:])

    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
