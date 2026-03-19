"""
core.ui.app — Jarvs Spotlight main application window.

Entry point: run `python main.py` from the project root, or
             run this file directly: `python -m core.ui.app`
"""
import sys
import os
import threading
import signal
import json

import keyboard
import markdown

from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QMenu, QFileDialog, QMessageBox
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QPixmap, QGuiApplication

# Ensure project root is in sys.path so `core` can be imported from anywhere
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core.tools.engine import setup_interpreter, reload_instructions
from core.tools.settings_manager import SettingsManager
from core.ui import styles
from core.ui.components import AttachmentBar, ChatInputBox, OutputBox
from core.ui.workers import AIWorker, VoiceTranscriptionThread, GlobalWheelFilter
from core.ui.settings_dialog import SettingsDialog
from core.vision.capture import capture_screen
from core.vision.attachments import store_attachment, encode_attachment_for_engine
from core.tools.logger import Logger


# ── Global AI engine ──────────────────────────────────────────────────────────
ai_engine = setup_interpreter()


# ── SpotlightUI ───────────────────────────────────────────────────────────────

class SpotlightUI(QWidget):
    """Layer 3: Tool — The frameless, translucent Jarvs Spotlight UI."""

    toggle_signal       = pyqtSignal()
    start_record_signal = pyqtSignal()
    stop_record_signal  = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.worker               = None
        self.offset               = None   # For window dragging
        self.current_prompt       = ""
        self.current_response     = ""
        self.loading_timer        = QTimer(self)
        self.loading_timer.timeout.connect(self.animate_loading)

        # Install app-level wheel filter to protect QComboBox in SettingsDialog
        app = QApplication.instance()
        if app:
            self._wheel_filter = GlobalWheelFilter()
            app.installEventFilter(self._wheel_filter)

        self.settings_manager = SettingsManager()
        self.current_theme    = self.settings_manager.get_theme()

        self.init_ui()
        self.apply_theme(self.current_theme)
        self.setup_trigger()

        self.toggle_signal.connect(self.toggle_ui)
        self.start_record_signal.connect(self.start_voice_recording)
        self.stop_record_signal.connect(self.stop_voice_recording)
        self.auto_send_transcription = False

    # ── UI construction ───────────────────────────────────────────────────────

    def init_ui(self):
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)

        # GradientWrapper: provides the coloured/gradient border ring
        self.gradient_wrapper = QWidget(self)
        self.gradient_wrapper.setObjectName("GradientWrapper")
        grad_layout = QVBoxLayout(self.gradient_wrapper)
        grad_layout.setContentsMargins(1, 1, 1, 1)  # 1px border thickness

        # MainContainer: the actual dark background inside the border
        self.container = QWidget(self.gradient_wrapper)
        self.container.setObjectName("MainContainer")
        layout = QVBoxLayout(self.container)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        # ── Top bar ───────────────────────────────────────────────────────────
        self.top_bar = QHBoxLayout()
        self.top_bar.setContentsMargins(4, 2, 4, 0)

        # Logo
        self.logo_label = QLabel()
        logo_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "assets", "ui", "jarvs_logo_v2.png"
        )
        if os.path.exists(logo_path):
            pixmap = QPixmap(logo_path)
            self.logo_label.setPixmap(
                pixmap.scaled(22, 22, Qt.AspectRatioMode.KeepAspectRatio,
                              Qt.TransformationMode.SmoothTransformation)
            )
        self.top_bar.addWidget(self.logo_label)

        self.title_label = QLabel("Jarvs Spotlight")
        self.title_label.setObjectName("TitleLabel")
        self.title_label.setToolTip("Drag here to move the window")
        self.top_bar.addWidget(self.title_label)

        self.settings_btn = QPushButton("⚙️")
        self.settings_btn.setObjectName("SettingsBtn")
        self.settings_btn.setFixedSize(30, 30)
        self.settings_btn.clicked.connect(self.open_settings)
        self.top_bar.addWidget(self.settings_btn)

        self.top_bar.addStretch()

        self.min_btn = QPushButton("-")
        self.min_btn.setObjectName("MinBtn")
        self.min_btn.setFixedSize(30, 30)
        self.min_btn.setToolTip("Hide (Esc)")
        self.min_btn.clicked.connect(self.hide)

        self.close_btn = QPushButton("✕")
        self.close_btn.setObjectName("CloseBtn")
        self.close_btn.setFixedSize(30, 30)
        self.close_btn.setToolTip("Clear Session & Hide (Resets memory)")
        self.close_btn.clicked.connect(self.close_app)

        self.top_bar.addWidget(self.min_btn)
        self.top_bar.addWidget(self.close_btn)
        layout.addLayout(self.top_bar)

        # ── Output box (hidden until first response) ──────────────────────────
        self.output_box = OutputBox(self.container)
        self.output_box.setObjectName("OutputBox")
        self.output_box.setReadOnly(True)
        self.output_box.hide()
        layout.addWidget(self.output_box)

        # ── Attachment bar (hidden until files are attached) ──────────────────
        self.attachment_bar = AttachmentBar(self.container)
        self.attachment_bar.hide()
        layout.addWidget(self.attachment_bar)

        # ── Input row: [ + ] [ 🎙️ ] [ input box ] ────────────────────────────
        input_row = QHBoxLayout()
        input_row.setContentsMargins(0, 0, 0, 0)
        input_row.setSpacing(6)

        self.attach_btn = QPushButton("➕")
        self.attach_btn.setObjectName("AttachBtn")
        self.attach_btn.setFixedSize(28, 28)
        self.attach_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.attach_btn.setToolTip("Attach file or capture screen")
        self.attach_btn.clicked.connect(self.show_attach_menu)
        input_row.addWidget(self.attach_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        self.mic_btn = QPushButton("🎙️")
        self.mic_btn.setObjectName("MicBtn")
        self.mic_btn.setFixedSize(28, 28)
        self.mic_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.mic_btn.setToolTip("Click to record voice input")
        self.mic_btn.clicked.connect(self.toggle_voice_recording)
        input_row.addWidget(self.mic_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        self.input_box = ChatInputBox(self.container)
        self.input_box.setObjectName("InputBox")
        self.input_box.setPlaceholderText("System Pilot awaiting commands...")
        self.input_box.setToolTip("Type your command and press Enter (Shift+Space for newline)")
        self.input_box.returnPressed.connect(self.handle_input)
        self.input_box.heightChanged.connect(self.adjust_window_size)
        input_row.addWidget(self.input_box)

        layout.addLayout(input_row)

        # ── Pending state ─────────────────────────────────────────────────────
        self.pending_attachments   = []
        self.voice_recorder        = None
        self.transcription_thread  = None

        grad_layout.addWidget(self.container)
        main_layout.addWidget(self.gradient_wrapper)
        self.setLayout(main_layout)

        # Mouse tracking for edge-resize detection
        self.setMouseTracking(True)
        self.gradient_wrapper.setMouseTracking(True)
        self.container.setMouseTracking(True)

        self.setMinimumWidth(300)

        w, h = self.settings_manager.get_window_size()
        if w < 500:
            w = 600
        self.resize(w, 80)
        self.adjustSize()
        self.setFixedHeight(self.height())
        self._compact_height   = self.height()
        self.expanded_height   = h if h > 200 else 500

    # ── Theme ─────────────────────────────────────────────────────────────────

    def apply_theme(self, theme_name: str):
        """Fetch QSS and Markdown CSS from styles module and apply them."""
        self.current_theme = theme_name

        # 1) Purge all inline stylesheets to prevent stale theme bleeding
        for widget in [
            self, self.gradient_wrapper, self.container, self.title_label,
            self.min_btn, self.close_btn, self.attach_btn, self.mic_btn,
            self.settings_btn, self.input_box, self.output_box,
        ]:
            widget.setStyleSheet("")

        # 2) Fetch theme strings
        main_qss, md_css = styles.get_theme_data(theme_name)

        # 3) Apply main QSS to container (ID selectors target individual widgets)
        self.container.setStyleSheet(main_qss)

        # 4) Apply Markdown CSS to the output box document
        self.output_box.document().setDefaultStyleSheet(md_css)

    # ── Size helpers ──────────────────────────────────────────────────────────

    def adjust_window_size(self):
        """Grow the window naturally with the input box height."""
        if self.output_box.isHidden():
            w = self.width()
            self.setMinimumHeight(0)
            self.setMaximumHeight(16777215)
            self.adjustSize()
            self.setFixedHeight(self.height())
            self.resize(w, self.height())
            self._compact_height = self.height()

    def _relock_to_content(self):
        if self.output_box.isHidden():
            self.adjust_window_size()

    def _snap_to_compact(self):
        """Re-bound window to its strictly wrapped compact height."""
        self.setMinimumWidth(300)
        self.setMinimumHeight(0)
        self.setMaximumHeight(16777215)
        w, _ = self.settings_manager.get_window_size()
        if w < 300:
            w = 600
        self.resize(w, 80)
        self.adjustSize()
        self.setFixedHeight(self.height())
        self.resize(w, self.height())
        self._compact_height = self.height()

    # ── Attachment / Dropup Menu ──────────────────────────────────────────────

    def show_attach_menu(self):
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu { background-color: #2a2a2a; color: #ddd; border: 1px solid #444; "
            "border-radius: 6px; padding: 4px 0px; } "
            "QMenu::item { padding: 6px 16px 6px 12px; } "
            "QMenu::item:selected { background-color: rgba(255,255,255,0.1); } "
            "QMenu::separator { height: 1px; background: #444; margin: 3px 8px; }"
        )
        act_image  = menu.addAction("🖼  Attach Image")
        act_file   = menu.addAction("📎  Attach File")
        menu.addSeparator()
        act_screen = menu.addAction("👁  Capture Screen")
        act_image.triggered.connect(self.attach_image)
        act_file.triggered.connect(self.attach_file)
        act_screen.triggered.connect(self.trigger_vision)

        from PyQt6.QtCore import QPoint
        btn_pos     = self.attach_btn.mapToGlobal(self.attach_btn.rect().topLeft())
        menu_height = menu.sizeHint().height()
        menu.exec(btn_pos - QPoint(0, menu_height))

    def attach_image(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Attach Image", "",
            "Images (*.png *.jpg *.jpeg *.webp *.heic *.heif *.bmp);;All Files (*)"
        )
        if path:
            dest = store_attachment(path, self.settings_manager.get_workspace_path())
            self._add_attachment(dest, "image")

    def attach_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Attach File", "",
            "Documents (*.pdf *.txt *.csv *.json *.py *.md *.html *.xml *.yaml *.yml "
            "*.log *.toml *.ini *.cfg);;All Files (*)"
        )
        if path:
            dest = store_attachment(path, self.settings_manager.get_workspace_path())
            self._add_attachment(dest, "file")

    def _add_attachment(self, path: str, kind: str):
        self.pending_attachments.append({"path": path, "kind": kind})
        self.attachment_bar.refresh(self.pending_attachments)
        self.attachment_bar.show()
        n     = len(self.pending_attachments)
        label = "1 file attached" if n == 1 else f"{n} files attached"
        self.input_box.setPlaceholderText(f"📎 {label} — type your prompt...")
        self.setMinimumHeight(0)
        self.setMaximumHeight(16777215)
        QTimer.singleShot(0, self._relock_to_content)

    def remove_attachment(self, index: int):
        if 0 <= index < len(self.pending_attachments):
            self.pending_attachments.pop(index)
        self.attachment_bar.refresh(self.pending_attachments)
        if not self.pending_attachments:
            self.attachment_bar.hide()
            self.input_box.setPlaceholderText("System Pilot awaiting commands...")
            self.setFixedWidth(600)
            self.setFixedHeight(self._compact_height)
        else:
            self.setMinimumHeight(0)
            self.setMaximumHeight(16777215)
            QTimer.singleShot(0, self._relock_to_content)

    # ── Vision ────────────────────────────────────────────────────────────────

    def trigger_vision(self):
        """Hide UI, wait for it to fade, then capture screen."""
        self.hide()
        QTimer.singleShot(250, self.capture_vision_context)

    def capture_vision_context(self):
        try:
            vision_path = capture_screen(self.settings_manager.get_workspace_path())
            self._add_attachment(vision_path, "image")
        except Exception as e:
            print(f"Vision capture failed: {e}")
        finally:
            self.show()
            self.activateWindow()
            self.input_box.setFocus()

    # ── Voice recording ───────────────────────────────────────────────────────

    def toggle_voice_recording(self):
        self.auto_send_transcription = False
        if self.voice_recorder and self.voice_recorder.recording:
            self.stop_voice_recording()
        else:
            self.start_voice_recording()

    def start_voice_recording(self):
        if self.voice_recorder and self.voice_recorder.recording:
            return
        from core.audio.recorder import VoiceRecorder
        self.input_box.setPlaceholderText("Listening... (Click again or release F4 to stop)")
        self.mic_btn.setStyleSheet(
            self.mic_btn.styleSheet() +
            " QPushButton { background-color: rgba(255,50,50,0.3); border-color: #ff5555; color: white; }"
        )
        tmp_dir    = os.path.join(self.settings_manager.get_workspace_path(), ".tmp")
        os.makedirs(tmp_dir, exist_ok=True)
        audio_path = os.path.join(tmp_dir, "voice_input.wav")
        try:
            self.voice_recorder = VoiceRecorder(filename=audio_path)
            self.voice_recorder.start_recording()
        except Exception as e:
            self.input_box.setPlaceholderText(f"[Voice Error] Could not start mic: {str(e)}")
            self.apply_theme(self.current_theme)
            self.voice_recorder           = None
            self.auto_send_transcription  = False

    def stop_voice_recording(self):
        if not self.voice_recorder or not self.voice_recorder.recording:
            return
        audio_path = self.voice_recorder.stop_recording()
        self.input_box.setPlaceholderText("Transcribing voice input...")
        self.apply_theme(self.current_theme)
        api_key = self.settings_manager.get_api_key()
        if not api_key:
            self.input_box.setPlaceholderText("Error: Gemini API Key missing.")
            return
        self.transcription_thread = VoiceTranscriptionThread(audio_path, api_key)
        self.transcription_thread.finished.connect(self.on_transcription_finished)
        self.transcription_thread.start()

    def on_transcription_finished(self, text: str):
        self.input_box.setPlaceholderText("System Pilot awaiting commands...")
        current = self.input_box.toPlainText()
        if text.startswith("[Error") or text.startswith("[Voice Error"):
            self.input_box.setText(f"{current}\n{text}".strip())
        else:
            self.input_box.setText(f"{current} {text}".strip())
            if getattr(self, "auto_send_transcription", False):
                self.auto_send_transcription = False
                self.handle_input()

    # ── Settings ──────────────────────────────────────────────────────────────

    def open_settings(self):
        self.settings_dialog = SettingsDialog(
            parent=self,
            settings_manager=self.settings_manager,
            engine=ai_engine,
        )
        screen = QGuiApplication.primaryScreen().geometry()
        x = (screen.width()  - self.settings_dialog.width())  // 2
        y = (screen.height() - self.settings_dialog.height()) // 2
        self.settings_dialog.move(x, y)
        self.settings_dialog.show()

    # ── Session management ────────────────────────────────────────────────────

    def clear_session(self):
        """Hide app, clear UI state, and spawn background summariser."""
        self.hide()
        self.output_box.clear()
        self.output_box.hide()
        self.input_box.clear()
        self.pending_attachments = []
        self.attachment_bar.refresh([])
        self.attachment_bar.hide()
        if hasattr(self, "vision_context_path"):
            self.vision_context_path = None
        self.input_box.setPlaceholderText("System Pilot awaiting commands...")
        if hasattr(self.input_box, "reset_size"):
            self.input_box.reset_size()
        QTimer.singleShot(0, self._snap_to_compact)
        threading.Thread(target=self._background_summarize_and_clear, daemon=True).start()

    def _background_summarize_and_clear(self):
        global ai_engine

        if len(ai_engine.messages) > 2:
            try:
                summary_prompt = (
                    "INSTRUCTION: Summarize the key takeaways, facts learned, and important context "
                    "from our conversation in a few bullet points. Focus heavily on user preferences "
                    "and ongoing project states. Output ONLY the summary."
                )
                messages = ai_engine.chat(summary_prompt, stream=False, display=False)

                response_text = ""
                for msg in messages:
                    if isinstance(msg, dict) and msg.get("role") == "assistant":
                        if isinstance(msg.get("content"), str):
                            response_text += msg["content"]
                        else:
                            for block in msg.get("content", []):
                                if isinstance(block, dict) and block.get("type") == "message":
                                    response_text += block.get("content", "")
                                elif isinstance(block, str):
                                    response_text += block

                if response_text.strip():
                    import datetime
                    memory_file = os.path.join(
                        self.settings_manager.get_workspace_path(), "long_term_memory.txt"
                    )
                    with open(memory_file, "a", encoding="utf-8") as f:
                        date_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
                        f.write(f"\n[{date_str}] Session Summary:\n{response_text.strip()}\n")

            except Exception as e:
                print(f"Background summarization failed: {e}")

        # Clear engine context
        ai_engine.messages = []

        # Wipe short-term history file
        history_file = os.path.join(
            self.settings_manager.get_workspace_path(), "history.json"
        )
        if os.path.exists(history_file):
            try:
                os.remove(history_file)
            except Exception as e:
                print(f"Failed to clear short-term history buffer: {e}")

        # Re-hydrate engine with updated permanent memory
        reload_instructions(ai_engine, self.settings_manager)

    def close_app(self):
        """✕ button: confirm if AI is running, then clear session."""
        if self.worker and self.worker.isRunning():
            reply = QMessageBox.warning(
                self, "Warning",
                "System Pilot is currently thinking.\nAre you sure you want to cancel the session?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.Yes:
                self.clear_session()
        else:
            self.clear_session()

    # ── Input handling ────────────────────────────────────────────────────────

    def handle_input(self):
        prompt = self.input_box.toPlainText().strip()
        if not prompt:
            return

        # Encode and inject all pending attachments
        if self.pending_attachments:
            for att in self.pending_attachments:
                msg = encode_attachment_for_engine(att)
                if msg:
                    ai_engine.messages.append(msg)
            self.pending_attachments = []
            self.attachment_bar.refresh([])
            self.attachment_bar.hide()
            self.input_box.setPlaceholderText("System Pilot awaiting commands...")

        self.current_prompt   = prompt
        self.current_response = ""
        self.loading_dots     = 0

        self.input_box.clear()
        self.input_box.reset_size()
        self.output_box.show()
        self.output_box.setMarkdown(f"**You:** {self.current_prompt}\n\n**Pilot:** Thinking...")

        self.setMinimumWidth(300)
        self.setMaximumWidth(16777215)
        self.setMinimumHeight(200)
        self.setMaximumHeight(16777215)
        target_h = max(getattr(self, "expanded_height", 500), 500)
        self.resize(self.width(), target_h)

        self.input_box.setReadOnly(True)
        self.input_box.setPlaceholderText("System is thinking...💭")

        self.worker = AIWorker(prompt, ai_engine)
        self.worker.chunk_received.connect(self.append_output)
        self.worker.finished.connect(self.worker_finished)
        self.worker.start()
        self.loading_timer.start(500)

    def animate_loading(self):
        self.loading_dots = (self.loading_dots + 1) % 4
        dots = "." * self.loading_dots
        self.output_box.setMarkdown(f"**You:** {self.current_prompt}\n\n**Pilot:** Thinking{dots}")

    def append_output(self, text: str):
        if self.loading_timer.isActive():
            self.loading_timer.stop()
        self.current_response += text
        try:
            html_content = markdown.markdown(
                self.current_response,
                extensions=["fenced_code", "tables", "nl2br"]
            )
        except Exception:
            html_content = f"<pre>{self.current_response}</pre>"

        full_html = f"<b>You:</b> {self.current_prompt}<br><br><b>Pilot:</b><br>{html_content}"
        self.output_box.setHtml(full_html)

        scrollbar = self.output_box.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def worker_finished(self, full_response: str):
        self.input_box.setReadOnly(False)
        self.input_box.setPlaceholderText("System Pilot awaiting commands...")
        self.input_box.setFocus()
        Logger.log_interaction(self.current_prompt, full_response)

    # ── Keyboard ──────────────────────────────────────────────────────────────

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
        super().keyPressEvent(event)

    # ── Mouse: drag & resize ──────────────────────────────────────────────────

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._resize_edge = self.check_resize_edge(event.pos())
            if self._resize_edge:
                self._is_resizing      = True
                self._start_geometry   = self.geometry()
                self._start_mouse_pos  = event.globalPosition().toPoint()
            else:
                self._is_resizing = False
                self.offset       = event.pos()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            if getattr(self, "_is_resizing", False):
                if not self.output_box.isHidden():
                    self.settings_manager.save_window_size(self.width(), self.height())
                    self.expanded_height = self.height()
                else:
                    _, h = self.settings_manager.get_window_size()
                    self.settings_manager.save_window_size(self.width(), h)
            self._is_resizing = False
            self.offset       = None

    def check_resize_edge(self, pos) -> int:
        margin = 8
        edge   = 0
        rect   = self.rect()
        if pos.x() <= margin:
            edge |= 1
        elif pos.x() >= rect.width() - margin:
            edge |= 2
        if not hasattr(self, "output_box") or not self.output_box.isHidden():
            if pos.y() <= margin:
                edge |= 4
            elif pos.y() >= rect.height() - margin:
                edge |= 8
        return edge

    def _update_cursor(self, edge: int):
        cursors = {
            1: Qt.CursorShape.SizeHorCursor,
            2: Qt.CursorShape.SizeHorCursor,
            4: Qt.CursorShape.SizeVerCursor,
            8: Qt.CursorShape.SizeVerCursor,
            5: Qt.CursorShape.SizeFDiagCursor,
            10: Qt.CursorShape.SizeFDiagCursor,
            9: Qt.CursorShape.SizeBDiagCursor,
            6: Qt.CursorShape.SizeBDiagCursor,
        }
        self.setCursor(cursors.get(edge, Qt.CursorShape.ArrowCursor))

    def mouseMoveEvent(self, event):
        global_pos = event.globalPosition().toPoint()

        if event.buttons() != Qt.MouseButton.LeftButton:
            self._update_cursor(self.check_resize_edge(event.pos()))
            return

        if getattr(self, "_is_resizing", False):
            delta = global_pos - self._start_mouse_pos
            rect  = self._start_geometry
            x, y, w, h = rect.x(), rect.y(), rect.width(), rect.height()
            min_w, min_h = 400, 150
            max_w, max_h = 2560, 1440

            edge = getattr(self, "_resize_edge", 0)
            if edge & 1:
                new_w = max(min_w, min(max_w, w - delta.x()))
                x += w - new_w
                w  = new_w
            elif edge & 2:
                w = max(min_w, min(max_w, w + delta.x()))
            if edge & 4:
                new_h = max(min_h, min(max_h, h - delta.y()))
                y += h - new_h
                h  = new_h
            elif edge & 8:
                h = max(min_h, min(max_h, h + delta.y()))
            self.setGeometry(x, y, w, h)

        elif getattr(self, "offset", None) is not None:
            self.move(self.pos() + event.pos() - self.offset)

    # ── Global hotkeys ────────────────────────────────────────────────────────

    def setup_trigger(self):
        self._f4_pressed = False
        threading.Thread(target=self._listen, daemon=True).start()

    def _listen(self):
        keyboard.add_hotkey("windows+alt", self.trigger_toggle)
        keyboard.on_press_key("F4",   self._on_f4_press)
        keyboard.on_release_key("F4", self._on_f4_release)
        keyboard.wait()

    def trigger_toggle(self):
        self.toggle_signal.emit()

    def _on_f4_press(self, event):
        if not self._f4_pressed:
            self._f4_pressed             = True
            self.auto_send_transcription = True
            self.start_record_signal.emit()

    def _on_f4_release(self, event):
        if self._f4_pressed:
            self._f4_pressed = False
            self.stop_record_signal.emit()

    @pyqtSlot()
    def toggle_ui(self):
        if self.isVisible():
            self.hide()
        else:
            self.show()
            self.activateWindow()
            self.input_box.setFocus()


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    def sigint_handler(signum, frame):
        print("\nForce quitting application...")
        os._exit(0)

    signal.signal(signal.SIGINT, sigint_handler)

    app    = QApplication(sys.argv)
    window = SpotlightUI()
    window.show()

    # Tiny recurring timer so Python can process SIGINT while the C++ event loop spins
    pulse = QTimer()
    pulse.start(500)
    pulse.timeout.connect(lambda: None)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
