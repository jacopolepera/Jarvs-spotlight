"""
core.ui.components — Custom PyQt6 widgets for Jarvs Spotlight.

Classes:
    NoScrollComboBox   — QComboBox that swallows wheel events (prevents font-size crashes)
    AttachmentBar      — Horizontal bar showing pending file attachment chips
    ChatInputBox       — Auto-resizing, zoom-aware input QTextEdit
    OutputBox          — Zoom-aware output QTextEdit for AI responses
"""
import os

from PyQt6.QtWidgets import (
    QComboBox, QWidget, QHBoxLayout, QLabel, QPushButton, QTextEdit
)
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont


# ── NoScrollComboBox ─────────────────────────────────────────────────────────

class NoScrollComboBox(QComboBox):
    """QComboBox subclass that swallows all wheel events.

    Prevents the native popup from adjusting font size on scroll,
    which otherwise causes a QFont::setPointSize <= 0 crash.
    """
    def wheelEvent(self, event):
        event.ignore()


# ── AttachmentBar ─────────────────────────────────────────────────────────────

class AttachmentBar(QWidget):
    """Horizontal bar showing pending attachment chips with ✕ remove buttons."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(4, 2, 4, 2)
        self._layout.setSpacing(4)
        self._layout.addStretch()
        self.setMaximumHeight(32)

    def refresh(self, attachments: list):
        """Rebuild the chip row from the attachments list."""
        while self._layout.count() > 0:
            item = self._layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        for i, att in enumerate(attachments):
            self._layout.addWidget(self._make_chip(att, i))
        self._layout.addStretch()

    def _make_chip(self, att: dict, index: int) -> QWidget:
        chip = QWidget(self)
        chip_layout = QHBoxLayout(chip)
        chip_layout.setContentsMargins(6, 2, 4, 2)
        chip_layout.setSpacing(3)

        basename = os.path.basename(att["path"])
        display = basename if len(basename) <= 20 else basename[:17] + "..."
        icon = "🖼" if att["kind"] == "image" else "📎"

        label = QLabel(f"{icon} {display}")
        label.setStyleSheet("color: #bbb; font-size: 11px;")
        chip_layout.addWidget(label)

        close_btn = QPushButton("✕")
        close_btn.setFixedSize(16, 16)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(
            "QPushButton { background: transparent; color: #888; border: none; "
            "font-size: 10px; padding: 0; } "
            "QPushButton:hover { color: #ff6666; }"
        )
        close_btn.clicked.connect(lambda checked, idx=index: self._remove(idx))
        chip_layout.addWidget(close_btn)

        chip.setStyleSheet(
            "background-color: rgba(255,255,255,0.06); "
            "border: 1px solid rgba(255,255,255,0.1); border-radius: 10px;"
        )
        return chip

    def _remove(self, index: int):
        """Walk up the parent chain to find the main window and delegate removal."""
        parent = self.parent()
        while parent and not hasattr(parent, "remove_attachment"):
            parent = parent.parent()
        if parent:
            parent.remove_attachment(index)


# ── ChatInputBox ──────────────────────────────────────────────────────────────

class ChatInputBox(QTextEdit):
    """Auto-resizing, zoom-capable input box.

    Signals:
        returnPressed  — emitted on plain Enter key
        heightChanged  — emitted whenever the widget changes its own height
    """
    returnPressed = pyqtSignal()
    heightChanged = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptRichText(False)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.textChanged.connect(self.adjust_height)
        self.min_height = 44
        self.max_height = 120
        self.setFixedHeight(self.min_height)
        self._font_size = 13  # Track pt size explicitly (QSS px breaks pointSize())

    def wheelEvent(self, event):
        if event.modifiers() == Qt.KeyboardModifier.ControlModifier:
            delta = event.angleDelta().y()
            if delta > 0 and self._font_size < 28:
                self._font_size += 1
                self._apply_zoom()
            elif delta < 0 and self._font_size > 8:
                self._font_size -= 1
                self._apply_zoom()
            event.accept()
            return
        super().wheelEvent(event)

    def _apply_zoom(self):
        """Re-apply only font-size so QSS background/border are preserved."""
        f = QFont()
        f.setPointSize(self._font_size)
        self.setFont(f)

    def adjust_height(self):
        doc_height = int(self.document().documentLayout().documentSize().height())
        target_height = doc_height + 14  # Padding allowance
        current_height = self.height()
        new_height = current_height

        if target_height <= self.min_height:
            new_height = self.min_height
            self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        elif target_height <= self.max_height:
            new_height = target_height
            self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        else:
            new_height = self.max_height
            self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        if new_height != current_height:
            self.setFixedHeight(new_height)
            self.heightChanged.emit()

    def reset_size(self):
        self.setFixedHeight(self.min_height)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.heightChanged.emit()

    def keyPressEvent(self, event):
        if self.isReadOnly():
            super().keyPressEvent(event)
            return

        key = event.key()
        mods = event.modifiers()

        if key == Qt.Key.Key_Space and mods == Qt.KeyboardModifier.ShiftModifier:
            self.insertPlainText("\n")
        elif key == Qt.Key.Key_Return and mods == Qt.KeyboardModifier.ShiftModifier:
            self.insertPlainText("\n")
        elif key == Qt.Key.Key_Return and mods == Qt.KeyboardModifier.NoModifier:
            self.returnPressed.emit()
        else:
            super().keyPressEvent(event)


# ── OutputBox ─────────────────────────────────────────────────────────────────

class OutputBox(QTextEdit):
    """Read-only output box with Ctrl+Scroll zoom support."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._font_size = 13

    def wheelEvent(self, event):
        if event.modifiers() == Qt.KeyboardModifier.ControlModifier:
            delta = event.angleDelta().y()
            if delta > 0 and self._font_size < 28:
                self._font_size += 1
                self._apply_zoom()
            elif delta < 0 and self._font_size > 8:
                self._font_size -= 1
                self._apply_zoom()
            event.accept()
            return
        super().wheelEvent(event)

    def _apply_zoom(self):
        f = QFont()
        f.setPointSize(self._font_size)
        self.setFont(f)
