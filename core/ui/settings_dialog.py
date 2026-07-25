"""
core.ui.settings_dialog — Settings window for Jarvs Spotlight.

Provides SettingsDialog: a frameless, translucent dialog for configuring
theme, LLM model selection, model management, and workspace path.

Inspired by the Continue extension's model-switching UX — supports both
cloud models (with API keys) and local models (LMStudio, Ollama, etc.).
"""
import os

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QFrame, QSizePolicy, QMessageBox
)
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

from core.ui.components import NoScrollComboBox


# ── Add Model Dialog ─────────────────────────────────────────────────────────

class AddModelDialog(QWidget):
    """Popup dialog for adding a new model to the user's model registry."""

    def __init__(self, parent_dialog=None, settings_manager=None):
        super().__init__(None, Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.parent_dialog = parent_dialog
        self.settings = settings_manager
        self.setFixedSize(420, 430)
        self.setObjectName("AddModelDialog")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        self.setStyleSheet("""
            QWidget#AddModelDialog { background-color: transparent; }
            QWidget#AddOuterWrapper { background-color: #3a3a3a; border-radius: 12px; }
            QWidget#AddInnerContent { background-color: #1e1e1e; border-radius: 11px; }
            QLabel { font-size: 10pt; font-weight: bold; margin-top: 6px; color: #ccc; }
            QLabel#SectionTitle { font-size: 12pt; color: #fff; margin-bottom: 4px; margin-top: 0px; }
            QLineEdit, QComboBox {
                background-color: #2b2b2b; color: #fff; border: 1px solid #444;
                border-radius: 4px; padding: 6px; font-size: 10pt;
            }
            QComboBox QAbstractItemView {
                background-color: #2b2b2b; color: #fff; border: 1px solid #444;
                selection-background-color: #555;
            }
            QPushButton {
                background-color: #3a3a3a; color: #fff; border: 1px solid #555;
                border-radius: 4px; padding: 6px 15px; font-weight: bold; font-size: 10pt;
            }
            QPushButton:hover { background-color: #4a4a4a; }
            QPushButton#AddBtn { background-color: #2d7a2d; border-color: #3b9c3b; }
            QPushButton#AddBtn:hover { background-color: #3b9c3b; }
        """)

        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)

        outer_wrapper = QWidget(self)
        outer_wrapper.setObjectName("AddOuterWrapper")
        ow_layout = QVBoxLayout(outer_wrapper)
        ow_layout.setContentsMargins(1, 1, 1, 1)

        inner = QWidget(outer_wrapper)
        inner.setObjectName("AddInnerContent")
        layout = QVBoxLayout(inner)
        layout.setSpacing(6)
        layout.setContentsMargins(20, 16, 20, 16)

        title = QLabel("Add New Model")
        title.setObjectName("SectionTitle")
        layout.addWidget(title)

        # Display Name
        layout.addWidget(QLabel("Display Name:"))
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("e.g. Qwen 2.5 7B (LM Studio)")
        layout.addWidget(self.name_input)

        # Provider
        layout.addWidget(QLabel("Provider:"))
        self.provider_cb = NoScrollComboBox()
        self.provider_cb.addItems(["cloud", "local"])
        self.provider_cb.currentTextChanged.connect(self._on_provider_change)
        layout.addWidget(self.provider_cb)

        # Model String
        layout.addWidget(QLabel("Model String:"))
        self.model_string_input = QLineEdit()
        self.model_string_input.setPlaceholderText("e.g. gemini/gemini-2.5-flash  or  openai/qwen2.5-coder-7b")
        layout.addWidget(self.model_string_input)

        # API Key (cloud) / Server URL (local) — dynamic
        self.dynamic_label = QLabel("API Key:")
        layout.addWidget(self.dynamic_label)
        self.dynamic_input = QLineEdit()
        self.dynamic_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.dynamic_input.setPlaceholderText("sk-... or AIza...")
        layout.addWidget(self.dynamic_input)

        # Context Window (optional)
        layout.addWidget(QLabel("Context Window (optional):"))
        self.ctx_input = QLineEdit()
        self.ctx_input.setPlaceholderText("e.g. 8192 — leave blank for default")
        layout.addWidget(self.ctx_input)

        layout.addStretch()

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.close)
        btn_row.addWidget(cancel_btn)

        add_btn = QPushButton("Add Model")
        add_btn.setObjectName("AddBtn")
        add_btn.clicked.connect(self._add_model)
        btn_row.addWidget(add_btn)

        layout.addLayout(btn_row)

        ow_layout.addWidget(inner)
        outer_layout.addWidget(outer_wrapper)

    def _on_provider_change(self, provider):
        if provider == "local":
            self.dynamic_label.setText("Server URL:")
            self.dynamic_input.setEchoMode(QLineEdit.EchoMode.Normal)
            self.dynamic_input.setPlaceholderText("http://localhost:1234/v1")
            self.model_string_input.setPlaceholderText("e.g. openai/qwen2.5-coder-7b-instruct")
        else:
            self.dynamic_label.setText("API Key:")
            self.dynamic_input.setEchoMode(QLineEdit.EchoMode.Password)
            self.dynamic_input.setPlaceholderText("sk-... or AIza...")
            self.model_string_input.setPlaceholderText("e.g. gemini/gemini-2.5-flash")

    def _add_model(self):
        name = self.name_input.text().strip()
        provider = self.provider_cb.currentText()
        model_string = self.model_string_input.text().strip()
        dynamic_val = self.dynamic_input.text().strip()
        ctx_text = self.ctx_input.text().strip()

        if not name or not model_string:
            QMessageBox.warning(self, "Missing fields", "Display Name and Model String are required.")
            return

        # Build ID from name (lowercase, hyphenated)
        model_id = name.lower().replace(" ", "-").replace("(", "").replace(")", "")

        model_dict = {
            "id": model_id,
            "name": name,
            "provider": provider,
            "model_string": model_string,
            "api_key": dynamic_val if provider == "cloud" else "lm-studio",
            "api_base": dynamic_val if provider == "local" else None,
            "supports_vision": True,
            "context_window": int(ctx_text) if ctx_text.isdigit() else None,
        }

        final_id = self.settings.add_model(model_dict)

        # Refresh the parent dialog's model dropdown
        if self.parent_dialog:
            self.parent_dialog._refresh_model_list()
            # Select the newly added model
            idx = self.parent_dialog.model_cb.findData(final_id)
            if idx >= 0:
                self.parent_dialog.model_cb.setCurrentIndex(idx)

        self.close()


