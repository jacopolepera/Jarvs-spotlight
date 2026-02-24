"""
Jarvs Spotlight — UI Test Suite
Run: pytest tests/test_ui.py -v -s

Requires: pip install pytest-qt
"""
import sys
import os
import warnings

# Ensure project root is importable from tests/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from unittest.mock import MagicMock
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication


# ── Helpers ────────────────────────────────────────────────────────────────

def _mock_settings(theme="Dark"):
    m = MagicMock()
    m.get_theme.return_value = theme
    m.get_model.return_value = "gemini/gemini-2.5-flash"
    m.get_api_key.return_value = ""
    m.get_workspace_path.return_value = "."
    return m


# ── Test 1: NoScrollComboBox swallows wheel events ─────────────────────────

def test_noscrollcombobox_ignores_wheel(qtbot):
    """
    NoScrollComboBox.wheelEvent must call event.ignore() and never change
    the selection — prevents QFont::setPointSize <= 0 crashes.
    """
    from tools.jarvs_ui import NoScrollComboBox

    combo = NoScrollComboBox()
    combo.addItems(["Option A", "Option B", "Option C"])
    qtbot.addWidget(combo)
    combo.show()

    initial_index = combo.currentIndex()
    mock_event = MagicMock()

    combo.wheelEvent(mock_event)

    mock_event.ignore.assert_called_once()
    assert combo.currentIndex() == initial_index


# ── Test 2: SettingsDialog uses NoScrollComboBox for all dropdowns ─────────

def test_settings_dialog_uses_noscroll_comboboxes(qtbot):
    """Both dropdowns in SettingsDialog must be NoScrollComboBox instances."""
    from tools.jarvs_ui import SettingsDialog, NoScrollComboBox

    dialog = SettingsDialog(parent=None, settings_manager=_mock_settings())
    qtbot.addWidget(dialog)

    assert isinstance(dialog.theme_cb, NoScrollComboBox), "theme_cb must be NoScrollComboBox"
    assert isinstance(dialog.model_cb, NoScrollComboBox), "model_cb must be NoScrollComboBox"


# ── Test 3: SettingsDialog has WA_TranslucentBackground ───────────────────

def test_settings_dialog_translucent_background(qtbot):
    """WA_TranslucentBackground is required for frameless rounded corners on Windows."""
    from tools.jarvs_ui import SettingsDialog

    dialog = SettingsDialog(parent=None, settings_manager=_mock_settings())
    qtbot.addWidget(dialog)

    assert dialog.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)


# ── Test 4: No QFont warnings when SettingsDialog opens ───────────────────

def test_no_qfont_warnings_on_settings_open(qtbot):
    """
    Opening SettingsDialog must not emit any QFont::setPointSize warnings.
    Root cause was px-based font-size in QSS; fix is to use pt units.
    """
    from PyQt6.QtCore import QtMsgType, qInstallMessageHandler
    from tools.jarvs_ui import SettingsDialog

    qfont_warnings = []

    def _handler(msg_type, context, message):
        if "QFont" in message or "Point size" in message:
            qfont_warnings.append(message)

    qInstallMessageHandler(_handler)

    try:
        dialog = SettingsDialog(parent=None, settings_manager=_mock_settings())
        qtbot.addWidget(dialog)
        dialog.show()
        qtbot.waitExposed(dialog)
        qtbot.wait(200)  # allow event loop to settle
    finally:
        qInstallMessageHandler(None)  # restore default handler

    assert qfont_warnings == [], f"QFont warnings detected: {qfont_warnings}"


# ── Test 5: ChatInputBox min_height regression guard ──────────────────────

def test_chat_input_box_min_height(qtbot):
    """
    ChatInputBox.min_height must be >= 44px so text isn't vertically clipped.
    This is a regression guard for the text-clipping UI bug.
    """
    from tools.jarvs_ui import ChatInputBox

    box = ChatInputBox()
    qtbot.addWidget(box)

    assert box.min_height >= 44, (
        f"ChatInputBox.min_height is {box.min_height}px — must be >= 44 to avoid text clipping"
    )


# ── Test 6: Screenshot of SettingsDialog saved to .tmp/ ───────────────────

def test_settings_dialog_screenshot(qtbot):
    """
    Renders SettingsDialog and saves a screenshot to .tmp/settings_dialog.png
    for manual visual inspection without running the full app.
    """
    from tools.jarvs_ui import SettingsDialog

    dialog = SettingsDialog(parent=None, settings_manager=_mock_settings())
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitExposed(dialog)

    screen = QApplication.primaryScreen()
    screenshot = screen.grabWindow(int(dialog.winId()))

    out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".tmp")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "settings_dialog.png")

    assert screenshot.save(out_path, "PNG"), f"Could not save screenshot to {out_path}"
    print(f"\n  [screenshot] {out_path}")
