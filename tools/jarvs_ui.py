import sys
import threading
import keyboard
from PyQt6.QtWidgets import QApplication, QWidget, QVBoxLayout, QLineEdit, QTextEdit, QMenu, QFileDialog
from PyQt6.QtCore import Qt, pyqtSignal, QThread, pyqtSlot
from PyQt6.QtGui import QAction
import os

# Ensure the root directory is in sys.path so 'tools' can be imported
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Layer 2: Navigation — engine import (activated in Phase 2)
from tools.engine import setup_interpreter
from tools.settings_manager import SettingsManager

# Initialize engine globally for the app
ai_engine = setup_interpreter()

from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QComboBox, QSizePolicy, QFrame
from PyQt6.QtCore import Qt, QObject, QEvent

class GlobalWheelFilter(QObject):
    """Blocks Ctrl+Scroll on QComboBox only — prevents QFont size <= 0 crashes in native popups.
    ChatInputBox and OutputBox handle their own zoom internally."""
    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.Wheel and event.modifiers() == Qt.KeyboardModifier.ControlModifier:
            if isinstance(obj, QComboBox):
                return True  # Block all Ctrl+Scroll on combo boxes
        return super().eventFilter(obj, event)


class NoScrollComboBox(QComboBox):
    """QComboBox subclass that swallows all wheel events to prevent QFont::setPointSize <= 0 crashes."""
    def wheelEvent(self, event):
        event.ignore()  # Do nothing — prevents native popup from adjusting font size on scroll

class SettingsDialog(QWidget):
    def __init__(self, parent=None, settings_manager=None):
        super().__init__(parent, Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.parent_ui = parent
        self.settings = settings_manager
        self.setFixedSize(450, 420)
        self.setObjectName("SettingsDialog")
        # WA_TranslucentBackground is required for rounded corners to work on frameless widgets
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        # Two-layer structure mirroring the main SpotlightUI (GradientWrapper + MainContainer)
        # This is the proven pattern for correct rounded corner rendering on Windows
        self.setStyleSheet("""
            QWidget#SettingsDialog {
                background-color: transparent;
            }
            QWidget#SettingsOuterWrapper {
                background-color: #3a3a3a;
                border-radius: 12px;
            }
            QWidget#SettingsInnerContent {
                background-color: #1e1e1e;
                border-radius: 11px;
            }
            QLabel { font-size: 10pt; font-weight: bold; margin-top: 10px; color: #ccc; }
            QLineEdit, QComboBox { background-color: #2b2b2b; color: #fff; border: 1px solid #444; border-radius: 4px; padding: 6px; font-size: 10pt; }
            QComboBox QAbstractItemView { background-color: #2b2b2b; color: #fff; border: 1px solid #444; selection-background-color: #555; }
            QPushButton { background-color: #3a3a3a; color: #fff; border: 1px solid #555; border-radius: 4px; padding: 6px 15px; font-weight: bold; font-size: 10pt; }
            QPushButton:hover { background-color: #4a4a4a; }
            QPushButton#SaveBtn { background-color: #2d7a2d; border-color: #3b9c3b; }
            QPushButton#SaveBtn:hover { background-color: #3b9c3b; }
            QPushButton#QuitBtn { background-color: #8b2a2a; border-color: #a83232; }
            QPushButton#QuitBtn:hover { background-color: #a83232; }
        """)

        # Outer layout on self (transparent) — just holds the wrapper
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)

        # Outer wrapper: provides the visible rounded border color
        self.outer_wrapper = QWidget(self)
        self.outer_wrapper.setObjectName("SettingsOuterWrapper")
        outer_wrapper_layout = QVBoxLayout(self.outer_wrapper)
        outer_wrapper_layout.setContentsMargins(1, 1, 1, 1)  # 1px border thickness

        # Inner content widget: the actual dark background
        self.inner_content = QWidget(self.outer_wrapper)
        self.inner_content.setObjectName("SettingsInnerContent")
        layout = QVBoxLayout(self.inner_content)
        layout.setSpacing(8)
        layout.setContentsMargins(20, 20, 20, 20)

        outer_wrapper_layout.addWidget(self.inner_content)
        outer_layout.addWidget(self.outer_wrapper)

        # Title
        title = QLabel("Jarvs Settings")
        title.setStyleSheet("font-size: 14pt; color: #fff; margin-bottom: 5px;")
        layout.addWidget(title)

        # Theme Dropdown
        layout.addWidget(QLabel("Theme:"))
        self.theme_cb = NoScrollComboBox()
        self.theme_cb.addItems(["AI Vaporwave", "Dark", "Light", "Glassy Windows"])
        self.theme_cb.setCurrentText(self.settings.get_theme())
        layout.addWidget(self.theme_cb)

        # Model Dropdown
        layout.addWidget(QLabel("LLM Model:"))
        self.model_cb = NoScrollComboBox()
        self.model_cb.addItems(["gemini/gemini-2.5-flash", "gemini/gemini-1.5-pro", "gpt-4o", "claude-3-5-sonnet"])
        self.model_cb.setCurrentText(self.settings.get_model())
        self.model_cb.setEditable(True)
        layout.addWidget(self.model_cb)

        # API Key
        layout.addWidget(QLabel("Gemini / LLM API Key (saved to .env):"))
        self.api_input = QLineEdit()
        self.api_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_input.setText(self.settings.get_api_key())
        layout.addWidget(self.api_input)

        # Workspace Path
        layout.addWidget(QLabel("Default Workspace Path:"))
        self.workspace_input = QLineEdit()
        self.workspace_input.setText(self.settings.get_workspace_path())
        layout.addWidget(self.workspace_input)

        layout.addStretch()

        # Buttons
        from PyQt6.QtWidgets import QHBoxLayout
        btn_layout = QHBoxLayout()
        
        quit_btn = QPushButton("Quit Jarvs Completely")
        quit_btn.setObjectName("QuitBtn")
        quit_btn.clicked.connect(self.quit_app)
        btn_layout.addWidget(quit_btn)
        
        btn_layout.addStretch()
        
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.close)
        btn_layout.addWidget(cancel_btn)
        
        save_btn = QPushButton("Save & Apply")
        save_btn.setObjectName("SaveBtn")
        save_btn.clicked.connect(self.save_and_close)
        btn_layout.addWidget(save_btn)

        layout.addLayout(btn_layout)

    def quit_app(self):
        from PyQt6.QtWidgets import QApplication
        QApplication.quit()

    def save_and_close(self):
        # Save to JSON (without API key)
        theme = self.theme_cb.currentText()
        api_key = self.api_input.text().strip()
        workspace = self.workspace_input.text().strip()
        model = self.model_cb.currentText().strip()
        
        self.settings.save_settings(theme, workspace, model)
        
        # Save API key ONLY to .env
        import os
        env_path = os.path.join(self.settings.root_dir, ".env")
        if api_key:
            with open(env_path, "w", encoding="utf-8") as f:
                f.write(f"GEMINI_API_KEY={api_key}\n")
        
        # Apply theme instantly
        self.parent_ui.apply_theme(theme)
        
        # Update engine dynamic variables
        global ai_engine
        if api_key:
            ai_engine.llm.api_key = api_key
        if model:
            ai_engine.llm.model = model
        if workspace:
            # Inject new strict path into custom instructions safely without duplicating it excessively
            # For this MVP, we just rely on engine.py pulling the latest on reboot, 
            # but we can hack the string here for live apply.
            ai_engine.custom_instructions += f"\n\n[UPDATE] Your MUST USE THIS NEW WORKSPACE ABSOLUTE PATH: {workspace}"
            
        self.close()


