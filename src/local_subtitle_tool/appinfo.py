"""Product identity: name, version and per-user data locations."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from ._version import __version__

APP_NAME = "MimiJi"
APP_ID = "MimiJi"  # folder name, QSettings organization/application
# Earlier names, newest first: PolyCue (0.3.0-0.7.x), LocalSubtitleTool (up to 0.2.x).
LEGACY_APP_IDS = ("PolyCue", "LocalSubtitleTool")
LEGACY_APP_ID = LEGACY_APP_IDS[-1]
APP_VERSION = __version__
APP_TITLE = f"{APP_NAME} {APP_VERSION}"

# Files carried over from an earlier data folder on first start of MimiJi.
_MIGRATED_FILES = ("jobs.json", "diagnostics.json")


def _base_dir() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data)
    return Path.home() / ".local" / "share"


def legacy_data_dir() -> Path:
    """The newest earlier data folder that exists (PolyCue, then LocalSubtitleTool)."""

    for legacy_id in LEGACY_APP_IDS:
        candidate = (_base_dir() / legacy_id).resolve()
        if candidate.is_dir():
            return candidate
    return (_base_dir() / LEGACY_APP_IDS[0]).resolve()


def default_data_dir() -> Path:
    """%LOCALAPPDATA%\\MimiJi, seeded once from the PolyCue or LocalSubtitleTool folder."""

    target = (_base_dir() / APP_ID).resolve()
    if not target.exists():
        legacy = legacy_data_dir()
        if legacy.is_dir():
            try:
                target.mkdir(parents=True, exist_ok=True)
                for name in _MIGRATED_FILES:
                    source = legacy / name
                    if source.is_file() and not (target / name).exists():
                        shutil.copy2(source, target / name)
            except OSError:
                # Migration is best effort; the app still works with an empty folder.
                pass
    return target
