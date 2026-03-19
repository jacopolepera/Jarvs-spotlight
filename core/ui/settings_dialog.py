"""
core.ui.settings_dialog — Settings window for Jarvs Spotlight.

Provides SettingsDialog: a frameless, translucent dialog for configuring
theme, LLM model, API key, and workspace path.
"""
import os

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton
)
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

from core.ui.components import NoScrollComboBox




class SettingsDialog(QWidget):
    """Frameless settings dialog.

    Args:
        parent:           The parent SpotlightUI window (used for theme application).
        settings_manager: A SettingsManager instance.
        engine:           The active open-interpreter instance (to apply live changes).
    """

    def __init__(self, parent=None, settings_manager=None, engine=None):
        super().__init__(parent, Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.parent_ui = parent
        self.settings = settings_manager
        self.engine = engine
        self.setFixedSize(450, 420)
        self.setObjectName("SettingsDialog")
        # WA_TranslucentBackground is required for rounded corners on frameless widgets
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        # Two-layer structure mirroring SpotlightUI (GradientWrapper + MainContainer)
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

        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)

        self.outer_wrapper = QWidget(self)
        self.outer_wrapper.setObjectName("SettingsOuterWrapper")
        outer_wrapper_layout = QVBoxLayout(self.outer_wrapper)
        outer_wrapper_layout.setContentsMargins(1, 1, 1, 1)  # 1px border thickness

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
        self.model_cb.addItems([
            "gemini/gemini-2.5-flash",
            "gemini/gemini-1.5-pro",
            "gpt-4o",
            "claude-3-5-sonnet",
        ])
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

        # Buttons row
        btn_layout = QHBoxLayout()

        quit_btn = QPushButton("Quit Jarvs Completely")
        quit_btn.setObjectName("QuitBtn")
        quit_btn.clicked.connect(self._quit_app)
        btn_layout.addWidget(quit_btn)

        btn_layout.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.close)
        btn_layout.addWidget(cancel_btn)

        save_btn = QPushButton("Save & Apply")
        save_btn.setObjectName("SaveBtn")
        save_btn.clicked.connect(self._save_and_close)
        btn_layout.addWidget(save_btn)

        layout.addLayout(btn_layout)

    def _quit_app(self):
        QApplication.quit()

    def _save_and_close(self):
        theme = self.theme_cb.currentText()
        api_key = self.api_input.text().strip()
        workspace = self.workspace_input.text().strip()
        model = self.model_cb.currentText().strip()

        # Persist settings to JSON (without API key)
        self.settings.save_settings(theme, workspace, model)

        # Persist API key ONLY to .env
        env_path = os.path.join(self.settings.root_dir, ".env")
        if api_key:
            with open(env_path, "w", encoding="utf-8") as f:
                f.write(f"GEMINI_API_KEY={api_key}\n")

        # Apply theme instantly on the parent window
        if self.parent_ui:
            self.parent_ui.apply_theme(theme)

        # Apply live changes to the engine (no global reference needed)
        if self.engine:
            if api_key:
                self.engine.llm.api_key = api_key
            if model:
                self.engine.llm.model = model
            if workspace:
                self.engine.custom_instructions += (
                    f"\n\n[UPDATE] Your MUST USE THIS NEW WORKSPACE ABSOLUTE PATH: {workspace}"
                )

        self.close()
