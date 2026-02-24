import os
import json

class SettingsManager:
    """Manages persistent user settings for Jarvs Spotlight using a local JSON file."""
    
    def __init__(self):
        self.root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.settings_file = os.path.join(self.root_dir, "settings.json")
        self.default_workspace = os.path.join(self.root_dir, "workspace")
        self.settings = self._load_settings()

    def _load_settings(self):
        default_settings = {
            "theme": "AI Vaporwave",
            "workspace_path": self.default_workspace,
            "model": "gemini/gemini-2.5-flash"
        }
        
        if os.path.exists(self.settings_file):
            try:
                with open(self.settings_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    # Merge loaded data with defaults to ensure all keys exist
                    for k, v in data.items():
                        if v: # Don't overwrite with empty strings
                            default_settings[k] = v
            except Exception as e:
                print(f"Warning: Failed to load settings.json: {e}")
                
        # Auto-save defaults if file didn't exist or was missing keys
        self._save_raw(default_settings)
        return default_settings

    def _save_raw(self, data):
        try:
            with open(self.settings_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4)
        except Exception as e:
            print(f"Failed to save settings: {e}")

    def save_settings(self, theme, workspace_path, model=None):
        self.settings["theme"] = theme
        if workspace_path:
            self.settings["workspace_path"] = workspace_path
        if model:
            self.settings["model"] = model
        self._save_raw(self.settings)

    def get_api_key(self):
        """Strictly loads the API key from the .env file. JSON bypass is removed."""
        from dotenv import load_dotenv
        env_path = os.path.join(self.root_dir, ".env")
        if os.path.exists(env_path):
            load_dotenv(dotenv_path=env_path, override=True) # Always load latest
            key = os.getenv("GEMINI_API_KEY")
            if key and key != "inserisci_qui_la_tua_chiave":
                return key
        return ""

    def get_theme(self):
        return self.settings.get("theme", "AI Vaporwave")

    def get_workspace_path(self):
        return self.settings.get("workspace_path", self.default_workspace)

    def get_model(self):
        return self.settings.get("model", "gemini/gemini-2.5-flash")

