"""Copy license/notice files of every bundled Python distribution into the build.

Usage:
    py -3.14 packaging/collect_licenses.py <dist>/MimiJi [<work>/local_subtitle_tool]

A distribution counts as bundled when one of its top-level import names exists in the
build's ``_internal`` folder, or in the PYZ archive listed by PyInstaller's
``PYZ-*.toc`` files in the work folder (pure-Python packages live only there). Its
LICENSE/COPYING/NOTICE/AUTHORS files are copied to ``<build>/licenses/<distribution>/``
and an index is written to ``licenses/INDEX.txt``.
"""

from __future__ import annotations

import ast
import re
import shutil
import sys
from importlib import metadata
from pathlib import Path

LICENSE_PATTERN = re.compile(r"(licen[cs]e|copying|notice|authors)", re.IGNORECASE)
SKIPPED = {
    "pyinstaller",
    "pyinstaller-hooks-contrib",
    "pip",
    "setuptools",
    "local-subtitle-tool",
    "mimiji",
    "polycue",
}


def _top_level_names(distribution: metadata.Distribution) -> set[str]:
    names: set[str] = set()
    text = distribution.read_text("top_level.txt")
    if text:
        names.update(line.strip() for line in text.splitlines() if line.strip())
    for file in distribution.files or []:
        parts = Path(str(file)).parts
        if parts and not parts[0].endswith((".dist-info", ".egg-info", ".data")):
            names.add(parts[0].removesuffix(".py"))
    return {name for name in names if name and name != ".."}


def _pyz_top_level(work_dir: Path | None) -> set[str]:
    names: set[str] = set()
    if work_dir is None or not work_dir.is_dir():
        return names
    for toc in work_dir.glob("PYZ-*.toc"):
        try:
            _archive, entries = ast.literal_eval(toc.read_text(encoding="utf-8"))
        except (OSError, ValueError, SyntaxError):
            continue
        for entry in entries:
            names.add(str(entry[0]).split(".", 1)[0])
    return names


def main(build_dir: Path, work_dir: Path | None = None) -> int:
    internal = build_dir / "_internal"
    if not internal.is_dir():
        print(f"빌드 폴더를 찾을 수 없습니다: {internal}", file=sys.stderr)
        return 1
    present = {item.name.removesuffix(".py") for item in internal.iterdir()}
    present |= _pyz_top_level(work_dir)
    output = build_dir / "licenses"
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    index: list[str] = []
    seen: set[str] = set()
    for distribution in sorted(
        metadata.distributions(), key=lambda item: (item.metadata["Name"] or "").lower()
    ):
        name = distribution.metadata["Name"] or "unknown"
        key = name.lower()
        if key in SKIPPED or key in seen:
            continue
        if not (_top_level_names(distribution) & present):
            continue
        seen.add(key)
        expression = (
            distribution.metadata.get("License-Expression")
            or distribution.metadata.get("License")
            or "-"
        ).splitlines()[0]
        copied = 0
        for file in distribution.files or []:
            relative = Path(str(file))
            if not LICENSE_PATTERN.search(relative.name):
                continue
            source = Path(str(distribution.locate_file(file)))
            if not source.is_file():
                continue
            target = output / name / relative.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied += 1
        note = (
            f"{copied} file(s)"
            if copied
            else "no license file in wheel; see THIRD_PARTY_NOTICES.md"
        )
        index.append("\t".join((f"{name} {distribution.version}", expression, note)))
    (output / "INDEX.txt").write_text("\n".join(index) + "\n", encoding="utf-8")
    print(f"{len(index)}개 배포 패키지의 라이선스를 {output}에 모았습니다.")
    return 0


if __name__ == "__main__":
    build = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("dist/MimiJi")
    work = Path(sys.argv[2]) if len(sys.argv) > 2 else None
    raise SystemExit(main(build, work))
