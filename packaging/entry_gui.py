"""Windowed entry point for the packaged GUI (MimiJi.exe)."""

from local_subtitle_tool.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main(["gui"]))