# ── Settings Dialog ──────────────────────────────────────────────────────────

class SettingsDialog(QWidget):
    """Frameless settings dialog with model management.

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
        self.setFixedSize(480, 600)
        self.setObjectName("SettingsDialog")
        # WA_TranslucentBackground is required for rounded corners on frameless widgets
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        # Two-layer structure mirroring SpotlightUI (GradientWrapper + MainContainer)
        # This is the proven pattern for correct rounded corner rendering on Windows
        self.setStyleSheet("""
            QWidget#SettingsDialog { background-color: transparent; }
            QWidget#SettingsOuterWrapper { background-color: #3a3a3a; border-radius: 12px; }
            QWidget#SettingsInnerContent { background-color: #1e1e1e; border-radius: 11px; }
            QLabel { font-size: 10pt; font-weight: bold; margin-top: 6px; color: #ccc; }
            QLabel#SectionTitle {
                font-size: 11pt; color: #9f9fff; margin-top: 12px; margin-bottom: 2px;
                font-weight: bold;
            }
            QLineEdit, QComboBox {
                background-color: #2b2b2b; color: #fff; border: 1px solid #444;
                border-radius: 4px; padding: 6px; font-size: 10pt;
            }
            QComboBox QAbstractItemView {
                background-color: #2b2b2b; color: #fff; border: 1px solid #444;
                selection-background-color: #555;
            }
            QFrame#Separator {
                background-color: #3a3a3a; max-height: 1px; margin-top: 6px; margin-bottom: 2px;
            }
            QPushButton {
                background-color: #3a3a3a; color: #fff; border: 1px solid #555;
                border-radius: 4px; padding: 6px 15px; font-weight: bold; font-size: 10pt;
            }
            QPushButton:hover { background-color: #4a4a4a; }
            QPushButton#SaveBtn { background-color: #2d7a2d; border-color: #3b9c3b; }
            QPushButton#SaveBtn:hover { background-color: #3b9c3b; }
            QPushButton#QuitBtn { background-color: #8b2a2a; border-color: #a83232; }
            QPushButton#QuitBtn:hover { background-color: #a83232; }
            QPushButton#AddModelBtn {
                background-color: transparent; border: 1px dashed #555;
                color: #8a8; font-size: 9pt; padding: 4px 10px;
            }
            QPushButton#AddModelBtn:hover { background-color: rgba(255,255,255,0.05); border-color: #8a8; }
            QPushButton#DeleteModelBtn {
                background-color: transparent; border: 1px solid #633;
                color: #c66; font-size: 9pt; padding: 4px 10px;
            }
            QPushButton#DeleteModelBtn:hover { background-color: rgba(255,50,50,0.12); border-color: #c66; }
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
        layout.setSpacing(4)
        layout.setContentsMargins(20, 16, 20, 16)

        outer_wrapper_layout.addWidget(self.inner_content)
        outer_layout.addWidget(self.outer_wrapper)

        # ── Title ─────────────────────────────────────────────────────────────
        title = QLabel("Jarvs Settings")
        title.setStyleSheet("font-size: 14pt; color: #fff; margin-bottom: 2px; margin-top: 0px;")
        layout.addWidget(title)

        # ── General Section ───────────────────────────────────────────────────

        # Theme
        layout.addWidget(QLabel("Theme:"))
        self.theme_cb = NoScrollComboBox()
        self.theme_cb.addItems(["AI Vaporwave", "Dark", "Light", "Glassy Windows"])
        self.theme_cb.setCurrentText(self.settings.get_theme())
        layout.addWidget(self.theme_cb)

        # Workspace Path
        layout.addWidget(QLabel("Default Workspace Path:"))
        self.workspace_input = QLineEdit()
        self.workspace_input.setText(self.settings.get_workspace_path())
        layout.addWidget(self.workspace_input)

        # ── Separator ─────────────────────────────────────────────────────────
        sep = QFrame()
        sep.setObjectName("Separator")
        sep.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(sep)

        # ── Model Configuration Section ───────────────────────────────────────
        model_title = QLabel("⚡ Model Configuration")
        model_title.setObjectName("SectionTitle")
        layout.addWidget(model_title)

        # Active Model dropdown
        layout.addWidget(QLabel("Active Model:"))
        self.model_cb = NoScrollComboBox()
        self._refresh_model_list()
        self.model_cb.currentIndexChanged.connect(self._on_model_selected)
        layout.addWidget(self.model_cb)

        # Dynamic config area — changes based on selected model's provider
        self.provider_label = QLabel("")
        self.provider_label.setStyleSheet("font-size: 9pt; color: #888; margin-top: 2px;")
        layout.addWidget(self.provider_label)

        # Model string (editable for advanced users)
        layout.addWidget(QLabel("Model String:"))
        self.model_string_input = QLineEdit()
        self.model_string_input.setToolTip("The litellm/OI model identifier, e.g. gemini/gemini-2.5-flash or openai/qwen2.5-coder-7b")
        layout.addWidget(self.model_string_input)

        # Dynamic field: API Key (cloud) or Server URL (local)
        self.dynamic_label = QLabel("API Key:")
        layout.addWidget(self.dynamic_label)
        self.dynamic_input = QLineEdit()
        layout.addWidget(self.dynamic_input)

        # Populate dynamic fields for the initially selected model
        self._on_model_selected()

        # ── Model management buttons ──────────────────────────────────────────
        model_btn_row = QHBoxLayout()

        add_btn = QPushButton("+ Add Model")
        add_btn.setObjectName("AddModelBtn")
        add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        add_btn.clicked.connect(self._open_add_model)
        model_btn_row.addWidget(add_btn)

        delete_btn = QPushButton("🗑 Delete")
        delete_btn.setObjectName("DeleteModelBtn")
        delete_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        delete_btn.clicked.connect(self._delete_current_model)
        model_btn_row.addWidget(delete_btn)

        model_btn_row.addStretch()
        layout.addLayout(model_btn_row)

        layout.addStretch()

        # ── Bottom buttons ────────────────────────────────────────────────────
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

    # ── Model list helpers ────────────────────────────────────────────────────

    def _refresh_model_list(self):
        """Rebuild the model dropdown from the user's model registry."""
        self.model_cb.blockSignals(True)
        self.model_cb.clear()

        models = self.settings.get_models_list()
        active_id = self.settings.get_active_model_id()

        for m in models:
            provider = m.get("provider", "cloud")
            icon = "☁️" if provider == "cloud" else "🖥️"
            display = f"{icon}  {m['name']}"
            self.model_cb.addItem(display, m["id"])

        # Select the active model
        for i in range(self.model_cb.count()):
            if self.model_cb.itemData(i) == active_id:
                self.model_cb.setCurrentIndex(i)
                break

        self.model_cb.blockSignals(False)

    def _on_model_selected(self):
        """Update the dynamic config fields when a different model is selected."""
        model_id = self.model_cb.currentData()
        if not model_id:
            return

        models = self.settings.get_models_list()
        selected = None
        for m in models:
            if m["id"] == model_id:
                selected = m
                break

        if not selected:
            return

        provider = selected.get("provider", "cloud")

        if provider == "local":
            self.provider_label.setText("🖥️  Local Model — connects to a local inference server")
            self.dynamic_label.setText("Server URL:")
            self.dynamic_input.setEchoMode(QLineEdit.EchoMode.Normal)
            self.dynamic_input.setText(selected.get("api_base", "http://localhost:1234/v1"))
            self.dynamic_input.setPlaceholderText("http://localhost:1234/v1")
        else:
            self.provider_label.setText("☁️  Cloud Model — requires an API key")
            self.dynamic_label.setText("API Key:")
            self.dynamic_input.setEchoMode(QLineEdit.EchoMode.Password)
            self.dynamic_input.setText(selected.get("api_key", ""))
            self.dynamic_input.setPlaceholderText("Paste your API key here")

        self.model_string_input.setText(selected.get("model_string", ""))

    # ── Add / Delete models ───────────────────────────────────────────────────

    def _open_add_model(self):
        from PyQt6.QtGui import QGuiApplication
        self.add_dialog = AddModelDialog(
            parent_dialog=self,
            settings_manager=self.settings
        )
        screen = QGuiApplication.primaryScreen().geometry()
        x = (screen.width() - self.add_dialog.width()) // 2
        y = (screen.height() - self.add_dialog.height()) // 2
        self.add_dialog.move(x, y)
        self.add_dialog.show()

    def _delete_current_model(self):
        model_id = self.model_cb.currentData()
        if not model_id:
            return

        models = self.settings.get_models_list()
        if len(models) <= 1:
            QMessageBox.warning(
                self, "Cannot Delete",
                "You must keep at least one model configured."
            )
            return

        # Find the display name
        name = model_id
        for m in models:
            if m["id"] == model_id:
                name = m["name"]
                break

        reply = QMessageBox.question(
            self, "Delete Model",
            f"Are you sure you want to remove '{name}' from your model list?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.settings.delete_model(model_id)
            self._refresh_model_list()
            self._on_model_selected()

    # ── Save & Apply ──────────────────────────────────────────────────────────

    def _quit_app(self):
        QApplication.quit()

    def _save_and_close(self):
        theme = self.theme_cb.currentText()
        workspace = self.workspace_input.text().strip()
        selected_model_id = self.model_cb.currentData()
        dynamic_val = self.dynamic_input.text().strip()
        model_string = self.model_string_input.text().strip()

        # ── Persist general settings ──────────────────────────────────────────
        self.settings.save_settings(theme, workspace, selected_model_id)

        # ── Update the selected model's config in the registry ────────────────
        if selected_model_id:
            models = self.settings.get_models_list()
            for m in models:
                if m["id"] == selected_model_id:
                    provider = m.get("provider", "cloud")

                    if model_string:
                        m["model_string"] = model_string

                    if provider == "local":
                        if dynamic_val:
                            m["api_base"] = dynamic_val
                    else:
                        if dynamic_val:
                            m["api_key"] = dynamic_val

                    break

            self.settings.set_active_model(selected_model_id)
            self.settings._save_models()

        # ── Apply theme instantly on the parent window ────────────────────────
        if self.parent_ui:
            self.parent_ui.apply_theme(theme)

        # ── Apply live changes to the engine ──────────────────────────────────
        if self.engine and selected_model_id:
            model_config = self.settings.get_active_model()
            provider = model_config.get("provider", "cloud")

            if model_string:
                self.engine.llm.model = model_string

            if provider == "local":
                api_base = model_config.get("api_base", "http://localhost:1234/v1")
                self.engine.llm.api_base = api_base
                self.engine.llm.api_key = model_config.get("api_key", "lm-studio")
                self.engine.offline = True
            else:
                api_key = model_config.get("api_key") or self.settings.get_api_key()
                if api_key:
                    self.engine.llm.api_key = api_key
                self.engine.offline = False

            self.engine.llm.supports_vision = model_config.get("supports_vision", True)

            ctx = model_config.get("context_window")
            if ctx:
                self.engine.llm.context_window = ctx

            if workspace:
                self.engine.custom_instructions += (
                    f"\n\n[UPDATE] Your MUST USE THIS NEW WORKSPACE ABSOLUTE PATH: {workspace}"
                )

        self.close()
