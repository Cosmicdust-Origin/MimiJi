from __future__ import annotations

import ctypes
import os
import sys
import threading
import traceback
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QUrl
from PySide6.QtGui import QFont, QIcon
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtWidgets import QApplication, QMessageBox

from .app_logging import get_logger, setup_logging
from .appinfo import APP_ID, APP_NAME, APP_VERSION
from .bridge import AppBridge

_logger = get_logger("gui")


def qml_directory() -> Path:
    return Path(__file__).resolve().parent / "qml"


def create_engine(bridge: AppBridge) -> QQmlApplicationEngine:
    engine = QQmlApplicationEngine()
    source_dir = qml_directory()
    engine.addImportPath(str(source_dir))
    engine.rootContext().setContextProperty("bridge", bridge)
    engine.load(QUrl.fromLocalFile(str(source_dir / "Main.qml")))
    if not engine.rootObjects():
        raise RuntimeError("QML 사용자 인터페이스를 불러오지 못했습니다.")
    return engine


def _show_error(title: str, message: str, log_path: Path | None) -> None:
    detail = message
    if log_path is not None:
        detail += f"\n\n자세한 내용은 다음 로그 파일에 기록했습니다.\n{log_path}"
    try:
        QMessageBox.critical(None, title, detail)
    except Exception:  # pragma: no cover - last-resort reporting only
        _logger.exception("오류 창을 표시하지 못했습니다.")


def _install_exception_hooks(log_path: Path | None) -> None:
    def handle_exception(exc_type, exc_value, exc_traceback) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        _logger.error(
            "처리되지 않은 예외가 발생했습니다.\n%s",
            "".join(traceback.format_exception(exc_type, exc_value, exc_traceback)),
        )
        _show_error(
            "예기치 않은 오류",
            f"예기치 않은 오류가 발생했습니다: {exc_value}",
            log_path,
        )

    def handle_thread_exception(args: threading.ExceptHookArgs) -> None:
        _logger.error(
            "백그라운드 작업에서 처리되지 않은 예외가 발생했습니다.\n%s",
            "".join(
                traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)
            ),
        )

    sys.excepthook = handle_exception
    threading.excepthook = handle_thread_exception


def apply_title_bar_theme(window, dark: bool) -> None:
    """Ask Windows to draw the native title bar dark or light (Windows 10 1809+)."""

    if os.name != "nt" or window is None:
        return
    try:
        hwnd = int(window.winId())
        value = ctypes.c_int(1 if dark else 0)
        dwm = ctypes.windll.dwmapi  # type: ignore[attr-defined]
        # 20 = DWMWA_USE_IMMERSIVE_DARK_MODE (19 on pre-20H1 builds)
        for attribute in (20, 19):
            result = dwm.DwmSetWindowAttribute(
                hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value)
            )
            if result == 0:
                break
    except Exception:
        _logger.warning("창 제목 표시줄 테마를 적용하지 못했습니다.", exc_info=True)


def run_gui() -> int:
    log_path = setup_logging()
    _logger.info("%s %s GUI를 시작합니다. 로그=%s", APP_NAME, APP_VERSION, log_path)
    QQuickStyle.setStyle("Basic")
    QCoreApplication.setOrganizationName(APP_ID)
    QCoreApplication.setApplicationName(APP_ID)
    QCoreApplication.setApplicationVersion(APP_VERSION)
    app = QApplication.instance() or QApplication(sys.argv)
    app.setFont(QFont("Malgun Gothic", 10))
    icon_path = Path(__file__).resolve().parent / "assets" / "mimiji.ico"
    if icon_path.is_file():
        app.setWindowIcon(QIcon(str(icon_path)))
    _install_exception_hooks(log_path)
    try:
        bridge = AppBridge()
        engine = create_engine(bridge)
    except Exception as exc:
        _logger.exception("GUI 초기화에 실패했습니다.")
        _show_error("시작 오류", f"앱을 시작하지 못했습니다: {exc}", log_path)
        return 1
    window = engine.rootObjects()[0]
    apply_title_bar_theme(window, bridge.darkMode)
    bridge.themeChanged.connect(lambda: apply_title_bar_theme(window, bridge.darkMode))
    app.aboutToQuit.connect(bridge.shutdown)
    exit_code = app.exec()
    del engine
    _logger.info("GUI를 종료합니다. 종료 코드=%s", exit_code)
    return int(exit_code)
