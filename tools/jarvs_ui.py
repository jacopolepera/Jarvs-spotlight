import sys
import threading
import keyboard
from PyQt6.QtWidgets import QApplication, QWidget, QVBoxLayout, QLineEdit, QTextEdit
from PyQt6.QtCore import Qt, pyqtSignal, QThread, pyqtSlot
import os

# Ensure the root directory is in sys.path so 'tools' can be imported
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Layer 2: Navigation — engine import (activated in Phase 2)
from tools.engine import setup_interpreter
from tools.settings_manager import SettingsManager

# Initialize engine globally for the app
ai_engine = setup_interpreter()

from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QComboBox
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

        self.vision_btn = QPushButton("👁")
        self.vision_btn.setFixedSize(30, 30)
        self.vision_btn.setStyleSheet("QPushButton { background-color: transparent; color: #666; border: none; font-weight: normal; font-size: 14px; } QPushButton:hover { color: white; background-color: rgba(100,200,255,0.3); border-radius: 5px; }")
        self.vision_btn.setToolTip("Capture Screen (Vision Context)")
        self.vision_btn.clicked.connect(self.trigger_vision)
        self.top_bar.addWidget(self.vision_btn)

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

        # Input box (placed BELOW output)
        self.input_box = ChatInputBox(self.container)
        self.input_box.setPlaceholderText("System Pilot awaiting commands...")
        self.input_box.setToolTip("Type your command here and press Enter (Shift+Space for newline)")
        self.input_box.returnPressed.connect(self.handle_input)
        self.input_box.heightChanged.connect(self.adjust_window_size)
        layout.addWidget(self.input_box)

        grad_layout.addWidget(self.container)
        main_layout.addWidget(self.gradient_wrapper)
        self.setLayout(main_layout)
        
        # Lock initial compact size to prevent horizontal shrinking
        self.setFixedWidth(600)
        self.adjustSize()
        self._compact_height = self.height()  # remember this for clear_session reset
        self.setFixedHeight(self._compact_height)

    def adjust_window_size(self):
        """Allows the window to grow vertically without shrinking width."""
        if self.output_box.isHidden():
            self.setMinimumHeight(0)
            self.setMaximumHeight(16777215)
            self.adjustSize()
            self.setFixedHeight(self.height())

    def apply_theme(self, theme_name):
        self.current_theme = theme_name
        
        # 1) Aggressive Stylesheet Purge (Fixes Sticky Glass Bug)
        self.setStyleSheet("")
        self.gradient_wrapper.setStyleSheet("")
        self.container.setStyleSheet("")
        self.title_label.setStyleSheet("")
        self.min_btn.setStyleSheet("")
        self.close_btn.setStyleSheet("")
        self.vision_btn.setStyleSheet("")
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
            self.vision_btn.setStyleSheet("QPushButton { background-color: transparent; color: rgba(200,210,230,0.8); border: none; font-size: 14px; } QPushButton:hover { color: white; background-color: rgba(100,160,255,0.3); border-radius: 5px; }")
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
            self.input_box.setStyleSheet("background-color: rgba(30, 30, 30, 200); color: white; border-radius: 6px; padding: 8px 10px; border: 1px solid rgba(80, 80, 80, 100);")
            self.output_box.setStyleSheet("QTextEdit { background-color: transparent; color: #D4D4D4; border: none; padding: 0px; }")
            self.output_box.document().setDefaultStyleSheet(markdown_css + "h1, h2, h3 { color: #eee; } pre { background-color: #1a1a1a; color: #e0e0e0; border: 1px solid #333; } code { background-color: #2b2b2b; color: #d4d4d4; } a { color: #58a6ff; }")

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
        
        self.vision_context_path = os.path.join(tmp_dir, "vision_context.png")
        img.save(self.vision_context_path, 'PNG')
        
        self.input_box.setPlaceholderText("👁 VISION ATTACHED - Ask about the screen...")
        self.show()
        self.activateWindow()
        self.input_box.setFocus()

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
        """Re-locks window to its stored compact size — no geometry computation on hidden widget."""
        self.setMinimumSize(0, 0)
        self.setMaximumSize(16777215, 16777215)
        self.setFixedWidth(600)
        self.setFixedHeight(self._compact_height)

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
        prompt = self.input_box.toPlainText().strip()
        if not prompt: return
        
        if hasattr(self, 'vision_context_path') and self.vision_context_path:
            v_path = self.vision_context_path.replace('\\', '/')
            
            # Read and encode image to base64
            import base64
            try:
                with open(v_path, "rb") as image_file:
                    base64_image = base64.b64encode(image_file.read()).decode('utf-8')
                
                # OI 0.4.x LMC message format: image is a separate message with type="image"
                # content must be a plain base64 string, NOT a list of dicts
                global ai_engine
                ai_engine.messages.append({
                    "role": "user",
                    "type": "image",
                    "format": "base64.png",
                    "content": base64_image
                })
            except Exception as e:
                print(f"Failed to encode vision context: {e}")
                
            self.vision_context_path = None # Consume it so it doesn't attach to next prompt
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
        self.setMinimumWidth(400)
        self.setMaximumWidth(16777215)
        self.setMinimumHeight(200)
        self.setMaximumHeight(16777215)
        self.resize(700, 500)

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
        """Enable dragging the frameless window."""
        if event.button() == Qt.MouseButton.LeftButton:
            self.offset = event.pos()

    def mouseMoveEvent(self, event):
        """Enable dragging the frameless window."""
        if self.offset is not None and event.buttons() == Qt.MouseButton.LeftButton:
            self.move(self.pos() + event.pos() - self.offset)

    def setup_trigger(self):
        """Phase 5: Trigger — Global hotkey Ctrl+Shift+Space to toggle visibility."""
        threading.Thread(target=self._listen, daemon=True).start()

    def _listen(self):
        # Phase 6: Global hotkey changed to Win+Alt per user request
        keyboard.add_hotkey('windows+alt', self.trigger_toggle)
        keyboard.wait()

    def trigger_toggle(self):
        # Safely emit the class-level signal from the keyboard background thread
        self.toggle_signal.emit()

    @pyqtSlot()
    def toggle_ui(self):
        if self.isVisible():
            self.hide()
        else:
            self.show()
            self.activateWindow()
            self.input_box.setFocus()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = SpotlightUI()
    window.show()
    sys.exit(app.exec())