class AIWorker(QThread):
    """Layer 2: Navigation - Handles async LLM execution off the main GUI thread."""
    chunk_received = pyqtSignal(str)
    finished = pyqtSignal(str) # Now emits the full response when finished

    def __init__(self, prompt):
        super().__init__()
        self.prompt = prompt
        self.full_response = ""

    def run(self):
        try:
            # Use stream=True to get a generator from open-interpreter
            # display=False prevents it from writing to stdout
            for chunk in ai_engine.chat(self.prompt, stream=True, display=False):
                # Parse open-interpreter 0.4.x chunk dictionary format
                if isinstance(chunk, dict):
                    role = chunk.get("role")
                    type_ = chunk.get("type")
                    
                    if role in ["assistant", "computer"]:
                        if chunk.get("start"):
                            if type_ == "code":
                                lang = chunk.get("format", "python")
                                prefix = f"\n```{lang}\n"
                                self.full_response += prefix
                                self.chunk_received.emit(prefix)
                            elif type_ == "console":
                                prefix = "\n```console\n"
                                self.full_response += prefix
                                self.chunk_received.emit(prefix)
                        
                        elif chunk.get("end"):
                            if type_ in ["code", "console"]:
                                suffix = "\n```\n"
                                self.full_response += suffix
                                self.chunk_received.emit(suffix)
                        
                        elif "content" in chunk:
                            content = chunk["content"]
                            
                            # We don't want to spam the UI with active line numbers being executed
                            if type_ == "console" and chunk.get("format") == "active_line":
                                continue
                                
                            if isinstance(content, str):
                                # Truncate massive base64 image strings from chunk streams
                                if type_ == "console" and len(content) > 1000 and " " not in content[:100]:
                                    content = "\n\n*[Large binary/base64 output omitted]*\n\n"
                                self.full_response += content
                                self.chunk_received.emit(content)
                            elif isinstance(content, list):
                                for block in content:
                                    if isinstance(block, dict) and block.get("type") == "message":
                                        text = block.get("content", "")
                                        self.full_response += text
                                        self.chunk_received.emit(text)
        except Exception as e:
            err_msg = f"\n[Error: {str(e)}]"
            self.full_response += err_msg
            self.chunk_received.emit(err_msg)
        finally:
            self.finished.emit(self.full_response)
            
            # Phase 8: Dump conversation history to maintain long-term context
            import json
            root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            history_file = os.path.join(root_dir, "workspace", "history.json")
            try:
                with open(history_file, "w", encoding="utf-8") as f:
                    # Open Interpreter messages format: list of dicts
                    json.dump(ai_engine.messages, f, indent=4, ensure_ascii=False)
            except Exception as e:
                print(f"Failed to save long-term memory: {e}")


class VoiceTranscriptionThread(QThread):
    finished = pyqtSignal(str) # Emits transcribed text or error message

    def __init__(self, audio_file_path, api_key):
        super().__init__()
        self.audio_file_path = audio_file_path
        self.api_key = api_key

    def run(self):
        try:
            from tools.transcriber import transcribe_audio_gemini
            text = transcribe_audio_gemini(self.audio_file_path, self.api_key)
            self.finished.emit(text)
        except Exception as e:
            self.finished.emit(f"[Voice Error: {e}]")


