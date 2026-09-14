import os
import json
import copy
import sqlite3
import threading
import tempfile

from core.agent.policy import ExecutionPolicy, parse_execution_policy
from core.agent.egress import CloudWorkspaceEgress


_UNSET = object()
_MAX_HERMES_PATH_CHARS = 32_768


class SettingsManager:
    """Manages persistent user settings for Jarvs Spotlight using a local JSON file.

    Settings hierarchy:
        settings.json    — local theme, workspace, policy, and window settings
        user_models.json — local model profiles and credentials
        models.json      — secret-free starter model catalog shipped with the app
        data/            — local memory, routines, and audit databases
    """

    def __init__(self, root_dir=None):
        # root_dir = Jarvs/ (three levels up from core/tools/settings_manager.py)
        self.root_dir = root_dir or os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        self._lock = threading.RLock()
        self.settings_file = os.path.join(self.root_dir, "settings.json")
        self.default_workspace = os.path.join(self.root_dir, "workspace")
        self.state_dir = os.path.join(self.root_dir, "data")

        # Model config files
        self.default_models_file = os.path.join(self.root_dir, "models.json")
        self.user_models_file = os.path.join(self.root_dir, "user_models.json")

        self.settings = self._load_settings()
        self.models_data = self._load_models()
        self._migrate_workspace_state()

    # ── Settings (settings.json) ─────────────────────────────────────────────

    def _load_settings(self):
        loaded_data = None
        default_settings = {
            "theme": "Default",
            "workspace_path": self.default_workspace,
            "model": "lmstudio-default",
            "window_size": [600, 120],
            # Missing/invalid values fail closed.  A user must explicitly opt
            # into automatic execution from the settings UI.
            "execution_policy": ExecutionPolicy.ALWAYS_ASK.value,
            # Sending local tool output to a cloud model is a distinct user
            # decision. Missing and invalid values fail closed.
            "cloud_workspace_egress": CloudWorkspaceEgress.DENY.value,
        }

        if os.path.exists(self.settings_file):
            try:
                with open(self.settings_file, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                    if content:
                        data = json.loads(content)
                        loaded_data = data
                        for k, v in data.items():
                            if v is not None:
                                default_settings[k] = v

                        # One-time migration for experimental builds that used
                        # a boolean.  The canonical setting is always an enum.
                        if "execution_policy" not in data and "auto_run" in data:
                            default_settings["execution_policy"] = (
                                ExecutionPolicy.AUTO_RUN.value
                                if data["auto_run"] is True
                                else ExecutionPolicy.ALWAYS_ASK.value
                            )
            except Exception as e:
                print(f"Warning: Failed to load settings.json: {e}. Reverting to defaults.")

        default_settings["execution_policy"] = parse_execution_policy(
            default_settings.get("execution_policy")
        ).value
        try:
            default_settings["cloud_workspace_egress"] = CloudWorkspaceEgress(
                default_settings.get("cloud_workspace_egress")
            ).value
        except (TypeError, ValueError):
            default_settings["cloud_workspace_egress"] = CloudWorkspaceEgress.DENY.value
        default_settings.pop("auto_run", None)

        # Hermes is an optional sidecar and remains disabled when these keys
        # are absent.  Validate persisted pilot settings without injecting the
        # defaults into otherwise canonical files on every startup.
        if "hermes_enabled" in default_settings and not isinstance(
            default_settings["hermes_enabled"], bool
        ):
            default_settings["hermes_enabled"] = False
        if "hermes_allow_effectful_fixture" in default_settings and not isinstance(
            default_settings["hermes_allow_effectful_fixture"], bool
        ):
            default_settings["hermes_allow_effectful_fixture"] = False
        for key in ("hermes_runtime_path", "hermes_home_path"):
            if key in default_settings:
                try:
                    default_settings[key] = self._validate_hermes_path(
                        default_settings[key], key
                    )
                except ValueError:
                    default_settings[key] = ""
        # A runtime version/commit is verified by the adapter contract, never
        # accepted as a user assertion in settings.
        default_settings.pop("hermes_version", None)

        if "voice" in default_settings:
            from core.audio.config import DEFAULT_VOICE_CONFIG, normalize_voice_config
            try:
                default_settings["voice"] = normalize_voice_config(default_settings["voice"])
            except ValueError:
                default_settings["voice"] = dict(DEFAULT_VOICE_CONFIG)

        # Avoid touching the file on every daemon import/start. Persist only a
        # first-run default or an actual migration/normalization.
        if loaded_data != default_settings:
            self._save_raw(default_settings)
        return default_settings

    def _save_raw(self, data):
        with self._lock:
            self._write_json_atomic(self.settings_file, data)

    @staticmethod
    def _write_json_atomic(path, data):
        """Persist JSON atomically and propagate failures to the caller.

        Security settings must never appear saved only in memory.  The
        temporary file lives beside the destination so ``os.replace`` stays on
        the same filesystem and is atomic on supported desktop platforms.
        """

        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=directory,
                prefix=f".{os.path.basename(path)}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_path = handle.name
                json.dump(data, handle, indent=4)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, path)
            temporary_path = None
        finally:
            if temporary_path is not None:
                try:
                    os.remove(temporary_path)
                except FileNotFoundError:
                    pass

    def save_settings(self, theme, workspace_path, model_id=None):
        with self._lock:
            updated = copy.deepcopy(self.settings)
            updated["theme"] = theme
            if workspace_path:
                updated["workspace_path"] = workspace_path
            if model_id:
                updated["model"] = model_id
            self._save_raw(updated)
            self.settings = updated

    def get_theme(self):
        return self.settings.get("theme", "Default")

    def get_workspace_path(self):
        return self.settings.get("workspace_path", self.default_workspace)

    def get_state_dir(self):
        """Return the application-owned directory for private persistent data."""

        return self.state_dir

    def _migrate_workspace_state(self):
        """Copy pre-release workspace databases into application-owned state.

        Early development builds stored memory, routines, and audit databases
        inside ``<workspace>/.jarvs``. Keep the source untouched so migration is
        recoverable, and record completion only after every available database
        has been copied successfully.
        """

        source_dir = os.path.join(self.get_workspace_path(), ".jarvs")
        marker = os.path.join(self.state_dir, ".workspace-state-migrated")
        if os.path.exists(marker) or not os.path.isdir(source_dir):
            return

        database_names = (
            "personal_memory.sqlite3",
            "routines.sqlite3",
            "run_audit.sqlite3",
        )
        try:
            os.makedirs(self.state_dir, exist_ok=True)
            for name in database_names:
                source = os.path.join(source_dir, name)
                destination = os.path.join(self.state_dir, name)
                if not os.path.isfile(source) or os.path.exists(destination):
                    continue
                temporary_path = None
                try:
                    with tempfile.NamedTemporaryFile(
                        dir=self.state_dir,
                        prefix=f".{name}.",
                        suffix=".migration",
                        delete=False,
                    ) as handle:
                        temporary_path = handle.name
                    source_connection = sqlite3.connect(source)
                    destination_connection = sqlite3.connect(temporary_path)
                    try:
                        source_connection.backup(destination_connection)
                    finally:
                        destination_connection.close()
                        source_connection.close()
                    os.replace(temporary_path, destination)
                    temporary_path = None
                finally:
                    if temporary_path is not None:
                        try:
                            os.remove(temporary_path)
                        except FileNotFoundError:
                            pass
            with open(marker, "x", encoding="utf-8") as handle:
                handle.write("1\n")
        except FileExistsError:
            return
        except Exception as exc:
            print(f"Warning: Failed to migrate local application state: {exc}")

    def get_window_size(self):
        return self.settings.get("window_size", [600, 120])

    def save_window_size(self, width, height):
        with self._lock:
            updated = copy.deepcopy(self.settings)
            updated["window_size"] = [width, height]
            self._save_raw(updated)
            self.settings = updated

    def get_execution_policy(self):
        """Return the validated global execution policy."""

        with self._lock:
            return parse_execution_policy(
                self.settings.get("execution_policy")
            ).value

    def set_execution_policy(self, policy):
        """Validate and persist the global execution policy."""

        try:
            parsed = ExecutionPolicy(policy)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Unsupported execution policy: {policy!r}") from exc
        with self._lock:
            updated = copy.deepcopy(self.settings)
            updated["execution_policy"] = parsed.value
            self._save_raw(updated)
            self.settings = updated
        return parsed.value

    def get_cloud_workspace_egress(self):
        """Return the validated workspace-to-cloud egress policy."""

        with self._lock:
            try:
                return CloudWorkspaceEgress(
                    self.settings.get("cloud_workspace_egress")
                ).value
            except (TypeError, ValueError):
                return CloudWorkspaceEgress.DENY.value

    def set_cloud_workspace_egress(self, policy):
        """Persist an explicit workspace-to-cloud egress decision."""

        try:
            parsed = CloudWorkspaceEgress(policy)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Unsupported cloud workspace egress policy: {policy!r}") from exc
        with self._lock:
            updated = copy.deepcopy(self.settings)
            updated["cloud_workspace_egress"] = parsed.value
            self._save_raw(updated)
            self.settings = updated
        return parsed.value

    # ── Optional Hermes specialist ───────────────────────────────────────

    @staticmethod
    def _validate_hermes_path(value, field_name):
        if not isinstance(value, str):
            raise ValueError(f"{field_name} must be a string")
        value = value.strip()
        if not value:
            return ""
        if (
            len(value) > _MAX_HERMES_PATH_CHARS
            or "\x00" in value
            or "\r" in value
            or "\n" in value
        ):
            raise ValueError(f"{field_name} is invalid")
        if not os.path.isabs(value):
            raise ValueError(f"{field_name} must be an absolute path")
        return os.path.normpath(value)

    def get_hermes_settings(self):
        """Return the secret-free, validated specialist configuration."""

        with self._lock:
            return {
                "hermes_enabled": self.settings.get("hermes_enabled") is True,
                "hermes_runtime_path": self._safe_hermes_path(
                    self.settings.get("hermes_runtime_path")
                ),
                "hermes_home_path": self._safe_hermes_path(
                    self.settings.get("hermes_home_path")
                ),
                # Informational and read-only. The adapter reports and pins the
                # installed runtime version; users cannot assert it here.
                "hermes_version": None,
                "hermes_allow_effectful_fixture": (
                    self.settings.get("hermes_allow_effectful_fixture") is True
                ),
            }

    # Explicit alias used by transport code to document that no credentials
    # or provider secrets are part of this response.
    get_public_hermes_settings = get_hermes_settings

    @classmethod
    def _safe_hermes_path(cls, value):
        try:
            return cls._validate_hermes_path(value or "", "Hermes path")
        except ValueError:
            return ""

    def set_hermes_settings(
        self,
        *,
        enabled=_UNSET,
        runtime_path=_UNSET,
        home_path=_UNSET,
        allow_effectful_fixture=_UNSET,
    ):
        """Validate and atomically persist a partial Hermes configuration."""

        changes = {}
        if enabled is not _UNSET:
            if not isinstance(enabled, bool):
                raise ValueError("hermes_enabled must be a boolean")
            changes["hermes_enabled"] = enabled
        if runtime_path is not _UNSET:
            changes["hermes_runtime_path"] = self._validate_hermes_path(
                runtime_path, "hermes_runtime_path"
            )
        if home_path is not _UNSET:
            changes["hermes_home_path"] = self._validate_hermes_path(
                home_path, "hermes_home_path"
            )
        if allow_effectful_fixture is not _UNSET:
            if not isinstance(allow_effectful_fixture, bool):
                raise ValueError(
                    "hermes_allow_effectful_fixture must be a boolean"
                )
            changes["hermes_allow_effectful_fixture"] = allow_effectful_fixture

        with self._lock:
            updated = copy.deepcopy(self.settings)
            updated.update(changes)
            self._save_raw(updated)
            self.settings = updated
        return self.get_hermes_settings()

    def validate_hermes_settings(
        self,
        *,
        enabled=_UNSET,
        runtime_path=_UNSET,
        home_path=_UNSET,
        allow_effectful_fixture=_UNSET,
    ):
        """Validate a partial update without mutating memory or disk."""

        if enabled is not _UNSET and not isinstance(enabled, bool):
            raise ValueError("hermes_enabled must be a boolean")
        if runtime_path is not _UNSET:
            self._validate_hermes_path(runtime_path, "hermes_runtime_path")
        if home_path is not _UNSET:
            self._validate_hermes_path(home_path, "hermes_home_path")
        if allow_effectful_fixture is not _UNSET and not isinstance(
            allow_effectful_fixture, bool
        ):
            raise ValueError("hermes_allow_effectful_fixture must be a boolean")

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
        with self._lock:
            if data is None:
                data = self.models_data
            self._write_json_atomic(self.user_models_file, data)

    def _read_env_api_key(self):
        """Read an optional GEMINI_API_KEY from the local environment file."""
        from dotenv import load_dotenv
        env_path = os.path.join(self.root_dir, ".env")
        if os.path.exists(env_path):
            load_dotenv(dotenv_path=env_path, override=True)
            key = os.getenv("GEMINI_API_KEY")
            if key and key != "replace-with-your-api-key":
                return key
        return ""

    # ── Model CRUD ───────────────────────────────────────────────────────────

    def get_models_list(self):
        """Return the full list of model config dicts."""
        with self._lock:
            return copy.deepcopy(self.models_data.get("models", []))

    @staticmethod
    def redact_model(model):
        """Return a renderer-safe copy of a model configuration."""

        public = copy.deepcopy(model)
        api_key = public.pop("api_key", None)
        public["api_key_configured"] = bool(
            api_key and api_key != "replace-with-your-api-key"
        )
        return public

    def get_public_models_list(self):
        return [self.redact_model(model) for model in self.get_models_list()]

    def get_public_active_model(self):
        return self.redact_model(self.get_active_model())

    def get_active_model_id(self):
        """Return the active model ID string."""
        with self._lock:
            return self.models_data.get(
                "active_model",
                self.settings.get("model", ""),
            )

    def get_active_model(self):
        """Return the full config dict for the active model, or a sensible fallback."""
        with self._lock:
            active_id = self.get_active_model_id()
            models = self.models_data.get("models", [])

            for model in models:
                if model.get("id") == active_id:
                    return copy.deepcopy(model)

            # Fallback: return first model or a hardcoded default
            if models:
                return copy.deepcopy(models[0])

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
        with self._lock:
            if not any(
                model.get("id") == model_id
                for model in self.models_data.get("models", [])
            ):
                raise KeyError(f"Unknown model: {model_id}")
            updated_models = copy.deepcopy(self.models_data)
            updated_settings = copy.deepcopy(self.settings)
            updated_models["active_model"] = model_id
            updated_settings["model"] = model_id

            # Persist the policy-bearing settings snapshot while the same lock
            # protects every writer. The duplicated model field is retained for
            # model registry remains authoritative on load.
            self._save_raw(updated_settings)
            self._save_models(updated_models)
            self.settings = updated_settings
            self.models_data = updated_models

    def add_model(self, model_dict):
        """Append a new model to the user registry and persist."""
        with self._lock:
            updated = copy.deepcopy(self.models_data)
            models = updated.setdefault("models", [])
            candidate = copy.deepcopy(model_dict)
            # Ensure unique ID
            existing_ids = {model["id"] for model in models}
            if candidate["id"] in existing_ids:
                # Auto-suffix to avoid collision
                base_id = candidate["id"]
                counter = 2
                while f"{base_id}-{counter}" in existing_ids:
                    counter += 1
                candidate["id"] = f"{base_id}-{counter}"
            models.append(candidate)
            self._save_models(updated)
            self.models_data = updated
            return candidate["id"]

    def update_model(self, model_id, updates):
        """Update fields of an existing model entry and persist."""
        with self._lock:
            updated = copy.deepcopy(self.models_data)
            for model in updated.get("models", []):
                if model["id"] == model_id:
                    model.update(copy.deepcopy(updates))
                    self._save_models(updated)
                    self.models_data = updated
                    return True
        return False

    def delete_model(self, model_id):
        """Remove a model from the registry. Returns True if found and removed."""
        with self._lock:
            updated_models = copy.deepcopy(self.models_data)
            models = updated_models.get("models", [])
            remaining = [model for model in models if model["id"] != model_id]
            if len(remaining) == len(models):
                return False

            updated_models["models"] = remaining
            updated_settings = copy.deepcopy(self.settings)
            active_changed = self.get_active_model_id() == model_id
            if active_changed:
                new_active = remaining[0]["id"] if remaining else ""
                updated_models["active_model"] = new_active
                updated_settings["model"] = new_active
                self._save_raw(updated_settings)

            self._save_models(updated_models)
            self.models_data = updated_models
            if active_changed:
                self.settings = updated_settings
            return True

    def get_model(self):
        """Return the provider model identifier for the active profile."""
        active = self.get_active_model()
        return active.get("model_string", "gemini/gemini-2.5-flash")

    def get_api_key(self):
        """Return the API key for the active model, with .env fallback."""
        active = self.get_active_model()
        key = active.get("api_key", "")
        if key and key != "replace-with-your-api-key":
            return key
        # Legacy fallback only for gemini models
        model_str = active.get("model_string", "")
        if "gemini" in model_str.lower():
            return self._read_env_api_key()
        return ""

    def get_voice_config(self):
        from core.audio.config import normalize_voice_config

        with self._lock:
            # Invalid persisted configuration must not silently opt into cloud.
            return normalize_voice_config(self.settings.get("voice"))

    def set_voice_config(self, changes):
        from core.audio.config import normalize_voice_config

        with self._lock:
            previous = self.settings.get("voice")
            merged = dict(previous) if isinstance(previous, dict) else {}
            merged.update(changes)
            voice = normalize_voice_config(merged)
            updated = {**self.settings, "voice": voice}
            self._save_raw(updated)
            self.settings = updated
            return dict(voice)

    def get_voice_api_key(self):
        """Return a Gemini key for speech transcription without exposing it."""

        with self._lock:
            for model in self.models_data.get("models", []):
                model_string = str(model.get("model_string", "")).lower()
                key = model.get("api_key", "")
                if (
                    "gemini" in model_string
                    and isinstance(key, str)
                    and key
                    and key != "replace-with-your-api-key"
                ):
                    return key
        return self._read_env_api_key()
