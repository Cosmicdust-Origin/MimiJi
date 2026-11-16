# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onedir build: MimiJi.exe (GUI) + MimiJiCli.exe (CLI).

Build from the project root:
    py -3.14 -m PyInstaller packaging/mimiji.spec --noconfirm

The base build never bundles PyTorch/pyannote. Set LST_WITH_DIARIZATION=1 in an
environment where `pip install .[diarization]` succeeded to build the optional
speaker-diarization variant into dist/MimiJi-Diarization.

The version comes from src/local_subtitle_tool/_version.py and is embedded in both
executables' Windows file properties.

Model weights (large-v3, community-1) are never bundled; they stay in the user's
Hugging Face cache. Executables are not UPX-compressed (fewer antivirus false
positives) and onedir avoids the per-start extraction of a single-file build.
"""

import os
import re
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
)

ROOT = Path(SPECPATH).resolve().parent  # noqa: F821 - provided by PyInstaller
SRC = ROOT / "src"
PACKAGE = SRC / "local_subtitle_tool"
WITH_DIARIZATION = os.environ.get("LST_WITH_DIARIZATION") == "1"
NAME = "MimiJi-Diarization" if WITH_DIARIZATION else "MimiJi"

_VERSION_MATCH = re.search(
    r'__version__\s*=\s*"(\d+)\.(\d+)\.(\d+)"',
    (PACKAGE / "_version.py").read_text(encoding="utf-8"),
)
if _VERSION_MATCH is None:
    raise SystemExit("src/local_subtitle_tool/_version.py에서 버전을 읽을 수 없습니다.")
VERSION_TUPLE = tuple(int(part) for part in _VERSION_MATCH.groups()) + (0,)
VERSION_TEXT = ".".join(_VERSION_MATCH.groups())


def version_resource(description: str, internal_name: str):
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo,
        StringFileInfo,
        StringStruct,
        StringTable,
        VarFileInfo,
        VarStruct,
        VSVersionInfo,
    )

    return VSVersionInfo(
        ffi=FixedFileInfo(filevers=VERSION_TUPLE, prodvers=VERSION_TUPLE),
        kids=[
            StringFileInfo(
                [
                    StringTable(
                        "041204B0",
                        [
                            StringStruct("ProductName", "MimiJi"),
                            StringStruct("FileDescription", description),
                            StringStruct("FileVersion", VERSION_TEXT),
                            StringStruct("ProductVersion", VERSION_TEXT),
                            StringStruct("InternalName", internal_name),
                            StringStruct("OriginalFilename", f"{internal_name}.exe"),
                        ],
                    )
                ]
            ),
            VarFileInfo([VarStruct("Translation", [0x0412, 1200])]),
        ],
    )

datas = [
    (str(PACKAGE / "qml"), "local_subtitle_tool/qml"),
    (str(PACKAGE / "assets"), "local_subtitle_tool/assets"),
    (str(ROOT / "THIRD_PARTY_NOTICES.md"), "."),
]
datas += collect_data_files("faster_whisper")  # assets/silero_vad_v6.onnx

binaries = []
binaries += collect_dynamic_libs("ctranslate2")  # ctranslate2.dll, cudnn64_9.dll, libiomp5md.dll
binaries += collect_dynamic_libs("onnxruntime")

hiddenimports = [
    "onnxruntime",  # imported lazily by faster_whisper.vad
    "faster_whisper.audio",
    "faster_whisper.vad",
    "huggingface_hub",
    "huggingface_hub.errors",
    "PySide6.QtQuickControls2",
]

excludes = [
    "tkinter",
    "_tkinter",
    "local_subtitle_tool.gui",  # legacy Tkinter GUI, kept in source only
    "subtitle_pipeline_defaults",
    "matplotlib",
    "IPython",
    "pytest",
    "PySide6.Qt3DCore",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtMultimedia",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    # Optional integrations of huggingface_hub/others that this app never uses.
    "boto3",
    "botocore",
    "s3transfer",
    "aiohttp",
    "aiosignal",
    "multidict",
    "frozenlist",
    "propcache",
    "yarl",
    "mcp",
    "pydantic",
    "pydantic_core",
    "email_validator",
    "cryptography",
    "PIL",
    "jsonschema",
    "jsonschema_specifications",
    "referencing",
    "rpds",
    "websockets",
    # Web server / CLI tooling pulled in by optional huggingface_hub modules. The app
    # is a local desktop app and never starts a web server.
    "fastapi",
    "starlette",
    "uvicorn",
    "multipart",
    "python_multipart",
    "typer",
    "rich",
    "click",
    "shellingham",
    "pygments",
    "markdown_it",
    "mdurl",
    "jinja2",
    "_pytest",
    "annotated_doc",
    "typing_inspection",
]

# The PySide6 QML hook collects every installed QML module. The app only uses
# QtQuick, QtQuick.Controls (Basic) and QtQuick.Layouts, so drop large unrelated
# Qt modules (WebEngine alone is ~190 MB).
UNUSED_QT = re.compile(
    r"(qt6webengine|qtwebengineprocess|/qtwebengine|qt63d|/qt3d|qt6quick3d|/qtquick3d"
    r"|qt6charts|/qtcharts|qt6datavisualization|/qtdatavisualization|qt6graphs|/qtgraphs"
    r"|qt6location|/qtlocation|qt6positioning|/qtpositioning|qt6multimedia|/qtmultimedia"
    r"|/plugins/multimedia/|qt6pdf|/qtpdf|qt6sensors|/qtsensors|qt6texttospeech"
    r"|/qttexttospeech|qt6scxml|/qtscxml|qt6statemachine|qt6remoteobjects"
    r"|/qtremoteobjects|qt6webchannel|/qtwebchannel|qt6websockets|/qtwebsockets"
    r"|qt6spatialaudio|/qtspatialaudio|/scene3d|/scene2d|qt6bluetooth|qt6nfc"
    r"|qt6serialport|qt6serialbus|qt6httpserver|qt6webview|/qtwebview)"
)

# PySide6's Windows wheels link Qt6Core against the unversioned ICU API
# supplied by Windows.  When MimiJi is built from Codex Desktop, PyInstaller
# can instead discover Poppler's version-suffixed ICU DLLs on PATH.  Those DLLs
# export names such as ``ucnv_open_78`` while Qt expects ``ucnv_open``, causing
# the packaged GUI to fail before Python logging starts.  Do not bundle that
# unrelated runtime; the Windows system ICU remains available at run time.
EXTERNAL_ICU_DLL = re.compile(r"(?:^|/)icu(?:uc|dt\d*)\.dll$")


def without_unused_qt(entries):
    return [
        entry
        for entry in entries
        if not UNUSED_QT.search("/" + entry[0].replace("\\", "/").lower())
    ]


def without_external_icu(entries):
    return [
        entry
        for entry in entries
        if not EXTERNAL_ICU_DLL.search("/" + entry[0].replace("\\", "/").lower())
    ]


if WITH_DIARIZATION:
    hiddenimports += collect_submodules("pyannote.audio")
    hiddenimports += ["torch", "torchaudio", "torchcodec"]
    datas += collect_data_files("pyannote.audio")
    binaries += collect_dynamic_libs("torch")
else:
    excludes += [
        "torch",
        "torchaudio",
        "torchcodec",
        "torchvision",
        "pyannote",
        "lightning",
        "pytorch_lightning",
        "speechbrain",
    ]


def analysis(script: str) -> Analysis:  # noqa: F821
    return Analysis(  # noqa: F821
        [str(ROOT / "packaging" / script)],
        pathex=[str(SRC)],
        binaries=binaries,
        datas=datas,
        hiddenimports=hiddenimports,
        excludes=excludes,
        noarchive=False,
    )


gui_analysis = analysis("entry_gui.py")
cli_analysis = analysis("entry_cli.py")
for _analysis in (gui_analysis, cli_analysis):
    _analysis.binaries = without_external_icu(without_unused_qt(_analysis.binaries))
    _analysis.datas = without_unused_qt(_analysis.datas)

gui_exe = EXE(  # noqa: F821
    PYZ(gui_analysis.pure),  # noqa: F821
    gui_analysis.scripts,
    [],
    exclude_binaries=True,
    name="MimiJi",
    console=False,
    icon=str(PACKAGE / "assets" / "mimiji.ico"),
    version=version_resource("MimiJi 자막 생성기", "MimiJi"),
    upx=False,
)
cli_exe = EXE(  # noqa: F821
    PYZ(cli_analysis.pure),  # noqa: F821
    cli_analysis.scripts,
    [],
    exclude_binaries=True,
    name="MimiJiCli",
    console=True,
    icon=str(PACKAGE / "assets" / "mimiji.ico"),
    version=version_resource("MimiJi 명령줄 도구", "MimiJiCli"),
    upx=False,
)

COLLECT(  # noqa: F821
    gui_exe,
    cli_exe,
    gui_analysis.binaries,
    gui_analysis.datas,
    cli_analysis.binaries,
    cli_analysis.datas,
    name=NAME,
    upx=False,
)