class AttachmentBar(QWidget):
    """Horizontal bar showing pending attachment chips with ✕ remove buttons."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(4, 2, 4, 2)
        self._layout.setSpacing(4)
        self._layout.addStretch()
        self.setMaximumHeight(32)

    def refresh(self, attachments):
        """Rebuild chips from the attachments list."""
        while self._layout.count() > 0:
            item = self._layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        for i, att in enumerate(attachments):
            chip = self._make_chip(att, i)
            self._layout.addWidget(chip)
        self._layout.addStretch()

    def _make_chip(self, att, index):
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

    def _remove(self, index):
        parent = self.parent()
        while parent and not hasattr(parent, 'remove_attachment'):
            parent = parent.parent()
        if parent:
            parent.remove_attachment(index)


class ChatInputBox(QTextEdit):
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
        self._font_size = 13  # track pt size ourselves (QSS px breaks pointSize())

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
        """Re-applies only the font-size so QSS background/border are preserved."""
        from PyQt6.QtGui import QFont
        f = QFont()
        f.setPointSize(self._font_size)
        self.setFont(f)

    def adjust_height(self):
        doc_height = int(self.document().documentLayout().documentSize().height())
        target_height = doc_height + 14 # padding allowance
        
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

        if event.key() == Qt.Key.Key_Space and event.modifiers() == Qt.KeyboardModifier.ShiftModifier:
            self.insertPlainText("\n")
            return
        elif event.key() == Qt.Key.Key_Return and event.modifiers() == Qt.KeyboardModifier.ShiftModifier:
            self.insertPlainText("\n")
            return
        elif event.key() == Qt.Key.Key_Return and event.modifiers() == Qt.KeyboardModifier.NoModifier:
            self.returnPressed.emit()
            return
        super().keyPressEvent(event)


class OutputBox(QTextEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._font_size = 13  # track pt size ourselves

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
        from PyQt6.QtGui import QFont
        f = QFont()
        f.setPointSize(self._font_size)
        self.setFont(f)


class SpotlightUI(QWidget):
    """Layer 3: Tool - The frameless, translucent Jarvs Spotlight UI."""

    # Define custom signal at the class level
    toggle_signal = pyqtSignal()
    start_record_signal = pyqtSignal()
    stop_record_signal = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.worker = None
        self.offset = None # For dragging the form
        self.current_prompt = ""
        self.current_response = ""
        from PyQt6.QtCore import QTimer
        self.loading_timer = QTimer(self)
        self.loading_timer.timeout.connect(self.animate_loading)
        
        # Install global wheel filter to protect settings UI
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance()
        if app:
            self._wheel_filter = GlobalWheelFilter()
            app.installEventFilter(self._wheel_filter)
            
        self.settings_manager = SettingsManager()
        self.current_theme = self.settings_manager.get_theme()
        
        self.init_ui()
        self.apply_theme(self.current_theme)
        
        self.setup_trigger()

        # Connect the custom signal to the UI toggle slot
        self.toggle_signal.connect(self.toggle_ui)
        self.start_record_signal.connect(self.start_voice_recording)
        self.stop_record_signal.connect(self.stop_voice_recording)
        self.auto_send_transcription = False


    def init_ui(self):
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        
        # Wrapping everything in a GradientWrapper to allow gradient border
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        
        self.gradient_wrapper = QWidget(self)
        self.gradient_wrapper.setObjectName("GradientWrapper")
        grad_layout = QVBoxLayout(self.gradient_wrapper)
        grad_layout.setContentsMargins(1, 1, 1, 1) # 1px thick gradient border
        
        self.container = QWidget(self.gradient_wrapper)
        self.container.setObjectName("MainContainer")
        
        layout = QVBoxLayout(self.container)
        layout.setContentsMargins(4, 4, 4, 4) # Drastically reduced black borders
        layout.setSpacing(4) # Tighter layout

        # Top Bar for controls and dragging
        from PyQt6.QtWidgets import QHBoxLayout, QPushButton, QLabel
        self.top_bar = QHBoxLayout()
        self.top_bar.setContentsMargins(4, 2, 4, 0)
        
        self.title_label = QLabel("Jarvs Spotlight")
        self.title_label.setStyleSheet("color: #666; font-weight: bold; font-size: 11px; padding: 5px;")
        self.title_label.setToolTip("Drag here to move the window")
        self.top_bar.addWidget(self.title_label)
        
        self.settings_btn = QPushButton("⚙")
        self.settings_btn.setFixedSize(30, 30) # Safe hitbox
        self.settings_btn.clicked.connect(self.open_settings)
        self.top_bar.addWidget(self.settings_btn)

        self.top_bar.addStretch()
        
        self.min_btn = QPushButton("-")
        self.min_btn.setFixedSize(30, 30)
        self.min_btn.setStyleSheet("QPushButton { background-color: transparent; color: #666; border: none; font-weight: normal; font-size: 16px; } QPushButton:hover { color: white; background-color: rgba(255,255,255,0.08); border-radius: 5px; }")
        self.min_btn.setToolTip("Hide (Esc)")
        self.min_btn.clicked.connect(self.hide)

        self.close_btn = QPushButton("✕")
        self.close_btn.setFixedSize(30, 30)
        self.close_btn.setStyleSheet("QPushButton { background-color: transparent; color: #666; border: none; font-weight: normal; font-size: 12px; } QPushButton:hover { color: white; background-color: rgba(255,85,85,0.3); border-radius: 5px; }")
        self.close_btn.setToolTip("Clear Session & Hide (Resets memory)")
        self.close_btn.clicked.connect(self.close_app)

        self.top_bar.addWidget(self.min_btn)
        self.top_bar.addWidget(self.close_btn)
        layout.addLayout(self.top_bar)

        # Output box (hidden until needed, placed ABOVE input)
        self.output_box = OutputBox(self.container)
        self.output_box.setReadOnly(True)
        self.output_box.hide()
        layout.addWidget(self.output_box)
        # Advanced stylesheet for beautiful Markdown Code frames
        self.output_box.document().setDefaultStyleSheet(
            "h1, h2, h3 { font-size: 14px; font-weight: bold; color: #eee; margin-top: 6px; margin-bottom: 6px; } "
            "p { margin-top: 4px; margin-bottom: 4px; line-height: 1.4; } "
            "pre { background-color: #1a1a1a; color: #e0e0e0; padding: 12px; border: 1px solid #333; border-radius: 8px; font-family: Consolas, monospace; margin: 10px 0px; } "
            "code { background-color: #2b2b2b; color: #d4d4d4; font-family: Consolas, monospace; padding: 2px 4px; border-radius: 4px; } "
            "a { color: #58a6ff; text-decoration: none; } "
        )
        self.output_box.hide()
        layout.addWidget(self.output_box)

        # Attachment bar (hidden until files are attached)
        self.attachment_bar = AttachmentBar(self.container)
        self.attachment_bar.hide()
        layout.addWidget(self.attachment_bar)

        # Input row: [ + button ] [ mic button ] [ input box ]
        input_row = QHBoxLayout()
        input_row.setContentsMargins(0, 0, 0, 0)
        input_row.setSpacing(6)

        self.attach_btn = QPushButton("+")
        self.attach_btn.setFixedSize(28, 28)
        self.attach_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.attach_btn.setToolTip("Attach file or capture screen")
        self.attach_btn.setStyleSheet(
            "QPushButton { background-color: transparent; color: #888; "
            "border: 1px solid rgba(255,255,255,0.15); border-radius: 14px; "
            "font-size: 16px; font-weight: normal; } "
            "QPushButton:hover { color: #ddd; background-color: rgba(255,255,255,0.1); "
            "border-color: rgba(255,255,255,0.3); }"
        )
        self.attach_btn.clicked.connect(self.show_attach_menu)
        input_row.addWidget(self.attach_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        self.mic_btn = QPushButton("🎙")
        self.mic_btn.setFixedSize(28, 28)
        self.mic_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.mic_btn.setToolTip("Click to record voice input")
        self.mic_btn.setStyleSheet(
            "QPushButton { background-color: transparent; color: #888; "
            "border: 1px solid rgba(255,255,255,0.15); border-radius: 14px; "
            "font-size: 16px; font-weight: normal; } "
            "QPushButton:hover { color: #ff5555; background-color: rgba(255,85,85,0.1); "
            "border-color: rgba(255,85,85,0.4); }"
        )
        self.mic_btn.clicked.connect(self.toggle_voice_recording)
        input_row.addWidget(self.mic_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        # Input box (placed BELOW output)
        self.input_box = ChatInputBox(self.container)
        self.input_box.setPlaceholderText("System Pilot awaiting commands...")
        self.input_box.setToolTip("Type your command here and press Enter (Shift+Space for newline)")
        self.input_box.returnPressed.connect(self.handle_input)
        self.input_box.heightChanged.connect(self.adjust_window_size)
        input_row.addWidget(self.input_box)

        layout.addLayout(input_row)

        # Initialize pending attachments list
        self.pending_attachments = []
        self.voice_recorder = None
        self.transcription_thread = None

        grad_layout.addWidget(self.container)
        main_layout.addWidget(self.gradient_wrapper)
        self.setLayout(main_layout)
        
        # Turn on mouse tracking for resize edge detection
        self.setMouseTracking(True)
        self.gradient_wrapper.setMouseTracking(True)
        self.container.setMouseTracking(True)
        
        # Set minimum compact size to allow horizontal shrinking
        self.setMinimumWidth(300)
        
        # Load persistent size from user preferences
        w, h = self.settings_manager.get_window_size()
        # Fallback to a nice comfortable default if the settings are tiny
        if w < 500: w = 600
        
        self.resize(w, 80)
        self.adjustSize()
        self.setFixedHeight(self.height())
        self._compact_height = self.height()
        self.expanded_height = h if h > 200 else 500

    def adjust_window_size(self):
        """Allows the window to grow naturally wrapping the text input, without locking width."""
        if self.output_box.isHidden():
            w = self.width()
            self.setMinimumHeight(0)
            self.setMaximumHeight(16777215)
            self.adjustSize()
            self.setFixedHeight(self.height())
            self.resize(w, self.height())
            self._compact_height = self.height()

    def apply_theme(self, theme_name):
        self.current_theme = theme_name
        
        # 1) Aggressive Stylesheet Purge (Fixes Sticky Glass Bug)
        self.setStyleSheet("")
        self.gradient_wrapper.setStyleSheet("")
        self.container.setStyleSheet("")
        self.title_label.setStyleSheet("")
        self.min_btn.setStyleSheet("")
        self.close_btn.setStyleSheet("")
        self.attach_btn.setStyleSheet("")
        self.mic_btn.setStyleSheet("")
        self.settings_btn.setStyleSheet("")
        self.input_box.setStyleSheet("")
        self.output_box.setStyleSheet("")
        
        # Base advanced stylesheet for Markdown
        markdown_css = (
            "h1, h2, h3 { font-size: 13px; font-weight: bold; margin-top: 4px; margin-bottom: 4px; } "
            "p { margin-top: 2px; margin-bottom: 2px; line-height: 1.3; } "
            "pre { padding: 10px; border-radius: 6px; font-family: Consolas, monospace; margin: 8px 0px; } "
            "code { font-family: Consolas, monospace; padding: 2px 4px; border-radius: 4px; } "
            "a { text-decoration: none; } "
        )
        
        # Globally hide ugly scrollbar arrows and make scrollbar slim
        base_scrollbar_css = (
            "QScrollBar:vertical { border: none; background: transparent; width: 6px; margin: 0px; } "
            "QScrollBar::handle:vertical { background: rgba(120, 120, 120, 150); border-radius: 3px; min-height: 20px; } "
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; background: none; } "
            "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; } "
        )

        if theme_name == "Light":
            # Light Theme
            self.setStyleSheet("""
                QWidget#GradientWrapper { background-color: #cccccc; border-radius: 10px; }
                QWidget#MainContainer { background-color: #ffffff; border-radius: 9px; }
                QToolTip { color: #000; background-color: #fafafa; border: 1px solid #ccc; border-radius: 4px; padding: 4px; font-size: 11px; }
            """ + base_scrollbar_css)
            self.title_label.setStyleSheet("color: #555; font-weight: bold; font-size: 11px; padding: 0px 5px;")
            self.settings_btn.setStyleSheet("QPushButton { background-color: transparent; color: #555; border: none; font-size: 14px; } QPushButton:hover { color: #000; background-color: rgba(0,0,0,0.08); border-radius: 5px; }")
            self.min_btn.setStyleSheet("QPushButton { background-color: transparent; color: #555; border: none; font-size: 16px; } QPushButton:hover { color: #000; background-color: rgba(0,0,0,0.08); border-radius: 5px; }")
            self.close_btn.setStyleSheet("QPushButton { background-color: transparent; color: #555; border: none; font-size: 12px; } QPushButton:hover { color: white; background-color: rgba(255,85,85,0.8); border-radius: 5px; }")
            self.input_box.setStyleSheet("background-color: #f1f1f1; color: #111; border-radius: 6px; padding: 8px 10px; border: 1px solid #ddd;")
            self.attach_btn.setStyleSheet("QPushButton { background-color: rgba(0,0,0,0.05); color: #777; border: 1px solid #ccc; border-radius: 14px; font-size: 16px; } QPushButton:hover { color: #333; background-color: rgba(0,0,0,0.1); border-color: #aaa; }")
            self.mic_btn.setStyleSheet("QPushButton { background-color: rgba(0,0,0,0.05); color: #777; border: 1px solid #ccc; border-radius: 14px; font-size: 16px; } QPushButton:hover { color: #ff5555; background-color: rgba(255,85,85,0.1); border-color: #ff5555; }")
            self.output_box.setStyleSheet("QTextEdit { background-color: transparent; color: #222; border: none; padding: 0px; }")
            self.output_box.document().setDefaultStyleSheet(markdown_css + "h1, h2, h3 { color: #111; } pre { background-color: #eee; color: #222; border: 1px solid #ddd; } code { background-color: #f5f5f5; color: #d03a3a; } a { color: #0066cc; }")

        elif theme_name == "Dark":
            # Dark Theme
            self.setStyleSheet("""
                QWidget#GradientWrapper { background-color: #333333; border-radius: 10px; }
                QWidget#MainContainer { background-color: #1a1a1a; border-radius: 9px; }
                QToolTip { color: #eee; background-color: #222; border: 1px solid #444; border-radius: 4px; padding: 4px; font-size: 11px; }
            """ + base_scrollbar_css)
            self.title_label.setStyleSheet("color: #888; font-weight: bold; font-size: 11px; padding: 0px 5px;")
            self.settings_btn.setStyleSheet("QPushButton { background-color: transparent; color: #888; border: none; font-size: 14px; } QPushButton:hover { color: white; background-color: rgba(255,255,255,0.08); border-radius: 5px; }")
            self.min_btn.setStyleSheet("QPushButton { background-color: transparent; color: #888; border: none; font-size: 16px; } QPushButton:hover { color: white; background-color: rgba(255,255,255,0.08); border-radius: 5px; }")
            self.close_btn.setStyleSheet("QPushButton { background-color: transparent; color: #888; border: none; font-size: 12px; } QPushButton:hover { color: white; background-color: rgba(255,85,85,0.3); border-radius: 5px; }")
            self.input_box.setStyleSheet("background-color: #262626; color: white; border-radius: 6px; padding: 8px 10px; border: 1px solid #333;")
            self.attach_btn.setStyleSheet("QPushButton { background-color: transparent; color: #888; border: 1px solid rgba(255,255,255,0.15); border-radius: 14px; font-size: 16px; } QPushButton:hover { color: #ddd; background-color: rgba(255,255,255,0.1); border-color: rgba(255,255,255,0.3); }")
            self.mic_btn.setStyleSheet("QPushButton { background-color: transparent; color: #888; border: 1px solid rgba(255,255,255,0.15); border-radius: 14px; font-size: 16px; } QPushButton:hover { color: #ff5555; background-color: rgba(255,85,85,0.1); border-color: rgba(255,85,85,0.4); }")
            self.output_box.setStyleSheet("QTextEdit { background-color: transparent; color: #ddd; border: none; padding: 0px; }")
            self.output_box.document().setDefaultStyleSheet(markdown_css + "h1, h2, h3 { color: #eee; } pre { background-color: #111; color: #e0e0e0; border: 1px solid #222; } code { background-color: #2b2b2b; color: #d4d4d4; } a { color: #58a6ff; }")

        elif theme_name == "Glassy Windows":
            # Glassy Windows — Win11 dark frosted glass style (matches Start menu acrylic)
            self.setStyleSheet("""
                QWidget#GradientWrapper {
                    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                        stop:0 rgba(50, 60, 80, 175),
                        stop:0.5 rgba(38, 44, 58, 155),
                        stop:1 rgba(45, 52, 70, 175));
                    border-radius: 10px;
                }
                QWidget#MainContainer {
                    background-color: rgba(20, 23, 30, 145);
                    border-radius: 9px;
                }
                QToolTip { color: #dde; background-color: rgba(30, 34, 46, 230); border: 1px solid rgba(120,140,200,0.4); border-radius: 4px; padding: 4px; font-size: 11px; }
            """ + base_scrollbar_css)
            self.title_label.setStyleSheet("color: rgba(200, 210, 230, 0.85); font-weight: bold; font-size: 11px; padding: 0px 5px;")
            self.settings_btn.setStyleSheet("QPushButton { background-color: transparent; color: rgba(200,210,230,0.8); border: none; font-size: 14px; } QPushButton:hover { color: white; background-color: rgba(255,255,255,0.12); border-radius: 5px; }")
            self.min_btn.setStyleSheet("QPushButton { background-color: transparent; color: rgba(200,210,230,0.8); border: none; font-size: 16px; } QPushButton:hover { color: white; background-color: rgba(255,255,255,0.12); border-radius: 5px; }")
            self.close_btn.setStyleSheet("QPushButton { background-color: transparent; color: rgba(200,210,230,0.8); border: none; font-size: 12px; } QPushButton:hover { color: white; background-color: rgba(255,85,85,0.45); border-radius: 5px; }")
            self.attach_btn.setStyleSheet("QPushButton { background-color: transparent; color: rgba(200,210,230,0.8); border: 1px solid rgba(120,140,200,0.3); border-radius: 14px; font-size: 16px; } QPushButton:hover { color: white; background-color: rgba(255,255,255,0.12); border-radius: 14px; }")
            self.mic_btn.setStyleSheet("QPushButton { background-color: transparent; color: rgba(200,210,230,0.8); border: 1px solid rgba(120,140,200,0.3); border-radius: 14px; font-size: 16px; } QPushButton:hover { color: #ff8888; background-color: rgba(255,85,85,0.15); border-color: rgba(255,85,85,0.4); }")
            self.input_box.setStyleSheet("background-color: rgba(255, 255, 255, 0.07); color: rgba(220,228,245,1); border-radius: 6px; padding: 8px 10px; border: 1px solid rgba(120,140,200,0.3);")
            self.output_box.setStyleSheet("QTextEdit { background-color: transparent; color: rgba(220,228,245,1); border: none; padding: 0px; }")
            self.output_box.document().setDefaultStyleSheet(markdown_css + "h1, h2, h3 { color: #ccd6f6; } pre { background-color: rgba(10,14,26,0.55); color: #ccd6f6; border: 1px solid rgba(100,130,200,0.25); } code { background-color: rgba(255,255,255,0.1); color: #a8d8ff; } a { color: #7eb8ff; }")


        else:
            # AI Vaporwave (Default)

            self.setStyleSheet("""
                QWidget#GradientWrapper {
                    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1, 
                        stop:0 rgba(80, 160, 255, 180), 
                        stop:0.5 rgba(150, 80, 255, 180), 
                        stop:1 rgba(255, 100, 140, 180));
                    border-radius: 10px;
                }
                QWidget#MainContainer {
                    background-color: rgba(18, 18, 18, 230);
                    border-radius: 9px;
                }
                QToolTip { color: #eee; background-color: #222; border: 1px solid #444; border-radius: 4px; padding: 4px; font-size: 11px; }
            """ + base_scrollbar_css)
            self.title_label.setStyleSheet("color: #888; font-weight: bold; font-size: 11px; padding: 0px 5px;")
            self.settings_btn.setStyleSheet("QPushButton { background-color: transparent; color: #888; border: none; font-size: 14px; } QPushButton:hover { color: white; background-color: rgba(255,255,255,0.1); border-radius: 5px; }")
            self.min_btn.setStyleSheet("QPushButton { background-color: transparent; color: #888; border: none; font-size: 16px; } QPushButton:hover { color: white; background-color: rgba(255,255,255,0.1); border-radius: 5px; }")
            self.close_btn.setStyleSheet("QPushButton { background-color: transparent; color: #888; border: none; font-size: 12px; } QPushButton:hover { color: white; background-color: rgba(255,85,85,0.4); border-radius: 5px; }")
            self.attach_btn.setStyleSheet("QPushButton { background-color: transparent; color: #888; border: 1px solid rgba(255,255,255,0.15); border-radius: 14px; font-size: 16px; } QPushButton:hover { color: #ddd; background-color: rgba(255,255,255,0.1); border-color: rgba(255,255,255,0.3); }")
            self.mic_btn.setStyleSheet("QPushButton { background-color: transparent; color: #888; border: 1px solid rgba(255,255,255,0.15); border-radius: 14px; font-size: 16px; } QPushButton:hover { color: #ff5555; background-color: rgba(255,85,85,0.1); border-color: rgba(255,85,85,0.4); }")
            self.input_box.setStyleSheet("background-color: rgba(30, 30, 30, 200); color: white; border-radius: 6px; padding: 8px 10px; border: 1px solid rgba(80, 80, 80, 100);")
            self.output_box.setStyleSheet("QTextEdit { background-color: transparent; color: #D4D4D4; border: none; padding: 0px; }")
            self.output_box.document().setDefaultStyleSheet(markdown_css + "h1, h2, h3 { color: #eee; } pre { background-color: #1a1a1a; color: #e0e0e0; border: 1px solid #333; } code { background-color: #2b2b2b; color: #d4d4d4; } a { color: #58a6ff; }")

    # ── Attachment / Dropup Menu ─────────────────────────────

    def show_attach_menu(self):
        """Show a dropup menu above the + button with attachment options."""
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu { background-color: #2a2a2a; color: #ddd; border: 1px solid #444; "
            "border-radius: 6px; padding: 4px 0px; } "
            "QMenu::item { padding: 6px 16px 6px 12px; } "
            "QMenu::item:selected { background-color: rgba(255,255,255,0.1); } "
            "QMenu::separator { height: 1px; background: #444; margin: 3px 8px; }"
        )
        act_image = menu.addAction("🖼  Attach Image")
        act_file  = menu.addAction("📎  Attach File")
        menu.addSeparator()
        act_screen = menu.addAction("👁  Capture Screen")
        act_image.triggered.connect(self.attach_image)
        act_file.triggered.connect(self.attach_file)
        act_screen.triggered.connect(self.trigger_vision)
        from PyQt6.QtCore import QPoint
        btn_pos = self.attach_btn.mapToGlobal(self.attach_btn.rect().topLeft())
        menu_height = menu.sizeHint().height()
        menu.exec(btn_pos - QPoint(0, menu_height))

    def attach_image(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Attach Image", "",
            "Images (*.png *.jpg *.jpeg *.webp *.heic *.heif *.bmp);;All Files (*)"
        )
        if path:
            stored = self._store_attachment(path)
            self._add_attachment(stored, "image")

    def attach_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Attach File", "",
            "Documents (*.pdf *.txt *.csv *.json *.py *.md *.html *.xml *.yaml *.yml *.log *.toml *.ini *.cfg);;All Files (*)"
        )
        if path:
            stored = self._store_attachment(path)
            self._add_attachment(stored, "file")

    def toggle_voice_recording(self):
        """Called when the Mic button is clicked to act as a toggle switch."""
        self.auto_send_transcription = False
        if self.voice_recorder and self.voice_recorder.recording:
            self.stop_voice_recording()
        else:
            self.start_voice_recording()

    def start_voice_recording(self):
        """Called to begin Voice Recording."""
        if self.voice_recorder and self.voice_recorder.recording:
            return
        from tools.voice_recorder import VoiceRecorder
        self.input_box.setPlaceholderText("Listening... (Click again or release F4 to stop)")
        self.mic_btn.setStyleSheet(self.mic_btn.styleSheet() + " QPushButton { background-color: rgba(255,50,50,0.3); border-color: #ff5555; color: white; }")
        tmp_dir = os.path.join(self.settings_manager.get_workspace_path(), ".tmp")
        os.makedirs(tmp_dir, exist_ok=True)
        audio_path = os.path.join(tmp_dir, "voice_input.wav")
        try:
            self.voice_recorder = VoiceRecorder(filename=audio_path)
            self.voice_recorder.start_recording()
        except Exception as e:
            self.input_box.setPlaceholderText(f"[Voice Error] Could not start mic: {str(e)}")
            self.apply_theme(self.current_theme)
            self.voice_recorder = None
            self.auto_send_transcription = False

    def stop_voice_recording(self):
        """Called to stop Voice Recording and begin transcription."""
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

    def on_transcription_finished(self, text):
        """Receives the text from the API and injects it into the input box."""
        self.input_box.setPlaceholderText("System Pilot awaiting commands...")
        if text.startswith("[Error") or text.startswith("[Voice Error"):
            current_text = self.input_box.toPlainText()
            self.input_box.setText(f"{current_text}\n{text}".strip())
        else:
            current_text = self.input_box.toPlainText()
            self.input_box.setText(f"{current_text} {text}".strip())
            if getattr(self, "auto_send_transcription", False):
                self.auto_send_transcription = False
                self.handle_input()

    def trigger_vision(self):
        self.hide()
        from PyQt6.QtCore import QTimer
        # Wait 250ms for the UI to completely fade so it isn't in the screenshot
        QTimer.singleShot(250, self.capture_vision_context)

    def capture_vision_context(self):
        import os
        from PyQt6.QtWidgets import QApplication
        screen = QApplication.primaryScreen()
        img = screen.grabWindow(0)
        
        tmp_dir = os.path.join(self.settings_manager.get_workspace_path(), ".tmp")
        os.makedirs(tmp_dir, exist_ok=True)
        
        vision_path = os.path.join(tmp_dir, "vision_context.png")
        img.save(vision_path, 'PNG')
        self._add_attachment(vision_path, "image")
        self.show()
        self.activateWindow()
        self.input_box.setFocus()

    def _store_attachment(self, source_path):
        """Copy file into workspace/.attachments/ with timestamp prefix. Returns dest path."""
        import shutil
        from datetime import datetime
        attach_dir = os.path.join(self.settings_manager.get_workspace_path(), ".attachments")
        os.makedirs(attach_dir, exist_ok=True)
        basename = os.path.basename(source_path)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = os.path.join(attach_dir, f"{ts}_{basename}")
        shutil.copy2(source_path, dest)
        return dest

    def _add_attachment(self, path, kind):
        """Register a pending attachment and show it in the attachment bar."""
        self.pending_attachments.append({"path": path, "kind": kind})
        self.attachment_bar.refresh(self.pending_attachments)
        self.attachment_bar.show()
        n = len(self.pending_attachments)
        label = "1 file attached" if n == 1 else f"{n} files attached"
        self.input_box.setPlaceholderText(f"📎 {label} — type your prompt...")
        self.setMinimumHeight(0)
        self.setMaximumHeight(16777215)
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(0, self._relock_to_content)

    def remove_attachment(self, index):
        """Remove a pending attachment by index."""
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
            from PyQt6.QtCore import QTimer
            QTimer.singleShot(0, self._relock_to_content)

    def _relock_to_content(self):
        """Resize window to its natural content height."""
        if self.output_box.isHidden():
            self.adjust_window_size()

    def open_settings(self):
        # Open the new customized Settings Dialog window
        self.settings_dialog = SettingsDialog(self, self.settings_manager)
        
        # Center it on the screen
        from PyQt6.QtGui import QGuiApplication
        screen = QGuiApplication.primaryScreen().geometry()
        x = (screen.width() - self.settings_dialog.width()) // 2
        y = (screen.height() - self.settings_dialog.height()) // 2
        self.settings_dialog.move(x, y)
        
        self.settings_dialog.show()

    def clear_session(self):
        """Soft close: hides app, clears UI, spawns background summarizer."""
        self.hide()
        
        # Clear UI elements
        self.output_box.clear()
        self.output_box.hide()
        self.input_box.clear()
        # Clear any pending attachments
        self.pending_attachments = []
        self.attachment_bar.refresh([])
        self.attachment_bar.hide()
        if hasattr(self, 'vision_context_path'):
            self.vision_context_path = None
        self.input_box.setPlaceholderText("System Pilot awaiting commands...")
        if hasattr(self.input_box, 'reset_size'):
            self.input_box.reset_size()
        
        # Defer compact-size lock to next event loop tick so all geometry
        # updates from hide/reset_size have settled before we measure
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(0, self._snap_to_compact)
        
        # Spawn background summarizer
        import threading
        threading.Thread(target=self._background_summarize_and_clear, daemon=True).start()

    def _snap_to_compact(self):
        """Re-bounds window to its strictly wrapped compact height."""
        self.setMinimumWidth(300)
        self.setMinimumHeight(0)
        self.setMaximumHeight(16777215)
        
        # Restore user saved width
        w, _ = self.settings_manager.get_window_size()
        if w < 300: w = 600
        
        self.resize(w, 80)
        self.adjustSize()
        self.setFixedHeight(self.height())
        self.resize(w, self.height())
        self._compact_height = self.height()

    def _background_summarize_and_clear(self):
        global ai_engine
        
        if len(ai_engine.messages) > 2:
            try:
                # Ask Jarvs to summarize the context
                summary_prompt = "INSTRUCTION: Summarize the key takeaways, facts learned, and important context from our conversation in a few bullet points. Focus heavily on user preferences and ongoing project states. Output ONLY the summary."
                
                # In open-interpreter 0.4.x, chat() with stream=False returns the messages list
                messages = ai_engine.chat(summary_prompt, stream=False, display=False)
                
                # Extract response text
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
                
                # Append to long term memory
                if response_text.strip():
                    import datetime
                    memory_file = os.path.join(self.settings_manager.get_workspace_path(), "long_term_memory.txt")
                    with open(memory_file, "a", encoding="utf-8") as f:
                        date_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
                        f.write(f"\n[{date_str}] Session Summary:\n{response_text.strip()}\n")
            
            except Exception as e:
                print(f"Background summarization failed: {e}")

        # Clear Background AI context
        ai_engine.messages = []
        
        # Delete history.json to ensure short-term persistence is wiped for the next trigger
        history_file = os.path.join(self.settings_manager.get_workspace_path(), "history.json")
        if os.path.exists(history_file):
            try:
                os.remove(history_file)
            except Exception as e:
                print(f"Failed to clear short-term history buffer: {e}")
                
        # Re-hydrate the active AI Engine with updated permanent memory so the next prompt has context!
        from tools.engine import reload_instructions
        reload_instructions(ai_engine, self.settings_manager)

    def close_app(self):
        """Close Button 'X' overrides to clear session instead of dead-stopping Qt App"""
        if self.worker and self.worker.isRunning():
            from PyQt6.QtWidgets import QMessageBox
            reply = QMessageBox.warning(
                self, 'Warning', 
                "System Pilot is currently thinking.\nAre you sure you want to cancel the session?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.Yes:
                self.clear_session()
        else:
            self.clear_session()

    def keyPressEvent(self, event):
        """Allow Esc to easily hide the app"""
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
        super().keyPressEvent(event)

    def handle_input(self):
        global ai_engine
        prompt = self.input_box.toPlainText().strip()
        if not prompt: return

        # Inject all pending attachments into OI messages
        if self.pending_attachments:
            import base64
            for att in self.pending_attachments:
                try:
                    if att["kind"] == "image":
                        with open(att["path"], "rb") as f:
                            b64 = base64.b64encode(f.read()).decode('utf-8')
                        ai_engine.messages.append({
                            "role": "user",
                            "type": "image",
                            "format": "base64.png",
                            "content": b64
                        })
                    else:
                        fname = os.path.basename(att["path"])
                        ext = os.path.splitext(fname)[1].lower()
                        if ext == ".pdf":
                            try:
                                from PyPDF2 import PdfReader
                                reader = PdfReader(att["path"])
                                pdf_text = "".join(page.extract_text() or "" for page in reader.pages)
                                content = f"[Attached PDF: {fname}]\n```\n{pdf_text.strip()}\n```" if pdf_text.strip() else f"[Attached PDF: {fname}] (No extractable text.)"
                            except Exception as pdf_err:
                                content = f"[Attached PDF: {fname}] (Failed to extract text: {pdf_err})"
                        else:
                            with open(att["path"], "r", encoding="utf-8", errors="replace") as f:
                                content = f"[Attached file: {fname}]\n```\n{f.read()}\n```"
                        ai_engine.messages.append({"role": "user", "type": "message", "content": content})
                except Exception as e:
                    print(f"Failed to inject attachment {att['path']}: {e}")
            # Consume attachments
            self.pending_attachments = []
            self.attachment_bar.refresh([])
            self.attachment_bar.hide()
            self.input_box.setPlaceholderText("System Pilot awaiting commands...")
        elif hasattr(self, 'vision_context_path') and self.vision_context_path:
            import base64
            v_path = self.vision_context_path.replace('\\', '/')
            try:
                with open(v_path, "rb") as image_file:
                    base64_image = base64.b64encode(image_file.read()).decode('utf-8')
                ai_engine.messages.append({"role": "user", "type": "image", "format": "base64.png", "content": base64_image})
            except Exception as e:
                print(f"Failed to encode vision context: {e}")
            self.vision_context_path = None
            self.input_box.setPlaceholderText("System Pilot awaiting commands...")
        
        self.current_prompt = prompt
        self.current_response = ""
        self.loading_dots = 0
        
        self.input_box.clear()
        self.input_box.reset_size() # Snap back to small height
        self.output_box.show()
        # Initialize output box with user prompt and thinking state
        self.output_box.setMarkdown(f"**You:** {self.current_prompt}\n\n**Pilot:** Thinking...")

        # Release fixed constraints to allow expanding
        self.setMinimumWidth(300)
        self.setMaximumWidth(16777215)
        self.setMinimumHeight(200)
        self.setMaximumHeight(16777215)
        
        target_h = getattr(self, 'expanded_height', 500)
        if target_h < 200: target_h = 500
        self.resize(self.width(), target_h)

        # Disable input while processing and change placeholder text
        self.input_box.setReadOnly(True)
        self.input_box.setPlaceholderText("System is thinking...💭")
        
        # Start AI worker thread
        self.worker = AIWorker(prompt)
        self.worker.chunk_received.connect(self.append_output)
        self.worker.finished.connect(self.worker_finished)
        self.worker.start()
        
        # Start animation timer
        self.loading_timer.start(500)

    def animate_loading(self):
        """Animates '...' while waiting for the first chunk."""
        self.loading_dots = (self.loading_dots + 1) % 4
        dots = "." * self.loading_dots
        self.output_box.setMarkdown(f"**You:** {self.current_prompt}\n\n**Pilot:** Thinking{dots}")

    def append_output(self, text):
        if self.loading_timer.isActive():
            self.loading_timer.stop() # Stop animation once we receive data
            
        self.current_response += text
        
        # Parse markdown into HTML for beautiful rendering
        import markdown
        try:
            # use fenced_code, tables, and nl2br (for single line breaks like shift+enter)
            html_content = markdown.markdown(self.current_response, extensions=['fenced_code', 'tables', 'nl2br'])
        except Exception:
            # fallback if markdown fails
            html_content = f"<pre>{self.current_response}</pre>"
            
        full_html = f"<b>You:</b> {self.current_prompt}<br><br><b>Pilot:</b><br>{html_content}"
        
        # Render HTML format in the QTextEdit
        self.output_box.setHtml(full_html)
        
        # Auto-scroll to bottom
        scrollbar = self.output_box.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def worker_finished(self, full_response):
        self.input_box.setReadOnly(False)
        self.input_box.setPlaceholderText("System Pilot awaiting commands...")
        self.input_box.setFocus()
        
        # Log to the background file
        from tools.logger import Logger
        Logger.log_interaction(self.current_prompt, full_response)

    def mousePressEvent(self, event):
        """Enable dragging the frameless window or resizing it using pure python logic."""
        if event.button() == Qt.MouseButton.LeftButton:
            self._resize_edge = self.check_resize_edge(event.pos())
            if self._resize_edge:
                self._is_resizing = True
                self._start_geometry = self.geometry()
                self._start_mouse_pos = event.globalPosition().toPoint()
            else:
                self._is_resizing = False
                self.offset = event.pos()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            if getattr(self, '_is_resizing', False):
                if not self.output_box.isHidden():
                    self.settings_manager.save_window_size(self.width(), self.height())
                    self.expanded_height = self.height()
                else:
                    _, h = self.settings_manager.get_window_size()
                    self.settings_manager.save_window_size(self.width(), h)
                    
            self._is_resizing = False
            self.offset = None

    def check_resize_edge(self, pos):
        margin = 8
        edge = 0
        rect = self.rect()
        
        # Always allow horizontal resizing
        if pos.x() <= margin: edge |= 1
        elif pos.x() >= rect.width() - margin: edge |= 2
            
        # Only allow vertical resizing if output box is visible (expanded mode)
        if not hasattr(self, 'output_box') or not self.output_box.isHidden():
            if pos.y() <= margin: edge |= 4
            elif pos.y() >= rect.height() - margin: edge |= 8
            
        return edge

    def _update_cursor(self, edge):
        if edge == 1 or edge == 2:  # Left or Right
            self.setCursor(Qt.CursorShape.SizeHorCursor)
        elif edge == 4 or edge == 8:  # Top or Bottom
            self.setCursor(Qt.CursorShape.SizeVerCursor)
        elif edge == 5 or edge == 10:  # Top-Left or Bottom-Right
            self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        elif edge == 9 or edge == 6:  # Bottom-Left or Top-Right
            self.setCursor(Qt.CursorShape.SizeBDiagCursor)
        else:
            self.setCursor(Qt.CursorShape.ArrowCursor)

    def mouseMoveEvent(self, event):
        """Enable dragging or resizing the frameless window."""
        global_pos = event.globalPosition().toPoint()
        
        # If no button is pressed, update cursor based on hover
        if event.buttons() != Qt.MouseButton.LeftButton:
            edge = self.check_resize_edge(event.pos())
            self._update_cursor(edge)
            return

        # If resizing active
        if getattr(self, '_is_resizing', False):
            delta = global_pos - self._start_mouse_pos
            rect = self._start_geometry
            x, y, w, h = rect.x(), rect.y(), rect.width(), rect.height()

            # Min bounds
            min_w, min_h = 400, 150
            max_w, max_h = 2560, 1440 

            edge = getattr(self, '_resize_edge', 0)
            if edge & 1: # Left
                new_w = max(min_w, min(max_w, w - delta.x()))
                x += (w - new_w)
                w = new_w
            elif edge & 2: # Right
                w = max(min_w, min(max_w, w + delta.x()))
                
            if edge & 4: # Top
                new_h = max(min_h, min(max_h, h - delta.y()))
                y += (h - new_h)
                h = new_h
            elif edge & 8: # Bottom
                h = max(min_h, min(max_h, h + delta.y()))

            self.setGeometry(x, y, w, h)
        elif getattr(self, 'offset', None) is not None:
            # We are dragging the window
            self.move(self.pos() + event.pos() - self.offset)

    def setup_trigger(self):
        """Phase 5: Trigger — Global hotkeys for UI toggle and Voice Input."""
        self._f4_pressed = False
        threading.Thread(target=self._listen, daemon=True).start()

    def _listen(self):
        # Phase 6: Global hotkey changed to Win+Alt per user request
        keyboard.add_hotkey('windows+alt', self.trigger_toggle)
        # Global push-to-talk on F4
        keyboard.on_press_key('F4', self._on_f4_press)
        keyboard.on_release_key('F4', self._on_f4_release)
        keyboard.wait()

    def trigger_toggle(self):
        self.toggle_signal.emit()

    def _on_f4_press(self, event):
        if not self._f4_pressed:
            self._f4_pressed = True
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


if __name__ == "__main__":
    import signal
    import os
    
    # Allow terminal to gracefully close the qt app with Ctrl+C
    # Using os._exit(0) to forcefully kill the keyboard.wait() daemon thread which blocks standard sys.exit()
    def sigint_handler(signum, frame):
        print("\nForce quitting application...")
        os._exit(0)
        
    signal.signal(signal.SIGINT, sigint_handler)
    
    app = QApplication(sys.argv)
    window = SpotlightUI()
    window.show()
    
    # A tiny recurring timer is required to let Python process signals safely while the C++ event loop spins
    from PyQt6.QtCore import QTimer
    timer = QTimer()
    timer.start(500)
    timer.timeout.connect(lambda: None)
    
    sys.exit(app.exec())
