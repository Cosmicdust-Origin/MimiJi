from __future__ import annotations

import os
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

OFFICIAL_URLS = {
    "codex": "https://developers.openai.com/codex/cli/",
    "claude": "https://code.claude.com/docs/en/setup",
    "ffmpeg": "https://ffmpeg.org/download.html",
    "nvidia": "https://www.nvidia.com/Download/index.aspx",
    "cuda": "https://developer.nvidia.com/cuda-downloads",
    "cudnn": "https://developer.nvidia.com/cudnn-downloads",
    "diarization_agreement": "https://huggingface.co/pyannote/speaker-diarization-community-1",
    "huggingface_token": "https://huggingface.co/settings/tokens",
}


class PackagedInstallUnavailable(RuntimeError):
    pass


def is_frozen() -> bool:
    """True when running from a packaged executable (PyInstaller and similar)."""

    return bool(getattr(sys, "frozen", False))


def launch_visible(command: list[str], *, cwd: Path | None = None) -> None:
    flags = subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0
    subprocess.Popen(command, cwd=cwd, creationflags=flags, close_fds=True, shell=False)


def install_python_dependencies(project_dir: Path) -> None:
    if is_frozen():
        # sys.executable is the app itself here; "-m pip" would relaunch the app.
        raise PackagedInstallUnavailable(
            "설치형 앱에는 전사 패키지가 포함되어 있습니다. 문제가 계속되면 앱을 다시 설치하십시오."
        )
    launch_visible([sys.executable, "-m", "pip", "install", "-e", str(project_dir)])


def install_diarization_dependencies(project_dir: Path) -> None:
    if is_frozen():
        raise PackagedInstallUnavailable(
            "설치형 앱에서는 화자 분리 구성 요소를 따로 설치할 수 없습니다. "
            "화자 분리 기능이 포함된 배포판을 사용하십시오."
        )
    launch_visible(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "-e",
            f"{project_dir}[diarization]",
        ]
    )


def login_huggingface() -> None:
    hf = shutil.which("hf")
    if hf:
        launch_visible([hf, "auth", "login"])
    else:
        open_official_page("huggingface_token")


def install_codex() -> None:
    npm = shutil.which("npm")
    if npm:
        launch_visible([npm, "install", "-g", "@openai/codex"])
    else:
        open_official_page("codex")


def install_claude() -> None:
    winget = shutil.which("winget")
    if winget:
        launch_visible(
            [winget, "install", "--id", "Anthropic.ClaudeCode", "--exact", "--source", "winget"]
        )
    else:
        open_official_page("claude")


def install_ffmpeg() -> None:
    winget = shutil.which("winget")
    if winget:
        launch_visible([winget, "install", "--id", "Gyan.FFmpeg", "--exact", "--source", "winget"])
    else:
        open_official_page("ffmpeg")


def open_official_page(component: str) -> None:
    try:
        url = OFFICIAL_URLS[component]
    except KeyError as exc:
        raise ValueError(f"지원하지 않는 구성 요소입니다: {component}") from exc
    webbrowser.open(url)
