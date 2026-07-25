import os
import json
import copy


class SettingsManager:
    """Manages persistent user settings for Jarvs Spotlight using a local JSON file.

    Settings hierarchy:
        settings.json   — theme, workspace, active model ID, window size (git-tracked)
        user_models.json — user's personalized model list with API keys (gitignored)
        models.json      — default model catalog shipped with the repo (git-tracked, no secrets)
    """

    def __init__(self):
        # root_dir = Jarvs/ (three levels up from core/tools/settings_manager.py)
        self.root_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.settings_file = os.path.join(self.root_dir, "settings.json")
        self.default_workspace = os.path.join(self.root_dir, "workspace")

        # Model config files
        self.default_models_file = os.path.join(self.root_dir, "models.json")
        self.user_models_file = os.path.join(self.root_dir, "user_models.json")

        self.settings = self._load_settings()
        self.models_data = self._load_models()

    # ── Settings (settings.json) ─────────────────────────────────────────────

    def _load_settings(self):
        default_settings = {
            "theme": "AI Vaporwave",
            "workspace_path": self.default_workspace,
            "model": "gemini-2.5-flash",  # Now stores model ID, not raw litellm string
            "window_size": [600, 120]
        }

        if os.path.exists(self.settings_file):
            try:
                with open(self.settings_file, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                    if content:
                        data = json.loads(content)
                        for k, v in data.items():
                            if v:
                                default_settings[k] = v
            except Exception as e:
                print(f"Warning: Failed to load settings.json: {e}. Reverting to defaults.")

        self._save_raw(default_settings)
        return default_settings

    def _save_raw(self, data):
        try:
            with open(self.settings_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4)
        except Exception as e:
            print(f"Failed to save settings: {e}")

    def save_settings(self, theme, workspace_path, model_id=None):
        self.settings["theme"] = theme
        if workspace_path:
            self.settings["workspace_path"] = workspace_path
        if model_id:
            self.settings["model"] = model_id
        self._save_raw(self.settings)

    def get_theme(self):
        return self.settings.get("theme", "AI Vaporwave")

    def get_workspace_path(self):
        return self.settings.get("workspace_path", self.default_workspace)

    def get_window_size(self):
        return self.settings.get("window_size", [600, 120])

    def save_window_size(self, width, height):
        self.settings["window_size"] = [width, height]
        self._save_raw(self.settings)

    # ── Model Registry (user_models.json / models.json) ──────────────────────

    def _load_models(self):
        """Load the user model registry. On first run, copies defaults from models.json."""
        if os.path.exists(self.user_models_file):
            try:
                with open(self.user_models_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if data.get("models"):
                        return data
            except Exception as e:
                print(f"Warning: Failed to load user_models.json: {e}. Regenerating from defaults.")

        # First run or corrupt file — bootstrap from defaults
        return self._bootstrap_user_models()

    def _bootstrap_user_models(self):
        """Copy models.json → user_models.json as the user's starting config."""
        default_data = {
            "schema_version": "1.0",
            "models": [],
            "active_model": "gemini-2.5-flash"
        }
        if os.path.exists(self.default_models_file):
            try:
                with open(self.default_models_file, "r", encoding="utf-8") as f:
                    default_data = json.load(f)
            except Exception as e:
                print(f"Warning: Failed to load models.json: {e}. Using empty defaults.")

        # Migrate existing .env API key into the Gemini models
        existing_key = self._read_env_api_key()
        if existing_key:
            for model in default_data.get("models", []):
                if model.get("model_string", "").startswith("gemini/") and not model.get("api_key"):
                    model["api_key"] = existing_key

        self._save_models(default_data)
        return default_data

    def _save_models(self, data=None):
        """Persist the model registry to user_models.json."""
        if data is None:
            data = self.models_data
        try:
            with open(self.user_models_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4)
        except Exception as e:
            print(f"Failed to save user_models.json: {e}")

    def _read_env_api_key(self):
        """Read the legacy GEMINI_API_KEY from .env for backward compatibility."""
        from dotenv import load_dotenv
        env_path = os.path.join(self.root_dir, ".env")
        if os.path.exists(env_path):
            load_dotenv(dotenv_path=env_path, override=True)
            key = os.getenv("GEMINI_API_KEY")
            if key and key != "inserisci_qui_la_tua_chiave":
                return key
        return ""

    # ── Model CRUD ───────────────────────────────────────────────────────────

    def get_models_list(self):
        """Return the full list of model config dicts."""
        return self.models_data.get("models", [])

    def get_active_model_id(self):
        """Return the active model ID string."""
        return self.models_data.get("active_model", self.settings.get("model", ""))

    def get_active_model(self):
        """Return the full config dict for the active model, or a sensible fallback."""
        active_id = self.get_active_model_id()
        models = self.get_models_list()

        for m in models:
            if m.get("id") == active_id:
                return m

        # Fallback: return first model or a hardcoded default
        if models:
            return models[0]

        return {
            "id": "gemini-2.5-flash",
            "name": "Gemini 2.5 Flash",
            "provider": "cloud",
            "model_string": "gemini/gemini-2.5-flash",
            "api_key": self._read_env_api_key(),
            "api_base": None,
            "supports_vision": True,
            "context_window": None
        }

    def set_active_model(self, model_id):
        """Set which model is active and persist."""
        self.models_data["active_model"] = model_id
        self.settings["model"] = model_id
        self._save_models()
        self._save_raw(self.settings)

    def add_model(self, model_dict):
        """Append a new model to the user registry and persist."""
        models = self.models_data.setdefault("models", [])
        # Ensure unique ID
        existing_ids = {m["id"] for m in models}
        if model_dict["id"] in existing_ids:
            # Auto-suffix to avoid collision
            base_id = model_dict["id"]
            counter = 2
            while f"{base_id}-{counter}" in existing_ids:
                counter += 1
            model_dict["id"] = f"{base_id}-{counter}"
        models.append(model_dict)
        self._save_models()
        return model_dict["id"]

    def update_model(self, model_id, updates):
        """Update fields of an existing model entry and persist."""
        for m in self.models_data.get("models", []):
            if m["id"] == model_id:
                m.update(updates)
                self._save_models()
                return True
        return False

    def delete_model(self, model_id):
        """Remove a model from the registry. Returns True if found and removed."""
        models = self.models_data.get("models", [])
        original_len = len(models)
        self.models_data["models"] = [m for m in models if m["id"] != model_id]

        if len(self.models_data["models"]) < original_len:
            # If we deleted the active model, switch to first available
            if self.get_active_model_id() == model_id:
                remaining = self.models_data["models"]
                new_active = remaining[0]["id"] if remaining else ""
                self.models_data["active_model"] = new_active
                self.settings["model"] = new_active
                self._save_raw(self.settings)
            self._save_models()
            return True
        return False

    def get_model(self):
        """Backward-compatible: return the litellm model string for the active model."""
        active = self.get_active_model()
        return active.get("model_string", "gemini/gemini-2.5-flash")

    def get_api_key(self):
        """Return the API key for the active model, with .env fallback."""
        active = self.get_active_model()
        key = active.get("api_key", "")
        if key and key not in ("", "inserisci_qui_la_tua_chiave"):
            return key
        # Legacy fallback
        return self._read_env_api_key()
